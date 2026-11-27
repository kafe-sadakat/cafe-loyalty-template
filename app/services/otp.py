"""
Yeni cihaz doğrulaması: tek kullanımlık SMS kodu (OTP). Ayrıntılı belge: docs/OTP.md.

Kural (OTP_MODE=new_device): KAYITLI (silinmemiş) bir müşterinin numarası, o müşteriye bağlı OLMAYAN bir cihazdan
(cihaz belirteci yok / başka müşteriye bağlı / sunucuda kaydı yok) girilirse cihaz bağlanmaz, numaraya 6 haneli kod gider
ve istemci {"status": "otp_required", ...} alır. Kod doğrulanınca istek bugünkü gibi işlenir. Yeni kayıt ve zaten bağlı
cihaz kod istemez. OTP_MODE=off (varsayılan) iken bu modül hiçbir şey yapmaz.

HESAP NUMARALANDIRMA (bilinçli kabul edilmiş ödünleşim): "otp_required" yanıtı, numaranın kayıtlı bir müşteriye ait
olduğunu ele verir (yeni numara doğrudan kayıt olur). İşletme sahibi bu politikayı seçti; zararı IP başına istek sınırları,
numara başına günlük SMS sınırı ve maskeli numara (+90 5** *** ** 12) ile sınırlanır.

Güvenlik özellikleri:
- Kod `secrets` ile üretilir (6 hane); veritabanında yalnızca HMAC-SHA256(SECRET_KEY, challenge_id + kod) özeti durur.
- Karşılaştırma sabit zamanlıdır (hmac.compare_digest). Kod tek kullanımlıktır (koşullu UPDATE ile tüketilir).
- OTP_MAX_ATTEMPTS hatalı denemeden sonra challenge kilitlenir (süresi dolana kadar o numaraya yeni kod da gitmez).
- Numara başına günlük gönderim sınırı (Europe/Istanbul günü) ve yeniden gönderme beklemesi sunucuda; IP başına gönderim
  sınırı bellek içi hız sınırlayıcıyla.
- Kod hiçbir yanıtta dönmez; yalnızca OTP_PROVIDER=console (geliştirme) kodu günlüğe yazar.
- SMS, veritabanı işlemi COMMIT edildikten sonra (yazma kilidi tutulmadan) gönderilir (db.run_after_commit). Gönderim
  başarısız olursa bu gönderim geri alınır ve aynı yanıt nesnesi yerinde "otp_unavailable" yapılır.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import math
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

from app.config import DEFAULT_BUSINESS_NAME, SECRET_KEY, get_otp_mode, get_otp_settings
from app.db import get_db, get_setting, log_audit, run_after_commit
from app.services.otp_providers import get_provider
from app.utils import (
    ISTANBUL_TZ,
    get_istanbul_now,
    hash_token,
    mask_phone_for_log,
    parse_to_istanbul,
    rate_limit_blocked,
    rate_limit_record,
    rate_limit_retry_after,
)

logger = logging.getLogger("sadakat")

PURPOSE_NEW_DEVICE = "new_device_login"
CODE_DIGITS = 6
IP_SEND_LIMIT = 10          # aynı IP'den saatte en fazla bu kadar SMS (kafe Wi-Fi'ı ortak IP: yeni cihaz girişi nadirdir)
IP_SEND_WINDOW = 3600

# Yanıttaki OTP alanları (gönderim başarısızsa yerinde temizlenir; _hold_secret gibi özel anahtarlar korunur)
_OTP_KEYS = ("status", "challenge_id", "masked_phone", "expires_in", "resend_in", "attempts_left", "retry_after", "message")

MSG_REQUIRED = ("Bu numara başka bir cihazda kayıtlı. Güvenliğin için {masked} numarasına SMS ile 6 haneli bir kod "
                "gönderdik; kodu yazınca bu cihaz kartına bağlanır.")
MSG_INVALID = "Kod hatalı. {left} deneme hakkın kaldı."
MSG_EXPIRED = "Kodun süresi doldu ya da kod artık geçerli değil. Yeni kod iste."
MSG_LOCKED = "Çok fazla hatalı deneme yapıldı. Lütfen {wait} sonra tekrar dene ya da kasiyerden yardım iste."
MSG_DAILY = "Bu numaraya bugün çok fazla kod gönderildi. Yarın tekrar dene ya da kasiyerden yardım iste."
MSG_IP = "Bu bağlantıdan çok fazla kod istendi. Lütfen {wait} sonra tekrar dene."
MSG_COOLDOWN = "Yeni kod istemek için {wait} bekle."
MSG_UNAVAILABLE = "SMS gönderilemedi, lütfen kasiyerden yardım isteyin."
MSG_SENT = "Yeni kod {masked} numarasına gönderildi."
SMS_TEXT = "{business} doğrulama kodunuz: {code}. Bu kodu kimseyle paylaşmayın."


@dataclass
class OtpContext:
    """İstemcinin gönderdiği OTP alanları + IP. Yalnızca dört müşteri giriş ucu bu bağlamı geçer (panel kayıtları geçmez)."""
    challenge_id: Optional[str] = None
    code: Optional[str] = None
    ip: str = ""

    @classmethod
    def from_request(cls, challenge_id: Any, code: Any, ip: str) -> "OtpContext":
        cid = str(challenge_id).strip()[:128] if isinstance(challenge_id, str) and challenge_id.strip() else None
        raw = str(code).strip()[:32] if isinstance(code, str) and code.strip() else None
        return cls(challenge_id=cid, code=raw, ip=(ip or "")[:64])


class OtpRequired(Exception):
    """Cihaz bağlanamaz: `response` istemciye aynen dönecek sözleşme yanıtıdır (otp_required / otp_invalid / ...)."""

    def __init__(self, response: dict[str, Any]):
        super().__init__(response.get("status", "otp"))
        self.response = response


def _now() -> datetime:
    """Şimdiki zaman (testler süre dolumunu denemek için bu işlevi değiştirir)."""
    return get_istanbul_now()


def is_enabled() -> bool:
    return get_otp_mode() == "new_device"


def is_otp_status(status: Any) -> bool:
    return isinstance(status, str) and status.startswith("otp_")


def mask_phone_otp(phone_e164: str) -> str:
    """+905321234512 → '+90 5** *** ** 12' (yalnızca operatörün ilk hanesi ve son iki hane görünür)."""
    if not phone_e164 or len(phone_e164) != 13:
        return "***"
    d = phone_e164[3:]
    return f"+90 {d[0]}** *** ** {d[-2:]}"


def _code_hash(challenge_id: str, code: str) -> str:
    return hmac.new(SECRET_KEY.encode("utf-8"), (challenge_id + code).encode("utf-8"), hashlib.sha256).hexdigest()


def _ip_hash(ip: str) -> str:
    if not ip:
        return ""
    return hmac.new(SECRET_KEY.encode("utf-8"), f"otp-ip:{ip}".encode("utf-8"), hashlib.sha256).hexdigest()


def _new_code() -> str:
    return f"{secrets.randbelow(10 ** CODE_DIGITS):0{CODE_DIGITS}d}"


def _secs(delta: timedelta) -> int:
    return max(0, math.ceil(delta.total_seconds()))


def _human_wait(seconds: int) -> str:
    if seconds >= 3600:
        return f"{math.ceil(seconds / 3600)} saat"
    if seconds >= 60:
        return f"{math.ceil(seconds / 60)} dakika"
    return f"{max(1, seconds)} saniye"


def _seconds_to_midnight(now: datetime) -> int:
    local = now.astimezone(ISTANBUL_TZ)
    midnight = datetime(local.year, local.month, local.day, tzinfo=ISTANBUL_TZ) + timedelta(days=1)
    return max(1, _secs(midnight - local))


def _today_start_iso(now: datetime) -> str:
    local = now.astimezone(ISTANBUL_TZ)
    return datetime(local.year, local.month, local.day, tzinfo=ISTANBUL_TZ).isoformat()


# ------------------------------------------------------------------ yanıtlar (API sözleşmesi)
def _required(row: sqlite3.Row | dict, now: datetime, resend_seconds: int) -> dict[str, Any]:
    expires = parse_to_istanbul(row["expires_at"])
    sent = parse_to_istanbul(row["last_sent_at"]) or now
    masked = mask_phone_otp(row["phone_e164"])
    return {
        "status": "otp_required",
        "challenge_id": row["id"],
        "masked_phone": masked,
        "expires_in": _secs(expires - now),
        "resend_in": _secs(sent + timedelta(seconds=resend_seconds) - now),
        "message": MSG_REQUIRED.format(masked=masked),
    }


def _expired() -> dict[str, Any]:
    return {"status": "otp_expired", "message": MSG_EXPIRED}


def _locked(retry_after: int) -> dict[str, Any]:
    retry_after = max(1, retry_after)
    return {"status": "otp_locked", "retry_after": retry_after, "message": MSG_LOCKED.format(wait=_human_wait(retry_after))}


def _rate_limited(retry_after: int, message: str) -> dict[str, Any]:
    return {"status": "otp_rate_limited", "retry_after": max(1, retry_after), "message": message}


def unavailable() -> dict[str, Any]:
    return {"status": "otp_unavailable", "message": MSG_UNAVAILABLE}


# ------------------------------------------------------------------ yardımcılar
def device_linked(conn: sqlite3.Connection, customer_id: int, device_token: Optional[str]) -> bool:
    """Bu cihaz belirteci zaten BU müşteriye bağlı mı?"""
    if not device_token or not device_token.strip():
        return False
    return conn.execute(
        "SELECT 1 FROM devices WHERE token_hash = ? AND customer_id = ?", (hash_token(device_token.strip()), customer_id)
    ).fetchone() is not None


def _get_challenge(conn: sqlite3.Connection, challenge_id: Optional[str]) -> Optional[sqlite3.Row]:
    if not challenge_id:
        return None
    return conn.execute("SELECT * FROM otp_challenges WHERE id = ?", (challenge_id,)).fetchone()


def _is_expired(row: sqlite3.Row, now: datetime) -> bool:
    expires = parse_to_istanbul(row["expires_at"])
    return expires is None or now >= expires


def _sent_today(conn: sqlite3.Connection, phone_e164: str, now: datetime) -> int:
    # Bugün gönderim yapılmış her challenge'ın TÜM gönderimleri sayılır (gece yarısını aşan challenge fazla sayılır,
    # hiçbir gönderim eksik sayılmaz).
    return conn.execute(
        "SELECT COALESCE(SUM(send_count), 0) FROM otp_challenges WHERE phone_e164 = ? AND last_sent_at >= ?",
        (phone_e164, _today_start_iso(now)),
    ).fetchone()[0]


def _issue_code(
    conn: sqlite3.Connection, row: Optional[sqlite3.Row], phone_e164: str, ip: str, cfg: dict[str, Any], now: datetime,
) -> tuple[Optional[dict[str, Any]], Optional[sqlite3.Row], Optional[tuple]]:
    """
    Yeni kod üretir (yeni challenge ya da var olanın kodunu yeniler). Dönüş: (sınır yanıtı, None, None) ya da
    (None, güncel satır, bekleyen gönderim). Bekleyen gönderimi `_dispatch` COMMIT sonrasına bağlar.
    """
    if _sent_today(conn, phone_e164, now) >= cfg["daily_limit_per_phone"]:
        log_audit(conn, "system", "otp_daily_limit", f"OTP günlük gönderim sınırı doldu (tel: {mask_phone_for_log(phone_e164)}).")
        return _rate_limited(_seconds_to_midnight(now), MSG_DAILY), None, None
    ip_key = f"otp_send_{ip}" if ip else ""
    if ip_key and rate_limit_blocked(ip_key, IP_SEND_LIMIT, IP_SEND_WINDOW):
        wait = rate_limit_retry_after(ip_key, IP_SEND_LIMIT, IP_SEND_WINDOW) or IP_SEND_WINDOW
        return _rate_limited(wait, MSG_IP.format(wait=_human_wait(wait))), None, None

    code = _new_code()
    now_iso = now.isoformat()
    expires_iso = (now + timedelta(seconds=cfg["ttl_seconds"])).isoformat()
    if row is None:
        challenge_id = secrets.token_urlsafe(24)   # 192 bit
        conn.execute(
            "INSERT INTO otp_challenges (id, phone_e164, code_hash, purpose, created_at, expires_at, attempts, "
            "consumed_at, last_sent_at, send_count, ip_hash) VALUES (?, ?, ?, ?, ?, ?, 0, NULL, ?, 1, ?)",
            (challenge_id, phone_e164, _code_hash(challenge_id, code), PURPOSE_NEW_DEVICE, now_iso, expires_iso,
             now_iso, _ip_hash(ip)),
        )
        previous = None
    else:
        challenge_id = row["id"]
        previous = dict(row)
        # Yeniden gönderimde yeni kod üretilir (eski kod geçersiz olur); deneme sayacı SIFIRLANMAZ.
        conn.execute(
            "UPDATE otp_challenges SET code_hash = ?, expires_at = ?, last_sent_at = ?, send_count = send_count + 1, "
            "ip_hash = ? WHERE id = ?",
            (_code_hash(challenge_id, code), expires_iso, now_iso, _ip_hash(ip), challenge_id),
        )
    if ip_key:
        rate_limit_record(ip_key)
    fresh = _get_challenge(conn, challenge_id)
    business = (get_setting(conn, "business_name", DEFAULT_BUSINESS_NAME) or DEFAULT_BUSINESS_NAME).strip()
    text = SMS_TEXT.format(business=business, code=code)
    return None, fresh, (challenge_id, phone_e164, text, cfg["provider"], previous)


def _dispatch(conn: sqlite3.Connection, response: dict[str, Any], pending: tuple) -> None:
    """SMS'i COMMIT sonrasına bağlar; başarısızlıkta gönderimi geri alır ve `response`u yerinde değiştirir."""
    challenge_id, phone_e164, text, provider_name, previous = pending

    def _revert(target: sqlite3.Connection) -> None:
        if previous is None:
            target.execute("DELETE FROM otp_challenges WHERE id = ?", (challenge_id,))
        else:
            target.execute(
                "UPDATE otp_challenges SET code_hash = ?, expires_at = ?, last_sent_at = ?, send_count = ? WHERE id = ?",
                (previous["code_hash"], previous["expires_at"], previous["last_sent_at"], previous["send_count"], challenge_id),
            )

    def job(db_path: Any = None, inline_conn: Optional[sqlite3.Connection] = None) -> None:
        try:
            get_provider(provider_name).send(phone_e164, text)
            return
        except Exception as exc:   # sağlayıcı hatası ya da beklenmeyen hata: kod/metin günlüğe YAZILMAZ
            logger.warning("OTP SMS gönderilemedi (tel: %s, sağlayıcı: %s): %s",
                           mask_phone_for_log(phone_e164), provider_name, exc)
        try:
            if inline_conn is not None:
                _revert(inline_conn)
            else:
                with get_db(immediate=True, db_path=db_path) as c2:
                    _revert(c2)
        except Exception as exc:  # pragma: no cover - savunma
            logger.warning("OTP gönderimi geri alınamadı: %s", exc)
        for key in _OTP_KEYS:
            response.pop(key, None)
        response.update(unavailable())

    if not run_after_commit(conn, job):
        job(inline_conn=conn)   # get_db dışındaki bağlantı (yalnızca doğrudan çağrılar): hemen gönder


# ------------------------------------------------------------------ giriş kapısı
def gate_new_device(
    conn: sqlite3.Connection, phone_e164: str, customer_id: int, device_token: Optional[str],
    ctx: Optional[OtpContext],
) -> None:
    """
    Kayıtlı müşterinin numarası bu cihazdan girildiğinde çağrılır (loyalty.get_or_create_customer_with_device, HERHANGİ bir
    yazmadan önce). Geçebiliyorsa None döner (OTP kapalı / bağlam yok / cihaz zaten bağlı / kod doğru ve tüketildi).
    Aksi halde OtpRequired fırlatır; çağıran işlemi COMMIT etmelidir (deneme sayacı ve yeni challenge kalıcı olmalı).
    """
    if ctx is None or not is_enabled():
        return
    if device_linked(conn, customer_id, device_token):
        return
    cfg = get_otp_settings()
    now = _now()
    if ctx.challenge_id:
        row = _get_challenge(conn, ctx.challenge_id)
        if row is None or row["purpose"] != PURPOSE_NEW_DEVICE:
            raise OtpRequired(_expired())
        if row["phone_e164"] == phone_e164:
            _verify(conn, row, ctx.code, customer_id, cfg, now)
            return
        # Challenge başka numaraya ait (müşteri numarayı değiştirdi): alanlar yok sayılır, bu numaraya kod istenir.
    raise OtpRequired(_challenge(conn, phone_e164, ctx.ip, cfg, now))


def _verify(conn: sqlite3.Connection, row: sqlite3.Row, code: Optional[str], customer_id: int,
            cfg: dict[str, Any], now: datetime) -> None:
    max_attempts = cfg["max_attempts"]
    if row["consumed_at"] or _is_expired(row, now):
        raise OtpRequired(_expired())
    if row["attempts"] >= max_attempts:
        raise OtpRequired(_locked(_secs(parse_to_istanbul(row["expires_at"]) - now)))
    candidate = (code or "").strip()
    expected = _code_hash(row["id"], candidate if candidate else "-")
    ok = hmac.compare_digest(expected, row["code_hash"]) and len(candidate) == CODE_DIGITS and candidate.isdigit()
    if not ok:
        conn.execute("UPDATE otp_challenges SET attempts = attempts + 1 WHERE id = ?", (row["id"],))
        left = max_attempts - (row["attempts"] + 1)
        if left <= 0:
            log_audit(conn, f"customer:{customer_id}", "otp_locked", "Yeni cihaz SMS kodu çok kez hatalı girildi; kilitlendi.")
            raise OtpRequired(_locked(_secs(parse_to_istanbul(row["expires_at"]) - now)))
        raise OtpRequired({"status": "otp_invalid", "attempts_left": left, "message": MSG_INVALID.format(left=left)})
    used = conn.execute(
        "UPDATE otp_challenges SET consumed_at = ? WHERE id = ? AND consumed_at IS NULL", (now.isoformat(), row["id"])
    ).rowcount
    if not used:   # aynı kod eşzamanlı iki istekte: yalnızca biri geçer
        raise OtpRequired(_expired())
    log_audit(conn, f"customer:{customer_id}", "otp_verified", "Yeni cihaz SMS koduyla doğrulandı ve karta bağlandı.")


def _challenge(conn: sqlite3.Connection, phone_e164: str, ip: str, cfg: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Kod iste: geçerli challenge varsa ve bekleme dolmadıysa onu döner (SMS yok); yoksa kod üretip gönderir."""
    row = conn.execute(
        "SELECT * FROM otp_challenges WHERE phone_e164 = ? AND purpose = ? AND consumed_at IS NULL "
        "ORDER BY created_at DESC LIMIT 1",
        (phone_e164, PURPOSE_NEW_DEVICE),
    ).fetchone()
    if row is not None and _is_expired(row, now):
        row = None
    if row is not None:
        if row["attempts"] >= cfg["max_attempts"]:
            return _locked(_secs(parse_to_istanbul(row["expires_at"]) - now))
        sent = parse_to_istanbul(row["last_sent_at"])
        if sent is not None and now - sent < timedelta(seconds=cfg["resend_seconds"]):
            return _required(row, now, cfg["resend_seconds"])      # aynı challenge, yeni SMS yok
    limited, fresh, pending = _issue_code(conn, row, phone_e164, ip, cfg, now)
    if limited is not None:
        return limited
    response = _required(fresh, now, cfg["resend_seconds"])
    _dispatch(conn, response, pending)
    return response


def resend(conn: sqlite3.Connection, challenge_id: Optional[str], ip: str) -> dict[str, Any]:
    """POST /api/auth/otp/resend: aynı challenge için yeni kod gönderir (bekleme / günlük sınır / IP sınırı)."""
    cfg = get_otp_settings()
    now = _now()
    cid = challenge_id.strip()[:128] if isinstance(challenge_id, str) else ""
    row = _get_challenge(conn, cid) if (cid and is_enabled()) else None
    if row is None or row["purpose"] != PURPOSE_NEW_DEVICE or row["consumed_at"] or _is_expired(row, now):
        return _expired()
    if row["attempts"] >= cfg["max_attempts"]:
        # Kilitli challenge'a yeni kod gönderilmez (kilit süresi dolunca istemci isteği baştan gönderir)
        wait = _secs(parse_to_istanbul(row["expires_at"]) - now)
        return _rate_limited(wait, MSG_LOCKED.format(wait=_human_wait(max(1, wait))))
    sent = parse_to_istanbul(row["last_sent_at"])
    if sent is not None:
        wait = _secs(sent + timedelta(seconds=cfg["resend_seconds"]) - now)
        if wait > 0:
            return _rate_limited(wait, MSG_COOLDOWN.format(wait=_human_wait(wait)))
    limited, fresh, pending = _issue_code(conn, row, row["phone_e164"], ip, cfg, now)
    if limited is not None:
        return limited
    required = _required(fresh, now, cfg["resend_seconds"])
    response = {
        "status": "otp_sent",
        "expires_in": required["expires_in"],
        "resend_in": required["resend_in"],
        "message": MSG_SENT.format(masked=required["masked_phone"]),
    }
    _dispatch(conn, response, pending)
    return response

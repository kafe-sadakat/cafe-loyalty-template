"""
Yeni cihaz doğrulaması: tek kullanımlık SMS kodu (OTP). Ayrıntılı belge: docs/OTP.md.

Kural (OTP_MODE=new_device): KAYITLI (silinmemiş) bir müşterinin numarası, o müşteriye bağlı OLMAYAN bir cihazdan
(cihaz belirteci yok / başka müşteriye bağlı / sunucuda kaydı yok) girilirse cihaz bağlanmaz, numaraya 6 haneli kod gider
ve istemci {"status": "otp_required", ...} alır. Kod doğrulanınca istek bugünkü gibi işlenir. Yeni kayıt ve zaten bağlı
cihaz kod istemez. OTP_MODE=off (varsayılan) iken bu modül hiçbir şey yapmaz ve hiçbir OTP ayarını okumaz.

HESAP NUMARALANDIRMA (bilinçli kabul edilmiş ödünleşim): "otp_required" yanıtı, numaranın kayıtlı bir müşteriye ait
olduğunu ele verir (yeni numara doğrudan kayıt olur). İşletme sahibi bu politikayı seçti; zararı IP başına istek sınırları,
numara başına günlük SMS sınırı ve maskeli numara (+90 5** *** ** 12) ile sınırlanır.

Güvenlik özellikleri:
- Kod `secrets` ile üretilen challenge'a özel rastgele tohumdan ve SECRET_KEY'den türetilir (6 hane); veritabanında kodun
  kendisi değil HMAC-SHA256(SECRET_KEY, "otp-code:" + challenge_id + ":" + kod) özeti ve tohum durur. Yeniden gönderimde
  AYNI kod gider (kod döndürülmez): başkasının "Tekrar gönder"i müşterinin elindeki kodu geçersiz kılamaz.
- Karşılaştırma sabit zamanlıdır (hmac.compare_digest). Kod tek kullanımlıktır (koşullu UPDATE ile tüketilir).
- Hatalı denemeler (challenge, IP) başına sayılır: OTP_MAX_ATTEMPTS'i aşan IP o challenge için kilitlenir, diğer IP'ler
  kilitlenmez. Challenge başına mutlak tavan (ATTEMPT_CEILING) dolunca challenge herkes için kilitlenir.
- SMS sınırları veritabanında: numara başına günlük, IP (IPv6'da /64) başına saatlik, genel saatlik ve günlük.
- Kasiyer / müdür, müşteri yanındayken numaranın kilitlerini ve günlük sayacını sıfırlayabilir (reset_phone, denetlenir).
- Kod hiçbir yanıtta dönmez; yalnızca OTP_PROVIDER=console (geliştirme, OTP_ALLOW_CONSOLE=1) kodu günlüğe yazar.
- SMS, veritabanı işlemi COMMIT edildikten sonra (yazma kilidi tutulmadan) gönderilir (db.run_after_commit). Gönderim
  başarısız olursa bu gönderim geri alınır ve aynı yanıt nesnesi yerinde "otp_unavailable" yapılır.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
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
    normalize_phone,
    parse_to_istanbul,
    phone_hash,
)

logger = logging.getLogger("sadakat")

PURPOSE_NEW_DEVICE = "new_device_login"
CODE_DIGITS = 6
IP_SEND_LIMIT = 10          # aynı IP'den (IPv6: aynı /64) saatte en fazla bu kadar SMS (kafe Wi-Fi'ı ortak IP)
IP_SEND_WINDOW = 3600
ATTEMPT_CEILING = 10        # bir challenge'a tüm IP'lerden toplam hatalı deneme tavanı (dolunca herkes için kilit)

# Yanıttaki OTP alanları (gönderim başarısızsa yerinde temizlenir; _hold_secret gibi özel anahtarlar korunur)
_OTP_KEYS = ("status", "challenge_id", "masked_phone", "expires_in", "resend_in", "attempts_left", "retry_after", "message")

MSG_REQUIRED = ("Bu numara başka bir cihazda kayıtlı. Güvenliğin için {masked} numarasına SMS ile 6 haneli bir kod "
                "gönderdik; kodu yazınca bu cihaz kartına bağlanır.")
MSG_INVALID = "Kod hatalı. {left} deneme hakkın kaldı."
MSG_EXPIRED = "Kodun süresi doldu ya da kod artık geçerli değil. Yeni kod iste."
MSG_LOCKED = "Çok fazla hatalı deneme yapıldı. Lütfen {wait} sonra tekrar dene ya da kasiyerden yardım iste."
MSG_DAILY = "Bu numaraya bugün çok fazla kod gönderildi. Yarın tekrar dene ya da kasiyerden yardım iste."
MSG_IP = "Bu bağlantıdan çok fazla kod istendi. Lütfen {wait} sonra tekrar dene."
MSG_GLOBAL = "Şu anda çok fazla doğrulama kodu istendi. Lütfen {wait} sonra tekrar dene ya da kasiyerden yardım iste."
MSG_COOLDOWN = "Kodu yeniden göndermek için {wait} bekle."
MSG_UNAVAILABLE = "SMS gönderilemedi, lütfen kasiyerden yardım isteyin."
MSG_SENT = "Kod {masked} numarasına yeniden gönderildi."
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


def _hmac(message: str) -> str:
    return hmac.new(SECRET_KEY.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def _code_hash(challenge_id: str, code: str) -> str:
    # Alan ayrımı ("otp-code:"): aynı SECRET_KEY'le üretilen diğer HMAC'lerle (telefon / IP özeti) karışmaz
    return _hmac(f"otp-code:{challenge_id}:{code}")


def _derive_code(challenge_id: str, seed: str) -> str:
    digest = hmac.new(SECRET_KEY.encode("utf-8"), f"otp-code-derive:{challenge_id}:{seed}".encode("utf-8"),
                      hashlib.sha256).digest()
    return f"{int.from_bytes(digest, 'big') % (10 ** CODE_DIGITS):0{CODE_DIGITS}d}"


def ip_key(ip: str) -> str:
    """Sınırlar için IP anahtarı: IPv4 olduğu gibi, IPv6 /64 ağı (bir abonenin tüm adresleri tek anahtar)."""
    raw = (ip or "").strip()
    if not raw:
        return ""
    try:
        addr = ipaddress.ip_address(raw)
    except ValueError:
        return raw
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None:
            return str(addr.ipv4_mapped)
        return str(ipaddress.ip_network(f"{addr}/64", strict=False))
    return str(addr)


def _ip_hash(ip: str) -> str:
    key = ip_key(ip)
    return _hmac(f"otp-ip:{key}") if key else ""


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
        "resend_in": min(_secs(sent + timedelta(seconds=resend_seconds) - now), _secs(expires - now)),
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


def _until_expiry(row: sqlite3.Row, now: datetime) -> int:
    return _secs(parse_to_istanbul(row["expires_at"]) - now)


def _ip_attempts(conn: sqlite3.Connection, challenge_id: str, ip: str) -> int:
    got = conn.execute(
        "SELECT attempts FROM otp_attempts WHERE challenge_id = ? AND ip_hash = ?", (challenge_id, _ip_hash(ip))
    ).fetchone()
    return int(got[0]) if got else 0


def _lock_state(conn: sqlite3.Connection, row: sqlite3.Row, ip: str, cfg: dict[str, Any]) -> Optional[str]:
    """'all' (challenge tavanı doldu: herkes için kilitli) | 'ip' (bu IP kilitli) | None."""
    if row["attempts"] >= ATTEMPT_CEILING:
        return "all"
    if _ip_attempts(conn, row["id"], ip) >= cfg["max_attempts"]:
        return "ip"
    return None


def _mark_once(conn: sqlite3.Connection, key: str, now: datetime) -> bool:
    """Aynı olay için yalnızca bir denetim kaydı yazılsın: işaret yeni eklendiyse True."""
    return conn.execute(
        "INSERT OR IGNORE INTO otp_audit_marks (key, created_at) VALUES (?, ?)", (key, now.isoformat())
    ).rowcount == 1


# ------------------------------------------------------------------ SMS gönderim sınırları (veritabanında)
def _send_limit(conn: sqlite3.Connection, phone_e164: str, ip: str, cfg: dict[str, Any], now: datetime,
                ) -> Optional[dict[str, Any]]:
    """Gönderim yapılamıyorsa otp_rate_limited yanıtı; yapılabiliyorsa None. Sıra: numara, IP, genel saatlik, genel günlük."""
    today = _today_start_iso(now)
    ph = phone_hash(phone_e164)
    sent_today = conn.execute(
        "SELECT COUNT(*) FROM otp_sends WHERE phone_hash = ? AND counts_for_phone = 1 AND sent_at >= ?", (ph, today)
    ).fetchone()[0]
    if sent_today >= cfg["daily_limit_per_phone"]:
        if _mark_once(conn, f"daily_phone:{ph}:{today[:10]}", now):   # numara başına günde TEK denetim kaydı
            log_audit(conn, "system", "otp_daily_limit",
                      f"OTP günlük gönderim sınırı doldu (tel: {mask_phone_for_log(phone_e164)}).")
        return _rate_limited(_seconds_to_midnight(now), MSG_DAILY)

    hour_ago = (now - timedelta(seconds=IP_SEND_WINDOW)).isoformat()
    ip_h = _ip_hash(ip)
    if ip_h:
        rows = conn.execute(
            "SELECT sent_at FROM otp_sends WHERE ip_hash = ? AND sent_at >= ? ORDER BY sent_at", (ip_h, hour_ago)
        ).fetchall()
        if len(rows) >= IP_SEND_LIMIT:
            oldest = parse_to_istanbul(rows[len(rows) - IP_SEND_LIMIT]["sent_at"]) or now
            wait = max(1, _secs(oldest + timedelta(seconds=IP_SEND_WINDOW) - now))
            return _rate_limited(wait, MSG_IP.format(wait=_human_wait(wait)))

    hourly = conn.execute(
        "SELECT sent_at FROM otp_sends WHERE sent_at >= ? ORDER BY sent_at", (hour_ago,)
    ).fetchall()
    if len(hourly) >= cfg["global_hourly_limit"]:
        oldest = parse_to_istanbul(hourly[len(hourly) - cfg["global_hourly_limit"]]["sent_at"]) or now
        wait = max(1, _secs(oldest + timedelta(hours=1) - now))
        if _mark_once(conn, f"global_hour:{now.astimezone(ISTANBUL_TZ):%Y-%m-%dT%H}", now):
            log_audit(conn, "system", "otp_global_limit",
                      f"OTP genel saatlik SMS sınırı ({cfg['global_hourly_limit']}) doldu; SMS gönderimi durduruldu.")
        return _rate_limited(wait, MSG_GLOBAL.format(wait=_human_wait(wait)))

    daily = conn.execute("SELECT COUNT(*) FROM otp_sends WHERE sent_at >= ?", (today,)).fetchone()[0]
    if daily >= cfg["global_daily_limit"]:
        wait = _seconds_to_midnight(now)
        if _mark_once(conn, f"global_day:{today[:10]}", now):
            log_audit(conn, "system", "otp_global_limit",
                      f"OTP genel günlük SMS sınırı ({cfg['global_daily_limit']}) doldu; SMS gönderimi yarına kadar durdu.")
        return _rate_limited(wait, MSG_GLOBAL.format(wait=_human_wait(wait)))
    return None


def _issue_code(
    conn: sqlite3.Connection, row: Optional[sqlite3.Row], phone_e164: str, ip: str, cfg: dict[str, Any], now: datetime,
) -> tuple[Optional[dict[str, Any]], Optional[sqlite3.Row], Optional[tuple]]:
    """
    Kodu gönderime hazırlar: `row` yoksa yeni challenge (yeni kod), varsa AYNI kodu yeniden gönderir (kod ve süre
    DEĞİŞMEZ; deneme sayaçları sıfırlanmaz). Dönüş: (sınır yanıtı, None, None) ya da (None, güncel satır, bekleyen
    gönderim). Bekleyen gönderimi `_dispatch` COMMIT sonrasına bağlar.
    """
    limited = _send_limit(conn, phone_e164, ip, cfg, now)
    if limited is not None:
        return limited, None, None

    now_iso = now.isoformat()
    if row is None:
        challenge_id = secrets.token_urlsafe(24)   # 192 bit
        seed = secrets.token_hex(16)
        code = _derive_code(challenge_id, seed)
        expires_iso = (now + timedelta(seconds=cfg["ttl_seconds"])).isoformat()
        conn.execute(
            "INSERT INTO otp_challenges (id, phone_e164, code_hash, purpose, created_at, expires_at, attempts, "
            "consumed_at, last_sent_at, send_count, ip_hash, code_seed) VALUES (?, ?, ?, ?, ?, ?, 0, NULL, ?, 1, ?, ?)",
            (challenge_id, phone_e164, _code_hash(challenge_id, code), PURPOSE_NEW_DEVICE, now_iso, expires_iso,
             now_iso, _ip_hash(ip), seed),
        )
        previous = None
    else:
        challenge_id = row["id"]
        previous = dict(row)
        code = _derive_code(challenge_id, row["code_seed"])
        conn.execute(
            "UPDATE otp_challenges SET last_sent_at = ?, send_count = send_count + 1 WHERE id = ?", (now_iso, challenge_id)
        )
    send_id = conn.execute(
        "INSERT INTO otp_sends (sent_at, phone_hash, ip_hash, challenge_id) VALUES (?, ?, ?, ?)",
        (now_iso, phone_hash(phone_e164), _ip_hash(ip), challenge_id),
    ).lastrowid
    fresh = _get_challenge(conn, challenge_id)
    business = (get_setting(conn, "business_name", DEFAULT_BUSINESS_NAME) or DEFAULT_BUSINESS_NAME).strip()
    text = SMS_TEXT.format(business=business, code=code)
    return None, fresh, (challenge_id, phone_e164, text, cfg["provider"], previous, send_id)


def _dispatch(conn: sqlite3.Connection, response: dict[str, Any], pending: tuple) -> None:
    """SMS'i COMMIT sonrasına bağlar; başarısızlıkta gönderimi geri alır ve `response`u yerinde değiştirir."""
    challenge_id, phone_e164, text, provider_name, previous, send_id = pending

    def _revert(target: sqlite3.Connection) -> None:
        target.execute("DELETE FROM otp_sends WHERE id = ?", (send_id,))   # gitmeyen SMS sınırlardan düşülür
        if previous is None:
            target.execute("DELETE FROM otp_challenges WHERE id = ?", (challenge_id,))
            target.execute("DELETE FROM otp_attempts WHERE challenge_id = ?", (challenge_id,))
        else:
            target.execute(
                "UPDATE otp_challenges SET last_sent_at = ?, send_count = ? WHERE id = ?",
                (previous["last_sent_at"], previous["send_count"], challenge_id),
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
            _verify(conn, row, ctx.code, customer_id, ctx.ip, cfg, now)
            return
        # Challenge başka numaraya ait (müşteri numarayı değiştirdi): alanlar yok sayılır, bu numaraya kod istenir.
    raise OtpRequired(_challenge(conn, phone_e164, ctx.ip, cfg, now))


def _verify(conn: sqlite3.Connection, row: sqlite3.Row, code: Optional[str], customer_id: int, ip: str,
            cfg: dict[str, Any], now: datetime) -> None:
    max_attempts = cfg["max_attempts"]
    if row["consumed_at"] or _is_expired(row, now):
        raise OtpRequired(_expired())
    if _lock_state(conn, row, ip, cfg):
        raise OtpRequired(_locked(_until_expiry(row, now)))
    candidate = (code or "").strip()
    expected = _code_hash(row["id"], candidate if candidate else "-")
    ok = hmac.compare_digest(expected, row["code_hash"]) and len(candidate) == CODE_DIGITS and candidate.isdigit()
    if not ok:
        ip_h = _ip_hash(ip)
        conn.execute("UPDATE otp_challenges SET attempts = attempts + 1 WHERE id = ?", (row["id"],))
        conn.execute(
            "INSERT INTO otp_attempts (challenge_id, ip_hash, attempts) VALUES (?, ?, 1) "
            "ON CONFLICT(challenge_id, ip_hash) DO UPDATE SET attempts = attempts + 1",
            (row["id"], ip_h),
        )
        ip_used = _ip_attempts(conn, row["id"], ip)
        left = min(max_attempts - ip_used, ATTEMPT_CEILING - (row["attempts"] + 1))
        if left <= 0:
            scope = "tüm cihazlar için" if row["attempts"] + 1 >= ATTEMPT_CEILING else "bu bağlantı için"
            log_audit(conn, f"customer:{customer_id}", "otp_locked",
                      f"Yeni cihaz SMS kodu çok kez hatalı girildi; {scope} kilitlendi.")
            raise OtpRequired(_locked(_until_expiry(row, now)))
        raise OtpRequired({"status": "otp_invalid", "attempts_left": left, "message": MSG_INVALID.format(left=left)})
    used = conn.execute(
        "UPDATE otp_challenges SET consumed_at = ? WHERE id = ? AND consumed_at IS NULL", (now.isoformat(), row["id"])
    ).rowcount
    if not used:   # aynı kod eşzamanlı iki istekte: yalnızca biri geçer
        raise OtpRequired(_expired())
    log_audit(conn, f"customer:{customer_id}", "otp_verified", "Yeni cihaz SMS koduyla doğrulandı ve karta bağlandı.")


def _open_challenge(conn: sqlite3.Connection, phone_e164: str, now: datetime) -> Optional[sqlite3.Row]:
    """Numaranın süresi dolmamış, kullanılmamış en yeni challenge'ı (tohumsuz eski kayıtlar yeniden gönderilemez)."""
    row = conn.execute(
        "SELECT * FROM otp_challenges WHERE phone_e164 = ? AND purpose = ? AND consumed_at IS NULL "
        "ORDER BY created_at DESC LIMIT 1",
        (phone_e164, PURPOSE_NEW_DEVICE),
    ).fetchone()
    if row is None or _is_expired(row, now) or not row["code_seed"]:
        return None
    return row


def _challenge(conn: sqlite3.Connection, phone_e164: str, ip: str, cfg: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Kod iste: geçerli challenge varsa onu döner (bekleme dolduysa AYNI kodu yeniden gönderir); yoksa yenisini açar."""
    row = _open_challenge(conn, phone_e164, now)
    if row is not None:
        if _lock_state(conn, row, ip, cfg):
            return _locked(_until_expiry(row, now))
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
    """
    POST /api/auth/otp/resend: aynı challenge'ın AYNI kodunu yeniden gönderir (kod döndürülmez, süre uzamaz). Başka bir
    IP'nin yeniden gönderme isteği müşterinin elindeki kodu geçersiz kılamaz. Bekleme / günlük / IP / genel sınırlar.
    """
    if not is_enabled():
        return _expired()
    cfg = get_otp_settings()
    now = _now()
    cid = challenge_id.strip()[:128] if isinstance(challenge_id, str) else ""
    row = _get_challenge(conn, cid) if cid else None
    if (row is None or row["purpose"] != PURPOSE_NEW_DEVICE or row["consumed_at"] or _is_expired(row, now)
            or not row["code_seed"]):
        return _expired()
    if _lock_state(conn, row, ip, cfg):
        # Kilitli challenge'a kod gönderilmez (kilit süresi dolunca istemci isteği baştan gönderir)
        wait = max(1, _until_expiry(row, now))
        return _rate_limited(wait, MSG_LOCKED.format(wait=_human_wait(wait)))
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


# ------------------------------------------------------------------ çerezsiz istemci: ayrılmış damga kodu ↔ challenge
def bind_hold_to_challenge(conn: sqlite3.Connection, code_row_id: int, challenge_id: str) -> None:
    """
    Kayıt sırasında SMS kodu istenince bu isteğe ayrılan damga kodu challenge'a da bağlanır (çerez tutmayan yerel uygulama
    için). Ayrılma süresi en az challenge'ın süresi kadar uzatılır (kod doğrulanana dek damga başkasına geçmez).
    """
    ch = _get_challenge(conn, challenge_id)
    if ch is None:
        return
    current = conn.execute("SELECT held_until FROM stamp_codes WHERE id = ?", (code_row_id,)).fetchone()
    held = parse_to_istanbul(current["held_until"]) if current else None
    ch_exp = parse_to_istanbul(ch["expires_at"])
    until = max(held, ch_exp) if (held and ch_exp) else (held or ch_exp)
    conn.execute(
        "UPDATE stamp_codes SET hold_challenge_hash = ?, held_until = ? WHERE id = ? AND used_at IS NULL",
        (hash_token(challenge_id), until.isoformat() if until else None, code_row_id),
    )


def owns_held_code(conn: sqlite3.Connection, code_row: sqlite3.Row, ctx: Optional[OtpContext],
                   phone_raw: Optional[str]) -> bool:
    """
    Çerezsiz istemci, kendisine ayrılmış damga kodunu challenge kimliğiyle sahiplenebilir mi? Koşullar: OTP açık, istek
    otp_challenge_id taşıyor, kod tam bu challenge'a bağlı, challenge kullanılmamış / süresi dolmamış VE istekteki numara
    challenge'ın numarası. Bu yalnızca SAHİPLİKTİR: damga yine de gate_new_device'ta doğru SMS kodu olmadan verilmez
    (yanlış kod → otp_invalid, kilit → otp_locked). Başkasının challenge'ı başka bir numarayla ya da başka bir koda
    bağlı ayrılmış damgayı alamaz; damga her durumda challenge'ın numarasına (kod sahibine) yazılır.
    """
    if ctx is None or not ctx.challenge_id or not is_enabled():
        return False
    bound = code_row["hold_challenge_hash"] if "hold_challenge_hash" in code_row.keys() else None
    if not bound or not hmac.compare_digest(hash_token(ctx.challenge_id), bound):
        return False
    ch = _get_challenge(conn, ctx.challenge_id)
    if ch is None or ch["purpose"] != PURPOSE_NEW_DEVICE or ch["consumed_at"] or _is_expired(ch, _now()):
        return False
    try:
        phone = normalize_phone(phone_raw or "")
    except ValueError:
        return False
    return hmac.compare_digest(ch["phone_e164"], phone)


# ------------------------------------------------------------------ kasiyer / müdür yardımı
def reset_phone(conn: sqlite3.Connection, phone_e164: str, actor: str) -> dict[str, int]:
    """
    "Doğrulama kilidini sıfırla" (müşteri kasada, kimliği görülerek): numaranın açık challenge'larındaki hatalı deneme
    sayaçlarını (tüm IP'ler) ve bugünkü numara başı SMS sayacını sıfırlar. Kod DEĞİŞMEZ: müşteri elindeki SMS kodunu
    yazabilir ya da yeni kod isteyebilir. Genel (işletme geneli) SMS sınırları sıfırlanmaz. Denetim günlüğüne yazılır.
    """
    now = _now()
    open_ids = [r["id"] for r in conn.execute(
        "SELECT id FROM otp_challenges WHERE phone_e164 = ? AND consumed_at IS NULL AND expires_at > ?",
        (phone_e164, now.isoformat()),
    ).fetchall()]
    for cid in open_ids:
        conn.execute("UPDATE otp_challenges SET attempts = 0 WHERE id = ?", (cid,))
        conn.execute("DELETE FROM otp_attempts WHERE challenge_id = ?", (cid,))
    today = _today_start_iso(now)
    ph = phone_hash(phone_e164)
    sends = conn.execute(
        "UPDATE otp_sends SET counts_for_phone = 0 WHERE phone_hash = ? AND counts_for_phone = 1 AND sent_at >= ?",
        (ph, today),
    ).rowcount
    conn.execute("DELETE FROM otp_audit_marks WHERE key = ?", (f"daily_phone:{ph}:{today[:10]}",))
    log_audit(conn, actor, "otp_reset",
              f"SMS doğrulama kilidi ve günlük sayaç sıfırlandı (tel: {mask_phone_for_log(phone_e164)}, "
              f"açık kod: {len(open_ids)}, sayaçtan düşülen SMS: {sends}).")
    return {"challenges": len(open_ids), "sends": sends}

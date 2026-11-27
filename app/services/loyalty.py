import logging
import re
import sqlite3
from datetime import datetime, timedelta
from typing import Optional, Any
from app.utils import (
    ISTANBUL_TZ,
    hash_token,
    generate_token,
    normalize_phone,
    phone_hash,
    format_phone,
    mask_phone,
    mask_phone_for_log,
    get_istanbul_now,
    get_istanbul_now_iso,
    parse_to_istanbul,
    rate_limit_blocked,
    rate_limit_record,
)
from app.config import DEFAULT_BUSINESS_NAME, is_push_enabled
from app.db import REGISTER_SEAL_TOKEN, get_all_settings, get_kvkk_text, get_setting, log_audit
from app.services.push import link_anonymous_subscriptions, notify_customer_after_stamp
from app.services.directus_sync import anonymize_directus_customer, sync_customer_to_directus
from app.services import activity, fraud, nfc_sun, stamp_codes
from app.services import otp as otp_service
from app.services.otp import OtpContext, OtpRequired
logger = logging.getLogger("sadakat")

DEVICE_COOKIE_NAME = "sadakat_device_token"
DEVICE_COOKIE_MAX_AGE = 31536000  # 1 yıl (365 gün, cihaz çerezinin ömrü)

# KVKK: sadakat programı verisi "sözleşmenin kurulması/ifası" (m.5/2-c) dayanağıyla işlenir; bu nedenle kayıt için
# YALNIZCA aydınlatma metninin okunduğu teyidi istenir. Açık rıza hizmet şartı yapılamaz (Kurul kararları,
# 2026/347 İlke Kararı): açık rıza yalnızca isteğe bağlı kampanya/ticari ileti kutusunda, ayrı metinle alınır.
CONSENT_REQUIRED_MSG = "Devam etmek için Aydınlatma Metni'ni okuyup anladığınızı onaylamanız gerekir."
_BONUS_FLAG_COLUMNS = {"pwa_bonus_claimed", "notif_bonus_claimed"}


class ConsentRequired(Exception):
    """Yeni müşteri kaydı için 'Aydınlatma Metni'ni okudum, anladım' teyidi eksik."""


# ---------------------------------------------------------------------------
# ANTI-FRAUD: silinen hesap kayıt defteri (deleted_customers_registry)
# ---------------------------------------------------------------------------
def register_deleted_identity(conn: sqlite3.Connection, phone_e164: str, device_hashes: list[str]) -> None:
    """
    Hesabını silen kişinin telefon numarasını ve cihaz imzalarını (token hash) kayıt defterine işler.
    Numara asla düz metin saklanmaz; yalnızca HMAC özeti tutulur.
    """
    now_iso = get_istanbul_now_iso()
    keys: list[tuple[str, str]] = []
    if phone_e164 and not phone_e164.startswith("+900000000000"):
        keys.append(("phone", phone_hash(phone_e164)))
    keys.extend(("device", h) for h in device_hashes if h)
    for key_type, key_hash in keys:
        conn.execute(
            """
            INSERT INTO deleted_customers_registry (key_type, key_hash, first_deleted_at, last_deleted_at, delete_count)
            VALUES (?, ?, ?, ?, 1)
            ON CONFLICT(key_type, key_hash) DO UPDATE SET
                last_deleted_at = excluded.last_deleted_at, delete_count = delete_count + 1
            """,
            (key_type, key_hash, now_iso, now_iso),
        )


def is_returning_deleted_identity(conn: sqlite3.Connection, phone_e164: str, device_hashes: list[str]) -> bool:
    """Telefon veya cihaz, daha önce hesabını silmiş birine mi ait?"""
    hashes = [h for h in device_hashes if h]
    if phone_e164 and conn.execute(
        "SELECT 1 FROM deleted_customers_registry WHERE key_type = 'phone' AND key_hash = ?",
        (phone_hash(phone_e164),),
    ).fetchone():
        return True
    for h in hashes:
        if conn.execute(
            "SELECT 1 FROM deleted_customers_registry WHERE key_type = 'device' AND key_hash = ?", (h,)
        ).fetchone():
            return True
    return False


def apply_returning_user_guard(
    conn: sqlite3.Connection, customer_id: int, phone_e164: str, device_hashes: list[str]
) -> bool:
    """
    Hesabını silip yeniden kayıt olan numara/cihaz için: hesap sıfırdan başlar (0 damga) ve
    indirme + bildirim hediyeleri KİLİTLENİR (bir daha verilmez). Kilitlendiyse True döner.
    """
    if not is_returning_deleted_identity(conn, phone_e164, device_hashes):
        return False
    conn.execute(
        "UPDATE customers SET pwa_bonus_claimed = 1, notif_bonus_claimed = 1, is_re_registered = 1 WHERE id = ?", (customer_id,)
    )
    log_audit(
        conn, f"customer:{customer_id}", "returning_deleted_identity",
        "Daha önce hesabını silmiş numara/cihaz yeniden kayıt oldu: hediye bonusları kilitlendi, hesap sıfırdan başladı.",
    )
    return True


def consent_is_valid(consent: Optional[dict[str, Any]]) -> bool:
    """Aydınlatma metninin okunup anlaşıldığı teyit edilmiş mi? Açık rıza kutusu kayıt ekranında zorunludur (ADR-23),
    sunucu ise yalnızca aydınlatma teyidini şart koşar (panel/personel kayıtları ve eski istemcilerle uyum)."""
    return bool(consent and consent.get("kvkk_read"))


def record_consent(
    conn: sqlite3.Connection,
    customer_id: int,
    consent: Optional[dict[str, Any]],
    source: str = "self",
) -> None:
    """
    Aydınlatma teyidini (kvkk_read_at) IP, metin sürümü ve kaynakla saklar (önceki kaydın üzerine yazmaz).
    Kayıt ekranındaki açık rıza kutusu (ADR-23: arayüzde zorunlu) işaretliyse ayrı "Açık Rıza ve Ticari Elektronik İleti Onayı" kaydedilir
    (marketing_consent + explicit_consent_at); denetim günlüğü ETK ispat yükü için onay metni sürümünü tutar.
    """
    if not consent:
        return
    now_iso = get_istanbul_now_iso()
    ip = (consent.get("ip") or "")[:64]
    version = get_setting(conn, "kvkk_version", "")
    cursor = conn.cursor()

    if consent_is_valid(consent):
        cursor.execute(
            """
            UPDATE customers
            SET kvkk_read_at = ?, consent_ip = ?, consent_version = ?, consent_source = ?
            WHERE id = ? AND kvkk_read_at IS NULL
            """,
            (now_iso, ip, version, source, customer_id),
        )
        if cursor.rowcount:
            log_audit(
                conn, f"customer:{customer_id}", "consent_recorded",
                f"Aydınlatma metni okundu/anlaşıldı teyidi alındı (sürüm={version}, kaynak={source}, ip={ip}).",
            )

    if consent.get("marketing"):
        cursor.execute(
            "SELECT COALESCE(marketing_consent, 0) AS mc FROM customers WHERE id = ?", (customer_id,)
        )
        row = cursor.fetchone()
        if row and not row["mc"]:
            set_marketing_consent(conn, customer_id, True, ip=ip, channel=f"kayıt formu ({source})")


def set_marketing_consent(
    conn: sqlite3.Connection, customer_id: int, given: bool, ip: str = "", channel: str = "uygulama"
) -> None:
    """
    Açık Rıza + Ticari Elektronik İleti (kampanya bildirimi) onayını verir veya geri alır.
    Onay/ret zamanı, IP, kanal ve onay metni sürümü denetim günlüğüne yazılır (ETK: ispat yükü hizmet sağlayıcıda).
    Geri alma (ret) anında geçerlidir: sonraki kampanya gönderiminde bu müşteri hedeflenmez.
    """
    now_iso = get_istanbul_now_iso()
    text_version = get_setting(conn, "marketing_consent_version", "")
    # Y-08: onay/ret kararının sürümü, kanalı ve IP'si müşteri satırında da kalıcı tutulur (denetim günlüğü yalnızca 3 yıl saklanır)
    proof = (text_version or None, (channel or "")[:80] or None, (ip or "")[:64] or None)
    if given:
        conn.execute(
            "UPDATE customers SET marketing_consent = 1, marketing_consent_at = ?, explicit_consent_at = ?, "
            "marketing_consent_version = ?, marketing_consent_channel = ?, marketing_consent_ip = ?, "
            "marketing_reconfirm_pending = 0 WHERE id = ?",
            (now_iso, now_iso, *proof, customer_id),
        )
    else:
        conn.execute(
            "UPDATE customers SET marketing_consent = 0, marketing_consent_at = ?, explicit_consent_at = NULL, "
            "marketing_consent_version = ?, marketing_consent_channel = ?, marketing_consent_ip = ?, "
            "marketing_reconfirm_pending = 0 WHERE id = ?",
            (now_iso, *proof, customer_id),
        )
    log_audit(
        conn, f"customer:{customer_id}",
        "marketing_consent_given" if given else "marketing_consent_revoked",
        f"Açık rıza + ticari ileti onayı {'verildi' if given else 'geri alındı (ret)'} "
        f"(metin sürümü={text_version}, kanal={channel}, ip={(ip or '')[:64]}).",
    )


RECONFIRM_CHANNEL = "reconfirm_card"
RECONFIRM_ANSWERS = ("yes", "no", "dismiss")


def answer_marketing_reconfirm(
    conn: sqlite3.Connection, customer_id: int, answer: str, ip: str = ""
) -> dict[str, Any]:
    """
    ADR-36: "Bildirimleri Aç" ile paketli verilmiş eski kampanya onayı geçersiz sayılan müşterinin uygulama içi kartına cevabı.
    - yes: açık onay verilir (kanal 'reconfirm_card'; sürüm/IP/zaman kalıcı kanıt sütunlarına yazılır).
    - no: ret kaydedilir (aynı kanal); kampanya gönderilmez.
    - dismiss: kart kapatıldı, cevap verilmedi → onay yok, kart bir daha gösterilmez (onay yine yok sayılır).
    Yalnızca yeniden onay bekleyen (marketing_reconfirm_pending = 1) müşteri için işler; diğerlerinde hiçbir şey değişmez.
    """
    answer = (answer or "").strip().lower()
    if answer not in RECONFIRM_ANSWERS:
        return {"status": "error", "message": "Geçersiz cevap."}
    row = conn.execute(
        "SELECT COALESCE(marketing_reconfirm_pending, 0) AS p FROM customers WHERE id = ? AND deleted_at IS NULL",
        (customer_id,),
    ).fetchone()
    if not row:
        return {"status": "error", "message": "Müşteri kaydı bulunamadı."}
    if not row["p"]:
        return {"status": "not_pending", "marketing_consent": has_active_marketing_consent(conn, customer_id)}
    if answer == "dismiss":
        conn.execute("UPDATE customers SET marketing_reconfirm_pending = 0 WHERE id = ?", (customer_id,))
        log_audit(conn, f"customer:{customer_id}", "marketing_reconfirm_dismissed",
                  "Yeniden onay kartı cevaplanmadan kapatıldı; kampanya onayı yok.")
        return {"status": "success", "answer": answer, "marketing_consent": False}
    given = answer == "yes"
    set_marketing_consent(conn, customer_id, given, ip=ip, channel=RECONFIRM_CHANNEL)   # bekleyen bayrağı da sıfırlar
    return {"status": "success", "answer": answer, "marketing_consent": given}


def has_active_marketing_consent(conn: sqlite3.Connection, customer_id: int) -> bool:
    row = conn.execute("SELECT COALESCE(marketing_consent, 0) AS mc FROM customers WHERE id = ?", (customer_id,)).fetchone()
    return bool(row and row["mc"])


def get_active_seal_by_token(conn: sqlite3.Connection, token: str) -> Optional[sqlite3.Row]:
    """Token ile mühür kaydını döner."""
    cursor = conn.cursor()
    cursor.execute("SELECT id, token, label, active FROM seals WHERE token = ?", (token,))
    return cursor.fetchone()


def _get_fallback_active_seal(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    """Sabit 'qr' mührünü veya mevcut ilk aktif mührü döner; hiçbiri yoksa 'qr' mührünü oluşturur."""
    cursor = conn.cursor()
    cursor.execute("SELECT id, token, label, active FROM seals WHERE token = 'qr' AND active = 1 LIMIT 1")
    row = cursor.fetchone()
    if row:
        return row
    # Kayıt QR mührü (ADR-35) hiçbir zaman damga mührü olarak seçilmez
    cursor.execute("SELECT id, token, label, active FROM seals WHERE active = 1 AND token != ? ORDER BY id ASC LIMIT 1",
                   (REGISTER_SEAL_TOKEN,))
    row = cursor.fetchone()
    if row:
        return row
    cursor.execute("INSERT OR IGNORE INTO seals (token, label, active) VALUES ('qr', 'Sabit Kasa QR Mührü', 1)")
    cursor.execute("UPDATE seals SET active = 1 WHERE token = 'qr'")
    cursor.execute("SELECT id, token, label, active FROM seals WHERE token = 'qr' LIMIT 1")
    return cursor.fetchone()


def get_customer_from_cookie(conn: sqlite3.Connection, device_token: Optional[str]) -> Optional[dict[str, Any]]:
    """Cihaz belirtecinin hash'i ile müşteriyi doğrular."""
    if not device_token:
        return None
    token_hash = hash_token(device_token.strip())
    cursor = conn.cursor()
    cursor.execute("""
        SELECT c.id, c.phone_e164, c.display_name, c.created_at, c.deleted_at, c.push_opt_in,
               COALESCE(c.pwa_bonus_claimed, 0) as pwa_bonus_claimed,
               COALESCE(c.notif_bonus_claimed, 0) as notif_bonus_claimed,
               COALESCE(c.marketing_consent, 0) as marketing_consent
        FROM customers c
        JOIN devices d ON d.customer_id = c.id
        WHERE d.token_hash = ? AND c.deleted_at IS NULL
    """, (token_hash,))
    row = cursor.fetchone()
    if not row:
        return None
    d = dict(row)
    if not d.get("display_name") or not str(d["display_name"]).strip():
        d["display_name"] = f"user{d['id']}"
        cursor.execute("UPDATE customers SET display_name = ? WHERE id = ?", (d["display_name"], d["id"]))
    return d


def get_customer_counts(conn: sqlite3.Connection, customer_id: int) -> tuple[int, int]:
    """(current_stamps, usable_rewards_count) döner."""
    cursor = conn.cursor()
    cursor.execute("""
        SELECT COUNT(*) as count FROM stamps
        WHERE customer_id = ? AND voided_at IS NULL AND reward_id IS NULL
    """, (customer_id,))
    current_stamps = cursor.fetchone()["count"]

    cursor.execute("""
        SELECT COUNT(*) as count FROM rewards
        WHERE customer_id = ? AND redeemed_at IS NULL AND voided_at IS NULL
    """, (customer_id,))
    usable_rewards_count = cursor.fetchone()["count"]
    return current_stamps, usable_rewards_count


WELCOME_GIFT_MESSAGE = "Hoş geldin! Kayıt hediyesi olarak +1 damga kartına eklendi 🎁"
WELCOME_LOCKED_MESSAGE = (
    "Hesabınız açıldı. Hoş geldin damgası daha önce kullanıldığı için bu kayıtta damga verilmedi; "
    "sonraki ziyaretlerinizde damga kazanabilirsiniz. ☕"
)

# ---------------------------------------------------------------------------
# ADR-39: rastgele numarayla kayıt olup hediye toplamaya karşı koruma
# ---------------------------------------------------------------------------
# Tek seferlik hediye damgaları (kayıt / uygulama yükleme / bildirim): gerçek alışveriş damgası değildir
GIFT_SOURCES = ("welcome", "bonus_pwa", "bonus_notif")
GIFT_PENDING_NOTE = "ilk ziyaretinde kasadaki ilk damganla birlikte kartına eklenecek 🎁"
WELCOME_PENDING_MESSAGE = "Hoş geldin! Kayıt hediyen (+1 damga) " + GIFT_PENDING_NOTE
NEW_ACCOUNT_IP_LIMIT = 15          # aynı IP'den saatte en fazla bu kadar YENİ hesap (kafe Wi-Fi'ı ortak IP'dir: cömert)
NEW_ACCOUNT_IP_WINDOW = 3600
TOO_MANY_NEW_ACCOUNTS_MSG = "Bu bağlantıdan çok fazla yeni kayıt yapıldı. Lütfen biraz sonra tekrar deneyin ya da kasaya sorun."
IMPLAUSIBLE_PHONE_MSG = "Bu numara geçerli görünmüyor. Lütfen kendi cep telefonu numaranızı yazınız."
# (bekleme sütunu, damga kaynağı, verildi-zamanı sütunu, denetim eylemi, denetim metni)
_PENDING_GIFTS = (
    ("welcome_bonus_pending", "welcome", "welcome_bonus_at", "welcome_gift",
     "Bekletilen kayıt hediyesi ilk gerçek damgayla birlikte verildi (+1)."),
    ("pwa_bonus_pending", "bonus_pwa", "pwa_bonus_at", "pwa_bonus_claimed",
     "Bekletilen uygulama indirme hediyesi ilk gerçek damgayla birlikte verildi (+1)."),
    ("notif_bonus_pending", "bonus_notif", "push_bonus_at", "notif_bonus_claimed",
     "Bekletilen bildirim hediyesi ilk gerçek damgayla birlikte verildi (+1)."),
)


class RegistrationBlocked(ValueError):
    """Yeni hesap açılmadı: numara uydurma görünüyor ya da aynı bağlantıdan çok fazla yeni kayıt var."""


# ADR-47: yeni müşteri kaydında ad-soyad zorunlu (ayar registration_requires_name, varsayılan açık).
NAME_REQUIRED_MSG = "Kayıt için adını ve soyadını yaz (ör. Ayşe Yılmaz)."
FULL_NAME_MAX = 40
_NAME_PART_RE = re.compile(r"[^\W\d_](?:[^\W\d_]|['’.\-])*")
_DEFAULT_NAME_RE = re.compile(r"user\d+")


class NameRequired(RegistrationBlocked):
    """Yeni hesap açılmadı: ad-soyad eksik ya da geçersiz."""


def clean_full_name(raw: Any) -> Optional[str]:
    """Ad-soyad: en az iki sözcük, yalnızca harf (' . - dahil), 3-40 karakter; boşluklar sadeleşir. Geçersizse None."""
    name = " ".join(str(raw or "").split())
    if not (3 <= len(name) <= FULL_NAME_MAX):
        return None
    parts = name.split(" ")
    if len(parts) < 2 or not all(_NAME_PART_RE.fullmatch(part) for part in parts):
        return None
    return name


def registration_requires_name(conn: sqlite3.Connection) -> bool:
    return (get_setting(conn, "registration_requires_name", "1") or "1").strip() != "0"


def gifts_require_visit(conn: sqlite3.Connection) -> bool:
    """ADR-39: hediye damgaları ilk gerçek (kasada verilen) damgaya kadar bekletilsin mi? Varsayılan açık."""
    return (get_setting(conn, "gift_requires_visit", "1") or "1").strip() != "0"


def has_verified_stamp(conn: sqlite3.Connection, customer_id: int) -> bool:
    """Müşterinin hediye dışında (kasa QR'ı / NFC / kasiyer / kampanya) iptal edilmemiş en az bir damgası var mı?"""
    marks = ",".join("?" for _ in GIFT_SOURCES)
    return conn.execute(
        f"SELECT 1 FROM stamps WHERE customer_id = ? AND voided_at IS NULL "
        f"AND COALESCE(source, '') NOT IN ({marks}) LIMIT 1",
        (customer_id, *GIFT_SOURCES),
    ).fetchone() is not None


def implausible_phone(phone_e164: str) -> bool:
    """
    Uydurma olduğu belli numaralar (yalnızca YENİ hesapta reddedilir): 5555555555, 5050505050 gibi en fazla iki farklı
    rakam; son 7 hanesi tek rakam (532 111 11 11) ya da düz sıra (532 123 45 67, 532 765 43 21).
    """
    national = (phone_e164 or "")[3:]
    if len(national) != 10:
        return False
    subscriber = national[3:]
    return (
        len(set(national)) <= 2
        or len(set(subscriber)) == 1
        or subscriber in "0123456789"
        or subscriber in "9876543210"
    )


def _device_already_gifted(conn: sqlite3.Connection, token_hash: Optional[str], customer_id: int) -> bool:
    """Bu cihaz daha önce kayıt hediyesi almış (ya da bekleyen) BAŞKA bir hesapta kullanıldı mı? (cihaz başına 1 hediye)"""
    if not token_hash:
        return False
    return conn.execute(
        "SELECT 1 FROM devices d JOIN customers c ON c.id = d.customer_id WHERE d.token_hash = ? AND c.id != ? "
        "AND (COALESCE(c.welcome_bonus_awarded, 0) = 1 OR COALESCE(c.welcome_bonus_pending, 0) = 1) LIMIT 1",
        (token_hash, customer_id),
    ).fetchone() is not None


def pending_gift_count(conn: sqlite3.Connection, customer_id: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(welcome_bonus_pending, 0) + COALESCE(pwa_bonus_pending, 0) + COALESCE(notif_bonus_pending, 0) "
        "AS n FROM customers WHERE id = ?", (customer_id,)
    ).fetchone()
    return int(row["n"] or 0) if row else 0


def release_pending_gifts(conn: sqlite3.Connection, customer_id: int, now_iso: str) -> tuple[int, bool]:
    """
    ADR-39: müşterinin ilk gerçek damgası verildiğinde bekletilen hediye damgalarını kartına ekler.
    Dönüş: (eklenen hediye sayısı, ödül açıldı mı). issue_stamp tarafından gerçek damgadan hemen sonra çağrılır.
    """
    row = conn.execute(
        "SELECT COALESCE(welcome_bonus_pending, 0) AS welcome_bonus_pending, COALESCE(pwa_bonus_pending, 0) AS "
        "pwa_bonus_pending, COALESCE(notif_bonus_pending, 0) AS notif_bonus_pending FROM customers WHERE id = ?",
        (customer_id,),
    ).fetchone()
    if not row or not (row["welcome_bonus_pending"] or row["pwa_bonus_pending"] or row["notif_bonus_pending"]):
        return 0, False
    seal = _get_fallback_active_seal(conn)
    released, unlocked_any = 0, False
    for col, source, at_col, action, text in _PENDING_GIFTS:
        if not row[col]:
            continue
        unlocked, _target, _name = issue_stamp(conn, customer_id, seal["id"] if seal else None, now_iso, source=source)
        extra = ", welcome_bonus_awarded = 1" if source == "welcome" else ""
        conn.execute(f"UPDATE customers SET {col} = 0, {at_col} = COALESCE({at_col}, ?){extra} WHERE id = ?",
                     (now_iso, customer_id))
        log_audit(conn, f"customer:{customer_id}", action, text)
        stamps, _rewards = get_customer_counts(conn, customer_id)
        if source == "welcome":
            activity.record_welcome_gift(conn, customer_id, "uygulama", stamps, unlocked)
        else:
            activity.record_bonus(conn, customer_id, source, stamps, unlocked)
        released += 1
        unlocked_any = unlocked_any or unlocked
    return released, unlocked_any


def grant_welcome_gift(
    conn: sqlite3.Connection, customer_id: int, phone_e164: str, display_name: str, now_iso: str,
    channel: str, locked: bool,
) -> dict[str, Any]:
    """
    Kayıt hediyesi (ADR-35): yeni açılan her hesaba, kayıt yolu ne olursa olsun, ömür boyu TEK +1 damga (source='welcome').
    Hesabını daha önce silmiş telefon/cihaz (`locked`, deleted_customers_registry kilidi) ya da hediyesi zaten verilmiş
    hesap hediye almaz. Hediye damga günlük limiti tüketmez ve "aynı ziyarette tek QR damgası" beklemesini başlatmaz.
    Dönüş: {"granted": bool, "locked": bool, "reward_unlocked": bool}. Yalnızca YENİ hesapta çağrılır (geriye dönük verilmez).
    """
    out: dict[str, Any] = {"granted": False, "locked": False, "reward_unlocked": False, "pending": False}
    row = conn.execute(
        "SELECT COALESCE(welcome_bonus_awarded, 0) AS a FROM customers WHERE id = ?", (customer_id,)
    ).fetchone()
    if locked or (row is not None and row["a"]):
        out["locked"] = True
        log_audit(conn, f"customer:{customer_id}", "welcome_stamp_denied",
                  "Daha önce hesabını silmiş kimlik ya da hediyesini almış cihaz: hoş geldin damgası verilmedi.")
        return out
    # ADR-39: evden rastgele numarayla açılan hesap hediye toplayamaz; hediye ilk gerçek damgayla birlikte gelir
    if gifts_require_visit(conn) and not has_verified_stamp(conn, customer_id):
        conn.execute("UPDATE customers SET welcome_bonus_pending = 1 WHERE id = ?", (customer_id,))
        log_audit(conn, f"customer:{customer_id}", "welcome_gift_pending",
                  "Kayıt hediyesi ilk gerçek damgaya (kasada) kadar bekletiliyor.")
        out["pending"] = True
        return out
    seal = _get_fallback_active_seal(conn)
    reward_unlocked, _target, _reward = issue_stamp(
        conn, customer_id, seal["id"] if seal else None, now_iso, source="welcome",
    )
    conn.execute(
        "UPDATE customers SET welcome_bonus_awarded = 1, welcome_bonus_at = COALESCE(welcome_bonus_at, ?) WHERE id = ?",
        (now_iso, customer_id),
    )
    log_audit(conn, f"customer:{customer_id}", "welcome_gift", "Kayıt hediyesi olarak +1 damga verildi.")
    stamps, rewards = get_customer_counts(conn, customer_id)
    activity.record_welcome_gift(conn, customer_id, channel, stamps, reward_unlocked)
    if phone_e164:
        sync_customer_to_directus(
            phone_e164=phone_e164, display_name=display_name, current_stamps=stamps, usable_rewards=rewards,
            add_stamp_code="WELCOME_GIFT",
            stamp_reason=f"{display_name} ({format_phone(phone_e164)}) - KAYIT_HEDIYESI",
        )
    out.update(granted=True, reward_unlocked=reward_unlocked)
    return out


def get_or_create_customer_with_device(
    conn: sqlite3.Connection,
    phone_e164: str,
    now_iso: Optional[str] = None,
    existing_device_token: Optional[str] = None,
    push_endpoint: Optional[str] = None,
    consent: Optional[dict[str, Any]] = None,
    channel: str = "uygulama",
    gift: Optional[dict[str, Any]] = None,
    otp: Optional[OtpContext] = None,
) -> tuple[int, str, str]:
    """
    Telefon numarasına göre müşteri bulur veya oluşturur.
    ADR-35: yeni (veya silinmişken geri dönen) hesaba kayıt hediyesi +1 damga burada, kayıt yolundan bağımsız verilir
    (grant_welcome_gift); `gift` verilirse sonuçla doldurulur: {"fresh": bool, "granted": bool, "locked": bool, ...}.
    `channel`: kaydın nereden geldiği — işlem kaydı içindir (ADR-31: scan | app | screen | display | nfc | uygulama).
    Yeni kayıt ve aynı telefonda farklı numarayla giriş (olası çoklu hesap) işlem kaydına yazılır.
    Yeni kayıt olan her müşteriye otomatik olarak user1, user2, user3 şeklinde varsayılan kullanıcı adı atar.
    Yeni (veya silinmiş sonra geri dönen) müşteri için "Aydınlatma Metni'ni okudum, anladım" teyidi zorunludur;
    eksikse ConsentRequired fırlatılır ve hiçbir kayıt oluşturulmaz.
    `otp` (yalnızca müşteri giriş uçları geçer): OTP_MODE=new_device iken KAYITLI müşterinin numarası ona bağlı olmayan
    bir cihazdan girilirse, hiçbir değişiklik yapılmadan OtpRequired fırlatılır (docs/OTP.md); kod doğruysa akış aynen sürer.
    Dönüş: (customer_id, raw_device_token, display_name)
    """
    cursor = conn.cursor()
    if not now_iso:
        now_iso = get_istanbul_now().isoformat()
    is_fresh_account = False  # yeni açılan (veya silinmişken geri dönen) hesap: anti-fraud denetimine girer

    cursor.execute("SELECT id, display_name, deleted_at FROM customers WHERE phone_e164 = ?", (phone_e164,))
    customer = cursor.fetchone()

    if not customer:
        cursor.execute("SELECT id, phone_e164, display_name, deleted_at FROM customers WHERE deleted_at IS NULL")
        for row in cursor.fetchall():
            try:
                if normalize_phone(row["phone_e164"]) == phone_e164:
                    customer = row
                    cursor.execute("UPDATE customers SET phone_e164 = ? WHERE id = ?", (phone_e164, row["id"]))
                    break
            except ValueError:
                continue

    if customer:
        customer_id = customer["id"]
        display_name = customer["display_name"]
        if not customer["deleted_at"]:
            # Yeni cihaz doğrulaması (OTP): cihaz bağlama / ad / rıza yazımından ÖNCE. Silinmiş hesap yeni kayıt sayılır.
            otp_service.gate_new_device(conn, phone_e164, customer_id, existing_device_token, otp)
        if customer["deleted_at"] and not consent_is_valid(consent):
            raise ConsentRequired()
        full_name = clean_full_name((consent or {}).get("full_name"))
        if customer["deleted_at"] and not full_name and registration_requires_name(conn):
            raise NameRequired(NAME_REQUIRED_MSG)
        # ADR-47: otomatik adlı (userN) ya da silinmişken dönen hesap girişte yazdığı ad-soyadı alır; adı olanın adı değişmez
        if full_name and (customer["deleted_at"] or not display_name or _DEFAULT_NAME_RE.fullmatch(str(display_name))):
            display_name = full_name
            cursor.execute("UPDATE customers SET display_name = ? WHERE id = ?", (display_name, customer_id))
        if not display_name or not str(display_name).strip():
            display_name = f"user{customer_id}"
            cursor.execute("UPDATE customers SET display_name = ? WHERE id = ?", (display_name, customer_id))
        if customer["deleted_at"]:
            is_fresh_account = True
            cursor.execute("UPDATE customers SET deleted_at = NULL WHERE id = ?", (customer_id,))
            activity.record_registration(conn, customer_id, channel, returning=True,
                                         ip=(consent or {}).get("ip") or "", device_token=existing_device_token)
            # Silinmiş kaydın eski rıza izleri yeni onayla yeniden yazılır
            cursor.execute(
                "UPDATE customers SET explicit_consent_at = NULL, kvkk_read_at = NULL, marketing_consent = 0, "
                "marketing_reconfirm_pending = 0 WHERE id = ?",
                (customer_id,),
            )
        logger.info("Mevcut müşteri #%s tanındı.", customer_id)  # G-16: günlüğe ad / telefon yazılmaz
    else:
        if not consent_is_valid(consent):
            raise ConsentRequired()
        full_name = clean_full_name((consent or {}).get("full_name"))
        if not full_name and registration_requires_name(conn):
            raise NameRequired(NAME_REQUIRED_MSG)
        # ADR-39: uydurma numara ve aynı bağlantıdan seri hesap açma engellenir (mevcut müşterinin girişi etkilenmez)
        if implausible_phone(phone_e164):
            raise RegistrationBlocked(IMPLAUSIBLE_PHONE_MSG)
        new_ip = ((consent or {}).get("ip") or "")[:64]
        if new_ip and rate_limit_blocked(f"new_account_{new_ip}", NEW_ACCOUNT_IP_LIMIT, NEW_ACCOUNT_IP_WINDOW):
            log_audit(conn, "system", "new_account_rate_limited", f"Aynı IP'den çok fazla yeni kayıt denemesi ({new_ip}).")
            raise RegistrationBlocked(TOO_MANY_NEW_ACCOUNTS_MSG)
        cursor.execute(
            "INSERT INTO customers (phone_e164, created_at) VALUES (?, ?)",
            (phone_e164, now_iso)
        )
        customer_id = cursor.lastrowid
        if new_ip:
            rate_limit_record(f"new_account_{new_ip}")
        is_fresh_account = True
        display_name = full_name or f"user{customer_id}"
        cursor.execute("UPDATE customers SET display_name = ? WHERE id = ?", (display_name, customer_id))
        # G-16: günlüğe ad yazılmaz (ad-soyad artık gerçek kişisel veri)
        log_audit(conn, "system", "customer_registered", f"Yeni müşteri #{customer_id} kaydedildi.")
        activity.record_registration(conn, customer_id, channel, ip=(consent or {}).get("ip") or "",
                                     device_token=existing_device_token)
        logger.info("Yeni müşteri #%s kaydedildi.", customer_id)

    # Onay kutuları işaretlenmişse (yeni veya eski müşteri) rıza kaydını IP/zaman/sürümle sakla
    record_consent(conn, customer_id, consent, source="self")

    prev_token_hash = hash_token(existing_device_token) if existing_device_token else None

    if gift is not None:
        gift.update(fresh=is_fresh_account, granted=False, locked=False, reward_unlocked=False, pending=False,
                    device_reuse=False)
    if is_fresh_account:
        locked = apply_returning_user_guard(conn, customer_id, phone_e164, [prev_token_hash] if prev_token_hash else [])
        # ADR-39: aynı telefonda ikinci, üçüncü numara kayıt hediyesi almaz (cihaz başına 1 hediye)
        device_reuse = not locked and _device_already_gifted(conn, prev_token_hash, customer_id)
        result = grant_welcome_gift(conn, customer_id, phone_e164, display_name, now_iso, channel, locked or device_reuse)
        if gift is not None:
            gift.update(result)
            gift["device_reuse"] = device_reuse

    reuse_existing = False
    if prev_token_hash:
        cursor.execute("SELECT customer_id FROM devices WHERE token_hash = ?", (prev_token_hash,))
        dev_row = cursor.fetchone()
        if not dev_row:
            # İstemcinin getirdiği (sunucuda kaydı olmayan) belirteç yalnızca sunucunun ürettiği güçlü biçimdeyse
            # benimsenir; kısa / tahmin edilebilir değer ("1", "test") kimlik olarak kabul edilmez, yenisi üretilir.
            reuse_existing = bool(_ADOPTABLE_TOKEN_RE.match(existing_device_token or ""))
        elif dev_row["customer_id"] != customer_id:
            # Aynı cihaza başka bir numarayla giriş: cihazın daha önce kullandığı hediye bonusları bu hesaba da
            # geçer (indirme/bildirim bonusu cihaz başına hayatta yalnızca 1 kez). İşlem kaydı: olası çoklu hesap.
            activity.record_device_switch(conn, customer_id, dev_row["customer_id"],
                                          ip=(consent or {}).get("ip") or "", device_token=existing_device_token)
            cursor.execute(
                """
                UPDATE customers SET
                    pwa_bonus_claimed = MAX(COALESCE(pwa_bonus_claimed, 0),
                        COALESCE((SELECT pwa_bonus_claimed FROM customers WHERE id = ?), 0)),
                    notif_bonus_claimed = MAX(COALESCE(notif_bonus_claimed, 0),
                        COALESCE((SELECT notif_bonus_claimed FROM customers WHERE id = ?), 0))
                WHERE id = ?
                """,
                (dev_row["customer_id"], dev_row["customer_id"], customer_id),
            )
        elif dev_row["customer_id"] == customer_id:
            link_anonymous_subscriptions(conn, customer_id, token_hash=prev_token_hash, endpoint=push_endpoint)
            stamps, rewards = get_customer_counts(conn, customer_id)
            sync_customer_to_directus(phone_e164, display_name, stamps, rewards)
            return customer_id, existing_device_token, display_name

    if reuse_existing and existing_device_token:
        raw_device_token = existing_device_token
        token_hash = prev_token_hash
    else:
        raw_device_token = generate_token(32)
        token_hash = hash_token(raw_device_token)

    cursor.execute(
        "INSERT OR IGNORE INTO devices (customer_id, token_hash, created_at) VALUES (?, ?, ?)",
        (customer_id, token_hash, now_iso)
    )

    link_anonymous_subscriptions(conn, customer_id, token_hash=prev_token_hash or token_hash, endpoint=push_endpoint)

    stamps, rewards = get_customer_counts(conn, customer_id)
    sync_customer_to_directus(phone_e164, display_name, stamps, rewards)

    return customer_id, raw_device_token, display_name


def update_customer_profile(
    conn: sqlite3.Connection,
    customer_id: int,
    new_display_name: str,
) -> tuple[bool, Optional[str], Optional[str]]:
    """
    Müşterinin kullanıcı adını / ad soyadını günceller.
    Hem SQLite hem de PostgreSQL directus_users tablosuna anında yansır.
    Dönüş: (başarı, güncellenen_ad, hata_mesajı)
    """
    cleaned = (new_display_name or "").strip()
    if not cleaned:
        return False, None, "Kullanıcı adı boş bırakılamaz."
    if len(cleaned) > 60:
        return False, None, "Kullanıcı adı en fazla 60 karakter olabilir."

    cursor = conn.cursor()
    cursor.execute("SELECT id, phone_e164 FROM customers WHERE id = ? AND deleted_at IS NULL", (customer_id,))
    cust = cursor.fetchone()
    if not cust:
        return False, None, "Müşteri kaydı bulunamadı."

    cursor.execute("UPDATE customers SET display_name = ? WHERE id = ?", (cleaned, customer_id))
    log_audit(conn, f"customer:{customer_id}", "profile_updated", f"Kullanıcı adı '{cleaned}' olarak güncellendi.")

    stamps, rewards = get_customer_counts(conn, customer_id)
    sync_customer_to_directus(cust["phone_e164"], cleaned, stamps, rewards)

    return True, cleaned, None


def count_today_stamps(conn: sqlite3.Connection, customer_id: int, now: Optional[datetime] = None) -> int:
    """
    Müşterinin Europe/Istanbul gününe göre bugünkü (iptal edilmemiş) damga sayısı — günlük limit için.
    Hediye damgaları (kayıt / uygulama / bildirim; ADR-35, ADR-39) günlük limiti tüketmez; sayılmaz.
    """
    today = (now or get_istanbul_now()).astimezone(ISTANBUL_TZ).date()
    marks = ",".join("?" for _ in GIFT_SOURCES)
    rows = conn.execute(
        f"SELECT created_at FROM stamps WHERE customer_id = ? AND voided_at IS NULL "
        f"AND COALESCE(source, '') NOT IN ({marks})",
        (customer_id, *GIFT_SOURCES),
    ).fetchall()
    total = 0
    for row in rows:
        dt = parse_to_istanbul(row["created_at"])
        if dt and dt.date() == today:
            total += 1
    return total


def issue_stamp(
    conn: sqlite3.Connection,
    customer_id: int,
    seal_id: Optional[int],
    now_iso: str,
    source: str = "qr",
    actor: str = "",
    note: str = "",
) -> tuple[bool, int, str]:
    """
    Tek bir damga ekler; ödüle bağlanmamış damga sayısı hedefe ulaştıysa aynı işlemde ödül bileti üretir.
    `source` (qr | welcome | bonus_pwa | bonus_notif | staff | admin | campaign) ve `actor` (kullanıcı adı) aylık
    rapor içindir; `note` ödüllü kampanya damgasında kampanya adıdır.
    Dönüş: (ödül_açıldı_mı, damga_hedefi, ödül_adı)
    """
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO stamps (customer_id, seal_id, created_at, source, actor, note) VALUES (?, ?, ?, ?, ?, ?)",
        (customer_id, seal_id, now_iso, source, actor or None, note or None),
    )
    try:
        stamp_target = int(get_setting(conn, "stamp_target", "5"))
    except ValueError:
        stamp_target = 5
    reward_name = get_setting(conn, "reward_name", "1 İkram El Yapımı Kahve")

    cursor.execute(
        """
        SELECT id FROM stamps
        WHERE customer_id = ? AND voided_at IS NULL AND reward_id IS NULL
        ORDER BY id ASC
        """,
        (customer_id,),
    )
    unrewarded_ids = [row["id"] for row in cursor.fetchall()]

    reward_unlocked = False
    if len(unrewarded_ids) >= stamp_target:
        target_stamp_ids = unrewarded_ids[:stamp_target]
        cursor.execute("INSERT INTO rewards (customer_id, issued_at) VALUES (?, ?)", (customer_id, now_iso))
        new_reward_id = cursor.lastrowid
        placeholders = ",".join("?" for _ in target_stamp_ids)
        cursor.execute(
            f"UPDATE stamps SET reward_id = ? WHERE id IN ({placeholders})",
            [new_reward_id] + target_stamp_ids,
        )
        reward_unlocked = True
        log_audit(conn, "system", "reward_issued", f"Müşteri #{customer_id} için ödül bileti #{new_reward_id} oluşturuldu.")
    # ADR-39: gerçek damga (hediye değil) bekletilen hediye damgalarını da kartına ekler
    if source not in GIFT_SOURCES:
        _released, gift_unlocked = release_pending_gifts(conn, customer_id, now_iso)
        reward_unlocked = reward_unlocked or gift_unlocked
    return reward_unlocked, stamp_target, reward_name


INACTIVE_QR_MESSAGE = "Bu QR kod işletme tarafından devre dışı bırakıldı. Kasadaki güncel QR kodu okut."
STALE_QR_MESSAGE = "Bu QR kodu artık kullanılmıyor. Damga için kasadaki güncel QR'ı okut."
# ADR-30 güvenli mod: basılı (sabit) QR yalnızca kayıt + uygulama içindir
STATIC_QR_REGISTER_MESSAGE = (
    "Kasadaki bu QR şu an yalnızca kayıt ve uygulama içindir; damga vermez. Damga için telefonunu kasadaki NFC etiketine dokundur ya da kasadaki "
    "ekranda görünen QR'ı okut."
)
# ADR-35: sistemi açan kayıt / uygulama QR'ı (/m/kayit) — damga vermez, damga ayrı "Damga" QR'ındadır
KAYIT_QR_MESSAGE = (
    "Bu QR uygulamayı açar ve kayıt içindir; damga vermez. Damga için kasadaki “Damga” QR'ını okut "
    "ya da NFC etiketine dokundur."
)
# NTAG 424 DNA dokunuşları (ADR-30)
_NFC_REJECTION_MESSAGES = {
    "used": "Bu NFC dokunuşu kullanıldı. Yeni damga için telefonunu kasadaki NFC etiketine yeniden dokundur.",
    "expired": "Bu NFC dokunuşunun süresi doldu. Telefonunu kasadaki NFC etiketine yeniden dokundur.",
    "held_elsewhere": "Bu NFC dokunuşu başka bir telefonda açıldı. Telefonunu kasadaki NFC etiketine yeniden dokundur.",
}
NFC_SELF_USED_MESSAGE = "Bu dokunuşla işlemin zaten yapıldı ✓ Yeni damga için telefonunu etikete yeniden dokundur."
NFC_INVALID_MESSAGE = "Bu NFC etiketi doğrulanamadı. Telefonunu kasadaki NFC etiketine yeniden dokundur ya da kasiyere söyle."
NFC_STALE_MESSAGE = "Bu NFC dokunuşu artık geçerli değil. Telefonunu kasadaki NFC etiketine yeniden dokundur."
NFC_DISABLED_MESSAGE = "Bu NFC etiketi işletme tarafından devre dışı bırakıldı. Kasadaki güncel etikete dokundur ya da kasiyere söyle."
NFC_NOT_CONFIGURED_MESSAGE = "NFC etiketi henüz kurulmadı. Kasadaki QR'ı okut ya da kasiyere söyle."
# Fabrika anahtarı (00…00) herkesçe bilinir: bu anahtarla "geçerli" imza herkes tarafından üretilebilir, damga verilmez.
NFC_ZERO_KEY_MESSAGE = "NFC damgası şu an kullanılamıyor. Kasadaki ekranda görünen QR'ı okut ya da kasiyere söyle."
_ZERO_KEY = bytes(16)
# Sunucunun ürettiği cihaz belirteci (generate_token(32) → 43 karakter URL-safe); benimsenecek istemci değeri bu biçimde olmalı
_ADOPTABLE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
# Kanal etiketleri (denetim günlüğü, Directus): origin → (günlük metni, Directus kodu öneki, Directus açıklaması)
_CHANNELS = {
    "nfc": ("Kasa NFC etiketi (424 DNA)", "KASA_NFC", "Kasa NFC"),
    "display": ("Kasa QR Ekranı", "KASA_EKRAN", "Kasa QR Ekranı"),
}
_DEFAULT_CHANNEL = ("Kasa Damga QR'ı", "KASA_QR", "Kasa Damga QR")
# "Aynı ziyarette tek QR damgası" beklemesine sayılmayan tek seferlik hediye damgaları (ADR-29)
_COOLDOWN_EXEMPT_SOURCES = ("bonus_pwa", "bonus_notif", "campaign", "welcome")
# durum → (status, müşteriye mesaj, şüphe nedeni)
_CODE_REJECTIONS = {
    "used": ("code_used", "Bu damga kodu kullanıldı. Yeni damga için kasadaki ekranda görünen güncel QR'ı okut.", "claim_replay"),
    "expired": ("code_expired", "Bu damga kodunun süresi doldu. Kasadaki ekranda görünen güncel QR'ı okut.", "stamp_code_expired"),
    "held_elsewhere": ("code_held", "Bu damga kodu başka bir telefonda açıldı. Kasadaki ekranda görünen güncel QR'ı okut.", "claim_replay"),
    "invalid": ("code_invalid", "Geçersiz damga kodu. Kasadaki ekranda görünen güncel QR'ı okut.", "qr_invalid"),
}
SELF_REPLAY_GRACE = timedelta(minutes=5)   # kendi kodunu hemen yeniden açmak (çift okutma / yenileme) şüphe sayılmaz


def _counts_payload(conn: sqlite3.Connection, customer: dict[str, Any], device_token: Optional[str]) -> dict[str, Any]:
    current_stamps, usable_rewards_count = get_customer_counts(conn, customer["id"])
    return {
        "customer_id": customer["id"],
        "display_name": customer["display_name"],
        "current_stamps": current_stamps,
        "usable_rewards_count": usable_rewards_count,
        "new_device_token": device_token,
        "device_token": device_token,
    }


def _qr_cooldown_until(conn: sqlite3.Connection, customer_id: int, now: datetime) -> Optional[datetime]:
    """
    "Aynı ziyarette tek QR damgası" (ADR-29): müşterinin son damgasından (tek seferlik hediye damgaları hariç; kasiyerin
    elle verdiği dahil) `min_interval_minutes` dakika geçmediyse beklemenin bittiği zamanı, aksi halde None döner.
    """
    try:
        minutes = int(get_setting(conn, "min_interval_minutes", "0") or 0)
    except ValueError:
        minutes = 0
    if minutes <= 0:
        return None
    marks = ",".join("?" for _ in _COOLDOWN_EXEMPT_SOURCES)
    row = conn.execute(
        f"SELECT created_at FROM stamps WHERE customer_id = ? AND voided_at IS NULL "
        f"AND COALESCE(source, '') NOT IN ({marks}) ORDER BY id DESC LIMIT 1",
        (customer_id, *_COOLDOWN_EXEMPT_SOURCES),
    ).fetchone()
    last = parse_to_istanbul(row["created_at"]) if row else None
    if last is None:
        return None
    until = last + timedelta(minutes=minutes)
    return until if now < until else None


def _rejected_stamp_code(
    conn: sqlite3.Connection, row: Optional[sqlite3.Row], state: str, customer: Optional[dict[str, Any]],
    device_token: Optional[str], client_ip: str, now: datetime,
) -> dict[str, Any]:
    """Kullanılmış / süresi dolmuş / başka telefona ayrılmış / uydurma kod: damga yok, şüphe olayı kaydedilir."""
    status, message, reason = _CODE_REJECTIONS[state]
    is_nfc = row is not None and row["origin"] == "nfc"
    if is_nfc:
        message = _NFC_REJECTION_MESSAGES.get(state, message)
    result: dict[str, Any] = {"status": status, "message": message}
    if customer:
        result.update(_counts_payload(conn, customer, device_token))
    if state == "used" and customer and row is not None and row["used_by_customer_id"] == customer["id"]:
        # Kendi kullandığı kodu yeniden açan müşteri (çift okutma, sekme yenileme veya kopyala-yapıştır): damga yok.
        result["message"] = NFC_SELF_USED_MESSAGE if is_nfc else (
            "Bu kodla işlemin zaten yapıldı ✓ Yeni damga için kasadaki ekranda görünen güncel QR'ı okut."
        )
        used_at = parse_to_istanbul(row["used_at"])
        if used_at is None or now - used_at <= SELF_REPLAY_GRACE:
            return result
        reason = "stamp_code_reused"
    fraud.record_event(conn, reason, customer_id=customer["id"] if customer else None, device_token=device_token, ip=client_ip)
    return result


def _claim_code_state(
    conn: sqlite3.Connection, row: sqlite3.Row, hold_secret: Optional[str], customer: Optional[dict[str, Any]],
    phone_raw: Optional[str], otp: Optional[OtpContext], now: datetime,
) -> str:
    """
    stamp_codes.code_state + çerezsiz istemci (docs/OTP.md): başka tarayıcıya ayrılmış görünen kod, istek o ayrılmaya
    bağlı geçerli challenge'ı aynı numarayla taşıyorsa bu isteğin sayılır ('held_here'). Damga yine de ancak SMS kodu
    doğrulanınca verilir (gate_new_device); tanınan (cihazı bağlı) müşteri için uygulanmaz.
    """
    state = stamp_codes.code_state(row, hold_secret, now)
    if state == "held_elsewhere" and customer is None and otp_service.owns_held_code(conn, row, otp, phone_raw):
        return "held_here"
    return state


def _log_code_for_cashier(
    conn: sqlite3.Connection, row: sqlite3.Row, customer_id: int, phone_e164: Optional[str],
    action: str, detail: str, ip: str,
) -> None:
    """QR damgası kodu gösteren kasiyerin işlemi sayılır: "Bugün Basılan" sayacı ve Son İşlemlerim listesi."""
    if not row["created_by"] and row["staff_id"] is None:
        return
    from app.services.staff import log_staff_action  # döngüsel içe aktarmayı önler (staff → loyalty)
    log_staff_action(conn, row["created_by"] or "kasa", action, customer_id,
                     mask_phone(phone_e164) if phone_e164 else "", detail, ip, staff_id=row["staff_id"])


def static_qr_stamps_enabled(conn: sqlite3.Connection) -> bool:
    """Basılı (sabit) kasa QR'ı ve eski NFC etiketi damga verir mi (ADR-29 akışı)? Kapalıysa yalnızca kayıt (ADR-30)."""
    return (get_setting(conn, "static_qr_stamps", "1") or "1").strip() != "0"


def _static_qr_registration(
    conn: sqlite3.Connection, customer: Optional[dict[str, Any]], device_token: Optional[str],
    phone_raw: Optional[str], consent: Optional[dict[str, Any]], now_iso: str, channel: str = "scan",
    message: str = STATIC_QR_REGISTER_MESSAGE, gift: Optional[dict[str, Any]] = None,
    otp: Optional[OtpContext] = None,
) -> dict[str, Any]:
    """
    Damga vermeyen QR (ADR-30 güvenli mod; ADR-35 /m/kayit): kayıtlıya bilgi, kayıtsıza kayıt. Kayıtsız müşteri yeni
    hesap açınca kayıt hediyesi +1 damga alır (grant_welcome_gift); bu okutuş ayrıca bir damga VERMEZ.
    """
    if customer:
        result: dict[str, Any] = {"status": "registration_qr", "message": message}
        result.update(_counts_payload(conn, customer, device_token))
        return result
    if phone_raw is None or str(phone_raw).strip() == "":
        return {"status": "need_auth", "message": "Numaranı yaz, kartın hemen açılsın. ☕"}
    try:
        phone_e164 = normalize_phone(phone_raw)
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    gift = gift if gift is not None else {}
    try:
        customer_id, active_token, display_name = get_or_create_customer_with_device(
            conn, phone_e164, now_iso=now_iso, existing_device_token=device_token, consent=consent, channel=channel,
            gift=gift, otp=otp,
        )
    except OtpRequired as exc:
        return exc.response   # aynı nesne: SMS gönderilemezse COMMIT sonrası yerinde otp_unavailable olur
    except ConsentRequired:
        return {"status": "consent_required", "message": CONSENT_REQUIRED_MSG}
    except RegistrationBlocked as e:
        return {"status": "error", "message": str(e)}
    current_stamps, usable_rewards_count = get_customer_counts(conn, customer_id)
    if gift.get("granted"):
        opening = "Hesabın açıldı ✓ " + WELCOME_GIFT_MESSAGE + " "
    elif gift.get("pending"):
        opening = "Hesabın açıldı ✓ " + WELCOME_PENDING_MESSAGE + " "
    elif gift.get("locked"):
        opening = "Hesabın açıldı ✓ Hoş geldin damgası daha önce kullanıldığı için kayıt hediyesi verilmedi. "
    else:
        opening = "Hesabın açıldı ✓ "
    return {
        "status": "registered",
        "message": opening + message,
        "customer_id": customer_id,
        "display_name": display_name,
        "current_stamps": current_stamps,
        "usable_rewards_count": usable_rewards_count,
        "welcome_gift": bool(gift.get("granted")),
        "welcome_locked": bool(gift.get("locked")),
        "welcome_pending": bool(gift.get("pending")),
        "new_device_token": active_token,
        "device_token": active_token,
    }


def _nfc_rejection(
    conn: sqlite3.Connection, customer: Optional[dict[str, Any]], device_token: Optional[str], status: str, message: str,
) -> dict[str, Any]:
    result: dict[str, Any] = {"status": status, "message": message}
    if customer:
        result.update(_counts_payload(conn, customer, device_token))
    return result


def _open_nfc_tap(
    conn: sqlite3.Connection, sun: "nfc_sun.SunMessage", customer: Optional[dict[str, Any]],
    device_token: Optional[str], client_ip: str, now: datetime,
) -> tuple[Optional[sqlite3.Row], Optional[dict[str, Any]]]:
    """
    NTAG 424 DNA dokunuşunu (ADR-30) tek kullanımlık açılışa çevirir. Dönüş: (stamp_codes satırı, None) ya da (None, ret).
    - İmza tutmazsa ret + şüphe (nfc_invalid). Aynı dokunuş daha önce açıldıysa satırı döner; kullanılmış / başka
      telefona ayrılmış / süresi dolmuş kuralları normal akışta uygulanır.
    - Yeni dokunuşun sayacı etiketin son sayacından büyük olmalı; değilse ret (nfc_stale): kopya ya da eski adres.
    """
    keys = nfc_sun.load_keys(conn)
    if keys is None:
        nfc_sun.record_event(conn, "not_configured")
        return None, _nfc_rejection(conn, customer, device_token, "nfc_unavailable", NFC_NOT_CONFIGURED_MESSAGE)
    if _ZERO_KEY in keys:
        # Fabrika anahtarıyla imza herkes tarafından taklit edilebilir (sınırsız sahte dokunuş = sınırsız damga)
        nfc_sun.record_event(conn, "zero_key")
        return None, _nfc_rejection(conn, customer, device_token, "nfc_unavailable", NFC_ZERO_KEY_MESSAGE)
    checked = nfc_sun.verify(sun, *keys)
    customer_id = customer["id"] if customer else None
    if not checked.ok:
        nfc_sun.record_event(conn, checked.reason, uid=checked.uid, counter=checked.counter)
        fraud.record_event(conn, "nfc_invalid", customer_id=customer_id, device_token=device_token, ip=client_ip)
        return None, _nfc_rejection(conn, customer, device_token, "nfc_invalid", NFC_INVALID_MESSAGE)
    tap = nfc_sun.tap_code(checked.uid, checked.counter)
    row = stamp_codes.find_code(conn, tap)
    if row is not None:
        return row, None
    state, tag = nfc_sun.register_tap(conn, checked.uid, checked.counter, now)
    nfc_sun.record_event(conn, state, uid=checked.uid, counter=checked.counter, tag_id=tag["id"] if tag else None)
    if state == "disabled":
        return None, _nfc_rejection(conn, customer, device_token, "nfc_unavailable", NFC_DISABLED_MESSAGE)
    if state == "stale":
        fraud.record_event(conn, "nfc_stale", customer_id=customer_id, device_token=device_token, ip=client_ip)
        return None, _nfc_rejection(conn, customer, device_token, "code_expired", NFC_STALE_MESSAGE)
    stamp_codes.create_stamp_code(conn, origin="nfc", ttl_seconds=stamp_codes.SCAN_TTL_SECONDS, now=now, code=tap)
    return stamp_codes.find_code(conn, tap), None


def process_claim_submission(
    conn: sqlite3.Connection,
    claim_token: str,
    device_token: Optional[str] = None,
    phone_raw: Optional[str] = None,
    consent: Optional[dict[str, Any]] = None,
    nonce: Optional[str] = None,
    client_ip: str = "",
    hold_secret: Optional[str] = None,
    client: Optional[dict[str, Any]] = None,
    otp: Optional[OtpContext] = None,
) -> dict[str, Any]:
    """
    Damga talebini işler (kurallar: _process_claim_submission) ve sonucunu — verildi / verilmedi + nedeni, gerekirse
    "olası kötüye kullanım" notu — işlem kaydına yazar (ADR-31, activity.record_claim). `client`: müşteri uygulamasının
    bildirdiği açılış şekli (nav, hist, standalone, via) + User-Agent; yalnızca kayıt ve şüphe notu içindir, damga
    kararını DEĞİŞTİRMEZ. `nonce` eski istemci uyumluluğu için kabul edilir ve yok sayılır.
    `otp`: kayıt alanlarıyla gelen istekte kayıtlı numara yeni cihazdan girilirse damga işlenmez, otp_* yanıtı döner.
    """
    trace: dict[str, Any] = {}
    result = _process_claim_submission(conn, claim_token, device_token, phone_raw, consent, client_ip, hold_secret, trace,
                                       otp)
    activity.record_claim(conn, result, trace, device_token=device_token, client=client, ip=client_ip)
    return result


def _process_claim_submission(
    conn: sqlite3.Connection,
    claim_token: str,
    device_token: Optional[str],
    phone_raw: Optional[str],
    consent: Optional[dict[str, Any]],
    client_ip: str,
    hold_secret: Optional[str],
    trace: dict[str, Any],
    otp: Optional[OtpContext] = None,
) -> dict[str, Any]:
    """
    Damga talebini işler (ADR-28, ADR-29, ADR-30). `trace` işlem kaydı için kodu, kanalı ve müşteriyi toplar:
    - ADR-30: NTAG 424 DNA dokunuşu (/n?p=…&c=…) imzası ve sayacı doğrulanınca tek kullanımlık bir açılışa bağlanır
      (_open_nfc_tap); aynı adres ikinci kez damga vermez. Basılı QR'ın damgası panelden kapatıldıysa
      (static_qr_stamps=0) sabit QR yalnızca kayıt + uygulama içindir (_static_qr_registration).
    - Her damga tek kullanımlık bir koda bağlıdır: kasadaki sabit QR / NFC'nin her okutuşu bir açılıştır (origin='scan',
      GET /m/qr sayfaya gömer) ya da kasa ekranının canlı kodu (origin='screen', /d/{kod}). Kod kullanılınca biter:
      sayfayı yenilemek, geri dönmek, bağlantıyı başkasına göndermek ikinci damga vermez.
    - Kod içermeyen sabit içerik (uygulama içi okutucunun okuduğu /m/qr, eski bağlantılar) o anda açılış sayılır.
      Devre dışı bırakılmış mühür damga açmaz.
    - Sabit adresi yeniden açmak yeni bir açılıştır: "aynı ziyarette tek QR damgası" beklemesi (min_interval_minutes,
      son damgadan itibaren, hediye damgaları hariç) ve günlük limit (Europe/Istanbul) sınırlar; bu durumlarda kod
      tüketilir, damga eklenmez. Kasiyerin elle verdiği damgaya bekleme uygulanmaz (günlük limit uygulanır).
    - Kayıtlı olmayan müşteri kodu açınca kod o tarayıcıya ayrılır (hold, HttpOnly çerez); kayıt bitince damga
      işlenir. Yeni ayrılan kodun gizli değeri `_hold_secret` anahtarıyla döner (rota çereze yazar, yanıttan çıkarır).
    - Hesabını silip dönen kişiye kayıt anındaki (hoş geldin) damga verilmez.
    - Hedefe ulaşınca aynı transaction'da ödül bileti; Directus senkronu ve Web Push bildirimi.
    """
    cursor = conn.cursor()
    now = get_istanbul_now().astimezone(ISTANBUL_TZ)
    now_iso = now.isoformat()

    # 1. Müşteri cihazdan tanınır (HttpOnly çerez / localStorage token)
    customer = get_customer_from_cookie(conn, device_token) if device_token else None
    if customer:
        trace["customer_id"] = customer["id"]

    # 1b. Kayıt / uygulama QR'ı (/m/kayit, ADR-35): HİÇBİR koşulda damga vermez (static_qr_stamps, kamera / NFC / uygulama
    # içi okutucu fark etmez; adreste NFC imzası olsa da yok sayılır). Yalnızca kayıt (+ kayıt hediyesi) ve uygulama içindir.
    if (stamp_codes.static_seal_token(claim_token) or "").lower() == REGISTER_SEAL_TOKEN:
        trace["channel"] = "app"
        trace["register_only"] = True
        trace["kayit_qr"] = True
        gift: dict[str, Any] = {}
        result = _static_qr_registration(
            conn, customer, device_token, phone_raw, consent, now_iso, channel="app",
            message=KAYIT_QR_MESSAGE, gift=gift, otp=otp,
        )
        trace["fresh_registration"] = bool(gift.get("fresh"))
        return result

    # 2. NTAG 424 DNA dokunuşu (ADR-30) mı, tek kullanımlık kod (sabit QR açılışı / kasa ekranı / QR ekranı) mı, kod
    # içermeyen sabit içerik mi?
    opened_now = False
    sun = nfc_sun.parse_claim_token(claim_token)
    if sun is not None:
        trace["channel"] = "nfc"
        row, nfc_rejected = _open_nfc_tap(conn, sun, customer, device_token, client_ip, now)
        if nfc_rejected is not None:
            trace["nfc_stale"] = nfc_rejected.get("message") == NFC_STALE_MESSAGE
            return nfc_rejected
        trace["row"] = row
        state = _claim_code_state(conn, row, hold_secret, customer, phone_raw, otp, now)
        if state not in ("valid", "held_here"):
            return _rejected_stamp_code(conn, row, state, customer, device_token, client_ip, now)
    else:
        code, explicit_code = stamp_codes.extract_code(claim_token)
        row = stamp_codes.find_code(conn, code) if code else None
        if row is None:
            if explicit_code:
                return _rejected_stamp_code(conn, None, "invalid", customer, device_token, client_ip, now)
            # Uygulama içi okutucunun okuduğu sabit QR (/m/qr) ya da eski bağlantı: bu okutuş o anda bir açılıştır
            trace["channel"] = "app"
            seal_name = stamp_codes.static_seal_token(claim_token)
            seal_row = get_active_seal_by_token(conn, seal_name) if seal_name else None
            if seal_row is not None and not seal_row["active"]:
                trace["seal_inactive"] = True
                return {"status": "error", "message": INACTIVE_QR_MESSAGE}
            if not stamp_codes.is_stamp_qr_token(conn, seal_name):
                # ADR-41: yalnızca kasadaki Damga QR'ının gizli adresi açılış üretir (eski /m/qr, tahmin edilen ad, ham değer: yok)
                trace["stale_qr"] = True
                return {"status": "error", "message": STALE_QR_MESSAGE}
            if not static_qr_stamps_enabled(conn):
                trace["register_only"] = True
                return _static_qr_registration(conn, customer, device_token, phone_raw, consent, now_iso, channel="app",
                                               otp=otp)
            if customer is None and (phone_raw is None or str(phone_raw).strip() == ""):
                return {"status": "need_auth", "message": "Numaranı yaz, damgan seni bekliyor. ☕"}
            opened = stamp_codes.create_stamp_code(conn, origin="scan", ttl_seconds=stamp_codes.SCAN_TTL_SECONDS, now=now)
            row = stamp_codes.find_code(conn, opened["code"])
            trace["row"] = row
            opened_now = True
            state = "valid"
        else:
            trace["row"] = row
            if row["origin"] == "scan" and not row["used_at"] and not static_qr_stamps_enabled(conn):
                # Basılı QR'ın damgası kapatılmadan önce açılmış (henüz kullanılmamış) açılış da damga vermez
                trace["register_only"] = True
                return _static_qr_registration(conn, customer, device_token, phone_raw, consent, now_iso, otp=otp)
            state = _claim_code_state(conn, row, hold_secret, customer, phone_raw, otp, now)
            if state not in ("valid", "held_here"):
                return _rejected_stamp_code(conn, row, state, customer, device_token, client_ip, now)

    # Kayıtlı olmayan tarayıcı: kod kayıt formu süresince bu tarayıcıya ayrılır (başka telefon kullanamaz)
    new_hold = (
        stamp_codes.hold_code(conn, row, now) if (customer is None and state == "valid" and not opened_now) else None
    )

    def _held(result: dict[str, Any]) -> dict[str, Any]:
        if new_hold:
            result["_hold_secret"] = new_hold
        return result

    customer_id = customer["id"] if customer else None
    active_device_token = device_token if customer else None
    display_name = customer["display_name"] if customer else None
    phone_e164 = customer["phone_e164"] if customer else None

    fresh_registration = False   # bu istekte yeni açılan (veya silinmişken geri dönen) hesap
    welcome_locked = False       # hesabını daha önce silmiş numara/cihaz: hoş geldin damgası verilmez
    gift: dict[str, Any] = {}    # ADR-35: kayıt hediyesinin sonucu (get_or_create içinde verilir)
    if not customer_id:
        if phone_raw is None or str(phone_raw).strip() == "":
            return _held({"status": "need_auth", "message": "Numaranı yaz, damgan seni bekliyor. ☕"})
        try:
            phone_e164 = normalize_phone(phone_raw)
            pre = conn.execute("SELECT deleted_at FROM customers WHERE phone_e164 = ?", (phone_e164,)).fetchone()
            fresh_registration = pre is None or pre["deleted_at"] is not None
        except ValueError as e:
            logger.warning(
                "Kayıt/damga telefon doğrulama hatası (tel: %s): %s",
                mask_phone_for_log(phone_raw),
                str(e)
            )
            return _held({"status": "error", "message": str(e)})

        try:
            customer_id, active_device_token, display_name = get_or_create_customer_with_device(
                conn,
                phone_e164,
                now_iso=now_iso,
                existing_device_token=device_token,
                consent=consent,
                channel=trace.get("channel") or activity.row_channel(row),
                gift=gift,
                otp=otp,
            )
        except OtpRequired as exc:
            # Kayıtlı numara yeni cihazdan: kod doğrulanmadan cihaz bağlanmaz, damga VERİLMEZ (kod ayrılmış kalır).
            # Ayrılan kod challenge'a da bağlanır: çerez tutmayan yerel uygulama aynı isteği otp_challenge_id + otp_code
            # ile yeniden gönderince kodun sahibi sayılır (owns_held_code) ve SMS kodu doğrulanınca damgayı alır.
            if (exc.response.get("status") == "otp_required" and exc.response.get("challenge_id")
                    and (new_hold or state == "held_here")):
                otp_service.bind_hold_to_challenge(conn, row["id"], exc.response["challenge_id"])
            return _held(exc.response)
        except ConsentRequired:
            return _held({"status": "consent_required", "message": CONSENT_REQUIRED_MSG})
        except RegistrationBlocked as e:
            return _held({"status": "error", "message": str(e)})

    trace["customer_id"] = customer_id
    trace["fresh_registration"] = fresh_registration

    # 2b. Kayıt hediyesi (ADR-35) get_or_create içinde verildi: yeni hesap +1 damga alır (source='welcome'; günlük limiti
    # tüketmez, beklemeyi başlatmaz). Telefon/cihaz ömür boyu yalnızca 1 kez alır: hesabını silip yeniden açan kişi
    # 0 damgayla başlar; bu okutuşun damgası da verilmez (sonraki ziyaretlerde normal damga kazanır).
    welcome_gift = bool(gift.get("granted"))
    # ADR-39: aynı telefonda yeni numara hediye almaz ama bu ziyaretin damgası verilir (aile üyesi olabilir)
    if fresh_registration and gift.get("locked") and not gift.get("device_reuse"):
        welcome_locked = True
        stamp_codes.consume_code(conn, row["id"], customer_id, "welcome_locked", now)
        current_stamps, usable_rewards_count = get_customer_counts(conn, customer_id)
        return {
            "status": "success",
            "message": WELCOME_LOCKED_MESSAGE,
            "customer_id": customer_id,
            "display_name": display_name,
            "current_stamps": current_stamps,
            "stamp_target": int(get_setting(conn, "stamp_target", "5") or 5),
            "reward_unlocked": False,
            "welcome_locked": welcome_locked,
            "usable_rewards_count": usable_rewards_count,
            "new_device_token": active_device_token,
            "device_token": active_device_token,
        }

    # 3. Günlük limit kontrolü (varsayılan 2, Europe/Istanbul gününe göre). Limit doluysa kod tüketilir (kasa ekranı
    # sonucu görür ve yeni kod gösterir), damga eklenmez.
    try:
        daily_limit = int(get_setting(conn, "daily_limit", "2"))
    except ValueError:
        daily_limit = 2

    if count_today_stamps(conn, customer_id, now) >= daily_limit:
        stamp_codes.consume_code(conn, row["id"], customer_id, "limit", now)
        _log_code_for_cashier(conn, row, customer_id, phone_e164, "stamp_denied_daily_limit",
                              f"Kasa Damga QR'ı — günlük limit {daily_limit}", client_ip)
        current_stamps, usable_rewards_count = get_customer_counts(conn, customer_id)
        return {
            "status": "limit_reached",
            "message": f"Bugünkü maksimum damga limitinize ({daily_limit} damga) ulaştınız. Yarın tekrar bekleriz! ☕",
            "current_stamps": current_stamps,
            "usable_rewards_count": usable_rewards_count,
            "display_name": display_name,
            "new_device_token": active_device_token
        }

    # 3b. Aynı ziyarette tek QR damgası (ADR-29): son damgadan bu yana bekleme süresi dolmadıysa (ör. QR'ı okuttuktan
    # hemen sonra sabit adresi yeniden açmak / kopyala-yapıştır) kod tüketilir, damga eklenmez.
    wait_until = _qr_cooldown_until(conn, customer_id, now)
    if wait_until is not None:
        stamp_codes.consume_code(conn, row["id"], customer_id, "cooldown", now)
        current_stamps, usable_rewards_count = get_customer_counts(conn, customer_id)
        return {
            "status": "interval_limit",
            "message": f"Bu ziyaretin damgası zaten eklendi ✓ Yeni QR damgası en erken {wait_until:%H:%M}'de alınabilir.",
            "current_stamps": current_stamps,
            "usable_rewards_count": usable_rewards_count,
            "display_name": display_name,
            "new_device_token": active_device_token,
            "next_stamp_at": wait_until.isoformat(),
        }

    # 4. Kod tek kullanımlıktır: yalnızca bir istek tüketebilir (BEGIN IMMEDIATE + koşullu UPDATE)
    if not stamp_codes.consume_code(conn, row["id"], customer_id, "stamped", now):
        trace["race"] = True
        status, message, _ = _CODE_REJECTIONS["used"]
        # Bu istekte cihaz bağlanmış olabilir (ör. SMS kodu doğrulandı): belirteç kaybolmasın
        return {"status": status, "message": message,
                "new_device_token": active_device_token, "device_token": active_device_token}

    # 5-6. Damgayı ekle ve hedefe ulaşıldıysa ödül biletini üret (rapor: işlemi yapan = kodu gösteren kasiyer)
    seal = _get_fallback_active_seal(conn)
    pending_before = pending_gift_count(conn, customer_id)   # ADR-39: bu damgayla eklenecek bekleyen hediyeler
    reward_unlocked, stamp_target, reward_name = issue_stamp(
        conn, customer_id, seal["id"] if seal else None, now_iso,
        source="qr", actor=row["actor"] or "",
    )
    if gift.get("pending"):
        welcome_gift = True              # yeni kayıt + ilk damga: bekletilen kayıt hediyesi şimdi eklendi
        pending_before -= 1
    channel_log, channel_code, channel_reason = _CHANNELS.get(row["origin"], _DEFAULT_CHANNEL)
    log_audit(conn, f"customer:{customer_id}", "stamp_added", f"{channel_log} (tek kullanımlık kod #{row['id']}) ile damga verildi.")
    _log_code_for_cashier(conn, row, customer_id, phone_e164, "stamp_added",
                          "Kasa Damga QR'ı (müşteri okuttu)" + (" — ödül hazır" if reward_unlocked else ""), client_ip)

    current_stamps, usable_rewards_count = get_customer_counts(conn, customer_id)

    # 7. PostgreSQL Directus senkronizasyonu
    if phone_e164 and display_name:
        sync_customer_to_directus(
            phone_e164=phone_e164,
            display_name=display_name,
            current_stamps=current_stamps,
            usable_rewards=usable_rewards_count,
            add_stamp_code=f"{channel_code}_{row['id']}",
            stamp_reason=f"{display_name} ({format_phone(phone_e164)}) - {channel_reason}",
        )

    # 8. Web Push otomatik bildirim (hata olsa bile damga akışını ASLA bozmaz)
    try:
        business_name = get_setting(conn, "business_name", DEFAULT_BUSINESS_NAME)
        notify_customer_after_stamp(
            conn=conn,
            customer_id=customer_id,
            current_stamps=current_stamps,
            stamp_target=stamp_target,
            reward_unlocked=reward_unlocked,
            reward_name=reward_name,
            business_name=business_name,
        )
    except Exception as push_exc:
        logger.warning("Damga sonrası push bildirim hatası yoksayıldı: %s", str(push_exc))

    message = "Tebrikler! Ödül kahveniz hazır! 🎁" if reward_unlocked else "Damganız başarıyla eklendi! ☕"
    if welcome_gift:
        message = "Hoş geldin! Kayıt hediyen +1 damga ve bu ziyaretin damgası kartına eklendi 🎁☕" + (
            " Ödül kahven hazır!" if reward_unlocked else "")
    if pending_before > 0:
        message += f" Bekleyen {pending_before} hediye damgan da kartına eklendi 🎁"
    return {
        "status": "success",
        "message": message,
        "customer_id": customer_id,
        "display_name": display_name,
        "welcome_gift": welcome_gift,
        "current_stamps": current_stamps,
        "stamp_target": stamp_target,
        "reward_unlocked": reward_unlocked,
        "reward_name": reward_name,
        "usable_rewards_count": usable_rewards_count,
        "new_device_token": active_device_token,
        "device_token": active_device_token,
    }


def _grant_bonus_stamp(
    conn: sqlite3.Connection,
    customer_id: int,
    flag_column: str,
    flag_key: str,
    audit_action: str,
    audit_text: str,
    sync_code: str,
    sync_label: str,
    success_message: str,
    already_message: str,
) -> dict[str, Any]:
    """
    Günlük limite takılmadan, tek seferlik +1 hediye damga verir (flag_column ile bir kez sınırlı).
    Hedefe ulaşılırsa ödül bileti üretilir; Directus senkronu ve push bildirimi tetiklenir.
    """
    if flag_column not in _BONUS_FLAG_COLUMNS:
        raise ValueError("Geçersiz bonus bayrağı.")

    cursor = conn.cursor()
    cursor.execute(
        f"""
        SELECT id, phone_e164, display_name, COALESCE({flag_column}, 0) AS flag,
               COALESCE({"pwa_bonus_pending" if flag_column == "pwa_bonus_claimed" else "notif_bonus_pending"}, 0) AS pending
        FROM customers
        WHERE id = ? AND deleted_at IS NULL
        """,
        (customer_id,),
    )
    customer = cursor.fetchone()
    if not customer:
        return {"status": "error", "message": "Müşteri kaydı bulunamadı."}

    current_stamps, usable_rewards_count = get_customer_counts(conn, customer_id)
    pending_column = "pwa_bonus_pending" if flag_column == "pwa_bonus_claimed" else "notif_bonus_pending"
    gift_label = "Uygulama hediyen" if flag_column == "pwa_bonus_claimed" else "Bildirim hediyen"
    pending_message = f"Teşekkürler! {gift_label} (+1 damga) {GIFT_PENDING_NOTE}"
    if customer["flag"] == 1:
        if customer["pending"]:
            return {"status": "pending", "message": pending_message, "current_stamps": current_stamps,
                    "usable_rewards_count": usable_rewards_count, flag_key: 1}
        return {
            "status": "already_claimed",
            "message": already_message,
            "current_stamps": current_stamps,
            "usable_rewards_count": usable_rewards_count,
            flag_key: 1,
        }

    # ADR-38: panelden kapatılmışsa damga verilmez ve hak kullanılmış sayılmaz (yeniden açılınca alınabilir)
    if not bonus_enabled(conn, "install" if flag_column == "pwa_bonus_claimed" else "notif"):
        return {
            "status": "disabled",
            "message": "Bu hediye damga şu an verilmiyor.",
            "current_stamps": current_stamps,
            "usable_rewards_count": usable_rewards_count,
            flag_key: 0,
        }

    # ADR-39: henüz kasada damga almamış hesap için hak ayrılır, damga ilk gerçek damgayla birlikte eklenir
    if gifts_require_visit(conn) and not has_verified_stamp(conn, customer_id):
        cursor.execute(f"UPDATE customers SET {flag_column} = 1, {pending_column} = 1 WHERE id = ?", (customer_id,))
        log_audit(conn, f"customer:{customer_id}", f"{audit_action}_pending",
                  f"{gift_label.replace('hediyen', 'hediyesi')} ilk gerçek damgaya (kasada) kadar bekletiliyor.")
        return {"status": "pending", "message": pending_message, "current_stamps": current_stamps,
                "usable_rewards_count": usable_rewards_count, flag_key: 1}

    now_iso = get_istanbul_now().astimezone(ISTANBUL_TZ).isoformat()
    seal = _get_fallback_active_seal(conn)
    seal_id = seal["id"] if seal else None

    bonus_source = "bonus_pwa" if flag_column == "pwa_bonus_claimed" else "bonus_notif"
    reward_unlocked, stamp_target, reward_name = issue_stamp(conn, customer_id, seal_id, now_iso, source=bonus_source)
    at_column = "pwa_bonus_at" if flag_column == "pwa_bonus_claimed" else "push_bonus_at"
    cursor.execute(f"UPDATE customers SET {flag_column} = 1, {at_column} = ? WHERE id = ?", (now_iso, customer_id))
    log_audit(conn, f"customer:{customer_id}", audit_action, audit_text)

    current_stamps, usable_rewards_count = get_customer_counts(conn, customer_id)
    activity.record_bonus(conn, customer_id, bonus_source, current_stamps, reward_unlocked)

    phone_e164 = customer["phone_e164"]
    display_name = customer["display_name"] or f"user{customer_id}"
    if phone_e164:
        sync_customer_to_directus(
            phone_e164=phone_e164,
            display_name=display_name,
            current_stamps=current_stamps,
            usable_rewards=usable_rewards_count,
            add_stamp_code=sync_code,
            stamp_reason=f"{display_name} ({format_phone(phone_e164)}) - {sync_label}",
        )

    try:
        notify_customer_after_stamp(
            conn=conn,
            customer_id=customer_id,
            current_stamps=current_stamps,
            stamp_target=stamp_target,
            reward_unlocked=reward_unlocked,
            reward_name=reward_name,
            business_name=get_setting(conn, "business_name", DEFAULT_BUSINESS_NAME),
        )
    except Exception as push_exc:
        logger.warning("Bonus damga sonrası push bildirim hatası yoksayıldı: %s", str(push_exc))

    return {
        "status": "success",
        "message": success_message,
        "current_stamps": current_stamps,
        "usable_rewards_count": usable_rewards_count,
        "reward_unlocked": reward_unlocked,
        flag_key: 1,
    }


_BONUS_SETTING_KEYS = {"install": "bonus_install_enabled", "notif": "bonus_notif_enabled"}


def bonus_enabled(conn: sqlite3.Connection, kind: str) -> bool:
    """ADR-38: 'install' (uygulamayı yükleme) / 'notif' (bildirimleri açma) hediye damgası açık mı? Varsayılan açık."""
    return (get_setting(conn, _BONUS_SETTING_KEYS[kind], "1") or "1").strip() != "0"


def claim_pwa_install_bonus(conn: sqlite3.Connection, customer_id: int) -> dict[str, Any]:
    """Uygulamayı telefona yükleme karşılığında tek seferlik +1 ücretsiz damga (günlük limite takılmaz)."""
    return _grant_bonus_stamp(
        conn,
        customer_id,
        flag_column="pwa_bonus_claimed",
        flag_key="pwa_bonus_claimed",
        audit_action="pwa_bonus_claimed",
        audit_text="Uygulama indirme hediyesi olarak +1 damga verildi.",
        sync_code="PWA_BONUS",
        sync_label="UYGULAMA_INDIRME_HEDIYESI",
        success_message="Tebrikler! Uygulamayı indirdiğiniz için +1 Ücretsiz Damga hesabınıza eklendi! 🎉",
        already_message="Uygulama indirme hediye damgası daha önce hesabınıza eklendi.",
    )


def claim_notif_bonus(conn: sqlite3.Connection, customer_id: int, require_subscription: bool = True) -> dict[str, Any]:
    """
    Bildirimleri açma karşılığında tek seferlik +1 hediye damga.
    `require_subscription=True` (izin bilgisi taşımayan eski /api/claim-notif-bonus ucu): yalnızca sunucuda kayıtlı
    aktif bir Web Push aboneliği varsa verilir. Tarayıcı izni 'granted' bildirildiğinde (ADR-23) abonelik şartı aranmaz.
    """
    if require_subscription:
        active = conn.execute(
            "SELECT COUNT(*) AS c FROM push_subscriptions WHERE customer_id = ? AND active = 1", (customer_id,)
        ).fetchone()["c"]
        if not active:
            return {"status": "no_subscription", "message": "Önce tarayıcı bildirim izni vermeniz gerekiyor."}
    return _grant_bonus_stamp(
        conn,
        customer_id,
        flag_column="notif_bonus_claimed",
        flag_key="notif_bonus_claimed",
        audit_action="notif_bonus_claimed",
        audit_text="Bildirimleri açma hediyesi olarak +1 damga verildi.",
        sync_code="NOTIF_BONUS",
        sync_label="BILDIRIM_ACMA_HEDIYESI",
        success_message="Teşekkürler! Bildirimleri açtığın için +1 Hediye Damga hesabına eklendi! 🎁",
        already_message="Bildirim hediye damgası daha önce hesabınıza eklendi.",
    )


VALID_BROWSER_PERMISSIONS = {"granted", "denied", "default"}


def confirm_browser_notification_permission(
    conn: sqlite3.Connection, customer_id: int, permission: str
) -> dict[str, Any]:
    """
    Tarayıcının resmî bildirim izni (Notification.permission) sonucunu işler ve hediye damgayı verir (ADR-23).
    - permission = 'granted' VE hak daha önce kullanılmadıysa (push_bonus_awarded == false) → ANINDA +1 hediye damga.
      Web Push aboneliği şart değildir: abonelik ayrıca (arka planda) kurulur; push servisi gecikse de damga gecikmez.
    - permission = 'denied' / 'default' (veya bilinmeyen) → KESİNLİKLE damga yok (0 damga).
    - ADR-36 (Y-02): bu işlem kampanya (ticari ileti) onayına HİÇ dokunmaz; hediye damga yalnızca tarayıcı iznine bağlıdır.
      Kampanya onayı ayrı, açık ve isteğe bağlı bir seçimdir (kayıt kutusu, Profil › Kampanya Bildirimleri, yeniden onay kartı).
    """
    row = conn.execute(
        "SELECT COALESCE(marketing_consent, 0) AS mc, push_bonus_at, COALESCE(notif_bonus_claimed, 0) AS claimed "
        "FROM customers WHERE id = ? AND deleted_at IS NULL", (customer_id,)
    ).fetchone()
    if not row:
        return {"status": "error", "message": "Müşteri kaydı bulunamadı."}
    permission = permission if permission in VALID_BROWSER_PERMISSIONS else "default"
    already = bool(row["claimed"] or row["push_bonus_at"])
    base = {"permission": permission, "bonus_awarded": False, "push_bonus_awarded": already}

    if permission != "granted":
        return {**base, "status": "not_granted",
                "message": "Tarayıcı bildirim izni verilmedi; hediye damga eklenmedi."}

    bonus = claim_notif_bonus(conn, customer_id, require_subscription=False)
    if bonus.get("status") not in ("success", "already_claimed", "disabled", "pending"):
        return {**base, "status": "error", "message": bonus.get("message") or "İşlem tamamlanamadı."}
    consent = bool(row["mc"])
    push_active = bool(conn.execute(
        "SELECT COUNT(*) AS c FROM push_subscriptions WHERE customer_id = ? AND active = 1", (customer_id,)
    ).fetchone()["c"])
    awarded = bonus.get("status") == "success"
    return {
        **base,
        "status": "success",
        "push_active": push_active,
        "marketing_consent": consent,
        "bonus_status": bonus.get("status"),
        "bonus_awarded": awarded,
        "push_bonus_awarded": awarded or already,
        "message": bonus.get("message") if awarded or bonus.get("status") == "pending" else (
            "✅ Bildirimler açık." if bonus.get("status") == "disabled"
            else "✅ Bildirimler açık. Hediye damganız daha önce hesabınıza eklenmişti."),
        "current_stamps": bonus.get("current_stamps"),
        "usable_rewards_count": bonus.get("usable_rewards_count"),
    }


def get_customer_card_data(conn: sqlite3.Connection, customer_id: int) -> dict[str, Any]:
    """Müşterinin kart, profil, damga ve ödül verilerini döner."""
    cursor = conn.cursor()
    settings = get_all_settings(conn)
    try:
        stamp_target = int(settings.get("stamp_target", "5"))
    except ValueError:
        stamp_target = 5
    try:
        daily_limit = int(settings.get("daily_limit", "2"))
    except ValueError:
        daily_limit = 2

    reward_name = settings.get("reward_name", "1 İkram El Yapımı Kahve")

    cursor.execute(
        """
        SELECT id, phone_e164, display_name, push_opt_in, created_at,
               COALESCE(pwa_bonus_claimed, 0) as pwa_bonus_claimed,
               COALESCE(notif_bonus_claimed, 0) as notif_bonus_claimed,
               COALESCE(marketing_consent, 0) as marketing_consent,
               COALESCE(marketing_reconfirm_pending, 0) as marketing_reconfirm_pending
        FROM customers WHERE id = ?
        """,
        (customer_id,),
    )
    cust = cursor.fetchone()
    display_name = (cust["display_name"] if cust and cust["display_name"] else f"user{customer_id}")
    phone_e164 = cust["phone_e164"] if cust else ""
    phone_formatted = format_phone(phone_e164) if phone_e164 else ""
    push_opt_in = bool(cust["push_opt_in"]) if cust else False
    pwa_bonus_claimed = int(cust["pwa_bonus_claimed"]) if cust and cust["pwa_bonus_claimed"] else 0
    notif_bonus_claimed = int(cust["notif_bonus_claimed"]) if cust and cust["notif_bonus_claimed"] else 0
    marketing_consent = bool(cust["marketing_consent"]) if cust else False
    reconfirm_pending = bool(cust["marketing_reconfirm_pending"]) if cust else False

    current_stamps, usable_rewards_count = get_customer_counts(conn, customer_id)

    cursor.execute("""
        SELECT id, issued_at FROM rewards
        WHERE customer_id = ? AND redeemed_at IS NULL AND voided_at IS NULL
        ORDER BY issued_at DESC
    """, (customer_id,))
    usable_rewards = [dict(row) for row in cursor.fetchall()]

    # Bugünkü alınan damga sayısı
    today_ist_date = get_istanbul_now().astimezone(ISTANBUL_TZ).date()
    cursor.execute("SELECT created_at, source FROM stamps WHERE customer_id = ? AND voided_at IS NULL", (customer_id,))
    today_stamps = 0
    total_lifetime_stamps = 0
    for row in cursor.fetchall():
        total_lifetime_stamps += 1
        dt = parse_to_istanbul(row["created_at"])
        # hediye damgaları günlük limiti tüketmez (ADR-35, ADR-39): "bugün X / limit" göstergesine sayılmaz
        if dt and dt.date() == today_ist_date and (row["source"] or "") not in GIFT_SOURCES:
            today_stamps += 1

    return {
        "id": customer_id,
        "customer_id": customer_id,
        "display_name": display_name,
        "phone_e164": phone_e164,
        "phone_formatted": phone_formatted,
        "formatted_phone": phone_formatted,
        "push_opt_in": push_opt_in,
        "pwa_bonus_claimed": pwa_bonus_claimed,
        "notif_bonus_claimed": notif_bonus_claimed,
        # ADR-38: hediye damga panelden kapalıysa uygulama "+1 hediye damga" vaat etmez
        "bonus_install_enabled": (settings.get("bonus_install_enabled", "1") or "1").strip() != "0",
        "bonus_notif_enabled": (settings.get("bonus_notif_enabled", "1") or "1").strip() != "0",
        # ADR-39: ilk gerçek damgayla birlikte eklenecek bekleyen hediye damgası sayısı
        "pending_gifts": pending_gift_count(conn, customer_id),
        "marketing_consent": marketing_consent,
        "marketing_reconfirm_pending": reconfirm_pending,
        "business_name": settings.get("business_name", DEFAULT_BUSINESS_NAME),
        "accent_color": settings.get("accent_color", "#1E4D3F"),
        "stamp_target": stamp_target,
        "daily_limit": daily_limit,
        "today_stamps": today_stamps,
        "total_lifetime_stamps": total_lifetime_stamps,
        "current_stamps": current_stamps,
        "reward_name": reward_name,
        "usable_rewards": usable_rewards,
        "usable_rewards_count": usable_rewards_count,
    }


def get_active_campaigns(
    conn: sqlite3.Connection, include_inactive: bool = False, customer_id: Optional[int] = None
) -> list[dict[str, Any]]:
    """
    Ana sayfa ve yönetim paneli için kampanyaları döner. Ödüllü görevlerde (reward_stamps > 0) `customer_id`
    verilirse müşterinin katılım durumu da eklenir: my_status = none | joined | pending | approved | rejected.
    """
    where = "" if include_inactive else "WHERE c.active = 1"
    rows = conn.execute(
        f"""
        SELECT c.id, c.title, c.description, c.image_url, c.badge, c.active, c.created_at,
               COALESCE(c.reward_stamps, 0) AS reward_stamps, c.action_url, c.action_label,
               p.status AS my_status, p.proof_name AS my_proof_name, p.reject_reason AS my_reject_reason
        FROM campaigns c
        LEFT JOIN campaign_participants p ON p.campaign_id = c.id AND p.customer_id = ?
        {where}
        ORDER BY c.id DESC
        """,
        (customer_id if customer_id is not None else -1,),
    ).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        if item["reward_stamps"] > 0 and customer_id is not None:
            item["my_status"] = item["my_status"] or "none"
        else:
            item.pop("my_status"); item.pop("my_proof_name"); item.pop("my_reject_reason")
        out.append(item)
    return out


def get_home_data(conn: sqlite3.Connection, device_token: Optional[str]) -> dict[str, Any]:
    """
    Ana sayfa (/api/home-data) için:
    1. Aktif Kampanyalar & Duyurular (campaigns tablosu)
    2. Müşteri Özeti (gerçek adı user1 vb., güncel damga X/5, kullanılabilir ödül bileti sayısı)
    3. PWA Kurulum & Bildirim durumu
    """
    settings = get_all_settings(conn)
    customer = get_customer_from_cookie(conn, device_token) if device_token else None
    campaigns = get_active_campaigns(conn, include_inactive=False, customer_id=customer["id"] if customer else None)

    try:
        stamp_target = int(settings.get("stamp_target", "5"))
    except ValueError:
        stamp_target = 5
    try:
        daily_limit = int(settings.get("daily_limit", "2"))
    except ValueError:
        daily_limit = 2

    if customer:
        card = get_customer_card_data(conn, customer["id"])
        customer_summary = {
            "identified": True,
            "id": customer["id"],
            "customer_id": customer["id"],
            "display_name": card["display_name"],
            "phone_e164": card["phone_e164"],
            "phone_formatted": card["phone_formatted"],
            "formatted_phone": card["formatted_phone"],
            "current_stamps": card["current_stamps"],
            "stamp_target": card["stamp_target"],
            "usable_rewards_count": card["usable_rewards_count"],
            "usable_rewards": card["usable_rewards"],
            "reward_name": card["reward_name"],
            "daily_limit": card["daily_limit"],
            "today_stamps": card["today_stamps"],
            "total_lifetime_stamps": card["total_lifetime_stamps"],
            "push_opt_in": card["push_opt_in"],
            "pwa_bonus_claimed": card.get("pwa_bonus_claimed", 0),
            "notif_bonus_claimed": card.get("notif_bonus_claimed", 0),
            "bonus_install_enabled": card.get("bonus_install_enabled", True),
            "bonus_notif_enabled": card.get("bonus_notif_enabled", True),
            "pending_gifts": card.get("pending_gifts", 0),
            "marketing_consent": card.get("marketing_consent", False),
            "marketing_reconfirm_pending": card.get("marketing_reconfirm_pending", False),
        }
    else:
        customer_summary = {
            "identified": False,
            "id": None,
            "customer_id": None,
            "display_name": None,
            "phone_e164": None,
            "phone_formatted": None,
            "formatted_phone": None,
            "current_stamps": 0,
            "stamp_target": stamp_target,
            "usable_rewards_count": 0,
            "usable_rewards": [],
            "reward_name": settings.get("reward_name", "1 İkram El Yapımı Kahve"),
            "daily_limit": daily_limit,
            "today_stamps": 0,
            "total_lifetime_stamps": 0,
            "push_opt_in": False,
            "pwa_bonus_claimed": 0,
            "notif_bonus_claimed": 0,
            "bonus_install_enabled": (settings.get("bonus_install_enabled", "1") or "1").strip() != "0",
            "bonus_notif_enabled": (settings.get("bonus_notif_enabled", "1") or "1").strip() != "0",
            "marketing_consent": False,
            "marketing_reconfirm_pending": False,
        }

    return {
        "status": "success",
        "identified": customer is not None,
        "business_name": settings.get("business_name", DEFAULT_BUSINESS_NAME),
        "reward_name": settings.get("reward_name", "1 İkram El Yapımı Kahve"),
        "stamp_target": stamp_target,
        "daily_limit": daily_limit,
        "kvkk_text": get_kvkk_text(settings),
        "push_enabled": is_push_enabled(),
        "customer": customer_summary,
        "campaigns": campaigns,
        "products": get_public_products(conn),
        "categories": get_public_categories(conn),
        "menu_note": settings.get("menu_note", ""),
        "info": get_public_info(settings),
    }


def get_public_info(settings: dict[str, str]) -> dict[str, str]:
    """Müşteri arayüzüne açık marka bilgisi (logo + ad). 'Hakkımızda' bölümü kaldırılmıştır."""
    return {
        "logo_url": settings.get("logo_url", "") or "",
        "business_name": settings.get("business_name", "") or "",
    }


def get_public_products(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Menüde gösterilecek aktif ürünler (yalnızca bilgilendirme; online ödeme yoktur)."""
    rows = conn.execute(
        """
        SELECT p.id, p.name, p.description, p.price, p.category, p.image_url, p.is_new, p.discount_price, p.discount_label
        FROM products p LEFT JOIN categories c ON LOWER(c.name) = LOWER(p.category)
        WHERE p.active = 1
        ORDER BY COALESCE(c.sort_order, 999999) ASC, p.category COLLATE NOCASE ASC, p.sort_order ASC, p.id ASC
        """
    ).fetchall()
    return [dict(r) for r in rows]


def get_public_categories(conn: sqlite3.Connection) -> list[str]:
    """Müşteri menüsü sekmeleri: yalnızca en az 1 aktif ürünü olan kategoriler, müdürün belirlediği sırayla."""
    rows = conn.execute(
        """
        SELECT p.category AS name, MIN(COALESCE(c.sort_order, 999999)) AS ord
        FROM products p LEFT JOIN categories c ON LOWER(c.name) = LOWER(p.category)
        WHERE p.active = 1 GROUP BY LOWER(p.category) ORDER BY ord ASC, p.category COLLATE NOCASE ASC
        """
    ).fetchall()
    return [r["name"] for r in rows]


def lookup_card_by_phone(
    conn: sqlite3.Connection,
    raw_phone: str,
    existing_device_token: Optional[str] = None,
    push_endpoint: Optional[str] = None,
    consent: Optional[dict[str, Any]] = None,
    otp: Optional[OtpContext] = None,
) -> tuple[bool, Optional[dict[str, Any]], Optional[str]]:
    """
    Telefon numarasıyla giriş/kayıt ve kalıcı cihaz bağlama.
    Yeni müşteri için onay eksikse ConsentRequired fırlatır. Kayıtlı numara yeni cihazdan girilirse (OTP_MODE=new_device)
    OtpRequired fırlatır: çağıran işlemi COMMIT edip `exc.response`u döndürmelidir.
    """
    try:
        phone_e164 = normalize_phone(raw_phone)
    except ValueError as e:
        logger.warning(
            "Kart sorgulama/giriş telefon doğrulama hatası (tel: %s): %s",
            mask_phone_for_log(raw_phone),
            str(e)
        )
        return False, None, str(e)

    gift: dict[str, Any] = {}
    try:
        customer_id, new_device_token, display_name = get_or_create_customer_with_device(
            conn,
            phone_e164,
            existing_device_token=existing_device_token,
            push_endpoint=push_endpoint,
            consent=consent,
            gift=gift,
            otp=otp,
        )
    except RegistrationBlocked as e:
        return False, None, str(e)

    settings = get_all_settings(conn)
    try:
        stamp_target = int(settings.get("stamp_target", "5"))
    except ValueError:
        stamp_target = 5

    current_stamps, usable_rewards = get_customer_counts(conn, customer_id)

    return True, {
        "display_name": display_name,
        "current_stamps": current_stamps,
        "stamp_target": stamp_target,
        "usable_rewards_count": usable_rewards,
        "pending_rewards": usable_rewards,
        "reward_name": settings.get("reward_name", "1 İkram El Yapımı Kahve"),
        "business_name": settings.get("business_name", DEFAULT_BUSINESS_NAME),
        # ADR-35: bu girişte yeni hesap açıldıysa kayıt hediyesi (+1 damga) verildi mi / kilitli mi (müşteriye mesaj)
        "welcome_gift": bool(gift.get("granted")),
        "welcome_locked": bool(gift.get("locked")),
        "welcome_pending": bool(gift.get("pending")),
        "welcome_message": WELCOME_GIFT_MESSAGE if gift.get("granted") else (
            WELCOME_PENDING_MESSAGE if gift.get("pending") else (
                WELCOME_LOCKED_MESSAGE if gift.get("locked") else "")),
        "_new_device_token": new_device_token,
    }, None


def check_staff_card(conn: sqlite3.Connection, staff_token: str) -> Optional[sqlite3.Row]:
    """Personel kartı hash'i ile kaydı döner."""
    if not staff_token:
        return None
    token_hash = hash_token(staff_token)
    cursor = conn.cursor()
    cursor.execute("SELECT id, label, active FROM staff_cards WHERE token_hash = ?", (token_hash,))
    return cursor.fetchone()


def check_reward_redemption_status(
    conn: sqlite3.Connection,
    staff_token: str,
    device_token: Optional[str]
) -> dict[str, Any]:
    """Personel kartı tarandığında müşterinin bilet durumunu sorgular."""
    staff = check_staff_card(conn, staff_token)
    if not staff or not staff["active"]:
        return {"status": "error", "message": "Geçersiz veya yetkisiz personel kartı."}

    customer = get_customer_from_cookie(conn, device_token)
    if not customer:
        return {
            "status": "error",
            "message": "Bu cihazda kayıtlı bir sadakat kartı bulunamadı. Lütfen müşterinin kendi telefonundan açınız."
        }

    customer_id = customer["id"]
    settings = get_all_settings(conn)

    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, issued_at FROM rewards
        WHERE customer_id = ? AND redeemed_at IS NULL AND voided_at IS NULL
        ORDER BY issued_at ASC LIMIT 1
    """, (customer_id,))
    reward = cursor.fetchone()

    if not reward:
        return {
            "status": "no_reward",
            "message": "Kullanılabilir ödül biletiniz bulunmuyor.",
            "business_name": settings.get("business_name", DEFAULT_BUSINESS_NAME),
            "staff_label": staff["label"]
        }

    return {
        "status": "ready",
        "reward_id": reward["id"],
        "reward_name": settings.get("reward_name", "1 İkram El Yapımı Kahve"),
        "business_name": settings.get("business_name", DEFAULT_BUSINESS_NAME),
        "staff_label": staff["label"]
    }


def confirm_reward_redemption(
    conn: sqlite3.Connection,
    staff_token: str,
    device_token: Optional[str],
    reward_id: int
) -> dict[str, Any]:
    """Ödül biletini personel kartı ile onaylayıp kullanıldı yapar."""
    staff = check_staff_card(conn, staff_token)
    if not staff or not staff["active"]:
        return {"status": "error", "message": "Yetkisiz personel kartı."}

    customer = get_customer_from_cookie(conn, device_token)
    if not customer:
        return {"status": "error", "message": "Müşteri kartı doğrulanamadı."}

    customer_id = customer["id"]
    now_iso = get_istanbul_now_iso()

    cursor = conn.cursor()
    cursor.execute("""
        UPDATE rewards
        SET redeemed_at = ?, staff_card_id = ?
        WHERE id = ? AND customer_id = ? AND redeemed_at IS NULL AND voided_at IS NULL
    """, (now_iso, staff["id"], reward_id, customer_id))

    if cursor.rowcount == 0:
        return {"status": "error", "message": "Bu ödül bileti daha önce kullanılmış veya geçersiz."}

    log_audit(
        conn,
        f"staff:{staff['id']}",
        "reward_redeemed",
        f"Ödül #{reward_id} personel '{staff['label']}' tarafından müşteri #{customer_id} için verildi."
    )

    stamps, rewards = get_customer_counts(conn, customer_id)
    sync_customer_to_directus(customer["phone_e164"], customer["display_name"], stamps, rewards)

    return {"status": "success", "message": "Afiyet olsun! Ödül başarıyla teslim edildi."}


def delete_customer_data(
    conn: sqlite3.Connection,
    raw_phone: str,
    device_token: Optional[str]
) -> tuple[bool, Optional[str]]:
    """KVKK Verilerimi Sil: Müşteri verilerini anonimleştirir/siler."""
    try:
        phone_e164 = normalize_phone(raw_phone)
    except ValueError as e:
        return False, str(e)

    customer = get_customer_from_cookie(conn, device_token)
    if not customer:
        return False, "Cihazınız doğrulanamadı."

    if customer["phone_e164"] != phone_e164:
        return False, "Telefon numarası ile cihaz eşleşmedi."

    anonymize_customer(conn, customer["id"], customer["phone_e164"], actor=f"customer:{customer['id']}",
                       reason="Müşteri veri silme talebi")
    log_audit(conn, f"customer:{customer['id']}", "customer_deleted", "Müşteri kendi verisini sildi.")
    return True, None


def export_customer_data(
    conn: sqlite3.Connection,
    raw_phone: str,
    device_token: Optional[str],
) -> tuple[bool, Optional[dict[str, Any]], Optional[str]]:
    """
    KVKK m.11: ilgili kişinin kendi verisinin kopyası (Verilerimi İndir). Silme ucuyla AYNI doğrulama: cihaz belirteci
    müşteriye ait olmalı ve telefon numarası eşleşmeli. Yalnızca bu müşterinin kayıtları döner; dahili güvenlik
    değerlendirmeleri (şüphe puanı/notları), işlem kayıtlarındaki IP/cihaz özetleri ve bildirim anahtarları verilmez.
    """
    try:
        phone_e164 = normalize_phone(raw_phone)
    except ValueError as e:
        return False, None, str(e)
    customer = get_customer_from_cookie(conn, device_token)
    if not customer:
        return False, None, "Cihazınız doğrulanamadı."
    if customer["phone_e164"] != phone_e164:
        return False, None, "Telefon numarası ile cihaz eşleşmedi."

    cid = customer["id"]
    row = conn.execute("SELECT * FROM customers WHERE id = ?", (cid,)).fetchone()

    def rows(sql: str) -> list[dict[str, Any]]:
        return [dict(r) for r in conn.execute(sql, (cid,)).fetchall()]

    data = {
        "disclaimer": "Bu dosya, sadakat kartı hesabınıza bağlı kayıtların bir kopyasıdır (KVKK m.11).",
        "exported_at": get_istanbul_now_iso(),
        "profile": {
            "display_name": row["display_name"],
            "phone": phone_e164,
            "registered_at": row["created_at"],
        },
        "consents": {
            "privacy_notice_read_at": row["kvkk_read_at"],
            "privacy_notice_version": row["consent_version"],
            "privacy_notice_channel": row["consent_source"],
            "privacy_notice_ip": row["consent_ip"],
            "marketing_consent": bool(row["marketing_consent"]),
            "marketing_consent_decided_at": row["marketing_consent_at"],
            "marketing_consent_text_version": row["marketing_consent_version"],
            "marketing_consent_channel": row["marketing_consent_channel"],
            "marketing_consent_ip": row["marketing_consent_ip"],
        },
        "notifications": {
            "browser_notifications_enabled": bool(row["push_opt_in"]),
            "devices": rows("SELECT created_at, last_success_at, active FROM push_subscriptions WHERE customer_id = ? ORDER BY id"),
        },
        "stamps": rows(
            "SELECT created_at, source, note, voided_at, voided_reason FROM stamps WHERE customer_id = ? ORDER BY id"
        ),
        "rewards": rows("SELECT issued_at, redeemed_at, voided_at FROM rewards WHERE customer_id = ? ORDER BY id"),
        "activity": rows(
            "SELECT at, kind, outcome, channel, title, detail FROM customer_events WHERE customer_id = ? ORDER BY id DESC LIMIT 500"
        ),
    }
    log_audit(conn, f"customer:{cid}", "customer_data_exported", "Müşteri kendi verisinin kopyasını indirdi.")
    return True, data, None


def anonymize_customer(
    conn: sqlite3.Connection,
    customer_id: int,
    phone_e164: str,
    actor: str,
    reason: str,
) -> None:
    """
    Müşteriyi anonimleştirir: telefon/isim/cihaz/abonelik/rıza izleri silinir, ödül ve damgalar iptal edilir.
    PostgreSQL (Directus) tarafındaki kopya da en iyi çabayla anonimleştirilir.
    """
    now_iso = get_istanbul_now_iso()
    cursor = conn.cursor()
    # Anti-fraud: silmeden ÖNCE telefon + cihaz imzalarını geri döndürülemez özet olarak kayıt defterine işle.
    device_hashes = [r["token_hash"] for r in cursor.execute(
        "SELECT token_hash FROM devices WHERE customer_id = ?", (customer_id,)
    ).fetchall()]
    register_deleted_identity(conn, phone_e164, device_hashes)
    anonymized_phone = f"+900000000000_silindi_{customer_id}"
    cursor.execute(
        """
        UPDATE customers
        SET phone_e164 = ?, display_name = ?, deleted_at = ?, push_opt_in = 0, push_permission_at = NULL,
            marketing_consent = 0, marketing_consent_at = NULL, kvkk_read_at = NULL, explicit_consent_at = NULL,
            consent_ip = NULL, consent_version = NULL, consent_source = NULL,
            marketing_consent_version = NULL, marketing_consent_channel = NULL, marketing_consent_ip = NULL,
            marketing_reconfirm_pending = 0
        WHERE id = ?
        """,
        (anonymized_phone, f"silindi_{customer_id}", now_iso, customer_id),
    )
    cursor.execute("DELETE FROM devices WHERE customer_id = ?", (customer_id,))
    cursor.execute("DELETE FROM push_subscriptions WHERE customer_id = ?", (customer_id,))
    # İşlem kayıtları (ADR-31) korunur (metinde ad/telefon yok); IP ve cihaz özeti silinir.
    cursor.execute("UPDATE customer_events SET ip = '', device_hash = '' WHERE customer_id = ?", (customer_id,))
    cursor.execute(
        "UPDATE rewards SET voided_at = ? WHERE customer_id = ? AND redeemed_at IS NULL AND voided_at IS NULL",
        (now_iso, customer_id),
    )
    cursor.execute(
        "UPDATE stamps SET voided_at = ?, voided_reason = ? WHERE customer_id = ? AND voided_at IS NULL",
        (now_iso, reason, customer_id),
    )
    # Ödüllü kampanya katılımları: tamamlanmamışlar silinir; onaylılarda yorum adı silinir, yalnızca geri döndürülemez
    # telefon özeti kalır (aynı kişi hesabını silip açarak aynı kampanyadan ikinci kez ödül alamasın — ADR-09 ile aynı ilke).
    cursor.execute("DELETE FROM campaign_participants WHERE customer_id = ? AND status != 'approved'", (customer_id,))
    cursor.execute("UPDATE campaign_participants SET proof_name = NULL WHERE customer_id = ?", (customer_id,))
    # Denetim günlüğünde metin olarak geçen kullanıcı adı silinir (Y-06). Onay/ret ve güvenlik satırları ispat için kalır; onlarda ad yoktur.
    cursor.execute(
        "UPDATE audit_log SET detail = 'Kullanıcı adı güncellendi (ad silindi).' WHERE action = 'profile_updated' AND actor = ?",
        (f"customer:{customer_id}",),
    )
    cursor.execute(
        "UPDATE audit_log SET detail = ? WHERE action = 'customer_registered' AND detail LIKE ?",
        (f"Yeni müşteri #{customer_id} (ad silindi) kaydedildi.", f"Yeni müşteri #{customer_id} (%"),
    )
    cursor.execute(
        "UPDATE audit_log SET detail = ? WHERE action IN ('customer_created', 'customer_restored_or_updated') AND detail LIKE ?",
        (f"Müşteri #{customer_id} (ad silindi) kaydı işlendi.", f"%#{customer_id} (%"),
    )
    cursor.execute(
        "UPDATE audit_log SET detail = ? WHERE action = 'customer_name_updated' AND detail LIKE ?",
        (f"Müşteri #{customer_id} adı güncellendi (ad silindi).", f"Müşteri #{customer_id} adı '%"),
    )
    log_audit(conn, actor, "customer_anonymized", f"Müşteri #{customer_id} anonimleştirildi ({reason}).")
    anonymize_directus_customer(phone_e164)

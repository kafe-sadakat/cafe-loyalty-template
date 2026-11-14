import ipaddress
import json
import logging
import os
import re
import sqlite3
from datetime import timedelta
from typing import Optional, Any
from urllib.parse import urlparse
from pywebpush import webpush, WebPushException

from app.config import (
    DEFAULT_BUSINESS_NAME,
    PUBLIC_BASE_PATH,
    PUBLIC_BASE_URL,
    edge_allowed_hosts,
    get_vapid_settings,
    is_push_enabled,
)
from app.db import get_all_settings, log_audit, run_after_commit
from app.utils import get_istanbul_now, get_istanbul_now_iso, ISTANBUL_TZ, parse_to_istanbul

logger = logging.getLogger("sadakat")

MAX_FAIL_COUNT = 3
# TTL (saniye): push servisi (FCM/APNs/Mozilla) cihaz o an çevrimdışıysa iletiyi bu süre boyunca bekletir.
# pywebpush varsayılanı ttl=0'dır: telefon uykudaysa/ağ yoksa bildirim ANINDA çöpe atılır ("gönderildi ama gelmedi").
TTL_CAMPAIGN = 24 * 3600
TTL_TRANSACTIONAL = 12 * 3600
TTL_TEST = 15 * 60
VALID_AUDIENCES = {"all", "almost_there", "unclaimed_rewards", "inactive_30d"}
AUDIENCE_LABELS = {
    "all": "Tüm Abone Müşteriler",
    "almost_there": "Ödülüne 1-2 Damga Kalanlar",
    "unclaimed_rewards": "Ödülü Hazır Ama Kullanmayanlar",
    "inactive_30d": "30 Gündür Gelmeyenler",
}
# Ödüllü kampanyayı henüz YAPMAYANLAR (ADR-26): "campaign:<id>" → doğrulama bekleyen veya ödülü alan müşteri hariç.
CAMPAIGN_AUDIENCE_PREFIX = "campaign:"


def campaign_audience_id(audience: Optional[str]) -> Optional[int]:
    """"campaign:12" → 12; başka biçimde None."""
    a = str(audience or "")
    if not a.startswith(CAMPAIGN_AUDIENCE_PREFIX):
        return None
    tail = a[len(CAMPAIGN_AUDIENCE_PREFIX):]
    return int(tail) if tail.isdigit() and len(tail) <= 9 else None


def _reward_campaign_title(conn: sqlite3.Connection, campaign_id: int) -> Optional[str]:
    row = conn.execute(
        "SELECT title FROM campaigns WHERE id = ? AND COALESCE(reward_stamps, 0) > 0", (campaign_id,)
    ).fetchone()
    return row["title"] if row else None


def audience_is_valid(conn: sqlite3.Connection, audience: str) -> bool:
    if audience in VALID_AUDIENCES:
        return True
    cid = campaign_audience_id(audience)
    return cid is not None and _reward_campaign_title(conn, cid) is not None


def audience_label(conn: sqlite3.Connection, audience: str) -> str:
    cid = campaign_audience_id(audience)
    if cid is not None:
        title = _reward_campaign_title(conn, cid)
        return f"'{title}' kampanyasını yapmayanlar" if title else audience
    return AUDIENCE_LABELS.get(audience, audience)


_TLD_RE = re.compile(r"^(?:[a-z]{2,63}|xn--[a-z0-9-]{1,59})$")
_BLOCKED_HOST_SUFFIXES = (".localhost", ".local", ".localdomain", ".internal", ".lan", ".home", ".corp", ".intranet")


def is_safe_push_endpoint(endpoint: Optional[str]) -> bool:
    """
    SSRF koruması: sunucu bildirim gönderirken abonelik adresine POST atar; adres müşteriden (tarayıcıdan) gelir.
    Gerçek push servisleri (FCM, Mozilla, Apple, Windows) her zaman https + alan adı + varsayılan port kullanır.
    IP adresi (127.0.0.1, 169.254.169.254, 0x7f.1 …), tek etiketli ad (Docker servis adları: db, directus …),
    localhost / iç alan adları, özel port ve kullanıcı bilgisi (kullanici@sunucu) reddedilir. https ile sertifika
    doğrulaması da iç ağdaki bir servise bu adresle istek iletilmesini engeller.
    """
    if not endpoint or not isinstance(endpoint, str) or len(endpoint) > 2048:
        return False
    try:
        parsed = urlparse(endpoint.strip())
        host = (parsed.hostname or "").rstrip(".").lower()
        port = parsed.port
    except ValueError:
        return False
    if parsed.scheme != "https" or not host or parsed.username is not None or parsed.password is not None:
        return False
    if port not in (None, 443) or "." not in host or host.endswith(_BLOCKED_HOST_SUFFIXES):
        return False
    try:
        ipaddress.ip_address(host.strip("[]"))
        return False
    except ValueError:
        pass
    return bool(_TLD_RE.match(host.rsplit(".", 1)[-1]))


def sanitize_push_url(url: Optional[str], default: str) -> str:
    """
    Bildirime dokununca açılacak adres (G-09): yalnızca bu uygulamanın kendi yolu ("/k/kart/…") ya da bilinen genel alan
    adları (PUBLIC_BASE_URL + kenar vekili alan adları) kabul edilir; başka bir adres varsayılana düşer. Çalınmış bir müdür
    oturumu, marka adından giden bildirimle müşterileri yabancı bir siteye (oltalama) yönlendiremesin.
    """
    url = (url or "").strip()
    if not url or len(url) > 500 or any(ord(ch) < 32 or ch == "\\" for ch in url):
        return default
    if url.startswith("/") and not url.startswith("//"):
        return url
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        return default
    # İzin verilen genel alan adları: PUBLIC_BASE_URL + EDGE_DEFAULT_HOST / EDGE_ALLOWED_HOSTS + isteğe bağlı EXTRA_PUBLIC_HOSTS.
    extra_hosts = {h.strip().lower() for h in os.getenv("EXTRA_PUBLIC_HOSTS", "").split(",") if h.strip()}
    allowed = {(urlparse(PUBLIC_BASE_URL).hostname or "").lower(), *extra_hosts, *edge_allowed_hosts()} - {""}
    if parsed.scheme == "https" and host in allowed and port in (None, 443) and parsed.username is None:
        return url
    return default


def validate_subscription_input(endpoint: str, p256dh: str, auth: str) -> tuple[bool, Optional[str]]:
    """Web Push abonelik parametrelerini doğrular."""
    if not endpoint or not isinstance(endpoint, str):
        return False, "Geçersiz bildirim uç noktası (endpoint)."
    endpoint = endpoint.strip()
    if len(endpoint) > 2048:
        return False, "Endpoint çok uzun."
    if not is_safe_push_endpoint(endpoint):
        return False, "Endpoint geçerli bir bildirim servisi (HTTPS) adresi olmalıdır."

    if not p256dh or not isinstance(p256dh, str) or len(p256dh.strip()) < 10 or len(p256dh.strip()) > 512:
        return False, "Geçersiz p256dh anahtarı."
    if not auth or not isinstance(auth, str) or len(auth.strip()) < 6 or len(auth.strip()) > 256:
        return False, "Geçersiz auth anahtarı."

    return True, None


def get_push_branding(conn: sqlite3.Connection) -> dict[str, str]:
    """Bildirimin telefonda kafenin kendi logosu ve temasıyla düşmesi için icon/badge/tema bilgisi."""
    settings = get_all_settings(conn)
    base = PUBLIC_BASE_PATH
    if (settings.get("logo_url") or "").strip():
        icon = f"{base}/static/uploads/logo-192.png"
        badge = f"{base}/static/uploads/badge-96.png"
    else:
        icon = f"{base}/static/img/icon-192.png"
        badge = f"{base}/static/img/badge-96.png"
    return {
        "icon": icon,
        "badge": badge,
        "theme_color": settings.get("accent_color", "#1E4D3F"),
        "business_name": settings.get("business_name", ""),
    }


# ADR-36 (Y-04, ADR-27'yi YALNIZCA kampanya bildirimleri için kısmen geri alır): ticari ileti niteliğindeki kampanya bildirimi
# göndereni başlıkta (marka adı) tanıtır ve gövdesinin sonunda kısa bir ret yolu taşır. Düğme (action) eklenmez. Damga/ödül
# (işlemsel) ve test bildirimleri bu satırı taşımaz. Müdür paneli önizlemesi aynı sabiti kullanır.
CAMPAIGN_OPTOUT_HINT = "Kapatmak için: Profil › Kampanya Bildirimleri"


def campaign_body(body: str) -> str:
    """Kampanya bildirimi gövdesi: müdürün metni + yeni satırda ret ipucu (zaten varsa tekrar eklenmez)."""
    body = (body or "").strip()
    if body.endswith(CAMPAIGN_OPTOUT_HINT):
        return body
    return body + "\n" + CAMPAIGN_OPTOUT_HINT


def brand_title(branding: dict[str, str], title: str) -> str:
    """Bildirim başlığına marka adını işler (başlıkta zaten geçiyorsa dokunmaz): "İşletme · Başlık"."""
    brand = (branding.get("business_name") or "").strip()
    title = (title or "").strip()
    if not brand or brand.lower() in title.lower():
        return title
    return f"{brand} · {title}"


def subscribe_push(
    conn: sqlite3.Connection,
    endpoint: str,
    p256dh: str,
    auth: str,
    customer_id: Optional[int] = None,
    token_hash: Optional[str] = None,
) -> dict[str, Any]:
    """
    Push aboneliğini kaydeder veya aynı endpoint tekrar kaydolursa günceller.
    Müşteri tanınmıyorsa anonim (customer_id=NULL) kaydeder; tanınıyorsa müşteriye bağlar.
    ADR-36 (Y-02): abonelik (tarayıcı izni + işlemsel damga/ödül bildirimi) kampanya onayından BAĞIMSIZDIR; bu işlem
    marketing_consent'e hiç dokunmaz. Kampanya onayı yalnızca ayrı, açık seçimle verilir (set_marketing_consent).
    """
    valid, err = validate_subscription_input(endpoint, p256dh, auth)
    if not valid:
        return {"status": "error", "message": err}

    endpoint = endpoint.strip()
    p256dh = p256dh.strip()
    auth = auth.strip()
    now_iso = get_istanbul_now_iso()
    cursor = conn.cursor()

    cursor.execute("SELECT id, customer_id FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
    existing = cursor.fetchone()

    resolved_customer_id = customer_id
    if existing and resolved_customer_id is None and existing["customer_id"] is not None:
        resolved_customer_id = existing["customer_id"]

    if existing:
        cursor.execute(
            """
            UPDATE push_subscriptions
            SET customer_id = ?,
                p256dh = ?,
                auth = ?,
                token_hash = COALESCE(?, token_hash),
                fail_count = 0,
                active = 1
            WHERE id = ?
            """,
            (resolved_customer_id, p256dh, auth, token_hash, existing["id"]),
        )
        sub_id = existing["id"]
    else:
        cursor.execute(
            """
            INSERT INTO push_subscriptions
                (customer_id, endpoint, p256dh, auth, token_hash, created_at, last_success_at, fail_count, active)
            VALUES (?, ?, ?, ?, ?, ?, NULL, 0, 1)
            """,
            (resolved_customer_id, endpoint, p256dh, auth, token_hash, now_iso),
        )
        sub_id = cursor.lastrowid

    if resolved_customer_id is not None:
        cursor.execute(
            "UPDATE customers SET push_opt_in = 1, push_permission_at = ? WHERE id = ?",
            (now_iso, resolved_customer_id),
        )

    consent_now = False
    if resolved_customer_id is not None:
        consent_now = bool(cursor.execute(
            "SELECT COALESCE(marketing_consent, 0) AS mc FROM customers WHERE id = ?", (resolved_customer_id,)
        ).fetchone()["mc"])
    return {
        "status": "success",
        "subscription_id": sub_id,
        "linked": resolved_customer_id is not None,
        "marketing_consent": consent_now,
    }


def link_anonymous_subscriptions(
    conn: sqlite3.Connection,
    customer_id: int,
    token_hash: Optional[str] = None,
    endpoint: Optional[str] = None,
) -> int:
    """
    Daha önce anonim (customer_id IS NULL) olarak kaydedilmiş push aboneliklerini
    müşteri kimliği doğrulandığında otomatik olarak customer_id ile ilişkilendirir.
    """
    cursor = conn.cursor()
    now_iso = get_istanbul_now_iso()
    linked_count = 0

    if endpoint:
        cursor.execute(
            "UPDATE push_subscriptions SET customer_id = ?, token_hash = COALESCE(?, token_hash) WHERE endpoint = ? AND (customer_id IS NULL OR customer_id = ?)",
            (customer_id, token_hash, endpoint.strip(), customer_id),
        )
        linked_count += cursor.rowcount

    if token_hash:
        cursor.execute(
            "UPDATE push_subscriptions SET customer_id = ? WHERE customer_id IS NULL AND token_hash = ?",
            (customer_id, token_hash),
        )
        linked_count += cursor.rowcount

    # Aktif aboneliği varsa müşterinin push_opt_in iznini güncelle
    cursor.execute(
        "SELECT COUNT(*) as cnt FROM push_subscriptions WHERE customer_id = ? AND active = 1",
        (customer_id,),
    )
    if cursor.fetchone()["cnt"] > 0:
        cursor.execute(
            "UPDATE customers SET push_opt_in = 1, push_permission_at = COALESCE(push_permission_at, ?) WHERE id = ?",
            (now_iso, customer_id),
        )

    return linked_count


def unsubscribe_push(
    conn: sqlite3.Connection,
    endpoint: Optional[str] = None,
    customer_id: Optional[int] = None,
    token_hash: Optional[str] = None,
    ip: str = "",
) -> dict[str, Any]:
    """
    Aboneliği pasife alır (active=0) ve müşterinin push izin durumunu günceller.
    Müşterinin aktif aboneliği kalmadıysa açık rıza + ticari ileti onayı da geri alınır (ETK ret; anında geçerli).
    """
    cursor = conn.cursor()
    affected = 0

    if endpoint and endpoint.strip():
        cursor.execute(
            "UPDATE push_subscriptions SET active = 0 WHERE endpoint = ?",
            (endpoint.strip(),),
        )
        affected += cursor.rowcount

    if customer_id is not None and not endpoint:
        cursor.execute(
            "UPDATE push_subscriptions SET active = 0 WHERE customer_id = ?",
            (customer_id,),
        )
        affected += cursor.rowcount
    elif token_hash and not endpoint:
        cursor.execute(
            "UPDATE push_subscriptions SET active = 0 WHERE token_hash = ?",
            (token_hash,),
        )
        affected += cursor.rowcount

    if customer_id is not None:
        cursor.execute(
            "SELECT COUNT(*) as cnt FROM push_subscriptions WHERE customer_id = ? AND active = 1",
            (customer_id,),
        )
        if cursor.fetchone()["cnt"] == 0:
            # Tüm abonelikler kapandı: bildirim izni ve ticari ileti onayı geri alınmış sayılır
            had_consent = bool(cursor.execute(
                "SELECT COALESCE(marketing_consent, 0) AS mc FROM customers WHERE id = ?", (customer_id,)
            ).fetchone()["mc"])
            cursor.execute(
                """
                UPDATE customers SET push_opt_in = 0, marketing_consent = 0, marketing_consent_at = ?,
                    explicit_consent_at = NULL, marketing_reconfirm_pending = 0
                WHERE id = ?
                """,
                (get_istanbul_now_iso(), customer_id),
            )
            if had_consent:
                version = get_all_settings(conn).get("marketing_consent_version", "")
                log_audit(conn, f"customer:{customer_id}", "marketing_consent_revoked",
                          f"Bildirimler kapatıldı; açık rıza + ticari ileti onayı geri alındı (ret, metin sürümü={version}, "
                          f"ip={(ip or '')[:64]}).")

    return {"status": "success", "deactivated": affected}


def _extract_status_code(exc: Exception) -> Optional[int]:
    """WebPushException veya benzeri hatalardan HTTP durum kodunu çıkarır."""
    resp = getattr(exc, "response", None)
    if resp is not None:
        code = getattr(resp, "status_code", None)
        if isinstance(code, int):
            return code
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    msg = str(exc)
    if "410" in msg:
        return 410
    if "404" in msg:
        return 404
    return None


_ERROR_HINTS = {
    400: "geçersiz istek",
    401: "VAPID imzası reddedildi",
    403: "VAPID anahtarı abonelikle uyuşmuyor (cihaz uygulamayı açınca yeniden abone olur)",
    404: "abonelik artık yok",
    410: "abonelik iptal edilmiş",
    413: "bildirim çok büyük",
    429: "push servisi hız sınırı",
}


def describe_push_error(status_code: Optional[int]) -> str:
    """Müdüre gösterilecek kısa, Türkçe hata açıklaması."""
    if status_code is None:
        return "push servisine ulaşılamadı"
    return f"HTTP {status_code}: " + _ERROR_HINTS.get(status_code, "push servisi hatası")


def send_webpush_to_subscription(
    conn: sqlite3.Connection,
    sub: sqlite3.Row | dict[str, Any],
    payload: dict[str, Any],
    ttl: int = TTL_CAMPAIGN,
    urgency: str = "high",
    errors: Optional[list[str]] = None,
) -> bool:
    """
    Tek bir push aboneliğine bildirim gönderir.
    - `ttl`: cihaz çevrimdışıyken push servisinin iletiyi bekleteceği süre (0 olursa uyuyan telefona hiç ulaşmaz).
    - `Urgency: high`: Android Doze / pil tasarrufu modundaki cihazı uyandırır.
    - Başarılıysa last_success_at güncellenir, fail_count=0 yapılır.
    - 404 / 410 dönerse abonelik derhal pasife alınır (active=0).
    - Diğer hatalarda fail_count artırılır; MAX_FAIL_COUNT (3) olursa pasife alınır.
    - `errors` listesi verilirse başarısızlığın kısa açıklaması eklenir (müdür ekranında gösterilir).
    """
    if not is_push_enabled():
        if errors is not None:
            errors.append("VAPID anahtarları tanımlı değil")
        return False
    outcome = _webpush_network(sub, payload, ttl, urgency)
    _record_webpush_outcome(conn, sub, outcome)
    if not outcome["ok"] and errors is not None and outcome.get("error_text"):
        errors.append(outcome["error_text"])
    return outcome["ok"]


def _webpush_network(sub: sqlite3.Row | dict[str, Any], payload: dict[str, Any], ttl: int, urgency: str) -> dict[str, Any]:
    """
    Tek aboneliğe ağ üzerinden gönderim. Veritabanına DOKUNMAZ (yazma kilidi tutulurken çağrılmamalıdır);
    sonuç `_record_webpush_outcome` ile kaydedilir. Dönüş: {"ok", "unsafe", "status_code", "exc", "error_text"}.
    """
    endpoint = sub["endpoint"]
    if not is_safe_push_endpoint(endpoint):
        # Eski sürümde kaydedilmiş iç ağ / IP / http adresi: sunucu oraya istek ATMAZ, abonelik kapatılır (SSRF)
        return {"ok": False, "unsafe": True, "status_code": None, "exc": None,
                "error_text": "geçersiz bildirim adresi (gönderilmedi)"}
    vapid = get_vapid_settings()
    try:
        webpush(
            subscription_info={"endpoint": endpoint, "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]}},
            data=json.dumps(payload, ensure_ascii=False),
            vapid_private_key=vapid["private_key"],
            vapid_claims={"sub": vapid["subject"]},
            timeout=10,
            ttl=ttl,
            headers={"Urgency": urgency},
        )
        return {"ok": True, "unsafe": False, "status_code": None, "exc": None, "error_text": ""}
    except Exception as exc:
        status_code = _extract_status_code(exc)
        return {"ok": False, "unsafe": False, "status_code": status_code, "exc": exc,
                "error_text": describe_push_error(status_code)}


def _record_webpush_outcome(conn: sqlite3.Connection, sub: sqlite3.Row | dict[str, Any], outcome: dict[str, Any]) -> None:
    """Gönderim sonucunu (başarı / hata sayacı / pasife alma) abonelik satırına yazar."""
    sub_id = sub["id"]
    current_fails = int(sub["fail_count"] or 0)
    cursor = conn.cursor()
    if outcome["unsafe"]:
        cursor.execute("UPDATE push_subscriptions SET active = 0 WHERE id = ?", (sub_id,))
        logger.warning("Push aboneliği #%s güvenli olmayan adres nedeniyle pasife alındı (gönderim yapılmadı).", sub_id)
    elif outcome["ok"]:
        cursor.execute(
            "UPDATE push_subscriptions SET last_success_at = ?, fail_count = 0, active = 1 WHERE id = ?",
            (get_istanbul_now_iso(), sub_id),
        )
    elif outcome["status_code"] in (404, 410):
        cursor.execute(
            "UPDATE push_subscriptions SET active = 0, fail_count = ? WHERE id = ?",
            (current_fails + 1, sub_id),
        )
        logger.info("Push aboneliği #%s HTTP %s nedeniyle pasife alındı.", sub_id, outcome["status_code"])
    else:
        new_fails = current_fails + 1
        new_active = 0 if new_fails >= MAX_FAIL_COUNT else 1
        cursor.execute(
            "UPDATE push_subscriptions SET fail_count = ?, active = ? WHERE id = ?",
            (new_fails, new_active, sub_id),
        )
        logger.warning("Push gönderim hatası (abonelik #%s, hata sayısı=%s): %s", sub_id, new_fails, str(outcome["exc"]))


def send_webpush_batch_without_lock(
    db_path: Any,
    subs: list[dict[str, Any]],
    payload: dict[str, Any],
    ttl: int,
    errors: Optional[list[str]] = None,
    log: Optional[dict[str, Any]] = None,
) -> tuple[int, int]:
    """
    Aboneliklere gönderim: her ağ çağrısı yazma kilidi OLMADAN yapılır, sonuç her abonelik için ayrı kısa işlemle kaydedilir
    (yavaş/ölü bir push sunucusu diğer müşterilerin damga/kayıt yazmalarını bekletmez, bkz. E-01). `log` verilirse
    gönderim sonunda notification_log satırı yazılır. Dönüş: (başarılı, başarısız).
    """
    from app.db import get_db  # döngüsel içe aktarma olmasın

    sent = failed = 0
    for sub in subs:
        outcome = _webpush_network(sub, payload, ttl, "high")
        try:
            with get_db(immediate=True, db_path=db_path) as conn:
                _record_webpush_outcome(conn, sub, outcome)
        except Exception as exc:
            logger.warning("Push sonucu kaydedilemedi (abonelik #%s): %s", sub["id"], exc)
        if outcome["ok"]:
            sent += 1
        else:
            failed += 1
            if errors is not None and outcome.get("error_text"):
                errors.append(outcome["error_text"])
    if log:
        with get_db(immediate=True, db_path=db_path) as conn:
            log_notification(conn, sent_count=sent, failed_count=failed, **log)
    return sent, failed


def log_notification(
    conn: sqlite3.Connection,
    kind: str,
    title: str,
    audience: str,
    sent_count: int,
    failed_count: int,
) -> int:
    """notification_log tablosuna gönderim kaydı ekler."""
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO notification_log (kind, title, audience, sent_count, failed_count, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (kind, title, audience, sent_count, failed_count, get_istanbul_now_iso()),
    )
    return cursor.lastrowid


# ADR-45: otomatik bildirimlerin (damga eklendi / ödül hazır) başlık ve metni müdür panelinden düzenlenir. Boş bırakılan
# alan varsayılana döner. Yer tutucular güvenli düz değiştirme ile doldurulur (str.format değil: süslü parantez hata vermez).
AUTO_NOTIF_DEFAULTS = {
    "notif_stamp_title": "{isletme} - Damgan eklendi ☕",
    "notif_stamp_body": "Damgan eklendi ({damga}/{hedef}), ödüle {kalan} kaldı!",
    "notif_reward_title": "{isletme} - Ödülün hazır! 🎁",
    "notif_reward_body": "Tebrikler! Ödülün hazır: {odul}. Kasada personel kartı ile hemen kullanabilirsin.",
}
AUTO_NOTIF_MAX = {"title": 80, "body": 240}
AUTO_NOTIF_TOKENS = ("isletme", "damga", "hedef", "kalan", "odul")


def auto_notification_text(settings: dict[str, Any], key: str, values: dict[str, Any]) -> str:
    """Kayıtlı (yoksa varsayılan) otomatik bildirim metnine değerleri yerleştirir."""
    text = (settings.get(key) or "").strip() or AUTO_NOTIF_DEFAULTS[key]
    for token in AUTO_NOTIF_TOKENS:
        text = text.replace("{" + token + "}", str(values.get(token, "")))
    return text


def notify_customer_after_stamp(
    conn: sqlite3.Connection,
    customer_id: int,
    current_stamps: int,
    stamp_target: int,
    reward_unlocked: bool,
    reward_name: str,
    business_name: Optional[str] = None,
) -> dict[str, int]:
    """
    Damga eklendiğinde veya ödül kazanıldığında müşterinin aktif aboneliklerine
    Türkçe bildirim gönderir. Hata olsa bile damga akışını ASLA bozmaz.
    """
    sent = 0
    failed = 0
    try:
        cursor = conn.cursor()
        settings = get_all_settings(conn)
        if not business_name:
            business_name = settings.get("business_name", DEFAULT_BUSINESS_NAME)
        values = {
            "isletme": business_name, "damga": current_stamps, "hedef": stamp_target,
            "kalan": max(0, stamp_target - current_stamps), "odul": reward_name,
        }
        prefix = "notif_reward" if reward_unlocked else "notif_stamp"
        kind = "reward_ready" if reward_unlocked else "stamp_added"
        # yer tutucular uzayabilir (işletme / ödül adı): gönderilen metin yine sınırlı kalır
        title = auto_notification_text(settings, prefix + "_title", values)[:120]
        body = auto_notification_text(settings, prefix + "_body", values)[:300]

        if not is_push_enabled():
            return {"sent": 0, "failed": 0}

        cursor.execute(
            """
            SELECT id, customer_id, endpoint, p256dh, auth, fail_count, active
            FROM push_subscriptions
            WHERE customer_id = ? AND active = 1
            """,
            (customer_id,),
        )
        subs = cursor.fetchall()
        if not subs:
            return {"sent": 0, "failed": 0}

        branding = get_push_branding(conn)
        payload = {
            "title": title,
            "body": body,
            "url": f"{PUBLIC_BASE_PATH}/kart/",
            "icon": branding["icon"],
            "badge": branding["badge"],
            "tag": kind,
            "kind": kind,
            "current_stamps": current_stamps,
            "stamp_target": stamp_target,
            "reward_unlocked": reward_unlocked,
        }

        # Ağ çağrısı yazma işlemi (damga yazımı) açıkken YAPILMAZ: yavaş push sunucusu tüm yazmaları kilitlemesin (E-01).
        # `get_db` bağlamında gönderim COMMIT'ten sonraya ertelenir; bağlam dışında (testler) hemen gönderilir.
        sub_rows = [dict(s) for s in subs]
        log_info = {"kind": kind, "title": title, "audience": f"customer:{customer_id}"}
        if run_after_commit(
            conn, lambda db_path: send_webpush_batch_without_lock(db_path, sub_rows, payload, TTL_TRANSACTIONAL, log=log_info)
        ):
            return {"sent": 0, "failed": 0}

        for sub in subs:
            if send_webpush_to_subscription(conn, sub, payload, ttl=TTL_TRANSACTIONAL):
                sent += 1
            else:
                failed += 1

        log_notification(conn, kind=kind, title=title, audience=f"customer:{customer_id}", sent_count=sent, failed_count=failed)
    except Exception as exc:
        logger.warning("Otomatik damga/ödül bildirimi sırasında hata (yutuldu): %s", str(exc))

    return {"sent": sent, "failed": failed}


def is_quiet_hours_istanbul(now=None) -> bool:
    """
    Europe/Istanbul saatine göre 22:00 - 09:00 arası sessiz saat kontrolü.
    Saat >= 22 veya saat < 9 ise True döner.
    """
    if now is None:
        now = get_istanbul_now()
    now_ist = now.astimezone(ISTANBUL_TZ)
    return now_ist.hour >= 22 or now_ist.hour < 9


def is_audience_rate_limited_24h(conn: sqlite3.Connection, audience: str, now=None) -> bool:
    """Aynı kitleye son 24 saat içinde toplu kampanya bildirimi gönderilmiş mi kontrol eder."""
    if now is None:
        now = get_istanbul_now()
    cutoff = (now.astimezone(ISTANBUL_TZ) - timedelta(hours=24))
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT created_at FROM notification_log
        WHERE kind = 'campaign' AND audience = ?
        ORDER BY id DESC LIMIT 20
        """,
        (audience,),
    )
    for row in cursor.fetchall():
        dt = parse_to_istanbul(row["created_at"])
        if dt and dt >= cutoff:
            return True
    return False


def get_campaign_target_customer_ids(conn: sqlite3.Connection, audience: str) -> list[int]:
    """Kitleye uyan, ticari ileti onayı (marketing_consent = 1) olan silinmemiş müşteri kimlikleri."""
    camp_id = campaign_audience_id(audience)
    if camp_id is not None:
        # Kampanyayı yapmayanlar: hiç katılmamış, katılıp bildirmemiş veya reddedilmiş (yeniden deneyebilir)
        return [r["id"] for r in conn.execute(
            """
            SELECT c.id FROM customers c
            WHERE c.deleted_at IS NULL AND COALESCE(c.marketing_consent, 0) = 1
              AND NOT EXISTS (
                SELECT 1 FROM campaign_participants p
                WHERE p.campaign_id = ? AND p.customer_id = c.id AND p.status IN ('pending', 'approved')
              )
            """,
            (camp_id,),
        )]
    if audience not in VALID_AUDIENCES:
        audience = "all"

    cursor = conn.cursor()
    settings = get_all_settings(conn)
    try:
        stamp_target = int(settings.get("stamp_target", "5"))
    except ValueError:
        stamp_target = 5

    now = get_istanbul_now()
    thirty_days_ago_iso = (now - timedelta(days=30)).isoformat()

    cursor.execute(
        """
        SELECT
            c.id as customer_id,
            (SELECT COUNT(*) FROM stamps s WHERE s.customer_id = c.id AND s.voided_at IS NULL AND s.reward_id IS NULL) as current_stamps,
            (SELECT COUNT(*) FROM rewards r WHERE r.customer_id = c.id AND r.redeemed_at IS NULL AND r.voided_at IS NULL) as pending_rewards,
            (SELECT MAX(created_at) FROM stamps s WHERE s.customer_id = c.id AND s.voided_at IS NULL) as last_visit
        FROM customers c
        WHERE c.deleted_at IS NULL AND COALESCE(c.marketing_consent, 0) = 1
        """
    )
    matching_customer_ids = []
    for row in cursor.fetchall():
        cid = row["customer_id"]
        stamps = row["current_stamps"] or 0
        rewards = row["pending_rewards"] or 0
        last_visit = row["last_visit"]

        if audience == "all":
            matching_customer_ids.append(cid)
        elif audience == "almost_there":
            diff = stamp_target - stamps
            if diff in (1, 2) and stamps > 0:
                matching_customer_ids.append(cid)
        elif audience == "unclaimed_rewards":
            if rewards > 0:
                matching_customer_ids.append(cid)
        elif audience == "inactive_30d":
            if last_visit and last_visit < thirty_days_ago_iso:
                matching_customer_ids.append(cid)
    return matching_customer_ids


def get_campaign_target_subscriptions(conn: sqlite3.Connection, audience: str) -> list[sqlite3.Row]:
    """
    Seçilen kitleye ('all', 'almost_there', 'unclaimed_rewards', 'inactive_30d', 'campaign:<id>')
    uygun aktif push aboneliklerini döner.
    """
    if audience not in VALID_AUDIENCES and campaign_audience_id(audience) is None:
        audience = "all"

    cursor = conn.cursor()
    # TİCARİ İLETİ ONAYI: kampanya bildirimi yalnızca marketing_consent = 1 olan, silinmemiş
    # müşterilere bağlı aboneliklere gider. Anonim (müşteriye bağlı olmayan) abonelikler kampanya almaz.
    if audience == "all":
        cursor.execute(
            """
            SELECT ps.id, ps.customer_id, ps.endpoint, ps.p256dh, ps.auth, ps.fail_count, ps.active
            FROM push_subscriptions ps
            JOIN customers c ON c.id = ps.customer_id
            WHERE ps.active = 1 AND c.deleted_at IS NULL AND COALESCE(c.marketing_consent, 0) = 1
            """
        )
        return cursor.fetchall()

    matching_customer_ids = get_campaign_target_customer_ids(conn, audience)
    if not matching_customer_ids:
        return []

    placeholders = ",".join("?" for _ in matching_customer_ids)
    cursor.execute(
        f"""
        SELECT id, customer_id, endpoint, p256dh, auth, fail_count, active
        FROM push_subscriptions
        WHERE active = 1 AND customer_id IN ({placeholders})
        """,
        matching_customer_ids,
    )
    return cursor.fetchall()


def preview_campaign(conn: sqlite3.Connection, audience: str) -> dict[str, Any]:
    """Kampanya gönderimi öncesi alıcı/cihaz sayısını ve kitle durumunu döner."""
    if not audience_is_valid(conn, audience):
        return {"status": "error", "message": "Geçersiz hedef kitle seçimi."}

    subs = get_campaign_target_subscriptions(conn, audience)
    unique_customers = {s["customer_id"] for s in subs if s["customer_id"] is not None}
    # Tarayıcı izni verip aboneliği olan ama ticari ileti onayı OLMAYAN müşteriler: kampanya alamaz (ETK).
    pending = conn.execute(
        """
        SELECT COUNT(DISTINCT c.id) AS n FROM push_subscriptions ps JOIN customers c ON c.id = ps.customer_id
        WHERE ps.active = 1 AND c.deleted_at IS NULL AND COALESCE(c.marketing_consent, 0) = 0
        """
    ).fetchone()["n"]

    # ADR-36: eski "Bildirimleri Aç" onayı geçersiz sayılıp yeniden onay bekleyenler (kampanya almaz; kart bir kez gösterilir)
    reconfirm = conn.execute(
        "SELECT COUNT(*) AS n FROM customers WHERE deleted_at IS NULL AND COALESCE(marketing_reconfirm_pending, 0) = 1"
    ).fetchone()["n"]

    return {
        "status": "success",
        "audience": audience,
        "audience_label": audience_label(conn, audience),
        "subscription_count": len(subs),
        "customer_count": len(unique_customers),
        "pending_consent_count": pending,
        "reconfirm_pending_count": reconfirm,
        "optout_hint": CAMPAIGN_OPTOUT_HINT,
        "quiet_hours": is_quiet_hours_istanbul(),
        "rate_limited_24h": is_audience_rate_limited_24h(conn, audience),
        "push_enabled": is_push_enabled(),
    }


def has_marketing_consent(conn: sqlite3.Connection, customer_id: Optional[int]) -> bool:
    """Müşterinin ticari ileti (kampanya bildirimi) onayı var mı? Müşteriye bağlı olmayan abonelik → False."""
    if customer_id is None:
        return False
    row = conn.execute(
        "SELECT COALESCE(marketing_consent, 0) AS mc FROM customers WHERE id = ? AND deleted_at IS NULL",
        (customer_id,),
    ).fetchone()
    return bool(row and row["mc"] == 1)


def send_campaign_push(
    conn: sqlite3.Connection,
    title: str,
    body: str,
    url: str = f"{PUBLIC_BASE_PATH}/kart/",
    audience: str = "all",
    enforce_limits: bool = True,
) -> dict[str, Any]:
    """
    Kampanya bildirimini gönderir.
    - Yalnızca ticari ileti onayı (marketing_consent = 1) olan müşterilere gider (sorguda VE gönderim
      öncesinde ikinci kez kod seviyesinde kontrol edilir).
    - enforce_limits=True iken (varsayılan): 22:00-09:00 sessiz saat ve aynı kitleye 24 saatte 1 bildirim kuralı uygulanır.
      Müdür paneli enforce_limits=False ile çağırır (kısıtlamasız gönderim).
    - Bildirim kafenin kendi logosu (icon/badge) ve temasıyla gider.
    - Tüm gönderimleri notification_log'a kaydeder.
    """
    title = (title or "").strip()
    body = (body or "").strip()
    default_url = f"{PUBLIC_BASE_PATH}/kart/"
    url = sanitize_push_url(url, default_url)

    if not title or not body:
        return {"status": "error", "message": "Bildirim başlığı ve metni zorunludur."}

    if not audience_is_valid(conn, audience):
        return {"status": "error", "message": "Geçersiz hedef kitle seçimi."}
    camp_id = campaign_audience_id(audience)
    if camp_id is not None:
        row = conn.execute("SELECT active FROM campaigns WHERE id = ?", (camp_id,)).fetchone()
        if not row or not row["active"]:
            return {"status": "error", "message": "Kampanya pasif. Önce yayınlayın; sonra yapmayanlara bildirim gönderin."}
        if url == default_url:
            url = f"{PUBLIC_BASE_PATH}/kart/?tab=kampanyalar"   # dokununca doğrudan kampanyalar sekmesi açılır

    if enforce_limits and is_quiet_hours_istanbul():
        return {
            "status": "quiet_hours",
            "message": "Gece 22:00 ile sabah 09:00 (İstanbul saati) arasında toplu kampanya bildirimi gönderilemez.",
        }

    if enforce_limits and is_audience_rate_limited_24h(conn, audience):
        return {
            "status": "rate_limited",
            "message": "Bu hedef kitleye son 24 saat içinde zaten bir kampanya bildirimi gönderildi.",
        }

    branding = get_push_branding(conn)
    if not is_push_enabled():
        return {
            "status": "error",
            "message": "VAPID anahtarları tanımlı olmadığı için Web Push servisi devre dışı.",
        }

    subs = get_campaign_target_subscriptions(conn, audience)
    # Gönderici kimliği başlıkta (marka adı, brand_title) + gövdenin sonunda kısa ret ipucu (ADR-36, Y-04). Düğme yok.
    payload = {
        "title": brand_title(branding, title),
        "body": campaign_body(body),
        "url": url,
        "icon": branding["icon"],
        "badge": branding["badge"],
        "theme_color": branding["theme_color"],
        "brand": branding["business_name"],
        "tag": "campaign",
        "kind": "campaign",
        "audience": audience,
    }

    skipped_no_consent = 0
    errors: list[str] = []
    eligible: list[dict[str, Any]] = []
    for sub in subs:
        # İkinci güvence: onayı olmayana asla kampanya bildirimi gönderme
        if not has_marketing_consent(conn, sub["customer_id"]):
            skipped_no_consent += 1
            continue
        eligible.append(dict(sub))
    # Yazma kilidi bırakılır (yalnızca okuma yapıldı) ve ağ çağrıları kilitsiz yapılır: yüzlerce abonelik × yavaş push
    # sunucusu, damga/kayıt yazan diğer isteklerin "database is locked" almasına yol açmasın (E-01).
    conn.commit()
    sent_count, failed_count = send_webpush_batch_without_lock(
        getattr(conn, "db_path", None), eligible, payload, TTL_CAMPAIGN, errors=errors
    )

    log_id = log_notification(
        conn,
        kind="campaign",
        title=title,
        audience=audience,
        sent_count=sent_count,
        failed_count=failed_count,
    )
    log_audit(
        conn,
        "admin",
        "campaign_push_sent",
        f"Kampanya bildirimi '{title}' ({audience}) gönderildi: {sent_count} başarılı, {failed_count} başarısız.",
    )

    if not subs:
        message = ("Bu kitlede kampanya bildirimi alabilecek abone yok (tarayıcı izni + kampanya onayı veren müşteri bulunmuyor). "
                   "Müşteriler uygulamada \"Bildirimleri Aç\" ile tarayıcı izni verince burada sayılır.")
    else:
        message = f"Kampanya bildirimi push servisine iletildi ({sent_count} başarılı, {failed_count} başarısız)."
        if errors:
            reasons = sorted({e for e in errors})
            message += " Hata: " + "; ".join(reasons[:3]) + "."
    return {
        "status": "success",
        "log_id": log_id,
        "audience": audience,
        "sent_count": sent_count,
        "failed_count": failed_count,
        "skipped_no_consent": skipped_no_consent,
        "total_targeted": len(subs),
        "errors": sorted(set(errors))[:5],
        "message": message,
    }


def send_test_push(
    conn: sqlite3.Connection,
    title: str,
    body: str,
    url: str = f"{PUBLIC_BASE_PATH}/kart/",
    customer_id: Optional[int] = None,
    token_hash: Optional[str] = None,
    endpoint: Optional[str] = None,
) -> dict[str, Any]:
    """Yöneticinin kendi cihazına (veya son aktif aboneliğe) test bildirimi gönderir."""
    title = (title or "Test Bildirimi").strip()
    body = (body or "Bu bir test bildirimidir.").strip()
    default_url = f"{PUBLIC_BASE_PATH}/kart/"
    url = sanitize_push_url(url, default_url)

    if not is_push_enabled():
        return {"status": "error", "message": "VAPID anahtarları yapılandırılmamış."}

    cursor = conn.cursor()
    subs = []
    if endpoint:
        cursor.execute(
            "SELECT id, customer_id, endpoint, p256dh, auth, fail_count, active FROM push_subscriptions WHERE endpoint = ? AND active = 1",
            (endpoint.strip(),),
        )
        subs = cursor.fetchall()
    elif customer_id is not None:
        cursor.execute(
            "SELECT id, customer_id, endpoint, p256dh, auth, fail_count, active FROM push_subscriptions WHERE customer_id = ? AND active = 1",
            (customer_id,),
        )
        subs = cursor.fetchall()
    elif token_hash:
        cursor.execute(
            "SELECT id, customer_id, endpoint, p256dh, auth, fail_count, active FROM push_subscriptions WHERE token_hash = ? AND active = 1",
            (token_hash,),
        )
        subs = cursor.fetchall()

    # Not: Rastgele bir müşterinin cihazına test bildirimi GÖNDERİLMEZ; yalnızca istek sahibinin kendi cihazına.
    if not subs:
        return {"status": "error", "message": "Bu cihazda aktif bir bildirim aboneliği bulunamadı. Önce bu cihazı abone yapın."}

    branding = get_push_branding(conn)
    payload = {
        "title": f"[TEST] {title}",
        "body": body,
        "url": url,
        "icon": branding["icon"],
        "badge": branding["badge"],
        "tag": "test",
        "kind": "test",
    }

    errors: list[str] = []
    conn.commit()  # yazma kilidi ağ çağrısı boyunca tutulmaz (E-01)
    sent, failed = send_webpush_batch_without_lock(
        getattr(conn, "db_path", None), [dict(s) for s in subs], payload, TTL_TEST, errors=errors
    )

    log_notification(conn, kind="test", title=title, audience="test_device", sent_count=sent, failed_count=failed)
    return {
        "status": "success" if sent > 0 else "error",
        "sent_count": sent,
        "failed_count": failed,
        "message": "Test bildirimi gönderildi." if sent > 0
        else "Test bildirimi gönderilemedi" + (f" ({'; '.join(sorted(set(errors)))})." if errors else "."),
    }


def subscription_status(
    conn: sqlite3.Connection, endpoint: Optional[str], customer_id: Optional[int]
) -> dict[str, Any]:
    """
    Bu cihazın (endpoint) sunucudaki gerçek durumu: istemcideki anahtar bununla çizilir.
    `active`: sunucuda aktif abonelik var mı (cihaz tarayıcıda abone görünse bile sunucu pasife almış olabilir),
    `linked`: abonelik bu müşteriye bağlı mı, `marketing_consent`: müşterinin ticari ileti onayı.
    """
    row = None
    if endpoint and endpoint.strip():
        row = conn.execute(
            "SELECT customer_id, active FROM push_subscriptions WHERE endpoint = ?", (endpoint.strip(),)
        ).fetchone()
    consent = has_marketing_consent(conn, customer_id)
    return {
        "status": "success",
        "active": bool(row and row["active"]),
        "linked": bool(row and customer_id is not None and row["customer_id"] == customer_id),
        "marketing_consent": consent,
        "push_enabled": is_push_enabled(),
    }


def get_notification_logs(conn: sqlite3.Connection, limit: int = 25) -> list[dict[str, Any]]:
    """Son bildirim gönderim kayıtlarını listeler."""
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, kind, title, audience, sent_count, failed_count, created_at
        FROM notification_log
        ORDER BY id DESC LIMIT ?
        """,
        (limit,),
    )
    return [dict(r) for r in cursor.fetchall()]


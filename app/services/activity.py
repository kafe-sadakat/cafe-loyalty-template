"""
Müşteri ve kasa işlem kayıtları — `customer_events` (ADR-31).

Ne tutulur: her damga isteğinin sonucu (verildi / verilmedi ve NEDENİ), kayıt, kasiyer ve müdür işlemleri, hediye
teslimi, hediye / kampanya damgaları ve şüphe incelemesi. Her kayıt müdürün tek bakışta anlayacağı sade bir Türkçe
cümle taşır (`title` kısa başlık, `detail` ne oldu). Müşterinin kendi adı ve telefonu metne yazılmaz; okunurken
`customer_id` ile birleştirilir (anonimleştirilen müşteri listede de "silindi_…" görünür).

Olası kötüye kullanım: `flagged=1` kayıtlarda `flag_note` neyin NEDEN şüpheli olduğunu açıkça yazar. Sistem kimseyi
engellemez (ADR-14); not eder ve yeni sinyallerde şüphe puanına (fraud_events) hafif puan ekler. Tespit edilenler:
- kullanılmış tek kullanımlık bağlantının başka telefonda ya da sonradan yeniden açılması, süresi dolmuş ya da uydurma
  bağlantı, eski / imzası tutmayan NFC adresi (puanları loyalty._rejected_stamp_code yazar),
- bekleme süresi / günlük limit içinde bağlantıyı yeniden kullanarak (yenileme, geri tuşu, açık sekmeye yapıştırma) ya
  da üst üste 3+ kez damga denemek (`link_retry`),
- bağlantının Instagram, Facebook, TikTok gibi bir uygulamanın içinden açılması (`link_social`): kasadaki QR'ı
  kamerayla okutmak bu uygulamaları açmaz,
- bekleme içinde deneyip süre dolar dolmaz sabit bağlantıdan damga almak (`cooldown_waiter`),
- aynı telefonda farklı numarayla hesap değiştirmek (`device_switch`),
- kasa QR'ının / damga bağlantısının WhatsApp, Telegram gibi uygulamalarda paylaşılması (bağlantı önizleyici isteği).

Açılış şekli (`opened_via`): müşteri uygulaması damgayı isterken sayfanın nasıl açıldığını bildirir (Navigation Timing
türü, sekme geçmişi uzunluğu, yüklü uygulama mı); User-Agent uygulama içi tarayıcıyı ele verir. Bunlar kanıt değil
ipucudur; notlarda "olabilir" diye yazılır. Kayıt yazılamazsa damga akışı ASLA bozulmaz (safe_record).
"""
from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Optional

from app.db import get_setting, set_setting
from app.utils import get_istanbul_now, hash_token, mask_phone, parse_to_istanbul

logger = logging.getLogger("sadakat")

OK, DENIED, INFO = "ok", "denied", "info"

CHANNEL_LABELS = {
    "scan": "Kasa QR / NFC (sabit)",
    "app": "Uygulama içi QR okutucu",
    "screen": "Kasa ekranı canlı kodu",
    "display": "Kasa QR Ekranı",
    "nfc": "NFC 424 DNA etiketi",
    "kasa": "Kasa (kasiyer)",
    "panel": "Müdür paneli",
    "uygulama": "Müşteri uygulaması",
    "kampanya": "Ödüllü kampanya",
    "link": "Paylaşılan / dışarıdan gelen bağlantı",
}
CLAIM_CHANNELS = ("scan", "app", "screen", "display", "nfc")
STATIC_CHANNELS = ("scan", "app")   # sabit adres: bekleme dolunca evden yeniden açılabilir (ADR-29)

_CHANNEL_ACTION = {
    "scan": "Kasadaki QR / NFC'yi okuttu",
    "app": "Uygulama içinden kasadaki QR'ı okuttu",
    "screen": "Kasa ekranındaki canlı damga QR'ını okuttu",
    "display": "Kasa QR Ekranındaki kodu okuttu",
    "nfc": "Kasadaki NFC etiketine dokundu",
}
_CHANNEL_PASSIVE = {
    "scan": "kasadaki QR / NFC okutuldu",
    "app": "kasadaki QR uygulama içinden okutuldu",
    "screen": "kasa ekranındaki damga QR'ı okutuldu",
    "display": "Kasa QR Ekranındaki kod okutuldu",
    "nfc": "kasadaki NFC etiketine dokunuldu",
}
# (yalın, belirtme hâli): Türkçe ekler doğru düşsün diye iki biçim ayrı yazılır
_CODE_NOUN = {
    "scan": ("tek kullanımlık QR açılışı", "tek kullanımlık QR açılışını"),
    "app": ("tek kullanımlık QR açılışı", "tek kullanımlık QR açılışını"),
    "screen": ("kasa ekranındaki tek kullanımlık damga bağlantısı", "kasa ekranındaki tek kullanımlık damga bağlantısını"),
    "display": ("Kasa QR Ekranının tek kullanımlık kodu", "Kasa QR Ekranının tek kullanımlık kodunu"),
    "nfc": ("NFC dokunuşu", "NFC dokunuşunu"),
}
_DEFAULT_NOUN = ("tek kullanımlık damga bağlantısı", "tek kullanımlık damga bağlantısını")
_REGISTER_DETAIL = {
    "scan": "Kasadaki QR / NFC'yi okutup telefon numarasıyla kayıt oldu.",
    "app": "Uygulama içinden kasadaki QR'ı okutup kayıt oldu.",
    "screen": "Kasa ekranındaki damga QR'ını okutup telefon numarasıyla kayıt oldu.",
    "display": "Kasa QR Ekranındaki kodu okutup telefon numarasıyla kayıt oldu.",
    "nfc": "Kasadaki NFC etiketine dokunup telefon numarasıyla kayıt oldu.",
    "uygulama": "Uygulamada telefon numarasıyla kayıt oldu.",
    "kasa": "Kasada kasiyer tarafından kaydedildi.",
    "panel": "Müdür panelinden kaydedildi.",
}

STAMP_OK_KINDS = ("stamp", "welcome_stamp", "welcome_gift", "staff_stamp", "admin_stamp", "bonus_stamp", "campaign_stamp")
_QR_STAMP_KINDS = ("stamp", "welcome_stamp")
_KASA_STAMP_KINDS = ("staff_stamp", "admin_stamp")
DENIED_REASON_LABELS = {
    "cooldown": "Bekleme süresi dolmadı",
    "daily_limit": "Günlük limit doldu",
    "staff_denied": "Kasiyer: günlük limit doldu",
    "link_reused": "Kullanılmış bağlantı yeniden açıldı",
    "link_shared": "Başkasının / paylaşılan bağlantı",
    "link_expired": "Süresi dolmuş bağlantı",
    "link_invalid": "Geçersiz / uydurma bağlantı",
    "nfc_invalid": "NFC imzası tutmadı",
    "nfc_stale": "Eski / kopya NFC dokunuşu",
}
GROUPS = {
    "all": "",
    "ok": "e.outcome = 'ok'",
    "denied": "e.outcome = 'denied'",
    "flagged": "e.flagged = 1",
    "kasa": "e.channel IN ('kasa', 'panel')",
    "qr": "e.channel IN ('scan', 'app', 'screen', 'display', 'nfc', 'link')",
}

_COOLDOWN_EXEMPT = ("bonus_pwa", "bonus_notif", "campaign", "welcome")   # loyalty._COOLDOWN_EXEMPT_SOURCES ile aynı
SELF_REPLAY_GRACE = timedelta(minutes=5)                       # loyalty.SELF_REPLAY_GRACE ile aynı
WAITER_WINDOW = timedelta(minutes=10)
RETRY_FLAG_AT = 3
PREVIEW_DEDUP = timedelta(minutes=10)

_VIA_LABELS = {
    "new_tab": "Yeni sekmede açıldı (QR / NFC okutma)",
    "existing_tab": "Açık bir sekmede açıldı (adres yapıştırılmış olabilir)",
    "reload": "Sayfa yenilenerek açıldı",
    "back_forward": "Geri / ileri tuşuyla açıldı",
    "scanner": "Uygulama içi QR okutucu",
    "installed": "Yüklü uygulamada açıldı",
}
_RETRY_HOW = {
    "reload": "QR sayfası yenilenerek",
    "back_forward": "tarayıcının geri / ileri tuşuyla eski QR sayfasına dönülerek",
    "existing_tab": ("bağlantı yeni bir sekmede değil, daha önce açık olan bir sekmede açılarak (adres kopyalanıp "
                     "yapıştırılmış olabilir)"),
}

# Uygulama içi tarayıcılar (müşterinin damga isteğinde). Messenger, Facebook'tan önce denetlenir.
_SOCIAL_APPS = (
    ("Messenger", re.compile(r"MessengerForiOS|MessengerLite|\bMESSENGER\b|Orca-Android", re.I)),
    ("Instagram", re.compile(r"\bInstagram\b", re.I)),
    ("Facebook", re.compile(r"FBAN/|FBAV/|FB_IAB|FBIOS|\[FB", re.I)),
    ("TikTok", re.compile(r"musical_ly|BytedanceWebview|TikTok|trill_", re.I)),
    ("Snapchat", re.compile(r"Snapchat", re.I)),
    ("Telegram", re.compile(r"Telegram", re.I)),
    ("WhatsApp", re.compile(r"WhatsApp", re.I)),
    ("LINE", re.compile(r"\bLine/\d", re.I)),
    ("X (Twitter)", re.compile(r"Twitter", re.I)),
    ("LinkedIn", re.compile(r"LinkedInApp", re.I)),
    ("Pinterest", re.compile(r"\[Pinterest|Pinterest/", re.I)),
)
# Bağlantı önizleyicileri (sayfa GET'i). Üçüncü alan True: yalnızca tarayıcı olmayan (Mozilla/ içermeyen) istemcide
# (ör. WhatsApp'ın önizleyicisi "WhatsApp/2.x"; uygulama içi tarayıcısı Mozilla/ ile başlar).
_PREVIEW_BOTS = (
    ("iMessage", re.compile(r"facebookexternalhit/1\.1 Facebot Twitterbot", re.I), False),
    ("WhatsApp / Signal", re.compile(r"WhatsApp/", re.I), True),
    ("Telegram", re.compile(r"TelegramBot", re.I), False),
    ("Facebook / Instagram / Messenger", re.compile(r"facebookexternalhit|Facebot|meta-externalagent", re.I), False),
    ("X (Twitter)", re.compile(r"Twitterbot", re.I), False),
    ("Slack", re.compile(r"Slackbot|Slack-ImgProxy", re.I), False),
    ("Discord", re.compile(r"Discordbot", re.I), False),
    ("Skype / Teams", re.compile(r"SkypeUriPreview|MicrosoftPreview", re.I), False),
    ("LinkedIn", re.compile(r"LinkedInBot", re.I), False),
    ("Viber", re.compile(r"Viber", re.I), True),
    ("Google", re.compile(r"Googlebot|Google-InspectionTool|GoogleOther|Google-PageRenderer", re.I), False),
    ("bağlantı önizleyici", re.compile(r"\b(bot|crawler|spider)\b", re.I), False),
    ("bağlantı önizleyici", re.compile(r"python-requests|curl/|Wget/|okhttp", re.I), True),
)


# ------------------------------------------------------------------ yardımcılar
def social_app(user_agent: str) -> str:
    """Uygulama içi tarayıcının adı (Instagram, Facebook…) ya da boş."""
    ua = user_agent or ""
    for name, rx in _SOCIAL_APPS:
        if rx.search(ua):
            return name
    return ""


def preview_bot(user_agent: str) -> str:
    """Bağlantı önizleyicisinin (WhatsApp, Telegram…) adı ya da boş. Gerçek tarayıcılar boş döner."""
    ua = user_agent or ""
    if not ua:
        return ""
    browser = "Mozilla/" in ua
    for name, rx, only_non_browser in _PREVIEW_BOTS:
        if rx.search(ua) and not (only_non_browser and browser):
            return name
    return ""


def opened_via(client: Optional[dict[str, Any]]) -> str:
    """Müşteri uygulamasının bildirdiği açılış şekli: new_tab | existing_tab | reload | back_forward | scanner |
    installed | social:<Uygulama> | '' (bilinmiyor)."""
    if not isinstance(client, dict):
        return ""
    app = social_app(str(client.get("ua") or ""))
    if app:
        return f"social:{app}"
    if str(client.get("via") or "") == "scanner":
        return "scanner"
    nav = str(client.get("nav") or "")
    if nav in ("reload", "back_forward"):
        return nav
    if client.get("standalone") is True:
        return "installed"   # yüklü uygulamada sekme geçmişi ölçütü uygulanmaz
    try:
        hist = int(client.get("hist") or 0)
    except (TypeError, ValueError):
        hist = 0
    if nav == "navigate" and hist >= 2:
        return "existing_tab"
    if nav in ("navigate", "prerender") and hist == 1:
        return "new_tab"
    return ""


def via_label(via: str) -> str:
    if via.startswith("social:"):
        return f"{via[7:]} uygulamasının içinde açıldı"
    return _VIA_LABELS.get(via, "")


def _social(via: str) -> str:
    return via[7:] if via.startswith("social:") else ""


def _hm(dt: Optional[datetime], now: Optional[datetime] = None) -> str:
    if dt is None:
        return "—"
    if now is not None and now - dt > timedelta(hours=20):
        return dt.strftime("%d.%m %H:%M")
    return dt.strftime("%H:%M")


def _ago(delta: Optional[timedelta]) -> str:
    if delta is None:
        return "bir süre"
    minutes = int(delta.total_seconds() // 60)
    if minutes < 1:
        return "1 dakikadan kısa süre"
    if minutes < 60:
        return f"{minutes} dk"
    hours, mins = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} sa {mins} dk" if mins else f"{hours} sa"
    return f"{hours // 24} gün"


def _when(dt: Optional[datetime], now: datetime) -> str:
    """'3 dk önce (14:05)'. Saat ekleri (14:05'te / 15:00'te) yanlış düşmesin diye ek kullanılmaz."""
    if dt is None:
        return "daha önce"
    return f"{_ago(now - dt)} önce ({_hm(dt, now)})"


def _int_setting(conn: sqlite3.Connection, key: str, default: int) -> int:
    try:
        return int(get_setting(conn, key, str(default)) or default)
    except (TypeError, ValueError):
        return default


def _progress(conn: sqlite3.Connection, stamps: Optional[int], unlocked: bool) -> str:
    target = _int_setting(conn, "stamp_target", 5)
    if unlocked:
        return f"Kart doldu: hediye kahve kazandı 🎁 (yeni kart {stamps or 0}/{target})."
    if stamps is None:
        return ""
    return f"Kartı: {stamps}/{target}."


def actor_parts(actor: str) -> tuple[str, str]:
    """'staff:Ayşe' → ('kasa', 'Kasiyer Ayşe'); 'mudur:Ali' / 'admin' → ('panel', 'Müdür …')."""
    actor = (actor or "").strip()
    if actor.startswith("staff:"):
        return "kasa", f"Kasiyer {actor[6:] or ''}".strip()
    if actor.startswith("mudur:"):
        return "panel", f"Müdür {actor[6:] or ''}".strip()
    return "panel", "Müdür"


# ------------------------------------------------------------------ yazma
def record(
    conn: sqlite3.Connection,
    kind: str,
    title: str,
    *,
    customer_id: Optional[int] = None,
    outcome: str = INFO,
    channel: str = "",
    detail: str = "",
    flagged: bool = False,
    flag_note: str = "",
    opened_via: str = "",
    actor: str = "",
    stamps_after: Optional[int] = None,
    code_id: Optional[int] = None,
    ip: str = "",
    device_token: Optional[str] = None,
    related_customer_id: Optional[int] = None,
    at: Optional[str] = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO customer_events (at, customer_id, related_customer_id, kind, outcome, channel, title, detail,
            flagged, flag_note, opened_via, actor, stamps_after, code_id, ip, device_hash)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            at or get_istanbul_now().isoformat(), customer_id, related_customer_id, kind[:40], outcome, channel[:20],
            (title or "")[:160], (detail or "")[:700], 1 if flagged else 0, (flag_note or "")[:900],
            (opened_via or "")[:40], (actor or "")[:60], stamps_after, code_id, (ip or "")[:64],
            hash_token(device_token) if device_token else "",
        ),
    )
    return int(cur.lastrowid or 0)


def safe_record(conn: sqlite3.Connection, kind: str, title: str, **kw: Any) -> int:
    """record() — hata olursa yalnızca uyarı yazar; damga / kayıt / hediye akışını asla bozmaz."""
    try:
        return record(conn, kind, title, **kw)
    except Exception as exc:  # pragma: no cover - savunma
        logger.warning("İşlem kaydı yazılamadı (%s): %s", kind, exc)
        return 0


def _add_fraud_points(conn: sqlite3.Connection, reasons: list[str], customer_id: Optional[int],
                      device_token: Optional[str], ip: str) -> None:
    if not customer_id or not reasons:
        return
    from app.services import fraud  # döngüsel içe aktarmayı önler (fraud → activity)
    for reason in reasons:
        try:
            fraud.record_event(conn, reason, customer_id=customer_id, device_token=device_token, ip=ip or "")
        except Exception as exc:  # pragma: no cover - savunma
            logger.warning("Şüphe puanı yazılamadı (%s): %s", reason, exc)


def _record_flagged(conn: sqlite3.Connection, kind: str, title: str, detail: str, outcome: str,
                    flags: list[tuple[str, str]], base: dict[str, Any]) -> None:
    safe_record(conn, kind, title, outcome=outcome, detail=detail, flagged=bool(flags),
                flag_note=" ".join(note for _, note in flags), **base)
    _add_fraud_points(conn, [reason for reason, _ in flags], base.get("customer_id"), base.get("device_token"),
                      base.get("ip") or "")


# ------------------------------------------------------------------ damga isteği (loyalty.process_claim_submission)
def row_channel(row: Optional[sqlite3.Row]) -> str:
    if row is None:
        return "scan"
    origin = row["origin"] or "screen"
    return origin if origin in ("scan", "screen", "display", "nfc") else "screen"


def _today_stamps(conn: sqlite3.Connection, customer_id: Optional[int], now: datetime) -> int:
    if not customer_id:
        return 0
    return conn.execute(
        "SELECT COUNT(*) FROM stamps WHERE customer_id = ? AND voided_at IS NULL AND substr(created_at, 1, 10) = ?",
        (customer_id, now.strftime("%Y-%m-%d")),
    ).fetchone()[0]


def _last_cooldown_stamp(conn: sqlite3.Connection, customer_id: Optional[int], skip: int = 0) -> Optional[datetime]:
    if not customer_id:
        return None
    marks = ",".join("?" for _ in _COOLDOWN_EXEMPT)
    row = conn.execute(
        f"SELECT created_at FROM stamps WHERE customer_id = ? AND voided_at IS NULL "
        f"AND COALESCE(source, '') NOT IN ({marks}) ORDER BY id DESC LIMIT 1 OFFSET ?",
        (customer_id, *_COOLDOWN_EXEMPT, skip),
    ).fetchone()
    return parse_to_istanbul(row["created_at"]) if row else None


def _denied_since_last_stamp(conn: sqlite3.Connection, customer_id: int) -> int:
    last = conn.execute(
        "SELECT MAX(created_at) AS m FROM stamps WHERE customer_id = ? AND voided_at IS NULL", (customer_id,)
    ).fetchone()["m"] or ""
    return conn.execute(
        "SELECT COUNT(*) FROM customer_events WHERE customer_id = ? AND kind IN ('cooldown', 'daily_limit') AND at > ?",
        (customer_id, last),
    ).fetchone()[0]


def _retry_flags(conn: sqlite3.Connection, customer_id: Optional[int], via: str) -> list[tuple[str, str]]:
    flags: list[tuple[str, str]] = []
    app = _social(via)
    if app:
        flags.append(("link_social", f"Bağlantı {app} uygulamasının içinden açıldı. Kasadaki QR'ı kamerayla okutmak bu "
                                     "uygulamayı açmaz; paylaşılan bir bağlantıyla damga denenmiş olabilir."))
    elif via in _RETRY_HOW:
        flags.append(("link_retry", f"Damga {_RETRY_HOW[via]} yeniden istendi; kasadaki QR yeniden okutulmadan "
                                    "bağlantı tekrar kullanılmış olabilir."))
    attempts = (_denied_since_last_stamp(conn, customer_id) + 1) if customer_id else 1
    if attempts >= RETRY_FLAG_AT:
        sentence = (f"Son damgasından bu yana {attempts}. kez damga denedi; bekleme süresi ya da günlük limit dolmadan "
                    "art arda deniyor.")
        if flags and flags[-1][0] == "link_retry":
            flags[-1] = ("link_retry", flags[-1][1] + " " + sentence)
        else:
            flags.append(("link_retry", sentence))
    return flags


def _cooldown_waiter(conn: sqlite3.Connection, customer_id: int, now: datetime) -> Optional[str]:
    """Bekleme içinde deneyip süre dolar dolmaz sabit bağlantıdan damga alan müşteri için not (yoksa None)."""
    minutes = _int_setting(conn, "min_interval_minutes", 0)
    if minutes <= 0:
        return None
    marks = ",".join("?" for _ in _COOLDOWN_EXEMPT)
    rows = conn.execute(
        f"SELECT created_at FROM stamps WHERE customer_id = ? AND voided_at IS NULL "
        f"AND COALESCE(source, '') NOT IN ({marks}) ORDER BY id DESC LIMIT 2",
        (customer_id, *_COOLDOWN_EXEMPT),
    ).fetchall()
    if len(rows) < 2:
        return None
    prev = parse_to_istanbul(rows[1]["created_at"])
    if prev is None:
        return None
    opened = prev + timedelta(minutes=minutes)
    if not (timedelta(0) <= now - opened <= WAITER_WINDOW):
        return None
    tries = conn.execute(
        "SELECT COUNT(*) FROM customer_events WHERE customer_id = ? AND kind = 'cooldown' AND at > ?",
        (customer_id, rows[1]["created_at"]),
    ).fetchone()[0]
    if tries < 1:
        return None
    return (f"Bekleme süresi içinde {tries} kez denedikten sonra süre dolar dolmaz damga aldı (bekleme saat "
            f"{_hm(opened)} itibarıyla bitti, damga {_ago(now - opened)} sonra alındı). Sabit QR bağlantısını kaydedip "
            "bekleme bitince kasaya gelmeden yeniden açıyor olabilir.")


def record_claim(
    conn: sqlite3.Connection, result: dict[str, Any], trace: dict[str, Any], *,
    device_token: Optional[str] = None, client: Optional[dict[str, Any]] = None, ip: str = "",
) -> None:
    """Damga isteğinin sonucunu işlem kaydına yazar (loyalty.process_claim_submission). Hata akışı bozmaz."""
    try:
        _record_claim(conn, result, trace, device_token, client, ip)
    except Exception as exc:  # pragma: no cover - savunma
        logger.warning("İşlem kaydı yazılamadı (damga akışı etkilenmedi): %s", exc)


def _record_claim(conn, result, trace, device_token, client, ip) -> None:  # noqa: C901 - durum başına tek dal
    status = str(result.get("status") or "")
    if status in ("", "consent_required", "registered") or (status == "error" and not trace.get("seal_inactive")):
        return   # telefon / onay hatası ara adımdır; kasa QR'ıyla kayıt get_or_create'te kaydedilir
    if status.startswith("otp_"):
        return   # yeni cihaz SMS doğrulaması bekleniyor (docs/OTP.md): damga isteği henüz işlenmedi
    now = get_istanbul_now()
    row = trace.get("row")
    # Kod tabloda yoksa (uydurma / değiştirilmiş bağlantı) kaynağı bilinmez: "dışarıdan gelen bağlantı"
    channel = trace.get("channel") or (row_channel(row) if row is not None else "link")
    via = opened_via(client)
    customer_id = result.get("customer_id") or trace.get("customer_id")
    base: dict[str, Any] = {
        "customer_id": customer_id,
        "channel": channel,
        "opened_via": via,
        "ip": ip,
        "device_token": result.get("device_token") or result.get("new_device_token") or device_token,
        "code_id": row["id"] if row is not None else None,
        "stamps_after": result.get("current_stamps") if customer_id else None,
        "actor": (row["created_by"] or "") if (row is not None and channel == "screen") else "",
    }
    action = _CHANNEL_ACTION.get(channel, "Damga istedi")
    nom, acc = _CODE_NOUN.get(channel, _DEFAULT_NOUN)

    if status == "success":
        if result.get("welcome_locked"):
            safe_record(conn, "welcome_locked", "Kayıt oldu — hoş geldin damgası verilmedi", outcome=INFO,
                        detail=("Bu numara ya da telefon daha önce hesabını silmiş. Hoş geldin damgası ömür boyu bir "
                                "kez verildiği için bu kayıtta damga eklenmedi; sonraki ziyaretlerinde normal damga "
                                "alabilir."), **base)
            return
        fresh = bool(trace.get("fresh_registration"))
        progress = _progress(conn, base["stamps_after"], bool(result.get("reward_unlocked")))
        if fresh:
            # ADR-35: kayıt hediyesi ayrı olay (welcome_gift); bu okutuşun damgası normal damgadır
            kind, title, detail = "stamp", "Damga verildi", f"{action}, kayıt oldu; kayıt hediyesinin yanında bu ziyaretin damgasını da aldı. {progress}"
        else:
            kind, title, detail = "stamp", "Damga verildi", f"{action}; damga eklendi. {progress}"
        flags: list[tuple[str, str]] = []
        app = _social(via)
        if app:
            flags.append(("link_social", f"Damga bağlantısı {app} uygulamasının içinden açıldı. Kasadaki QR'ı telefon "
                                         "kamerasıyla okutmak ya da NFC'ye dokunmak bu uygulamayı açmaz; damga, bir "
                                         "mesajda ya da gönderide paylaşılan bağlantıyla kasaya gelmeden alınmış "
                                         "olabilir."))
        if channel in STATIC_CHANNELS and not fresh and customer_id:
            waiter = _cooldown_waiter(conn, customer_id, now)
            if waiter:
                flags.append(("cooldown_waiter", waiter))
        _record_flagged(conn, kind, title, detail.strip(), OK, flags, base)

    elif status == "limit_reached":
        limit = _int_setting(conn, "daily_limit", 2)
        today = _today_stamps(conn, customer_id, now)
        detail = (f"{action} ama bugün zaten {today} damga almış; günlük üst sınır {limit}. Damga eklenmedi, yarın "
                  "yeniden alabilir.")
        _record_flagged(conn, "daily_limit", "Damga verilmedi — günlük limit doldu", detail, DENIED,
                        _retry_flags(conn, customer_id, via), base)

    elif status == "interval_limit":
        minutes = _int_setting(conn, "min_interval_minutes", 0)
        last = _last_cooldown_stamp(conn, customer_id)
        nxt = parse_to_istanbul(result.get("next_stamp_at"))
        detail = (f"{action} ama son damgasını {_when(last, now)} almıştı. Aynı ziyarette ikinci QR damgası verilmez "
                  f"(bekleme {minutes} dk); yeni QR damgası en erken saat {_hm(nxt)} itibarıyla alınabilir.")
        _record_flagged(conn, "cooldown", "Damga verilmedi — bekleme süresi dolmadı", detail, DENIED,
                        _retry_flags(conn, customer_id, via), base)

    elif status == "code_used":
        if row is None or trace.get("race") or not row["used_at"]:
            safe_record(conn, "link_refresh", "Aynı kod aynı anda iki kez gönderildi", outcome=INFO,
                        detail=f"Aynı {nom} aynı anda iki kez gönderildi; yalnızca biri işlendi.", **base)
            return
        used_at = parse_to_istanbul(row["used_at"])
        owner = row["used_by_customer_id"]
        if customer_id and owner == customer_id:
            elapsed = (now - used_at) if used_at else None
            if elapsed is None or elapsed <= SELF_REPLAY_GRACE:
                safe_record(conn, "link_refresh", "Aynı bağlantı yeniden açıldı — damga yok", outcome=INFO,
                            detail=(f"Bu {nom} {_when(used_at, now)} bu müşterinin işleminde kullanılmıştı; sayfa "
                                    "yenilendi, geri dönüldü ya da aynı kod iki kez okutuldu. İkinci damga verilmedi."),
                            **base)
                return
            safe_record(conn, "link_reused", "Damga verilmedi — kullanılmış bağlantı yeniden açıldı", outcome=DENIED,
                        detail=(f"Müşteri, {_when(used_at, now)} kendi işleminde kullanılan {acc} yeniden açtı. "
                                "Bağlantı tek kullanımlık olduğu için damga verilmedi."),
                        flagged=True,
                        flag_note=(f"Kullandığı damga bağlantısını {_ago(elapsed)} sonra yeniden kullanmaya çalıştı: "
                                   "bağlantıyı kaydedip ya da kopyalayıp yeniden açarak ikinci damga almak istemiş "
                                   "olabilir."),
                        **base)
            return
        where = "bu müşterinin telefonunda" if customer_id else "kayıtlı olmayan bir telefonda"
        safe_record(conn, "link_shared", "Damga verilmedi — başkasının bağlantısı", outcome=DENIED,
                    detail=(f"Bu {nom} {_when(used_at, now)} başka bir müşterinin işleminde kullanılmıştı; şimdi "
                            f"{where} açılmak istendi. Damga verilmedi."),
                    flagged=True,
                    flag_note=("Tek kullanımlık damga bağlantısı başka bir telefona geçmiş: bağlantı kopyalanıp "
                               "paylaşılmış ya da ekranın fotoğrafı başkasına gönderilmiş olabilir. Bağlantı yalnızca "
                               "kasada okutan kişiye damga verir."),
                    related_customer_id=owner, **base)

    elif status == "code_held":
        where = "bu müşterinin telefonunda" if customer_id else "başka bir telefonda"
        safe_record(conn, "link_shared", "Damga verilmedi — bağlantı başka telefonda açık", outcome=DENIED,
                    detail=(f"Bu {nom}, kayıt olmak üzere olan başka bir telefona ayrılmışken {where} da açılmak "
                            "istendi. Damga verilmedi."),
                    flagged=True,
                    flag_note=("Aynı damga bağlantısı aynı anda iki farklı telefonda açıldı: bağlantı paylaşılmış ya "
                               "da kopyalanmış olabilir."),
                    **base)

    elif status == "code_expired" and trace.get("nfc_stale"):
        safe_record(conn, "nfc_stale", "Damga verilmedi — eski NFC dokunuşu", outcome=DENIED,
                    detail=("Daha önce okunmuş eski bir NFC dokunuş adresi açıldı (etiketin sayacı bu adresten ileri "
                            "gitmişti); damga verilmedi."),
                    flagged=True,
                    flag_note=("Eski NFC adresi kaydedilip ya da kopyalanıp sonradan açılmış olabilir; her dokunuşun "
                               "adresi yalnızca bir kez geçerlidir."),
                    **base)

    elif status == "code_expired":
        created = parse_to_istanbul(row["created_at"]) if row is not None else None
        ends = parse_to_istanbul((row["held_until"] or row["expires_at"])) if row is not None else None
        valid_for = _ago(ends - created) if (created and ends and ends > created) else "kısa bir süre"
        safe_record(conn, "link_expired", "Damga verilmedi — süresi dolmuş bağlantı", outcome=DENIED,
                    detail=(f"Bu {nom} {_when(created, now)} oluşturulmuştu ve {valid_for} geçerliydi; açıldığında "
                            "süresi dolmuştu. Damga verilmedi."),
                    flagged=True,
                    flag_note=("Süresi dolmuş damga bağlantısı açıldı: bağlantı kasada açıldıktan sonra saklanıp "
                               "sonradan (ör. kasadan ayrıldıktan sonra) yeniden açılmış olabilir. Zayıf internet ya "
                               "da kayıt formunda uzun bekleme de buna yol açabilir."),
                    **base)

    elif status == "code_invalid":
        safe_record(conn, "link_invalid", "Damga verilmedi — geçersiz bağlantı", outcome=DENIED,
                    detail=("Sistemde olmayan bir damga kodu gönderildi (bağlantı elle yazılmış, değiştirilmiş ya da "
                            "uydurulmuş)."),
                    flagged=True,
                    flag_note=("Damga bağlantısı uydurulmuş ya da değiştirilmiş olabilir. Gerçek damga bağlantıları "
                               "yalnızca kasadaki QR, ekran ya da NFC etiketinden gelir."),
                    **base)

    elif status == "nfc_invalid":
        safe_record(conn, "nfc_invalid", "Damga verilmedi — NFC imzası tutmadı", outcome=DENIED,
                    detail="NFC dokunuş adresinin imzası doğrulanamadı; damga verilmedi.",
                    flagged=True,
                    flag_note=("NFC adresi elle değiştirilmiş ya da uydurulmuş olabilir. Etiket yeni programlandıysa "
                               "Müdür → QR Kodlar → Damga Güvenliği'ndeki anahtarları kontrol edin."),
                    **base)

    elif status == "nfc_unavailable":
        safe_record(conn, "nfc_unavailable", "NFC etiketi kullanılamadı", outcome=INFO,
                    detail=str(result.get("message") or "NFC etiketi devre dışı ya da henüz kurulmadı."), **base)

    elif status == "need_auth":
        passive = _CHANNEL_PASSIVE.get(channel, "damga bağlantısı açıldı")
        tail = ("kayıt ekranı açıldı (Kayıt QR'ı damga vermez)." if trace.get("kayit_qr")
                else "kayıt ekranı açıldı (basılı QR şu an damga vermez)." if trace.get("register_only")
                else "numara ve aydınlatma onayı ekranı açıldı. Numara girilince kayıt ve damga işlenir.")
        safe_record(conn, "qr_opened", "QR okutuldu — kayıt bekleniyor", outcome=INFO,
                    detail=f"Kayıtlı olmayan bir telefondan {passive}; {tail}", **base)

    elif status == "registration_qr":
        detail = ("Kayıt / Uygulama QR'ını okuttu; bu QR sistemi açar, damga vermez. Damga kasadaki Damga QR'ından "
                  "(ya da NFC, kasiyer) alınır." if trace.get("kayit_qr") else
                  "Kayıt QR'ını okuttu. Basılı QR şu an yalnızca kayıt ve uygulama için (Müdür → QR "
                  "Kodlar → Damga Güvenliği); damga NFC 424 DNA etiketi, QR ekranı, canlı kod ya da "
                  "kasiyerle verilir.")
        safe_record(conn, "register_qr", "Kayıt QR'ı okutuldu — damga vermez", outcome=INFO, detail=detail, **base)

    elif status == "error":   # seal_inactive
        safe_record(conn, "seal_inactive", "Devre dışı QR okutuldu", outcome=INFO,
                    detail="Devre dışı bırakılmış eski bir QR / NFC okutuldu; damga açılmadı.", **base)


# ------------------------------------------------------------------ kayıt, cihaz, bağlantı paylaşımı
def record_registration(conn: sqlite3.Connection, customer_id: int, channel: str, *, returning: bool = False,
                        ip: str = "", device_token: Optional[str] = None, actor: str = "") -> None:
    detail = _REGISTER_DETAIL.get(channel, "Telefon numarasıyla kayıt oldu.")
    if actor and channel in ("kasa", "panel"):
        detail = detail.rstrip(".") + f" (işlemi yapan: {actor})."
    if returning:
        detail += " Bu numara daha önce hesabını silmişti; hoş geldin, uygulama ve bildirim hediyeleri bir daha verilmez."
    safe_record(conn, "registered", "Yeniden kayıt oldu" if returning else "Kayıt oldu", customer_id=customer_id,
                outcome=INFO, channel=channel, detail=detail, ip=ip, device_token=device_token, actor=actor)


def record_device_switch(conn: sqlite3.Connection, customer_id: int, previous_customer_id: int, *, ip: str = "",
                         device_token: Optional[str] = None) -> None:
    safe_record(conn, "device_switch", "Aynı telefonda farklı hesap", customer_id=customer_id, outcome=INFO,
                channel="uygulama",
                detail=("Bu telefon daha önce başka bir müşteri hesabıyla kullanılıyordu; şimdi farklı bir numarayla bu "
                        "hesaba giriş yapıldı."),
                flagged=True,
                flag_note=("Aynı telefonda birden fazla numarayla hesap açılıp damga toplanıyor olabilir (bir kişinin "
                           "iki hesabı). Telefonu birlikte kullanan aile üyeleri de bu duruma yol açabilir."),
                related_customer_id=previous_customer_id, ip=ip, device_token=device_token)
    _add_fraud_points(conn, ["device_switch"], customer_id, device_token, ip)


def record_link_preview(conn: sqlite3.Connection, bot: str, path_kind: str, *, ip: str = "",
                        code_row: Optional[sqlite3.Row] = None) -> None:
    """Kasa QR'ı / damga bağlantısı bir mesajlaşma uygulamasında paylaşıldı (önizleyici sayfayı çekti).
    path_kind: 'static' (/m/…), 'code' (/d/…), 'nfc' (/n, imzalı adres). Aynı IP'den 10 dk içinde tek kayıt."""
    try:
        since = (get_istanbul_now() - PREVIEW_DEDUP).isoformat()
        if conn.execute(
            "SELECT 1 FROM customer_events WHERE kind = 'link_preview' AND ip = ? AND at >= ? LIMIT 1", ((ip or "")[:64], since)
        ).fetchone():
            return
        related = None
        if path_kind == "code" and code_row is not None:
            related = code_row["used_by_customer_id"]
            created = parse_to_istanbul(code_row["created_at"])
            detail = (f"Kasa ekranındaki tek kullanımlık damga bağlantısı (oluşturulma: {_hm(created, get_istanbul_now())}) "
                      f"{bot} üzerinden paylaşıldı: uygulama bağlantının önizlemesini çekti."
                      + (" Bu bağlantı daha önce bir müşterinin işleminde kullanılmıştı." if related else ""))
            note = ("Damga bağlantısı mesajla başkasına gönderiliyor. Kullanılmış ya da süresi dolmuş bağlantı damga "
                    "vermez; kullanılmamış bağlantıyı alan kişi kasaya gelmeden damga almaya çalışabilir.")
        elif path_kind == "nfc":
            detail = f"Kasadaki NFC etiketinin dokunuş adresi {bot} üzerinden paylaşıldı: uygulama bağlantının önizlemesini çekti."
            note = "NFC dokunuş adresi mesajla başkasına gönderiliyor; her dokunuş adresi tek kullanımlıktır, bu adresle yeni damga alınamaz."
        else:
            detail = (f"Kasadaki QR'ın adresi {bot} üzerinden bir sohbete gönderildi: uygulama bağlantının önizlemesini "
                      "çekti. Paylaşanın kim olduğu görünmez (önizlemeyi uygulama çeker).")
            note = ("Kasa QR bağlantısı mesajla paylaşılıyor. Bağlantıyı alan kişi kasaya gelmeden açarsa yalnızca "
                    "bekleme süresi ve günlük limit onu durdurur; tamamen kapatmak için Müdür → QR Kodlar → Damga "
                    "Güvenliği'ndeki NFC 424 DNA etiketi ya da QR ekranı kullanılabilir.")
        record(conn, "link_preview", "Bağlantı mesajlaşma uygulamasında paylaşıldı", outcome=INFO, channel="link",
               detail=detail, flagged=True, flag_note=note, related_customer_id=related, ip=ip)
    except Exception as exc:  # pragma: no cover - savunma
        logger.warning("Bağlantı paylaşımı kaydı yazılamadı: %s", exc)


# ------------------------------------------------------------------ kasa / müdür / hediye işlemleri
def record_staff_stamp(conn: sqlite3.Connection, customer_id: int, staff_name: str, stamps_after: Optional[int],
                       reward_unlocked: bool, ip: str = "") -> None:
    detail = f"Kasiyer {staff_name} +1 damga bastı. {_progress(conn, stamps_after, reward_unlocked)}".strip()
    safe_record(conn, "staff_stamp", "Kasiyer +1 damga bastı", customer_id=customer_id, outcome=OK, channel="kasa",
                detail=detail, actor=staff_name, stamps_after=stamps_after, ip=ip)


def record_staff_denied(conn: sqlite3.Connection, customer_id: int, staff_name: str, today: int, limit: int,
                        stamps_after: Optional[int] = None, ip: str = "") -> None:
    safe_record(conn, "staff_denied", "Kasiyer damgası verilmedi — günlük limit", customer_id=customer_id,
                outcome=DENIED, channel="kasa", actor=staff_name, stamps_after=stamps_after, ip=ip,
                detail=(f"Kasiyer {staff_name} +1 damga basmak istedi; müşteri bugün {today} damga almış, günlük üst "
                        f"sınır {limit}. Damga eklenmedi."))


def record_redeem(conn: sqlite3.Connection, customer_id: int, actor: str, ok: bool = True, reason: str = "",
                  stamps_after: Optional[int] = None, ip: str = "") -> None:
    channel, who = actor_parts(actor)
    if ok:
        safe_record(conn, "reward_redeemed", "Hediye kahve teslim edildi", customer_id=customer_id, outcome=OK,
                    channel=channel, actor=who, stamps_after=stamps_after, ip=ip,
                    detail=f"{who} müşterinin hediye kahvesini teslim etti (1 hediye kullanıldı).")
    else:
        safe_record(conn, "redeem_denied", "Hediye verilemedi", customer_id=customer_id, outcome=DENIED,
                    channel=channel, actor=who, stamps_after=stamps_after, ip=ip,
                    detail=f"{who} hediye kahve vermek istedi; {reason or 'kullanılabilir hediye yok'}.")


def record_admin_stamp(conn: sqlite3.Connection, customer_id: int, delta: int, actor_username: str = "",
                       stamps_after: Optional[int] = None, reward_unlocked: bool = False) -> None:
    who = f"Müdür ({actor_username})" if actor_username else "Müdür"
    if delta > 0:
        safe_record(conn, "admin_stamp", "Müdür +1 damga ekledi", customer_id=customer_id, outcome=OK, channel="panel",
                    actor=actor_username, stamps_after=stamps_after,
                    detail=f"{who} panelden +1 damga ekledi. {_progress(conn, stamps_after, reward_unlocked)}".strip())
    else:
        safe_record(conn, "admin_unstamp", "Müdür 1 damga düşürdü", customer_id=customer_id, outcome=INFO,
                    channel="panel", actor=actor_username, stamps_after=stamps_after,
                    detail=f"{who} panelden 1 damga düşürdü (damga iptal edildi). {_progress(conn, stamps_after, False)}".strip())


def record_admin_set(conn: sqlite3.Connection, customer_id: int, old: int, new: int) -> None:
    safe_record(conn, "admin_set", "Müdür damga sayısını ayarladı", customer_id=customer_id, outcome=INFO,
                channel="panel", stamps_after=new,
                detail=f"Müdür panelden karttaki damga sayısını {old} → {new} olarak ayarladı.")


def record_bonus(conn: sqlite3.Connection, customer_id: int, source: str, stamps_after: Optional[int],
                 reward_unlocked: bool) -> None:
    why = "Uygulamayı telefonuna yüklediği" if source == "bonus_pwa" else "Bildirimleri açtığı"
    safe_record(conn, "bonus_stamp", "Hediye damga verildi", customer_id=customer_id, outcome=OK, channel="uygulama",
                stamps_after=stamps_after,
                detail=(f"{why} için tek seferlik +1 hediye damga verildi (günlük limite sayılmaz). "
                        f"{_progress(conn, stamps_after, reward_unlocked)}").strip())


def record_welcome_gift(conn: sqlite3.Connection, customer_id: int, channel: str, stamps_after: Optional[int],
                        reward_unlocked: bool) -> None:
    """ADR-35: yeni kayda verilen tek seferlik +1 kayıt hediyesi (günlük limite sayılmaz, bekleme başlatmaz)."""
    safe_record(conn, "welcome_gift", "Kayıt hediyesi verildi", customer_id=customer_id, outcome=OK,
                channel=channel or "uygulama", stamps_after=stamps_after,
                detail=("Kayıt olduğu için tek seferlik +1 hediye damga verildi (günlük limite sayılmaz). "
                        f"{_progress(conn, stamps_after, reward_unlocked)}").strip())


def record_campaign(conn: sqlite3.Connection, customer_id: int, title: str, approved: bool, who: str,
                    stamps: int = 0, reason: str = "", stamps_after: Optional[int] = None,
                    reward_unlocked: bool = False) -> None:
    label = actor_parts(who)[1] if ":" in (who or "") else (who or "Müdür")
    if approved:
        safe_record(conn, "campaign_stamp", "Kampanya damgası verildi", customer_id=customer_id, outcome=OK,
                    channel="kampanya", actor=label, stamps_after=stamps_after,
                    detail=(f"“{title}” görevi doğrulandı: +{stamps} damga (onaylayan: {label}). "
                            f"{_progress(conn, stamps_after, reward_unlocked)}").strip())
    else:
        safe_record(conn, "campaign_rejected", "Kampanya görevi reddedildi", customer_id=customer_id, outcome=INFO,
                    channel="kampanya", actor=label, stamps_after=stamps_after,
                    detail=f"“{title}” görevi reddedildi: {reason or 'yorum bulunamadı'} (inceleyen: {label}). Damga verilmedi.")


def record_fraud_review(conn: sqlite3.Connection, customer_id: int, action: str, actor: str = "") -> None:
    who = actor_parts(actor)[1]
    if action == "reset_stamps":
        title = "Şüphe incelemesi: damgalar sıfırlandı"
        detail = f"{who} olası kötüye kullanım notlarını inceledi ve müşterinin tüm damgalarını ve bekleyen hediyelerini iptal etti."
    else:
        title = "Şüphe incelemesi: uyarı kapatıldı"
        detail = f"{who} olası kötüye kullanım notlarını inceledi ve uyarıyı kapattı (önceki notlar 'İncelendi' görünür)."
    safe_record(conn, "fraud_review", title, customer_id=customer_id, outcome=INFO, channel="panel", actor=who,
                detail=detail, stamps_after=0 if action == "reset_stamps" else None)


# ------------------------------------------------------------------ okuma (müdür paneli)
def _like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _event_view(r: sqlite3.Row) -> dict[str, Any]:
    cid = r["customer_id"]
    name = ((r["c_name"] or "").strip() or f"user{cid}") if cid else "Kayıtsız ziyaretçi"
    rid = r["related_customer_id"]
    return {
        "id": r["id"],
        "at": r["at"],
        "customer_id": cid,
        "customer_name": name,
        "customer_phone": mask_phone(r["c_phone"]) if (cid and r["c_phone"] and not r["c_deleted"]) else "",
        "related_customer_id": rid,
        "related_name": ((r["r_name"] or "").strip() or f"user{rid}") if rid else "",
        "kind": r["kind"],
        "outcome": r["outcome"],
        "channel": r["channel"],
        "channel_label": CHANNEL_LABELS.get(r["channel"], ""),
        "title": r["title"],
        "detail": r["detail"],
        "flagged": bool(r["flagged"]),
        "flag_note": r["flag_note"],
        "opened_via": r["opened_via"],
        "opened_via_label": via_label(r["opened_via"] or ""),
        "actor": r["actor"],
        "stamps_after": r["stamps_after"],
        "reviewed": bool(r["flagged"] and r["c_reviewed"] and (r["at"] or "") <= r["c_reviewed"]),
    }


def _since_day(days: int) -> Optional[str]:
    if not days or days <= 0:
        return None
    return (get_istanbul_now() - timedelta(days=days - 1)).strftime("%Y-%m-%d")


def list_events(
    conn: sqlite3.Connection, group: str = "all", days: int = 7, customer_id: Optional[int] = None,
    q: Optional[str] = None, before_id: Optional[int] = None, limit: int = 50,
) -> dict[str, Any]:
    """Müdür paneli listesi (yeniden eskiye). group: all | ok | denied | flagged | kasa | qr; days 0 = tümü."""
    limit = max(1, min(int(limit or 50), 200))
    where: list[str] = []
    params: list[Any] = []
    cond = GROUPS.get(group or "all", "")
    if cond:
        where.append(cond)
    since = _since_day(int(days or 0))
    if since:
        where.append("e.at >= ?")
        params.append(since)
    if customer_id:
        where.append("(e.customer_id = ? OR e.related_customer_id = ?)")
        params += [customer_id, customer_id]
    needle = (q or "").strip()
    if needle:
        parts = ["LOWER(COALESCE(c.display_name, '')) LIKE ? ESCAPE '\\'"]
        params.append(f"%{_like_escape(needle.lower())}%")
        digits = re.sub(r"\D", "", needle)
        if digits.startswith("90") and len(digits) > 10:
            digits = digits[2:]
        digits = digits.lstrip("0")
        if len(digits) >= 3:
            parts.append("c.phone_e164 LIKE ? ESCAPE '\\'")
            params.append(f"%{_like_escape(digits)}%")
        where.append("(" + " OR ".join(parts) + ")")
    if before_id:
        where.append("e.id < ?")
        params.append(int(before_id))
    sql = (
        "SELECT e.*, c.display_name AS c_name, c.phone_e164 AS c_phone, c.deleted_at AS c_deleted, "
        "c.fraud_reviewed_at AS c_reviewed, r.display_name AS r_name "
        "FROM customer_events e LEFT JOIN customers c ON c.id = e.customer_id "
        "LEFT JOIN customers r ON r.id = e.related_customer_id "
        + ("WHERE " + " AND ".join(where) + " " if where else "")
        + "ORDER BY e.id DESC LIMIT ?"
    )
    rows = conn.execute(sql, (*params, limit + 1)).fetchall()
    events = [_event_view(r) for r in rows[:limit]]
    out: dict[str, Any] = {
        "events": events,
        "has_more": len(rows) > limit,
        "next_before_id": events[-1]["id"] if (len(rows) > limit and events) else None,
    }
    if customer_id:
        c = conn.execute(
            "SELECT id, display_name, phone_e164, deleted_at, COALESCE(fraud_risk_score, 0) AS score FROM customers WHERE id = ?",
            (customer_id,),
        ).fetchone()
        if c:
            from app.services.fraud import fraud_level
            out["customer"] = {
                "id": c["id"],
                "display_name": (c["display_name"] or "").strip() or f"user{c['id']}",
                "phone": mask_phone(c["phone_e164"]) if not c["deleted_at"] else "",
                "fraud_risk_score": c["score"],
                "fraud_level": fraud_level(int(c["score"] or 0)),
            }
    return out


SEEN_KEY = "activity_seen_id"   # müdürün İşlem Kayıtları'nda gördüğü son customer_events.id


def seen_id(conn: sqlite3.Connection) -> int:
    try:
        return int(get_setting(conn, SEEN_KEY, "0") or 0)
    except ValueError:
        return 0


def mark_seen(conn: sqlite3.Connection) -> int:
    """Müdür İşlem Kayıtları'nı açtı: o ana kadarki tüm kayıtlar okunmuş sayılır, menüdeki uyarı rozeti söner."""
    last = int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM customer_events").fetchone()[0] or 0)
    if last > seen_id(conn):
        set_setting(conn, SEEN_KEY, str(last))
    return last


def summary(conn: sqlite3.Connection, days: int = 7) -> dict[str, Any]:
    """Ana ekran analizi: verilen / verilmeyen damgalar, ret nedenleri, olası kötüye kullanım, günlük seri."""
    from app.services.fraud import fraud_level
    days = 30 if days >= 30 else 7
    now = get_istanbul_now()
    keys = [(now - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days - 1, -1, -1)]
    first, today = keys[0], keys[-1]
    per_day = {k: {"date": k, "label": f"{k[8:10]}.{k[5:7]}", "ok": 0, "denied": 0, "flagged": 0} for k in keys}
    totals: dict[str, Any] = {
        "stamps": 0, "stamps_qr": 0, "stamps_kasa": 0, "stamps_gift": 0, "denied": 0, "denied_qr": 0, "flagged": 0,
        "registrations": 0, "redeemed": 0, "today_ok": 0, "today_denied": 0, "today_flagged": 0,
    }
    reasons: dict[str, int] = {}
    rows = conn.execute(
        "SELECT substr(at, 1, 10) AS d, kind, flagged, COUNT(*) AS n FROM customer_events WHERE at >= ? "
        "GROUP BY d, kind, flagged",
        (first,),
    ).fetchall()
    for r in rows:
        d, kind, flagged, n = r["d"], r["kind"], r["flagged"], r["n"]
        day = per_day.get(d)
        if kind in STAMP_OK_KINDS:
            totals["stamps"] += n
            bucket = "stamps_qr" if kind in _QR_STAMP_KINDS else ("stamps_kasa" if kind in _KASA_STAMP_KINDS else "stamps_gift")
            totals[bucket] += n
            if day:
                day["ok"] += n
            if d == today:
                totals["today_ok"] += n
        elif kind in DENIED_REASON_LABELS:
            totals["denied"] += n
            if kind != "staff_denied":
                totals["denied_qr"] += n
            reasons[kind] = reasons.get(kind, 0) + n
            if day:
                day["denied"] += n
            if d == today:
                totals["today_denied"] += n
        elif kind == "registered":
            totals["registrations"] += n
        elif kind == "reward_redeemed":
            totals["redeemed"] += n
        if flagged:
            totals["flagged"] += n
            if day:
                day["flagged"] += n
            if d == today:
                totals["today_flagged"] += n
    qr_total = totals["stamps_qr"] + totals["denied_qr"]
    totals["success_rate"] = round(totals["stamps_qr"] * 100.0 / qr_total) if qr_total else None
    totals["flagged_customers"] = conn.execute(
        "SELECT COUNT(DISTINCT customer_id) FROM customer_events WHERE flagged = 1 AND at >= ? AND customer_id IS NOT NULL",
        (first,),
    ).fetchone()[0]
    # menüdeki uyarı rozeti: müdürün İşlem Kayıtları'nı son açışından sonra gelen notlar (mark_seen)
    totals["unseen_flagged"] = conn.execute(
        "SELECT COUNT(*) FROM customer_events WHERE flagged = 1 AND at >= ? AND id > ?",
        (first, seen_id(conn)),
    ).fetchone()[0]

    top = []
    for t in conn.execute(
        "SELECT customer_id, COUNT(*) AS n, MAX(id) AS last_id FROM customer_events "
        "WHERE flagged = 1 AND at >= ? AND customer_id IS NOT NULL GROUP BY customer_id ORDER BY n DESC, last_id DESC LIMIT 5",
        (first,),
    ).fetchall():
        c = conn.execute(
            "SELECT display_name, phone_e164, deleted_at, COALESCE(fraud_risk_score, 0) AS score FROM customers WHERE id = ?",
            (t["customer_id"],),
        ).fetchone()
        last = conn.execute("SELECT title, flag_note, at FROM customer_events WHERE id = ?", (t["last_id"],)).fetchone()
        top.append({
            "customer_id": t["customer_id"],
            "display_name": ((c["display_name"] or "").strip() if c else "") or f"user{t['customer_id']}",
            "phone": mask_phone(c["phone_e164"]) if (c and not c["deleted_at"]) else "",
            "count": t["n"],
            "fraud_level": fraud_level(int(c["score"] or 0)) if c else None,
            "last_title": last["title"] if last else "",
            "last_note": last["flag_note"] if last else "",
            "last_at": last["at"] if last else "",
        })
    return {
        "days": days,
        "totals": totals,
        "series": list(per_day.values()),
        "reasons": [
            {"kind": k, "label": DENIED_REASON_LABELS[k], "count": n}
            for k, n in sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
        "top_flagged": top,
        "recent_flagged": list_events(conn, group="flagged", days=days, limit=5)["events"],
        "recent": list_events(conn, group="all", days=0, limit=10)["events"],
    }


# ------------------------------------------------------------------ geçmiş verinin bir kez aktarılması
_BACKFILL_STAMPS = {
    "qr": ("stamp", "Damga verildi", "scan", "Kasadaki QR / NFC ile damga aldı."),
    "welcome": ("welcome_stamp", "Hoş geldin damgası verildi", "scan", "Kasadaki QR ile kayıt olup ilk damgasını aldı."),
    "staff": ("staff_stamp", "Kasiyer +1 damga bastı", "kasa", "Kasiyer +1 damga bastı."),
    "admin": ("admin_stamp", "Müdür +1 damga ekledi", "panel", "Müdür panelden +1 damga ekledi."),
    "bonus_pwa": ("bonus_stamp", "Hediye damga verildi", "uygulama", "Uygulamayı telefonuna yüklediği için +1 hediye damga verildi."),
    "bonus_notif": ("bonus_stamp", "Hediye damga verildi", "uygulama", "Bildirimleri açtığı için +1 hediye damga verildi."),
    "campaign": ("campaign_stamp", "Kampanya damgası verildi", "kampanya", "Ödüllü kampanya görevi doğrulandı: +1 damga."),
}
_BACKFILL_CODES = {
    "limit": ("daily_limit", "Damga verilmedi — günlük limit doldu", DENIED, "QR okuttu ama günlük damga limiti dolmuştu; damga eklenmedi."),
    "cooldown": ("cooldown", "Damga verilmedi — bekleme süresi dolmadı", DENIED,
                 "QR okuttu ama aynı ziyarette damga almıştı (bekleme süresi dolmamıştı); damga eklenmedi."),
    "welcome_locked": ("welcome_locked", "Kayıt oldu — hoş geldin damgası verilmedi", INFO,
                       "Daha önce hesabını silmiş numara / telefon; hoş geldin damgası verilmedi."),
}
_BACKFILL_FRAUD = {
    "claim_replay": ("link_shared", "Damga verilmedi — başkasının bağlantısı",
                     "Tek kullanımlık damga bağlantısı başka bir telefonda yeniden açıldı.",
                     "Bağlantı kopyalanıp paylaşılmış olabilir; tek kullanımlık bağlantı yalnızca kasada okutan kişiye damga verir."),
    "stamp_code_reused": ("link_reused", "Damga verilmedi — kullanılmış bağlantı yeniden açıldı",
                          "Müşteri kendisine damga veren bağlantıyı 5 dakikadan sonra yeniden açtı.",
                          "Bağlantıyı kaydedip yeniden açarak ikinci damga almak istemiş olabilir."),
    "stamp_code_expired": ("link_expired", "Damga verilmedi — süresi dolmuş bağlantı",
                           "Süresi dolmuş bir damga bağlantısı açıldı.",
                           "Bağlantı kasada açıldıktan sonra saklanıp sonradan açılmış olabilir."),
    "qr_invalid": ("link_invalid", "Damga verilmedi — geçersiz bağlantı",
                   "Sistemde olmayan ya da imzası bozuk bir damga bağlantısı denendi.",
                   "Damga bağlantısı uydurulmuş ya da değiştirilmiş olabilir."),
    "qr_expired": ("link_expired", "Damga verilmedi — eski güvenli QR",
                   "Süresi dolmuş güvenli (dinamik) kasa QR'ı ile damga istendi.",
                   "QR'ın fotoğrafı ya da eski bağlantısı sonradan kullanılmış olabilir."),
    "qr_missing": ("link_expired", "Damga verilmedi — eski güvenli QR",
                   "Güvenli kasa QR'ı açıkken zaman damgası olmayan eski bir bağlantı kullanıldı.",
                   "Basılı QR'ın fotoğrafı ya da eski bağlantısı kullanılmış olabilir."),
    "nfc_invalid": ("nfc_invalid", "Damga verilmedi — NFC imzası tutmadı", "NFC dokunuş adresinin imzası doğrulanamadı.",
                    "NFC adresi elle değiştirilmiş ya da uydurulmuş olabilir."),
    "nfc_stale": ("nfc_stale", "Damga verilmedi — eski NFC dokunuşu", "Daha önce okunmuş eski bir NFC dokunuş adresi açıldı.",
                  "Eski NFC adresi kaydedilip sonradan açılmış olabilir."),
}
_OLD = " (geçmiş kayıt)"


def backfill_once(conn: sqlite3.Connection) -> int:
    """İlk açılışta mevcut kayıtları (müşteri kaydı, damgalar, hediye teslimi, son 30 günün reddedilen açılışları,
    şüphe olayları) işlem listesine bir kez aktarır; bayrak: settings.customer_events_backfill_v1."""
    if conn.execute("SELECT 1 FROM settings WHERE key = 'customer_events_backfill_v1'").fetchone():
        return 0
    events: list[tuple[Any, ...]] = []

    def add(at, cid, kind, outcome, channel, title, detail, flagged=0, note="", actor="", ip="", dev=""):
        if at:
            events.append((at, cid, kind, outcome, channel, title, detail + _OLD, flagged, note, actor or "", ip or "", dev or ""))

    if not conn.execute("SELECT 1 FROM customer_events LIMIT 1").fetchone():
        for c in conn.execute("SELECT id, created_at FROM customers"):
            add(c["created_at"], c["id"], "registered", INFO, "", "Kayıt oldu", "Müşteri kaydı oluşturuldu.")
        for s in conn.execute("SELECT customer_id, created_at, source, actor, note, voided_at, voided_reason FROM stamps"):
            kind, title, channel, detail = _BACKFILL_STAMPS.get(s["source"] or "qr", _BACKFILL_STAMPS["qr"])
            if s["source"] == "campaign" and s["note"]:
                detail = f"Ödüllü kampanya “{s['note']}” görevi doğrulandı: +1 damga."
            if s["actor"] and kind in ("staff_stamp", "admin_stamp", "campaign_stamp"):
                detail = detail.rstrip(".") + f" (işlemi yapan: {s['actor']})."
            if s["voided_at"]:
                detail += f" Bu damga sonradan iptal edildi: {s['voided_reason'] or 'neden belirtilmedi'}."
            add(s["created_at"], s["customer_id"], kind, OK, channel, title, detail, actor=s["actor"] or "")
        for r in conn.execute("SELECT customer_id, redeemed_at, redeemed_by FROM rewards WHERE redeemed_at IS NOT NULL"):
            who = f" (işlemi yapan: {r['redeemed_by']})" if r["redeemed_by"] else ""
            add(r["redeemed_at"], r["customer_id"], "reward_redeemed", OK, "kasa", "Hediye kahve teslim edildi",
                f"Hediye kahve teslim edildi{who}.", actor=r["redeemed_by"] or "")
        for k in conn.execute(
            "SELECT used_at, used_by_customer_id, result FROM stamp_codes WHERE used_at IS NOT NULL "
            "AND used_by_customer_id IS NOT NULL AND result IN ('limit', 'cooldown', 'welcome_locked')"
        ):
            kind, title, outcome, detail = _BACKFILL_CODES[k["result"]]
            add(k["used_at"], k["used_by_customer_id"], kind, outcome, "scan", title, detail)
        for f in conn.execute("SELECT at, customer_id, reason, ip, device_hash FROM fraud_events"):
            kind, title, detail, note = _BACKFILL_FRAUD.get(
                f["reason"], ("flag", "Şüpheli işlem", f"Şüpheli işlem kaydı ({f['reason']}).", "Olası kötüye kullanım kaydı.")
            )
            add(f["at"], f["customer_id"], kind, DENIED, "", title, detail, flagged=1, note=note, ip=f["ip"], dev=f["device_hash"])
        events.sort(key=lambda e: e[0] or "")
        conn.executemany(
            "INSERT INTO customer_events (at, customer_id, kind, outcome, channel, title, detail, flagged, flag_note, "
            "actor, ip, device_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            events,
        )
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('customer_events_backfill_v1', '1')")
    return len(events)

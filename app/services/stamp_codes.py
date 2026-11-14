"""
Tek kullanımlık damga kodları ve açılışlar (ADR-28, ADR-29, ADR-30).

Kaynaklar (`origin`; ADR-30 ile 'nfc' = NTAG 424 DNA dokunuşu, 'display' = Kasa QR Ekranı da eklendi):
- 'scan'   — kasadaki SABİT QR / NFC her okutulduğunda (GET /m/qr) üretilen tek kullanımlık açılış (ADR-29). QR ve
             NFC etiketi değişmez ama her okutuş bir kez açılır: sayfayı yenilemek, geri dönmek, ekranı/bağlantıyı
             başkasına göndermek ikinci damga vermez. Sabit adresi yeniden açmak yeni bir açılıştır; onu "aynı ziyarette
             tek damga" beklemesi (min_interval_minutes) ve günlük limit sınırlar (loyalty.process_claim_submission).
- 'screen' — kasa ekranında (Kasa paneli → "Damga QR'ı Göster") gösterilen canlı kod (ADR-28, isteğe bağlı).
- Kod çalışan/müdür oturumuyla üretilir, CODE_TTL_SECONDS içinde açılmalıdır ve TEK KULLANIMLIKTIR.
- Kullanılınca kasa ekranı hemen yeni kod gösterir; kullanılmasa da ROTATE_SECONDS'ta bir yenilenir (önceki kod
  RETIRE_GRACE_SECONDS sonra geçersizleşir).
- Sayfayı açmak (GET) kodu tüketmez: bağlantı önizleyicileri kodu yakamaz. Damga /api/claim (POST) ile istenir.
- Kayıtlı olmayan müşteri kodu açınca kod o tarayıcıya HOLD_SECONDS (kayıt formu süresi) ayrılır; başka telefon
  kullanamaz (HttpOnly çerez HOLD_COOKIE). Kayıt bitince damga işlenir.
- Kodun kendisi saklanmaz; yalnızca SHA-256 özeti tutulur. Kodlar CODE_RETENTION_DAYS sonra silinir (retention).
"""
from __future__ import annotations

import hmac
import re
import secrets
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Optional

from app.config import PUBLIC_BASE_PATH, PUBLIC_BASE_URL
from app.utils import generate_token, get_istanbul_now, hash_token, parse_to_istanbul

CODE_TTL_SECONDS = 120        # kasa ekranında görünen kodun açılma süresi
SCAN_TTL_SECONDS = 600        # sabit QR / NFC açılışının geçerlilik süresi (kayıt formu dahil)
ROTATE_SECONDS = 60           # okutulmasa da kasa ekranındaki kod bu sürede bir yenilenir
RETIRE_GRACE_SECONDS = 15     # yeni koda geçilince öncekinin geçerli kaldığı kısa süre (o an okutan müşteri için)
HOLD_SECONDS = 600            # kayıtlı olmayan müşterinin kayıt formunu doldurma süresi (kod ona ayrılır)
HOLD_COOKIE = "sadakat_stamp_hold"
CODE_RETENTION_DAYS = 30

_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_URL_RE = re.compile(r"(?:^|/)d/([^/?#&\s]*)")
_SEAL_URL_RE = re.compile(r"/m/([A-Za-z0-9_-]{1,64})")
_STAFF_STATES = {"used": "used", "held_elsewhere": "held", "expired": "expired", "valid": "waiting"}


def stamp_url(code: str, base_url: Optional[str] = None, base_path: Optional[str] = None) -> str:
    """Kasa ekranındaki QR'ın açtığı adres (/k/d/{kod})."""
    u = PUBLIC_BASE_URL if base_url is None else base_url
    p = PUBLIC_BASE_PATH if base_path is None else base_path
    return f"{u}{p}/d/{code}"


def extract_code(raw: Optional[str]) -> tuple[Optional[str], bool]:
    """
    QR içeriği, bağlantı veya ham değerden damga kodunu ayıklar. Dönüş: (kod | None, açıkça damga bağlantısı mı).
    `/d/…` içeren değer açık damga bağlantısıdır; biçimi bozuksa (None, True) döner. Ham değer kod biçimindeyse
    (kod, False) döner — tabloda yoksa sabit/eski bağlantı sayılır (ör. eski claim token'ları).
    """
    val = (raw or "").strip()
    if not val:
        return None, False
    m = _URL_RE.search(val)
    if m:
        code = m.group(1)
        return (code if _CODE_RE.match(code) else None), True
    if _CODE_RE.match(val):
        return val, False
    return None, False


def static_seal_token(raw: Optional[str]) -> Optional[str]:
    """Sabit kasa QR'ı / NFC içeriğinden mühür adını çıkarır (/m/{ad} ya da ham ad, ör. 'qr'); bulunamazsa None."""
    val = (raw or "").strip()
    m = _SEAL_URL_RE.search(val)
    if m:
        return m.group(1)
    if val and "/" not in val and len(val) <= 64:
        return val
    return None


# ADR-41: kasadaki Damga QR'ının adresi tahmin edilemez: /m/{gizli kod}. Kod ayarlarda (stamp_qr_token) durur, ilk
# ihtiyaçta üretilir, müdür panelinden yenilenebilir (eskisi hemen geçersizleşir). Yalnızca bu kod sabit QR açılışı
# üretir; /m/qr ve /m/ altındaki başka her ad (ve uygulama içi okutucuya okutulan ham değer) damga VERMEZ.
STAMP_QR_TOKEN_KEY = "stamp_qr_token"
_STAMP_TOKEN_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"   # karışan harf/rakam yok (l, i, o, 0, 1)
STAMP_TOKEN_LENGTH = 12


def new_stamp_qr_token() -> str:
    return "".join(secrets.choice(_STAMP_TOKEN_ALPHABET) for _ in range(STAMP_TOKEN_LENGTH))


def stamp_qr_token(conn: sqlite3.Connection) -> str:
    """Damga QR'ının gizli adres kodu; yoksa üretir (eşzamanlı ilk istekte tek kod kalır)."""
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (STAMP_QR_TOKEN_KEY,)).fetchone()
    tok = (row[0] if row else "") or ""
    if tok.strip():
        return tok.strip()
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value "
        "WHERE TRIM(COALESCE(settings.value, '')) = ''",
        (STAMP_QR_TOKEN_KEY, new_stamp_qr_token()),
    )
    return (conn.execute("SELECT value FROM settings WHERE key = ?", (STAMP_QR_TOKEN_KEY,)).fetchone()[0] or "").strip()


def rotate_stamp_qr_token(conn: sqlite3.Connection) -> str:
    """Yeni gizli adres; eski Damga QR'ı / NFC etiketi hemen damga vermez olur (yeniden basılmalı)."""
    tok = new_stamp_qr_token()
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (STAMP_QR_TOKEN_KEY, tok),
    )
    return tok


def is_stamp_qr_token(conn: sqlite3.Connection, token: Optional[str]) -> bool:
    t = (token or "").strip().lower()
    return bool(t) and hmac.compare_digest(t.encode(), stamp_qr_token(conn).lower().encode())


# Panel / kasa QR görselleri (ADR-35): `kind` → (adres yolu, indirme dosya adı). 'kayit' sistemi açar ve damga VERMEZ
# (/m/kayit); 'damga' kasadaki Damga QR'ıdır (/m/{gizli kod}, ADR-41). Eski adlar geriye uyum için durur:
# 'register' / 'stamp' → damga QR'ı.
_STAMP_PATH = "/m/{stamp}"
QR_IMAGE_KINDS = {
    "kayit": ("/m/kayit", "qr-kayit-uygulama"),
    "damga": (_STAMP_PATH, "qr-kasa-damga"),
    "install": ("/indir", "qr-masa-menu"),
    "misafir": ("/misafir", "qr-misafir-menu"),   # ADR-40: sosyal medya / afiş — menü + kampanyalar, damga yok
    "register": (_STAMP_PATH, "qr-kasa-damga"),
    "stamp": (_STAMP_PATH, "qr-kasa-damga"),
}


def stamp_qr_path(conn: sqlite3.Connection) -> str:
    return _STAMP_PATH.format(stamp=stamp_qr_token(conn))


def qr_image_target(conn: sqlite3.Connection, kind: Optional[str]) -> tuple[str, str]:
    """`kind` → (adres yolu, indirme dosya adı); bilinmeyen kind damga QR'ına düşer."""
    path, fname = QR_IMAGE_KINDS.get((kind or "").strip().lower(), QR_IMAGE_KINDS["damga"])
    return (stamp_qr_path(conn) if path == _STAMP_PATH else path), fname


ORIGINS = ("screen", "scan", "nfc", "display")


def create_stamp_code(
    conn: sqlite3.Connection,
    created_by: str = "",
    staff_id: Optional[int] = None,
    actor: str = "",
    now: Optional[datetime] = None,
    origin: str = "screen",
    ttl_seconds: Optional[int] = None,
    code: Optional[str] = None,
    display_id: Optional[int] = None,
    base_url: Optional[str] = None,
    base_path: Optional[str] = None,
) -> dict[str, Any]:
    """
    Yeni tek kullanımlık kod üretir. origin='screen': kasa ekranının canlı kodu (`created_by`/`staff_id` kodu gösteren
    kasiyer, `actor` rapordaki kullanıcı adı); origin='scan': sabit QR / NFC okutmasıyla açılan tek kullanımlık açılış;
    origin='nfc': NTAG 424 DNA dokunuşu (`code` = nfc_sun.tap_code, dokunuş başına sabit); origin='display': Kasa QR
    Ekranı (`display_id`).
    """
    now = now or get_istanbul_now()
    code = code or generate_token(12)
    ttl = int(ttl_seconds or CODE_TTL_SECONDS)
    expires = now + timedelta(seconds=ttl)
    cur = conn.execute(
        "INSERT INTO stamp_codes (code_hash, created_at, expires_at, created_by, staff_id, actor, origin, display_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (hash_token(code), now.isoformat(), expires.isoformat(), (created_by or "")[:60], staff_id, (actor or "")[:60],
         origin if origin in ORIGINS else "screen", display_id),
    )
    return {
        "id": cur.lastrowid,
        "code": code,
        "url": stamp_url(code, base_url=base_url, base_path=base_path),
        "expires_at": expires.isoformat(),
        "ttl": ttl,
        "rotate_after": ROTATE_SECONDS,
        "origin": origin,
    }


def find_code(conn: sqlite3.Connection, code: Optional[str]) -> Optional[sqlite3.Row]:
    if not code:
        return None
    return conn.execute("SELECT * FROM stamp_codes WHERE code_hash = ?", (hash_token(code),)).fetchone()


def code_state(row: sqlite3.Row, hold_secret: Optional[str] = None, now: Optional[datetime] = None) -> str:
    """'used' | 'held_here' (bu tarayıcıya ayrılmış) | 'held_elsewhere' | 'expired' | 'valid'."""
    now = now or get_istanbul_now()
    if row["used_at"]:
        return "used"
    held_until = parse_to_istanbul(row["held_until"])
    if held_until is not None:
        if now > held_until:
            return "expired"
        mine = bool(hold_secret and row["hold_hash"]) and hmac.compare_digest(hash_token(hold_secret), row["hold_hash"])
        return "held_here" if mine else "held_elsewhere"
    expires = parse_to_istanbul(row["expires_at"])
    if expires is None or now > expires:
        return "expired"
    return "valid"


def hold_code(conn: sqlite3.Connection, row: sqlite3.Row, now: Optional[datetime] = None) -> str:
    """Kodu bu tarayıcıya ayırır (kayıt formu süresi) ve tarayıcıya çerezle verilecek gizli değeri döner."""
    now = now or get_istanbul_now()
    secret = generate_token(24)
    conn.execute(
        "UPDATE stamp_codes SET hold_hash = ?, held_until = ? WHERE id = ? AND used_at IS NULL",
        (hash_token(secret), (now + timedelta(seconds=HOLD_SECONDS)).isoformat(), row["id"]),
    )
    return secret


def consume_code(
    conn: sqlite3.Connection, code_id: int, customer_id: Optional[int], result: str, now: Optional[datetime] = None
) -> bool:
    """Kodu kalıcı olarak kullanılmış sayar; yalnızca bir kez başarılı olur. result: stamped | limit | welcome_locked."""
    now = now or get_istanbul_now()
    cur = conn.execute(
        "UPDATE stamp_codes SET used_at = ?, used_by_customer_id = ?, result = ? WHERE id = ? AND used_at IS NULL",
        (now.isoformat(), customer_id, result, code_id),
    )
    return cur.rowcount == 1


def retire_code(conn: sqlite3.Connection, code_id: int, now: Optional[datetime] = None) -> None:
    """Kasa ekranı yeni koda geçince önceki (okutulmamış, ayrılmamış) kod RETIRE_GRACE_SECONDS sonra geçersizleşir."""
    now = now or get_istanbul_now()
    row = conn.execute("SELECT expires_at, used_at, held_until FROM stamp_codes WHERE id = ?", (code_id,)).fetchone()
    if not row or row["used_at"] or row["held_until"]:
        return
    cutoff = now + timedelta(seconds=RETIRE_GRACE_SECONDS)
    expires = parse_to_istanbul(row["expires_at"])
    if expires is None or expires > cutoff:
        conn.execute("UPDATE stamp_codes SET expires_at = ? WHERE id = ?", (cutoff.isoformat(), code_id))


def code_status_for_staff(
    conn: sqlite3.Connection, code_id: int, now: Optional[datetime] = None, with_customer: bool = True
) -> dict[str, Any]:
    """Kasa ekranı yoklaması: waiting | held (müşteri kayıt oluyor) | used (kime, sonucu) | expired.
    `with_customer=False`: müşteriye dönük Kasa QR Ekranı için müşteri adı verilmez (ADR-30)."""
    row = conn.execute("SELECT * FROM stamp_codes WHERE id = ?", (code_id,)).fetchone()
    if not row:
        return {"status": "error", "id": code_id, "message": "Kod bulunamadı."}
    out: dict[str, Any] = {
        "status": "success",
        "id": row["id"],
        "state": _STAFF_STATES.get(code_state(row, None, now), "expired"),
        "result": row["result"],
    }
    if with_customer and row["used_by_customer_id"]:
        cust = conn.execute("SELECT display_name FROM customers WHERE id = ?", (row["used_by_customer_id"],)).fetchone()
        out["customer"] = (cust["display_name"] if cust and cust["display_name"] else None) or f"user{row['used_by_customer_id']}"
    return out

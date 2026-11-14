"""
Kasa QR Ekranı (ADR-30): basılı QR'ın yerine kasaya konan tablet / eski telefon. Her okutuşta yenilenen tek kullanımlık
Damga QR'ı gösterir (stamp_codes, origin='display'). Kasiyer oturumu gerektirmez ve başka hiçbir işleme erişemez.

- Müdür paneli "Ekran Ekle" → PAIR_TTL_SECONDS geçerli, tek kullanımlık eşleştirme bağlantısı (/k/ekran/eslestir/{kod}).
- Tablet bağlantıyı açıp "Bu cihazı QR ekranı yap"a dokunur (POST: bağlantı önizleyicileri eşleştirmeyi yakamaz) → cihaza
  HttpOnly DISPLAY_COOKIE yazılır; sunucuda yalnızca SHA-256 özeti tutulur. Ekran adresi: /k/ekran.
- Ekran yalnızca kendi kodlarını üretir ve yoklar; müşteri adı göstermez (ekran müşteriye dönüktür).
- "Yeniden eşleştir" yeni bağlantı verir; yeni cihaz eşleşince önceki cihaz düşer. "Kaldır" ekranı hemen kapatır.
"""
from __future__ import annotations

import hmac
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Optional

from app.config import PUBLIC_BASE_PATH, PUBLIC_BASE_URL
from app.utils import generate_token, get_istanbul_now, hash_token, parse_to_istanbul

DISPLAY_COOKIE = "sadakat_qr_display"
DISPLAY_COOKIE_MAX_AGE = 400 * 24 * 3600   # tarayıcıların izin verdiği en uzun süre; ekran her açılışta yeniler
PAIR_TTL_SECONDS = 15 * 60


def pair_url(code: str, base_url: Optional[str] = None, base_path: Optional[str] = None) -> str:
    u = PUBLIC_BASE_URL if base_url is None else base_url
    p = PUBLIC_BASE_PATH if base_path is None else base_path
    return f"{u}{p}/ekran/eslestir/{code}"


def display_url(base_url: Optional[str] = None, base_path: Optional[str] = None) -> str:
    u = PUBLIC_BASE_URL if base_url is None else base_url
    p = PUBLIC_BASE_PATH if base_path is None else base_path
    return f"{u}{p}/ekran"


def _new_pairing(
    conn: sqlite3.Connection, display_id: int, now: datetime,
    base_url: Optional[str] = None, base_path: Optional[str] = None,
) -> dict[str, Any]:
    code = generate_token(12)
    expires = now + timedelta(seconds=PAIR_TTL_SECONDS)
    conn.execute(
        "UPDATE qr_displays SET pair_hash = ?, pair_expires_at = ? WHERE id = ?",
        (hash_token(code), expires.isoformat(), display_id),
    )
    return {
        "id": display_id, "pair_code": code,
        "pair_url": pair_url(code, base_url=base_url, base_path=base_path),
        "pair_expires_at": expires.isoformat(),
    }


def create_display(
    conn: sqlite3.Connection, label: str, created_by: str = "", now: Optional[datetime] = None,
    base_url: Optional[str] = None, base_path: Optional[str] = None,
) -> dict[str, Any]:
    """Yeni ekran kaydı + eşleştirme bağlantısı."""
    now = now or get_istanbul_now()
    number = conn.execute("SELECT COUNT(*) FROM qr_displays").fetchone()[0] + 1
    name = (label or "").strip()[:40] or f"QR Ekranı {number}"
    cur = conn.execute(
        "INSERT INTO qr_displays (label, created_at, created_by, active) VALUES (?, ?, ?, 1)",
        (name, now.isoformat(), (created_by or "")[:60]),
    )
    return {"label": name, **_new_pairing(conn, cur.lastrowid, now, base_url=base_url, base_path=base_path)}


def new_pairing(
    conn: sqlite3.Connection, display_id: int, now: Optional[datetime] = None,
    base_url: Optional[str] = None, base_path: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Etkin ekran için yeni eşleştirme bağlantısı (cihaz değişince). Önceki cihaz, yenisi eşleşene kadar çalışır."""
    row = conn.execute("SELECT id, label FROM qr_displays WHERE id = ? AND active = 1", (display_id,)).fetchone()
    if not row:
        return None
    return {"label": row["label"], **_new_pairing(conn, row["id"], now or get_istanbul_now(), base_url=base_url, base_path=base_path)}


def pairing_row(conn: sqlite3.Connection, code: Optional[str], now: Optional[datetime] = None) -> Optional[sqlite3.Row]:
    """Geçerli (süresi dolmamış, kullanılmamış) eşleştirme kodunun ekranı; yoksa None."""
    if not code or len(code) > 80:
        return None
    row = conn.execute(
        "SELECT * FROM qr_displays WHERE pair_hash = ? AND active = 1", (hash_token(code),)
    ).fetchone()
    expires = parse_to_istanbul(row["pair_expires_at"]) if row else None
    if row is None or expires is None or (now or get_istanbul_now()) > expires:
        return None
    return row


def pair_device(conn: sqlite3.Connection, code: Optional[str], now: Optional[datetime] = None) -> Optional[tuple[sqlite3.Row, str]]:
    """Eşleştirme kodunu tüketir; (ekran, cihaza çerezle verilecek gizli değer) döner. Önceki cihaz düşer."""
    now = now or get_istanbul_now()
    row = pairing_row(conn, code, now)
    if row is None:
        return None
    secret = generate_token(32)
    cur = conn.execute(
        "UPDATE qr_displays SET token_hash = ?, pair_hash = NULL, pair_expires_at = NULL, paired_at = ?, last_seen_at = ? "
        "WHERE id = ? AND pair_hash = ?",
        (hash_token(secret), now.isoformat(), now.isoformat(), row["id"], row["pair_hash"]),
    )
    if cur.rowcount != 1:
        return None
    return row, secret


def display_from_secret(conn: sqlite3.Connection, secret: Optional[str]) -> Optional[sqlite3.Row]:
    """Çerezdeki gizli değere ait etkin ekran; yoksa None."""
    if not secret or len(secret) > 200:
        return None
    digest = hash_token(secret)
    row = conn.execute("SELECT * FROM qr_displays WHERE token_hash = ? AND active = 1", (digest,)).fetchone()
    if row is None or not hmac.compare_digest(row["token_hash"] or "", digest):
        return None
    return row


def touch(conn: sqlite3.Connection, display_id: int, now: Optional[datetime] = None) -> None:
    conn.execute("UPDATE qr_displays SET last_seen_at = ? WHERE id = ?", ((now or get_istanbul_now()).isoformat(), display_id))


def revoke_display(conn: sqlite3.Connection, display_id: int) -> bool:
    """Ekranı kaldırır: çerez ve bekleyen eşleştirme hemen geçersizleşir."""
    cur = conn.execute(
        "UPDATE qr_displays SET active = 0, token_hash = NULL, pair_hash = NULL, pair_expires_at = NULL WHERE id = ? AND active = 1",
        (display_id,),
    )
    return cur.rowcount == 1


def list_displays(conn: sqlite3.Connection, now: Optional[datetime] = None) -> list[dict[str, Any]]:
    """Etkin ekranlar (müdür paneli): eşleşti mi, son görülme, bekleyen eşleştirme süresi."""
    now = now or get_istanbul_now()
    out = []
    for r in conn.execute("SELECT * FROM qr_displays WHERE active = 1 ORDER BY id ASC").fetchall():
        pair_expires = parse_to_istanbul(r["pair_expires_at"])
        out.append({
            "id": r["id"],
            "label": r["label"],
            "paired": bool(r["token_hash"]),
            "paired_at": r["paired_at"],
            "last_seen_at": r["last_seen_at"],
            "pairing_open": bool(r["pair_hash"] and pair_expires and now <= pair_expires),
            "created_at": r["created_at"],
        })
    return out

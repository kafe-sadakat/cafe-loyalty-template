"""
Panel kullanıcıları (müdür + kasiyer/personel): kullanıcı adı + parola ile giriş.

- `admin_users` tablosu iki rolü de tutar: role = 'manager' | 'staff'.
- Parolalar PBKDF2-SHA256 özeti olarak saklanır (düz metin asla).
- Müdür, Ayarlar ve Personel sekmelerinden kullanıcı adı/parolaları değiştirebilir; yeni personel ekleyebilir.
"""
from __future__ import annotations

import re
import sqlite3
from typing import Any, Optional

from app.db import log_audit
from app.services.staff import _DUMMY_HASH, hash_password, verify_password
from app.utils import format_phone, get_istanbul_now_iso, mask_phone

MIN_PASSWORD_LEN = 8
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,29}$")

# Eski sürümlerin kaynak kodunda yazılı olan (dolayısıyla depoyu gören herkesin bildiği) başlangıç parolaları.
# Artık hiçbir hesap bunlarla oluşturulmaz; yalnızca hâlâ bunları kullanan hesabı tespit edip parolayı değiştirtmek
# ve yeniden seçilmelerini engellemek için tutulur.
LEGACY_DEFAULT_PASSWORDS = frozenset({"Blvd2026!", "Kasa2026!"})
LEGACY_PASSWORD_MSG = "Bu parola herkesçe bilinen eski varsayılan paroladır; başka bir parola seçin."


def is_legacy_default_password(password: Optional[str]) -> bool:
    return (password or "") in LEGACY_DEFAULT_PASSWORDS


def normalize_username(raw: str) -> str:
    return (raw or "").strip().lower()


def _validate_username(username: str) -> Optional[str]:
    if not USERNAME_RE.match(username):
        return "Kullanıcı adı 3-30 karakter olmalı; yalnızca küçük harf, rakam, nokta, tire ve alt çizgi kullanılabilir."
    return None


def _public(row: sqlite3.Row) -> dict[str, Any]:
    phone = row["phone_e164"] or ""
    return {
        "id": row["id"],
        "username": row["username"],
        "name": row["display_name"] or row["username"],
        "role": row["role"],
        "phone": format_phone(phone) if phone else "",
        "masked_phone": mask_phone(phone) if phone else "",
        "active": bool(row["active"]),
        "created_at": row["created_at"],
        "created_by": row["created_by"],
        "last_login_at": row["last_login_at"],
    }


# ------------------------------------------------------------------ giriş
def authenticate_user(conn: sqlite3.Connection, username: str, password: str) -> Optional[dict[str, Any]]:
    """Kullanıcı adı + parola doğrular. Başarılıysa {'id','name','role','username'}; aksi halde None."""
    uname = normalize_username(username)
    row = conn.execute("SELECT * FROM admin_users WHERE username = ?", (uname,)).fetchone() if uname else None
    ok = verify_password(password or "", row["password_hash"] if row else _DUMMY_HASH)
    if not row or not ok or not row["active"]:
        return None
    conn.execute("UPDATE admin_users SET last_login_at = ? WHERE id = ?", (get_istanbul_now_iso(), row["id"]))
    return {
        "id": row["id"],
        "name": row["display_name"] or row["username"],
        "role": row["role"],
        "username": row["username"],
    }


# Oturumun hesabı hâlâ etkin mi / parolası değişti mi denetimi: app.auth._session_account_ok


# ------------------------------------------------------------------ personel yönetimi (müdür)
def list_staff_accounts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM admin_users WHERE role = 'staff' ORDER BY active DESC, display_name COLLATE NOCASE ASC"
    ).fetchall()
    return [_public(r) for r in rows]


def create_staff_account(
    conn: sqlite3.Connection, name: str, username: str, password: str, created_by: str = "mudur"
) -> tuple[bool, Optional[dict[str, Any]], Optional[str]]:
    name = (name or "").strip()
    uname = normalize_username(username)
    if not name or len(name) > 40:
        return False, None, "Personel adı zorunludur (en fazla 40 karakter)."
    if (err := _validate_username(uname)):
        return False, None, err
    if len(password or "") < MIN_PASSWORD_LEN:
        return False, None, f"Parola en az {MIN_PASSWORD_LEN} karakter olmalıdır."
    if is_legacy_default_password(password):
        return False, None, LEGACY_PASSWORD_MSG
    if conn.execute("SELECT 1 FROM admin_users WHERE username = ?", (uname,)).fetchone():
        return False, None, "Bu kullanıcı adı zaten kullanılıyor."
    cur = conn.execute(
        "INSERT INTO admin_users (username, password_hash, role, display_name, active, created_at, created_by) "
        "VALUES (?, ?, 'staff', ?, 1, ?, ?)",
        (uname, hash_password(password), name, get_istanbul_now_iso(), created_by[:40]),
    )
    log_audit(conn, created_by, "staff_account_created", f"Personel hesabı #{cur.lastrowid} ('{uname}') oluşturuldu.")
    row = conn.execute("SELECT * FROM admin_users WHERE id = ?", (cur.lastrowid,)).fetchone()
    return True, _public(row), None


def update_staff_account(
    conn: sqlite3.Connection, staff_id: int, password: Optional[str] = None, active: Optional[bool] = None,
    username: Optional[str] = None, name: Optional[str] = None, actor: str = "mudur",
) -> tuple[bool, Optional[dict[str, Any]], Optional[str]]:
    row = conn.execute("SELECT * FROM admin_users WHERE id = ? AND role = 'staff'", (staff_id,)).fetchone()
    if not row:
        return False, None, "Personel bulunamadı."
    if password and len(password) < MIN_PASSWORD_LEN:
        return False, None, f"Parola en az {MIN_PASSWORD_LEN} karakter olmalıdır."
    if password and is_legacy_default_password(password):
        return False, None, LEGACY_PASSWORD_MSG
    if username:
        uname = normalize_username(username)
        if uname != row["username"]:
            if (err := _validate_username(uname)):
                return False, None, err
            if conn.execute("SELECT 1 FROM admin_users WHERE username = ? AND id != ?", (uname, staff_id)).fetchone():
                return False, None, "Bu kullanıcı adı zaten kullanılıyor."
            conn.execute("UPDATE admin_users SET username = ? WHERE id = ?", (uname, staff_id))
            log_audit(conn, actor, "staff_username_changed", f"Personel #{staff_id} kullanıcı adı '{uname}' yapıldı.")
    if name is not None and name.strip():
        conn.execute("UPDATE admin_users SET display_name = ? WHERE id = ?", (name.strip()[:40], staff_id))
    if password:
        conn.execute("UPDATE admin_users SET password_hash = ? WHERE id = ?", (hash_password(password), staff_id))
        log_audit(conn, actor, "staff_password_reset", f"Personel #{staff_id} parolası sıfırlandı.")
    if active is not None:
        conn.execute("UPDATE admin_users SET active = ? WHERE id = ?", (1 if active else 0, staff_id))
        log_audit(conn, actor, "staff_account_toggled", f"Personel #{staff_id} {'etkinleştirildi' if active else 'devre dışı bırakıldı'}.")
    row = conn.execute("SELECT * FROM admin_users WHERE id = ?", (staff_id,)).fetchone()
    return True, _public(row), None


def delete_staff_account(conn: sqlite3.Connection, staff_id: int, actor: str = "mudur") -> tuple[bool, Optional[str]]:
    """Hesabı siler; geçmiş işlem günlükleri (isim kopyasıyla) denetim izi olarak KORUNUR."""
    row = conn.execute("SELECT username FROM admin_users WHERE id = ? AND role = 'staff'", (staff_id,)).fetchone()
    if not row:
        return False, "Personel bulunamadı."
    conn.execute("DELETE FROM admin_users WHERE id = ?", (staff_id,))
    log_audit(conn, actor, "staff_account_deleted", f"Personel #{staff_id} ('{row['username']}') silindi.")
    return True, None


# ------------------------------------------------------------------ müdürün kendi hesabı
def get_account(conn: sqlite3.Connection, user_id: int) -> Optional[dict[str, Any]]:
    row = conn.execute("SELECT * FROM admin_users WHERE id = ?", (user_id,)).fetchone()
    return _public(row) if row else None


def update_own_account(
    conn: sqlite3.Connection, user_id: int, current_password: str,
    new_username: Optional[str] = None, new_password: Optional[str] = None, new_name: Optional[str] = None,
) -> tuple[bool, Optional[dict[str, Any]], Optional[str]]:
    """Müdürün kendi kullanıcı adı / parolası / görünen adı. Mevcut parola doğrulanmadan değişiklik yapılmaz."""
    row = conn.execute("SELECT * FROM admin_users WHERE id = ? AND role = 'manager'", (user_id,)).fetchone()
    if not row:
        return False, None, "Hesap bulunamadı."
    if not verify_password(current_password or "", row["password_hash"]):
        return False, None, "Mevcut parola hatalı."
    if new_password and len(new_password) < MIN_PASSWORD_LEN:
        return False, None, f"Yeni parola en az {MIN_PASSWORD_LEN} karakter olmalıdır."
    if new_password and is_legacy_default_password(new_password):
        return False, None, LEGACY_PASSWORD_MSG
    if new_username:
        uname = normalize_username(new_username)
        if uname != row["username"]:
            if (err := _validate_username(uname)):
                return False, None, err
            if conn.execute("SELECT 1 FROM admin_users WHERE username = ? AND id != ?", (uname, user_id)).fetchone():
                return False, None, "Bu kullanıcı adı zaten kullanılıyor."
            conn.execute("UPDATE admin_users SET username = ? WHERE id = ?", (uname, user_id))
    if new_name is not None and new_name.strip():
        conn.execute("UPDATE admin_users SET display_name = ? WHERE id = ?", (new_name.strip()[:40], user_id))
    if new_password:
        conn.execute("UPDATE admin_users SET password_hash = ? WHERE id = ?", (hash_password(new_password), user_id))
    log_audit(conn, f"mudur:{row['username']}", "manager_account_updated", "Müdür hesabı (kullanıcı adı/parola) güncellendi.")
    return True, get_account(conn, user_id), None

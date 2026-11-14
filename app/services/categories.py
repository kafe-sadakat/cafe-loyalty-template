"""Menü kategorileri (müdür: ekle / yeniden adlandır / sırala / sil). Müşteri menüsünde sekme (chip) olarak görünür."""
from __future__ import annotations

import sqlite3
from typing import Any, Optional

from app.db import log_audit
from app.utils import get_istanbul_now_iso

FALLBACK_CATEGORY = "Genel"


def _clean_name(name: str) -> tuple[str, Optional[str]]:
    clean = " ".join((name or "").split())
    if not clean or len(clean) > 40:
        return "", "Kategori adı zorunludur (en fazla 40 karakter)."
    return clean, None


def list_categories(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT c.id, c.name, c.sort_order,
               (SELECT COUNT(*) FROM products p WHERE LOWER(p.category) = LOWER(c.name)) AS product_count
        FROM categories c ORDER BY c.sort_order ASC, c.id ASC
        """
    ).fetchall()
    return [dict(r) for r in rows]


def ensure_category(conn: sqlite3.Connection, name: str) -> str:
    """Ürüne atanan kategori listede yoksa sona eklenir; kayıtlı (büyük/küçük harf farkıyla) yazımı döner."""
    clean, err = _clean_name(name)
    if err:
        return FALLBACK_CATEGORY
    row = conn.execute("SELECT name FROM categories WHERE name = ? COLLATE NOCASE", (clean,)).fetchone()
    if row:
        return row["name"]
    nxt = conn.execute("SELECT COALESCE(MAX(sort_order), 0) + 10 AS n FROM categories").fetchone()["n"]
    conn.execute(
        "INSERT INTO categories (name, sort_order, created_at) VALUES (?, ?, ?)", (clean, nxt, get_istanbul_now_iso())
    )
    return clean


def create_category(conn: sqlite3.Connection, name: str, actor: str = "mudur") -> tuple[bool, Optional[dict[str, Any]], Optional[str]]:
    clean, err = _clean_name(name)
    if err:
        return False, None, err
    if conn.execute("SELECT 1 FROM categories WHERE name = ? COLLATE NOCASE", (clean,)).fetchone():
        return False, None, "Bu kategori zaten var."
    ensure_category(conn, clean)
    log_audit(conn, actor, "category_created", f"Kategori '{clean}' eklendi.")
    row = next(c for c in list_categories(conn) if c["name"] == clean)
    return True, row, None


def rename_category(conn: sqlite3.Connection, category_id: int, name: str, actor: str = "mudur") -> tuple[bool, Optional[dict[str, Any]], Optional[str]]:
    clean, err = _clean_name(name)
    if err:
        return False, None, err
    old = conn.execute("SELECT name FROM categories WHERE id = ?", (category_id,)).fetchone()
    if not old:
        return False, None, "Kategori bulunamadı."
    if conn.execute("SELECT 1 FROM categories WHERE name = ? COLLATE NOCASE AND id != ?", (clean, category_id)).fetchone():
        return False, None, "Bu adla başka bir kategori var."
    conn.execute("UPDATE categories SET name = ? WHERE id = ?", (clean, category_id))
    conn.execute("UPDATE products SET category = ? WHERE LOWER(category) = LOWER(?)", (clean, old["name"]))
    log_audit(conn, actor, "category_renamed", f"Kategori '{old['name']}' → '{clean}'.")
    row = next(c for c in list_categories(conn) if c["id"] == category_id)
    return True, row, None


def delete_category(conn: sqlite3.Connection, category_id: int, actor: str = "mudur") -> tuple[bool, Optional[str]]:
    """Kategoriyi siler; içindeki ürünler silinmez, 'Genel' kategorisine taşınır."""
    row = conn.execute("SELECT name FROM categories WHERE id = ?", (category_id,)).fetchone()
    if not row:
        return False, "Kategori bulunamadı."
    moved = conn.execute(
        "UPDATE products SET category = ? WHERE LOWER(category) = LOWER(?)", (FALLBACK_CATEGORY, row["name"])
    ).rowcount
    conn.execute("DELETE FROM categories WHERE id = ?", (category_id,))
    if moved:
        ensure_category(conn, FALLBACK_CATEGORY)
    log_audit(conn, actor, "category_deleted", f"Kategori '{row['name']}' silindi ({moved} ürün 'Genel'e taşındı).")
    return True, None


def reorder_categories(conn: sqlite3.Connection, ids: list[int], actor: str = "mudur") -> list[dict[str, Any]]:
    """Verilen kimlik sırasına göre sort_order yeniden yazılır (listede olmayanlar sona kalır)."""
    order = 10
    seen: set[int] = set()
    for cid in ids:
        if cid in seen:
            continue
        seen.add(cid)
        conn.execute("UPDATE categories SET sort_order = ? WHERE id = ?", (order, cid))
        order += 10
    for row in conn.execute("SELECT id FROM categories ORDER BY sort_order ASC, id ASC").fetchall():
        if row["id"] not in seen:
            conn.execute("UPDATE categories SET sort_order = ? WHERE id = ?", (order, row["id"]))
            order += 10
    log_audit(conn, actor, "categories_reordered", "Kategori sırası güncellendi.")
    return list_categories(conn)

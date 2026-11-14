"""Menü / ürün yönetimi (müdür paneli). Fiyatlar yalnızca bilgilendirme amaçlıdır; online ödeme yoktur."""
from __future__ import annotations

import sqlite3
from typing import Any, Optional

from app.db import log_audit
from app.services.categories import ensure_category
from app.services.uploads import UploadError, delete_upload, normalize_image_ref
from app.utils import get_istanbul_now_iso

_COLUMNS = (
    "id, name, description, price, category, image_url, active, sort_order, created_at, updated_at, "
    "is_new, discount_price, discount_label"
)


def list_products_admin(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"""
        SELECT {_COLUMNS}
        FROM products ORDER BY category COLLATE NOCASE ASC, sort_order ASC, id ASC
        """
    ).fetchall()
    return [dict(r) for r in rows]


def _clean(
    name: str, description: str, price: Any, category: str, image_url: str,
    discount_price: Any = None, discount_label: str = "",
) -> tuple[dict[str, Any], Optional[str]]:
    name = (name or "").strip()
    if not name or len(name) > 80:
        return {}, "Ürün adı zorunludur (en fazla 80 karakter)."
    try:
        price_val = round(float(str(price).replace(",", ".")), 2)
    except (TypeError, ValueError):
        return {}, "Geçerli bir fiyat giriniz."
    if price_val < 0 or price_val > 100000:
        return {}, "Fiyat 0 ile 100.000 arasında olmalıdır."

    # İndirimli fiyat: boş/0 ise indirim yok. Doluysa normal fiyattan KÜÇÜK olmalıdır.
    discount_val: Optional[float] = None
    if discount_price not in (None, "", 0, "0"):
        try:
            discount_val = round(float(str(discount_price).replace(",", ".")), 2)
        except (TypeError, ValueError):
            return {}, "Geçerli bir indirimli fiyat giriniz."
        if discount_val <= 0:
            discount_val = None
        elif discount_val >= price_val:
            return {}, "İndirimli fiyat normal fiyattan düşük olmalıdır."

    try:
        image = normalize_image_ref(image_url)
    except UploadError as exc:
        return {}, str(exc)
    return {
        "name": name,
        "description": (description or "").strip()[:500],
        "price": price_val,
        "category": ((category or "").strip() or "Genel")[:40],
        "image_url": image or None,
        "discount_price": discount_val,
        "discount_label": (discount_label or "").strip()[:30] if discount_val is not None else "",
    }, None


def create_product(
    conn: sqlite3.Connection, name: str, description: str, price: Any, category: str, image_url: str = "",
    active: int = 1, sort_order: int = 0, is_new: int = 0, discount_price: Any = None, discount_label: str = "",
) -> tuple[bool, Optional[dict[str, Any]], Optional[str]]:
    data, err = _clean(name, description, price, category, image_url, discount_price, discount_label)
    if err:
        return False, None, err
    data["category"] = ensure_category(conn, data["category"])
    now = get_istanbul_now_iso()
    cur = conn.execute(
        """
        INSERT INTO products (name, description, price, category, image_url, active, sort_order, created_at, updated_at,
                              is_new, discount_price, discount_label)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (data["name"], data["description"], data["price"], data["category"], data["image_url"],
         1 if active else 0, int(sort_order or 0), now, now,
         1 if is_new else 0, data["discount_price"], data["discount_label"]),
    )
    log_audit(conn, "admin", "product_created", f"Ürün #{cur.lastrowid} ('{data['name']}') eklendi.")
    row = conn.execute("SELECT * FROM products WHERE id = ?", (cur.lastrowid,)).fetchone()
    return True, dict(row), None


def update_product(
    conn: sqlite3.Connection, product_id: int, name: str, description: str, price: Any, category: str,
    image_url: str = "", active: int = 1, sort_order: int = 0, is_new: int = 0,
    discount_price: Any = None, discount_label: str = "",
) -> tuple[bool, Optional[dict[str, Any]], Optional[str]]:
    existing = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    if not existing:
        return False, None, "Ürün bulunamadı."
    data, err = _clean(name, description, price, category, image_url, discount_price, discount_label)
    if err:
        return False, None, err
    data["category"] = ensure_category(conn, data["category"])
    conn.execute(
        """
        UPDATE products SET name = ?, description = ?, price = ?, category = ?, image_url = ?,
               active = ?, sort_order = ?, updated_at = ?, is_new = ?, discount_price = ?, discount_label = ?
        WHERE id = ?
        """,
        (data["name"], data["description"], data["price"], data["category"], data["image_url"],
         1 if active else 0, int(sort_order or 0), get_istanbul_now_iso(),
         1 if is_new else 0, data["discount_price"], data["discount_label"], product_id),
    )
    if existing["image_url"] and existing["image_url"] != data["image_url"]:
        _delete_if_unreferenced(conn, existing["image_url"])
    log_audit(conn, "admin", "product_updated", f"Ürün #{product_id} ('{data['name']}') güncellendi.")
    row = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    return True, dict(row), None


def delete_product(conn: sqlite3.Connection, product_id: int) -> tuple[bool, Optional[str]]:
    row = conn.execute("SELECT name, image_url FROM products WHERE id = ?", (product_id,)).fetchone()
    if not row:
        return False, "Ürün bulunamadı."
    conn.execute("DELETE FROM products WHERE id = ?", (product_id,))
    if row["image_url"]:
        _delete_if_unreferenced(conn, row["image_url"])
    log_audit(conn, "admin", "product_deleted", f"Ürün #{product_id} ('{row['name']}') silindi.")
    return True, None


def _delete_if_unreferenced(conn: sqlite3.Connection, ref: str) -> None:
    """Görsel başka bir üründe/kampanyada kullanılmıyorsa dosyayı diskten de siler."""
    used = conn.execute(
        "SELECT (SELECT COUNT(*) FROM products WHERE image_url = ?) + (SELECT COUNT(*) FROM campaigns WHERE image_url = ?) AS c",
        (ref, ref),
    ).fetchone()["c"]
    if not used:
        delete_upload(ref)

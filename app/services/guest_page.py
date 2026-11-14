"""
ADR-40: Misafir sayfası (/misafir) — sosyal medya profilindeki bağlantı için menü + kampanyalar.

Damga, kayıt, giriş yoktur; çerez yazmaz. Uygulama gibi görünmez: manifest, yükleme penceresi, alt menü, servis
çalışanı yüklenmez; sunucuda çizilen sade bir web sayfasıdır (Instagram / TikTok uygulama içi tarayıcıda hızlı açılır).
Veri müşteri uygulamasıyla aynı kaynaktan gelir. Ödüllü görevler (reward_stamps > 0) damga işi olduğundan gösterilmez.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from app.config import DEFAULT_BUSINESS_NAME
from app.db import get_all_settings
from app.services.loyalty import get_active_campaigns, get_public_categories, get_public_products


def asset_path(url: str) -> str:
    """Yüklenmiş görselin uygulama köküne göre göreli yolu (müşteri uygulamasındaki imgSrc kuralı).
    Dış adres / data: / javascript: kabul edilmez (yerel görseller dışında istek yok)."""
    u = (url or "").strip()
    if not u or "://" in u or ":" in u.split("/")[0]:
        return ""
    u = u.lstrip("/")
    if u.startswith("k/"):
        u = u[2:]
    return u


def format_price(value: Any) -> str:
    """125 → '125 ₺', 127.5 → '127,50 ₺', 1250 → '1.250 ₺'. Sıfır / geçersiz fiyat boş döner."""
    try:
        n = round(float(value or 0), 2)
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    s = f"{n:,.0f}" if n == int(n) else f"{n:,.2f}"
    return s.replace(",", "\x00").replace(".", ",").replace("\x00", ".") + " ₺"


def _product(p: dict[str, Any]) -> dict[str, Any]:
    try:
        price = float(p.get("price") or 0)
        sale = float(p.get("discount_price") or 0)
    except (TypeError, ValueError):
        price, sale = 0.0, 0.0
    on_sale = 0 < sale < price
    return {
        "name": p.get("name") or "",
        "description": (p.get("description") or "").strip(),
        "price": format_price(sale if on_sale else price),
        "old_price": format_price(price) if on_sale else "",
        "sale_label": (p.get("discount_label") or "").strip() if on_sale else "",
        "is_new": bool(p.get("is_new")),
        "image": asset_path(p.get("image_url") or ""),
    }


def build_guest_page(conn: sqlite3.Connection) -> dict[str, Any]:
    settings = get_all_settings(conn)
    business_name = (settings.get("business_name") or "").strip() or DEFAULT_BUSINESS_NAME

    campaigns = [
        {
            "title": c.get("title") or "",
            "description": (c.get("description") or "").strip(),
            "badge": (c.get("badge") or "").strip(),
            "image": asset_path(c.get("image_url") or ""),
        }
        for c in get_active_campaigns(conn)
        if int(c.get("reward_stamps") or 0) <= 0   # ödüllü görev = damga işi (giriş ister) → misafirde yok
    ]

    groups: dict[str, dict[str, Any]] = {}
    for p in get_public_products(conn):
        name = (p.get("category") or "").strip() or "Genel"
        groups.setdefault(name.lower(), {"name": name, "products": []})["products"].append(_product(p))
    order = [str(c).strip().lower() for c in get_public_categories(conn)]
    keys = [k for k in dict.fromkeys(order) if k in groups] + [k for k in groups if k not in order]
    sections = [{"id": f"kat-{i + 1}", **groups[k]} for i, k in enumerate(keys)]

    logo = asset_path(settings.get("logo_url") or "")
    return {
        "business_name": business_name,
        "campaigns": campaigns,
        "sections": sections,
        "menu_note": (settings.get("menu_note") or "").strip(),
        "logo_src": logo.replace("logo-512", "logo-192") if logo else "static/img/icon-192.png",
        "og_image_path": logo if logo else "static/img/icon-512.png",
    }

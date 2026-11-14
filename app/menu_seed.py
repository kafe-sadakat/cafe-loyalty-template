"""
Örnek kafe menüsü (varsayılan tohumlama, panelden düzenlenebilir).

`seed_menu_once()` YALNIZCA bir kez çalışır (settings.menu_seeded_v1 bayrağı): menüdeki ürünler adı daha önce
yoksa eklenir. Sonrasında müdür panelinden yapılan her değişiklik (silme, fiyat, görsel) korunur; ürünler
bir daha geri eklenmez. Büyük boy fiyatı açıklamaya yazılır (ürün tablosu tek fiyat tutar: standart boy).
Tatlı (cup) fotoğrafları için adı/fiyatı henüz belirlenmemiş TASLAK ürünler GİZLİ (menüde görünmez) eklenir.
"""
from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import app.config as app_config
from app.utils import get_istanbul_now_iso

SEED_FLAG = "menu_seeded_v1"
SEED_IMG_DIR = Path(__file__).resolve().parent / "static" / "img" / "menu-seed"

# (ad, standart fiyat, büyük fiyat | None, açıklama)
MENU: dict[str, list[tuple[str, int, int | None, str]]] = {
    "Espresso Bar": [
        ("Espresso", 130, None, "Yoğun aromalı klasik İtalyan espresso."),
        ("Americano", 170, None, "Çift shot espresso."),
        ("Espresso Lungo", 170, None, "Uzun çekimli, güçlü ve yoğun tat."),
        ("Filter Coffee", 170, None, "Demleme premium lezzet."),
        ("Filter Coffee with Milk", 195, None, "Espresso ve sıcak süt ile hazırlanır."),
        ("Con Panna", 145, None, "Espresso üzerine krema."),
    ],
    "Milk Coffee": [
        ("Cappuccino", 195, None, "Kahvenin köpüğü ile klasik İtalyan lezzeti."),
        ("Caffè Latte", 195, None, "Bol sütlü, yumuşak içimli kahve."),
        ("Mocha", 215, None, "Gerçek çikolatanın yoğun aroması."),
        ("Flat White", 195, None, "Yoğun espresso ve ince dokulu süt."),
        ("Angel White Mocha", 205, None, "Beyaz çikolata ve espresso uyumu."),
        ("Caramel Macchiato", 215, None, "Karamel soslu espresso."),
        ("Tiramisu Latte", 245, None, "İtalyan tatlısı espresso ile buluştu."),
        ("Pumpkin Spice Latte", 245, None, "Balkabağı baharatlarının doygun yorumu."),
        ("Salted Caramel Latte", 245, None, "Karamelin en iddialı hali."),
        ("Popcorn Latte", 245, None, "Patlamış mısır aromasıyla hazırlanan nostaljik lezzet."),
        ("Matcha Latte", 245, None, "Asya'nın binlerce yıllık çay seremonisi bardağınızda."),
        ("Ube Latte", 245, None, "Filipinler'den gelen büyüleyici mor lezzet."),
    ],
    "Iced Coffee": [
        ("Iced Americano", 185, 200, "Saf espresso ve buz gibi suyun ferahlatıcı buluşması."),
        ("Iced Latte", 205, 220, "Yoğun espressonun soğuk süt ve buzla pürüzsüz uyumu."),
        ("Iced Dirty Matcha Latte", 250, 265, "Asya'nın dinginliği, İtalya'nın enerjisiyle buluştu."),
        ("Iced Dirty Ube Latte", 250, 265, "Vanilya ve kurabiye notalarıyla Filipinler'den gelen büyüleyici lezzet."),
        ("Ice Pumpkin Spice Latte", 250, 265, "Balkabağı baharatların ferahlatıcı yorumu."),
        ("Iced Tiramisu Latte", 250, 265, "İtalyan tatlısının en kahve sever hali."),
        ("Iced Salted Caramel Latte", 250, 265, "Tuzlu karamel ile benzersiz tat."),
        ("Iced Pistachio Cream Latte", 250, 275, "Antep fıstığının yeşil ve tatlı notalarıyla taçlanıyor."),
        ("Ice Mocha", 245, 260, "Tatlı bir serinlik arayanlara."),
        ("Ice Filter Coffee", 185, 200, "Soğuk demleme, yumuşak içim."),
        ("Iced Biscoff Latte", 250, 265, "Gerçek Lotus Biscoff ezmesinin zengin dokusuyla."),
    ],
    "Dessert Coffee": [
        ("Affogato", 240, None, "Vanilyalı dondurma üzerine sıcak espresso."),
        ("Caramel Hazelnut Affogato", 260, None, "Karamel sos ve kavrulmuş fındık eşliğinde sunulur."),
        ("Biscoff Affogato", 260, None, "Biscoff aroması ve espresso uyumu."),
        ("Affogato al Pistachio", 260, None, "Antep fıstıklı dondurma ve sıcak espresso."),
    ],
    "Signature": [
        ("House Signature Latte", 245, 265, "Vanilya, karamel ve çift espresso ile özel tarif."),
        ("Kuzu Kulağı", 235, 255, "Kuzu kulağının kendine has hafif mayhoş notalarıyla sizlerle."),
        ("Green Detox", 235, 260, "Doğadan bardağa taptaze bir enerji. Şekersiz ve hafif."),
        ("Aşkolate", 235, 245, "Kalbinizi çalacak tatlı bir espresso molası."),
        ("Shakerato", 235, 245, "İtalyan usulü buzlu espresso."),
        ("Lemon Shakerato", 235, 245, "Limon ve şeker ile ferahlatıcı dokunuş."),
    ],
    "Mocktails": [
        ("Cool Lime", 235, 250, "Lime, taze nane ve soda ile hazırlanan canlı ve serinletici kokteyl."),
        ("Berry Hibiscus", 235, 250, "Orman meyveleri, hibiskus ve ferahlatıcı dokunuşların dengeli buluşması."),
        ("Mürver", 235, 250, "Mürver çiçeği, limon ve soda ile hazırlanan zarif, aromatik ve ferah bir kokteyl."),
        ("Orange Mango", 235, 250, "Taze portakal ve mango aromalarının tropikal, tatlı ve ferah uyumu."),
        ("Nektarin Reyhan", 235, 250, "Nektarinin tatlılığı, reyhan ve ferahlatıcı dokunuşuyla."),
        ("Velvet Berry", 235, 250, "Yoğun orman meyveleri ve biberiyenin sofistike dokunuşu."),
        ("Strawberry Basil", 235, 250, "Taze çilek, reyhan ve limonun aromatik ve ferahlatıcı buluşması."),
        ("Peach Blossom", 235, 250, "Şeftali, zencefil ve limonun hafif baharatlı, ferah ve dengeli karışımı."),
        ("Skylime", 235, 250, "Turunçgil, lime, taze nane ve ferahlatıcı soda ile hazırlanan özgün kokteyl."),
    ],
    "Frozen": [(n, 240, 255, "") for n in (
        "Mango Frozen", "Peach Frozen", "Strawberry Frozen", "Blackberry Frozen", "Green Apple Frozen")],
    "Frappe": [(n, 240, 255, "") for n in (
        "Classic Frappe", "Vanilla Frappe", "Strawberry Frappe", "Popcorn Frappe", "Pumpkin Spice Frappe",
        "Matcha Frappe", "Biscoff Frappe", "Salted Caramel Frappe")],
    "Milkshakes": [(n, 230, 245, "") for n in ("Vanilya Milkshake", "Chocolate Milkshake", "Strawberry Milkshake")],
    "Alaturka": [
        ("Türk Kahvesi", 140, None, ""),
        ("Double Türk Kahvesi", 195, None, ""),
        ("Çay", 60, None, ""),
        ("Fincan Çay", 70, None, ""),
        ("Limonata", 165, None, ""),
        ("Churchill", 125, None, ""),
        ("Soda", 60, None, ""),
        ("Soda Meyveli", 70, None, ""),
        ("Premium İçecek", 140, None, ""),
        ("Kutu İçecek", 110, None, ""),
        ("Su", 60, None, ""),
        ("Bir Top Dondurma", 50, None, ""),
    ],
    "Extra": [
        ("Ek Espresso Shot", 30, None, ""),
        ("Bitkisel Süt Değişimi", 35, None, ""),
        ("Extra Aromalar", 35, None, ""),
    ],
}

# Fotoğrafı olan tatlılar (cup): adı/fiyatı müdür panelden netleştirene kadar GİZLİ taslak.
DESSERT_DRAFTS = [
    ("Çikolata & Karamel Cup Tatlı", "cup-cikolata.jpg"),
    ("Yaban Mersinli Cup Tatlı", "cup-yabanmersini.jpg"),
    ("Antep Fıstıklı Cup Tatlı", "cup-fistik.jpg"),
    ("Red Velvet Cup Tatlı", "cup-redvelvet.jpg"),
    ("Lotus Karamelli Cup Tatlı", "cup-lotus.jpg"),
]


def _copy_seed_image(filename: str) -> str | None:
    """Paketle gelen tohum görselini upload klasörüne kopyalar; DB'ye yazılacak `static/uploads/...` yolunu döner."""
    src = SEED_IMG_DIR / filename
    if not src.is_file():
        return None
    upload_dir = Path(app_config.UPLOAD_DIR)
    upload_dir.mkdir(parents=True, exist_ok=True)
    dst = upload_dir / f"seed-{filename}"
    if not dst.exists():
        shutil.copyfile(src, dst)
    return f"static/uploads/seed-{filename}"


def seed_menu_once(conn: sqlite3.Connection) -> int:
    """Menüyü bir kez ekler. Eklenen ürün sayısını döner (bayrak varsa 0)."""
    if conn.execute("SELECT 1 FROM settings WHERE key = ?", (SEED_FLAG,)).fetchone():
        return 0
    now = get_istanbul_now_iso()
    added = 0
    order = 0
    for category, items in MENU.items():
        for name, price, big, desc in items:
            order += 1
            if conn.execute("SELECT 1 FROM products WHERE name = ? COLLATE NOCASE", (name,)).fetchone():
                continue
            text = (desc + (" " if desc else "") + (f"Büyük boy: {big} ₺." if big else "")).strip()
            conn.execute(
                """
                INSERT INTO products (name, description, price, category, image_url, active, sort_order, created_at, updated_at)
                VALUES (?, ?, ?, ?, NULL, 1, ?, ?, ?)
                """,
                (name, text, float(price), category, order, now, now),
            )
            added += 1
    for i, (name, fname) in enumerate(DESSERT_DRAFTS, start=1):
        if conn.execute("SELECT 1 FROM products WHERE name = ? COLLATE NOCASE", (name,)).fetchone():
            continue
        conn.execute(
            """
            INSERT INTO products (name, description, price, category, image_url, active, sort_order, created_at, updated_at)
            VALUES (?, 'TASLAK: adı ve fiyatı belirleyip "Menüde göster"i işaretleyin.', 0, 'Tatlılar', ?, 0, ?, ?, ?)
            """,
            (name, _copy_seed_image(fname), 100 + i, now, now),
        )
        added += 1
    conn.execute("INSERT INTO settings (key, value) VALUES (?, '1')", (SEED_FLAG,))
    return added

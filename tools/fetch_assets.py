"""
Yerelleştirilmiş statik varlıkları (font + QR kütüphanesi) indirir.

Amaç (KVKK / yurt dışı aktarım): Çalışma zamanında tarayıcının fonts.googleapis.com,
fonts.gstatic.com, unpkg.com vb. yurt dışı sunuculara istek atmaması için tüm varlıklar
BU betikle geliştirme makinesinde bir kez indirilir ve repoya eklenir. Sunucuda ve
ziyaretçi tarayıcısında hiçbir harici istek yapılmaz.

Kimlik:
  app/static/fonts/*.woff2        (EB Garamond, Hanken Grotesk, Newsreader, Plus Jakarta Sans, Material Symbols alt kümesi)
  app/static/css/fonts.css        (@font-face tanımları, yalnızca yerel yollar)
  app/static/js/vendor/html5-qrcode.min.js

Kullanım:  python tools/fetch_assets.py
(Şablonlara yeni bir Material Symbols ikonu eklenince tekrar çalıştırılmalıdır.)
"""
from __future__ import annotations

import hashlib
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "app" / "static"
TEMPLATES = ROOT / "app" / "templates"
FONTS_DIR = STATIC / "fonts"
VENDOR_DIR = STATIC / "js" / "vendor"
CSS_OUT = STATIC / "css" / "fonts.css"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
# Türkçe karakterler için yeterli alt kümeler (latin + latin-ext)
KEEP_SUBSETS = {"latin", "latin-ext"}

# Kullanıcı uygulaması (PWA): Newsreader + Plus Jakarta Sans
# Yönetim panelleri: EB Garamond + Hanken Grotesk
TEXT_FONT_QUERY = (
    "https://fonts.googleapis.com/css2?"
    "family=Newsreader:ital,opsz,wght@0,6..72,400..700;1,6..72,400..600"
    "&family=Plus+Jakarta+Sans:wght@400;500;600;700"
    "&family=EB+Garamond:ital,wght@0,400..800;1,400..800"
    "&family=Hanken+Grotesk:wght@100..900&display=swap"
)
ICON_FONT_QUERY = (
    "https://fonts.googleapis.com/css2?"
    "family=Material+Symbols+Outlined:wght,FILL@100..700,0..1"
    "&icon_names={names}&display=block"
)
QR_LIB_URL = "https://unpkg.com/html5-qrcode@2.3.8/html5-qrcode.min.js"

# Şablonlarda dinamik (JS içinde) kullanılan ve regex ile yakalanamayan ikonlar
EXTRA_ICONS = {
    "check_circle", "error", "info", "verified", "close", "download",
    "admin_panel_settings", "point_of_sale",
    "schedule", "location_on", "call", "photo_camera", "public", "language",
    "light_mode", "dark_mode", "lock", "badge", "settings", "install_mobile",
    # Panel JS parçalarında (innerHTML/textContent) kullanılan ikonlar
    "local_cafe", "card_giftcard", "redeem", "celebration", "add_circle",
    "remove_circle", "person_add", "person", "phone_iphone", "qr_code_scanner",
    "trending_up", "workspace_premium", "approval", "bolt", "speed",
    "how_to_reg", "receipt_long", "shield", "lock_reset", "person_off",
    "verified_user", "warning", "key", "edit", "delete", "sync",
    # İçleme(structure) duruma göre JS'te üretilen güvence ikonları
    "block", "check",
}


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def collect_icon_names() -> list[str]:
    names: set[str] = set(EXTRA_ICONS)
    ident = r"[a-z][a-z0-9_]*"
    for tpl in TEMPLATES.glob("*.html"):
        text = tpl.read_text(encoding="utf-8")
        # <span class="material-symbols-outlined ...">icon_name</span>
        for m in re.finditer(
            r'class="[^"]*material-symbols-outlined[^"]*"[^>]*>\s*(' + ident + r")\s*<", text
        ):
            names.add(m.group(1))
        # data-icon="name"  /  icon: "name"  /  icon.textContent = "name"
        for m in re.finditer(r'data-icon="(' + ident + r')"', text):
            names.add(m.group(1))
        for m in re.finditer(r'(?:icon|iconName)\s*[:=]\s*["\'](' + ident + r')["\']', text):
            names.add(m.group(1))
        for m in re.finditer(r'icon\.textContent\s*=\s*["\'](' + ident + r')["\']', text):
            names.add(m.group(1))
    return sorted(names)


def split_font_faces(css: str) -> list[tuple[str, str]]:
    """(subset_adı, @font-face bloğu) çiftlerini döndürür."""
    out = []
    for m in re.finditer(r"/\*\s*([a-z-]+)\s*\*/\s*(@font-face\s*\{.*?\})", css, re.S):
        out.append((m.group(1), m.group(2)))
    return out


def download_text_fonts() -> str:
    css = fetch(TEXT_FONT_QUERY).decode("utf-8")
    blocks = []
    for subset, block in split_font_faces(css):
        if subset not in KEEP_SUBSETS:
            continue
        url = re.search(r"url\((https://[^)]+)\)", block).group(1)
        data = fetch(url)
        family = re.search(r"font-family:\s*'([^']+)'", block).group(1).lower().replace(" ", "-")
        style = re.search(r"font-style:\s*(\w+)", block).group(1)
        fname = f"{family}-{style}-{subset}.woff2"
        (FONTS_DIR / fname).write_bytes(data)
        block = re.sub(r"url\(https://[^)]+\)", f"url(../fonts/{fname})", block)
        blocks.append(block)
        print(f"  + {fname} ({len(data)} bayt)")
    return "\n".join(blocks)


def download_icon_font(names: list[str]) -> str:
    css = fetch(ICON_FONT_QUERY.format(names=",".join(names))).decode("utf-8")
    url = re.search(r"url\((https://[^)]+)\)", css).group(1)
    data = fetch(url)
    fname = "material-symbols-outlined.woff2"
    (FONTS_DIR / fname).write_bytes(data)
    print(f"  + {fname} ({len(data)} bayt, {len(names)} ikon)")
    return (
        "@font-face {\n"
        "  font-family: 'Material Symbols Outlined';\n"
        "  font-style: normal;\n"
        "  font-weight: 100 700;\n"
        "  font-display: block;\n"
        f"  src: url(../fonts/{fname}) format('woff2');\n"
        "}\n"
        ".material-symbols-outlined {\n"
        "  font-family: 'Material Symbols Outlined';\n"
        "  font-weight: normal;\n"
        "  font-style: normal;\n"
        "  font-size: 24px;\n"
        "  line-height: 1;\n"
        "  letter-spacing: normal;\n"
        "  text-transform: none;\n"
        "  display: inline-block;\n"
        "  white-space: nowrap;\n"
        "  word-wrap: normal;\n"
        "  direction: ltr;\n"
        "  -webkit-font-feature-settings: 'liga';\n"
        "  font-feature-settings: 'liga';\n"
        "  -webkit-font-smoothing: antialiased;\n"
        "}\n"
    )


def download_qr_lib() -> None:
    VENDOR_DIR.mkdir(parents=True, exist_ok=True)
    data = fetch(QR_LIB_URL)
    dest = VENDOR_DIR / "html5-qrcode.min.js"
    dest.write_bytes(data)
    print(f"  + {dest.relative_to(ROOT)} ({len(data)} bayt, sha256={hashlib.sha256(data).hexdigest()[:16]})")


def main() -> int:
    FONTS_DIR.mkdir(parents=True, exist_ok=True)
    CSS_OUT.parent.mkdir(parents=True, exist_ok=True)

    print("Metin fontları indiriliyor...")
    text_css = download_text_fonts()

    names = collect_icon_names()
    print(f"Material Symbols alt kümesi indiriliyor ({len(names)} ikon)...")
    icon_css = download_icon_font(names)

    CSS_OUT.write_text(
        "/* Otomatik üretildi: python tools/fetch_assets.py — yalnızca yerel (self-hosted) fontlar */\n"
        + text_css
        + "\n"
        + icon_css,
        encoding="utf-8",
    )
    print(f"  + {CSS_OUT.relative_to(ROOT)}")

    print("QR okuyucu kütüphanesi indiriliyor...")
    download_qr_lib()
    print("Tamam.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

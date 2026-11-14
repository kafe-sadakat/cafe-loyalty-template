"""
Varsayılan PWA / bildirim ikonlarını ve panel logosunu (PNG) üretir.
Kullanım: python tools/make_icons.py

Üretilenler (app/static/img/):
  - icon-192.png, icon-512.png (any)
  - icon-maskable-512.png (güvenli bölgeli maskable ikon)
  - badge-96.png (tek renkli beyaz Android bildirim rozeti)
  - logo.png (yönetim / kasa paneli üst bar logosu)
"""
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
OUT = HERE.parent / "app" / "static" / "img"
S = 1024
CREAM = (252, 249, 248, 255)
BRAND_GREEN = (30, 77, 63, 255)
WHITE = (255, 255, 255, 255)


def _draw_cup_glyph(size: int, color: tuple[int, int, int, int]) -> Image.Image:
    """1024x1024 kanvas üzerinde yüksek çözünürlüklü minimalist kahve fincanı çizer."""
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(canvas)
    sc = size / 512.0

    def p(x: float, y: float) -> tuple[float, float]:
        return (x * sc, y * sc)

    # Buhar çizgileri (3 yumuşak S-eğrisi)
    sw = max(2, int(round(14 * sc)))
    r = sw / 2.0
    for cx, top_y, bot_y in ((186, 122, 174), (238, 102, 170), (290, 122, 174)):
        pts = []
        steps = 24
        for i in range(steps + 1):
            t = i / steps
            # Tek sinüs dalgası: pürüzsüz dikey S-eğrisi
            import math
            x = (cx + math.sin(t * math.pi * 2) * 7.5) * sc
            y = (top_y + (bot_y - top_y) * t) * sc
            pts.append((x, y))
        for a, b in zip(pts[:-1], pts[1:]):
            d.line([a, b], fill=color, width=sw)
        for px, py in pts:
            d.ellipse((px - r, py - r, px + r, py + r), fill=color)

    # Fincan gövdesi (yuvarlatılmış alt)
    d.rounded_rectangle(
        (132 * sc, 200 * sc, 344 * sc, 372 * sc),
        radius=int(72 * sc),
        fill=color,
    )
    # Üst dudak düzeltmesi
    d.rectangle((132 * sc, 200 * sc, 344 * sc, 272 * sc), fill=color)

    # Kulp (sağda halka)
    handle_box = (318 * sc, 218 * sc, 398 * sc, 314 * sc)
    d.arc(handle_box, start=-90, end=90, fill=color, width=max(3, int(round(20 * sc))))
    # Kulp iç düzeltme kalınlığı için elips halkası
    inner = (328 * sc, 234 * sc, 382 * sc, 298 * sc)
    d.pieslice(handle_box, start=-90, end=90, fill=color)
    # Kulp boşluğunu temizle (saydam kesim)
    mask_cut = Image.new("L", (size, size), 255)
    mc = ImageDraw.Draw(mask_cut)
    mc.pieslice(inner, start=-90, end=90, fill=0)
    # Kulp içini saydamlaştır; fincan gövdesini koru
    r_ch, g_ch, b_ch, a_ch = canvas.split()
    keep_cup = Image.new("L", (size, size), 0)
    ImageDraw.Draw(keep_cup).rectangle((132 * sc, 200 * sc, 344 * sc, 372 * sc), fill=255)
    from PIL import ImageChops
    final_alpha = ImageChops.multiply(a_ch, ImageChops.lighter(mask_cut, keep_cup))
    canvas = Image.merge("RGBA", (r_ch, g_ch, b_ch, final_alpha))
    d = ImageDraw.Draw(canvas)

    # Tabak (altlık)
    d.rounded_rectangle(
        (106 * sc, 392 * sc, 370 * sc, 426 * sc),
        radius=int(17 * sc),
        fill=color,
    )
    return canvas


def _paste_center(canvas: Image.Image, layer: Image.Image) -> None:
    canvas.alpha_composite(layer, ((canvas.width - layer.width) // 2, (canvas.height - layer.height) // 2))


def make_app_icon() -> Image.Image:
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(img).rounded_rectangle((0, 0, S - 1, S - 1), radius=int(S * 0.22), fill=CREAM)
    cup = _draw_cup_glyph(int(S * 0.72), BRAND_GREEN)
    _paste_center(img, cup)
    return img


def make_maskable_icon() -> Image.Image:
    """Tam kenarlı zemin; içerik güvenli bölgede (merkez %56) kalır — Android her şekle kırpabilir."""
    img = Image.new("RGBA", (S, S), CREAM)
    cup = _draw_cup_glyph(int(S * 0.56), BRAND_GREEN)
    _paste_center(img, cup)
    return img


def make_badge() -> Image.Image:
    """Android durum çubuğu rozeti: yalnızca alfa kanalı kullanılır (beyaz siluet)."""
    return _draw_cup_glyph(S, WHITE)


def make_logo() -> Image.Image:
    """Panel üst çubuğu için saydam arka planlı marka fincan simgesi."""
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(img).rounded_rectangle((48, 48, S - 49, S - 49), radius=int(S * 0.24), fill=BRAND_GREEN)
    cup = _draw_cup_glyph(int(S * 0.68), WHITE)
    _paste_center(img, cup)
    return img


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    icon = make_app_icon()
    for size in (512, 192):
        icon.resize((size, size), Image.LANCZOS).save(OUT / f"icon-{size}.png", optimize=True)
    make_maskable_icon().resize((512, 512), Image.LANCZOS).save(OUT / "icon-maskable-512.png", optimize=True)
    make_badge().resize((96, 96), Image.LANCZOS).save(OUT / "badge-96.png", optimize=True)
    make_logo().resize((256, 256), Image.LANCZOS).save(OUT / "logo.png", optimize=True)
    print("İkonlar üretildi:", sorted(p.name for p in OUT.glob("*.png")))

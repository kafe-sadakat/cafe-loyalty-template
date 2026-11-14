"""
Yönetici görsel yüklemeleri (ürün fotoğrafı, kampanya görseli, kafe logosu).

Güvenlik: dosya içeriği Pillow ile gerçekten görüntü mü diye doğrulanır, EXIF/meta veri
atılarak yeniden kodlanır, dosya adı rastgele üretilir (kullanıcı adı yola karışmaz).
Veritabanında yol `static/uploads/<ad>` biçiminde (başında '/' olmadan) saklanır; böylece
hem `/` hem `/k/` alt yolunda çalışır.
"""
from __future__ import annotations

import io
import re
import uuid
from pathlib import Path
from typing import Optional

from PIL import Image, ImageOps

import app.config as app_config

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
Image.MAX_IMAGE_PIXELS = 30_000_000

ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP"}
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
# Gerçek dosya imzası (magic bytes): uzantı/Content-Type yalan söylese de içerik bu imzalardan biri olmalı.
_MAGIC_PREFIXES = (b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n")
UPLOAD_PREFIX = "static/uploads/"
_UPLOAD_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,150}$")
LOGO_SIZES = {"logo-512.png": 512, "logo-192.png": 192}
BADGE_NAME = "badge-96.png"


class UploadError(ValueError):
    """Kullanıcıya gösterilebilir yükleme hatası."""


def validate_upload_name_and_magic(filename: Optional[str], data: bytes) -> None:
    """Dosya adı uzantısı (.jpg/.jpeg/.png/.webp) ve magic-byte imzasını doğrular; .svg/.html/.py/.sh/.txt reddedilir."""
    ext = Path(filename or "").suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise UploadError("Yalnızca .jpg, .jpeg, .png veya .webp görsel dosyaları yüklenebilir.")
    is_webp = data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    if not (data.startswith(_MAGIC_PREFIXES) or is_webp):
        raise UploadError("Dosya içeriği geçerli bir görsel değil (JPG, PNG veya WEBP).")


def normalize_image_ref(value: Optional[str]) -> str:
    """
    Kampanya/ürün görsel alanını doğrular: boş veya yalnızca kendi yüklemelerimiz (static/uploads/...) olabilir.
    Harici adresler (yurt dışı istek riski) reddedilir.
    """
    ref = (value or "").strip()
    if not ref:
        return ""
    ref = ref.lstrip("/")
    if ref.startswith("k/"):
        ref = ref[2:]
    name = ref[len(UPLOAD_PREFIX):]
    # Yalnızca kendi yüklemelerimizin düz dosya adı (alt klasör, tırnak, boşluk, < > yok): yol HTML özniteliğine gömülür
    if not ref.startswith(UPLOAD_PREFIX) or not _UPLOAD_NAME_RE.match(name) or ".." in name or len(ref) > 200:
        raise UploadError("Görsel yalnızca panelden yüklenen dosyalardan seçilebilir (harici adres kullanılamaz).")
    return ref


def _open_verified(data: bytes) -> Image.Image:
    if not data:
        raise UploadError("Dosya boş.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise UploadError("Dosya çok büyük (en fazla 5 MB).")
    try:
        probe = Image.open(io.BytesIO(data))
        probe.verify()
        img = Image.open(io.BytesIO(data))
        fmt = (img.format or "").upper()
        img.load()
    except Exception:
        raise UploadError("Geçerli bir görsel dosyası (JPG, PNG veya WEBP) yükleyiniz.")
    if fmt not in ALLOWED_FORMATS:
        raise UploadError("Yalnızca JPG, PNG veya WEBP görseller yüklenebilir.")
    return ImageOps.exif_transpose(img)


def _ensure_dir() -> Path:
    upload_dir = Path(app_config.UPLOAD_DIR)
    upload_dir.mkdir(parents=True, exist_ok=True)
    return upload_dir


def save_content_image(data: bytes) -> str:
    """Ürün/kampanya görselini (en fazla 1600px, JPEG) kaydeder ve `static/uploads/<ad>` yolunu döner."""
    img = _open_verified(data)
    if img.mode in ("RGBA", "LA", "P"):
        rgba = img.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (252, 249, 248))
        bg.paste(rgba, mask=rgba.split()[-1])
        img = bg
    else:
        img = img.convert("RGB")
    img.thumbnail((1600, 1600))
    name = f"{uuid.uuid4().hex}.jpg"
    img.save(_ensure_dir() / name, format="JPEG", quality=85, optimize=True)
    return UPLOAD_PREFIX + name


def save_logo(data: bytes) -> str:
    """
    Kafe logosunu PNG olarak kaydeder; manifest/bildirim için 512 ve 192 px sürümlerini ve
    tek renkli bildirim rozetini (badge) üretir. `static/uploads/logo-512.png` yolunu döner.
    """
    img = _open_verified(data).convert("RGBA")
    upload_dir = _ensure_dir()

    for fname, size in LOGO_SIZES.items():
        square = ImageOps.contain(img, (size, size))
        canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        canvas.paste(square, ((size - square.width) // 2, (size - square.height) // 2), square)
        canvas.save(upload_dir / fname, format="PNG", optimize=True)

    # Android bildirim rozeti: yalnızca alfa kanalı kullanılır (beyaz siluet)
    alpha = ImageOps.contain(img, (96, 96)).split()[-1]
    if alpha.getextrema()[0] < 250:  # gerçek saydamlık var
        badge = Image.new("RGBA", (96, 96), (255, 255, 255, 0))
        white = Image.new("RGBA", alpha.size, (255, 255, 255, 255))
        badge.paste(white, ((96 - alpha.width) // 2, (96 - alpha.height) // 2), alpha)
        badge.save(upload_dir / BADGE_NAME, format="PNG", optimize=True)
    else:
        default_badge = Path(__file__).resolve().parent.parent / "static" / "img" / "badge-96.png"
        if default_badge.exists():
            (upload_dir / BADGE_NAME).write_bytes(default_badge.read_bytes())
    return UPLOAD_PREFIX + "logo-512.png"


def delete_upload(ref: Optional[str]) -> None:
    """Yüklenen dosyayı güvenli biçimde siler (yalnızca upload klasörü içindeyse)."""
    try:
        clean = normalize_image_ref(ref)
    except UploadError:
        return
    if not clean:
        return
    upload_dir = Path(app_config.UPLOAD_DIR).resolve()
    target = (upload_dir / clean[len(UPLOAD_PREFIX):]).resolve()
    if upload_dir in target.parents and target.is_file() and not target.name.startswith(("logo-", "badge-")):
        try:
            target.unlink()
        except OSError:
            pass

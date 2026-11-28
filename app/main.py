import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
import app.config as app_config
from app.config import get_secret_key, validate_otp_config
from app.db import init_db, get_db
from app.routes import customer, api, manager, staff, panels, display
from app.services.directus_sync import sync_all_sqlite_to_directus
from app.services.retention import run_retention

STATIC_DIR = Path(__file__).resolve().parent / "static"

# Yurt dışı sunucuya hiçbir istek yapılmaz: yalnızca kendi kaynağımız ('self') + data:/blob: görseller.
# (Yazı tipleri, Tailwind CSS ve QR kütüphanesi kendi sunucumuzdan sunulur.)
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "font-src 'self'; "
    "connect-src 'self'; "
    "media-src 'self' blob: mediastream:; "
    "worker-src 'self'; "
    "manifest-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)


def _retention_pass() -> None:
    """KVKK saklama/imha görevinin tek turu (ayrı iş parçacığında çalışır; hata uygulamayı durdurmaz)."""
    try:
        with get_db(immediate=True) as conn:
            run_retention(conn)
    except Exception as exc:  # pragma: no cover - yalnızca canlı ortam günlüğü
        logging.getLogger("sadakat").warning("Saklama/imha görevi başarısız: %s", exc)


async def _retention_loop() -> None:
    """Açılışta bir kez, sonra 24 saatte bir saklama sürelerini uygular (Aydınlatma Metni m.6)."""
    while True:
        await asyncio.to_thread(_retention_pass)
        await asyncio.sleep(24 * 3600)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Zorunlu SECRET_KEY kontrolü
    get_secret_key()
    # OTP (yeni cihaz SMS kodu) yapılandırması: OTP_MODE=new_device iken sağlayıcı eksik/yanlışsa uygulama AÇILMAZ
    validate_otp_config()
    # Uygulama açılışında SQLite tablolarını, migration'ları ve varsayılan ayarları hazırla
    init_db()
    Path(app_config.UPLOAD_DIR).mkdir(parents=True, exist_ok=True)
    # Mevcut SQLite müşterilerini ve kampanyalarını PostgreSQL Directus ile senkronize et
    try:
        with get_db() as conn:
            sync_all_sqlite_to_directus(conn)
    except Exception:
        pass
    retention_task = asyncio.create_task(_retention_loop()) if os.getenv("RETENTION_JOB", "1") != "0" else None
    try:
        yield
    finally:
        if retention_task:
            retention_task.cancel()


MAX_BODY_BYTES = 1024 * 1024            # JSON / form istekleri
MAX_UPLOAD_BODY_BYTES = 6 * 1024 * 1024  # görsel yükleme (5 MB dosya + multipart payı)


class BodySizeLimitMiddleware:
    """
    İstek gövdesi üst sınırı (DoS koruması): FastAPI JSON gövdesini tamamen belleğe okur; sınırsız gövde (ör. 1 GB JSON)
    sunucunun belleğini tüketir. Content-Length sınırı aşıyorsa istek hemen 413 ile reddedilir; uzunluk bildirmeyen
    (chunked) gövde sınırı aşınca okuma kesilir (istemci bağlantıyı kesmiş sayılır → 400).
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = MAX_UPLOAD_BODY_BYTES if scope.get("path", "").endswith("/api/admin/upload") else MAX_BODY_BYTES
        for name, value in scope.get("headers") or []:
            if name == b"content-length":
                try:
                    too_big = int(value) > limit
                except ValueError:
                    too_big = True
                if too_big:
                    await JSONResponse({"status": "error", "message": "İstek çok büyük."}, status_code=413)(scope, receive, send)
                    return
                break
        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    return {"type": "http.disconnect"}
            return message

        await self.app(scope, limited_receive, send)


app = FastAPI(
    title="Dijital Sadakat Kartı",
    description="Web Tabanlı PWA Sadakat Sistemi ve Yönetim Panelleri",
    version="3.1.0",
    lifespan=lifespan,
    # Eğik çizgi yönlendirmesi (307) KAPALI: Caddy /k önekini kırptığı için Location '/k'sız adrese giderdi (ADR-01).
    # Sayfa rotalarının hepsi hem "/yol" hem "/yol/" biçiminde tanımlıdır; olmayan biçim 404 döner.
    redirect_slashes=False,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
# Güvenlik başlıkları ara katmanından ÖNCE eklenir → onun içinde çalışır: /k öneki kırpılmış yolu görür, 413 yanıtı da
# güvenlik başlıklarını alır.
app.add_middleware(BodySizeLimitMiddleware)


# Güvenlik Başlıkları ve /k Alt Yol (Subpath) ASGI Middleware
@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    path = request.scope.get("path", "")
    request.scope["original_path"] = path
    if path == "/k" or path == "/k/":
        request.scope["path"] = "/"
        if "raw_path" in request.scope:
            request.scope["raw_path"] = b"/"
    elif path.startswith("/k/"):
        stripped = path[2:] or "/"
        request.scope["path"] = stripped
        if "raw_path" in request.scope:
            request.scope["raw_path"] = stripped.encode("utf-8")

    response = await call_next(request)

    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Strict-Transport-Security"] = "max-age=31536000"
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = "camera=(self), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
    if not path.startswith("/static") and not path.startswith("/k/static"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
    return response


@app.exception_handler(401)
async def unauthorized_handler(request: Request, exc):
    """Yönetim uçlarında oturum yoksa JSON 401 (panel JS'i giriş formunu gösterir)."""
    detail = getattr(exc, "detail", "Oturum gerekli.")
    return JSONResponse({"status": "error", "message": detail}, status_code=401)


# Yüklenen görseller (ayrı volume) — genel /static bağlamasından ÖNCE tanımlanmalıdır
_upload_dir = Path(app_config.UPLOAD_DIR)
_upload_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static/uploads", StaticFiles(directory=_upload_dir), name="uploads")
app.mount("/k/static/uploads", StaticFiles(directory=_upload_dir), name="uploads_k")

# Statik dosyaları hem /static hem /k/static altında bağla
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.mount("/k/static", StaticFiles(directory=STATIC_DIR), name="static_k")

# Rotaları ekle
app.include_router(customer.router)
app.include_router(api.router)
app.include_router(manager.router)
app.include_router(staff.router)
app.include_router(panels.router)
app.include_router(display.page_router)   # ADR-30: Kasa QR Ekranı sayfası (/ekran)
app.include_router(display.router)        # ADR-30: /api/ekran/* (yalnızca eşleştirilmiş ekranın çereziyle)


@app.get("/")
def root(request: Request):
    """Kök adres yönlendirme döngüsüne girmeden doğrudan 200 OK ile Müşteri Panelini (PWA) açar."""
    return customer.handle_card_page(request)

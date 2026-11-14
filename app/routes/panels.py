"""
Müdür (/mudur, /yonetim), Çalışan (/personel) panel sayfaları, ortak giriş (/giris) + giriş/çıkış uçları.
Not: Caddy /k önekini kırptığı için burada 302 yönlendirme KULLANILMAZ; oturum yoksa giriş formu 200 OK ile gösterilir.

Tek giriş ekranı: Kullanıcı adı + parola. Rol (manager | staff) veritabanındaki hesaptan gelir;
müdür tam panele, personel kısıtlı kasa paneline yönlendirilir. Kasa panelinin QR metinleri basılı QR'ın damga verip
vermediğine (static_qr_stamps, ADR-30) göre değişir.
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader
from pydantic import BaseModel

from app import auth
from app.config import DEFAULT_BUSINESS_NAME
from app.db import get_all_settings, get_db
from app.services import security
from app.services.accounts import authenticate_user, is_legacy_default_password
from app.services.push import CAMPAIGN_OPTOUT_HINT
from app.utils import check_rate_limit, rate_limit_blocked, rate_limit_clear, rate_limit_record

router = APIRouter()

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
jinja_env = Environment(loader=FileSystemLoader(TEMPLATES_DIR), autoescape=True)


class LoginIn(BaseModel):
    """Tek giriş ekranı: kullanıcı adı + parola. Rol hesabın kendisinden belirlenir."""
    username: str = ""
    password: str = ""
    remember: bool = False   # ADR-42: "Bu cihazda oturumum açık kalsın" (formda varsayılan işaretli)


def _rel_base(request: Request) -> str:
    app_path = request.scope.get("path") or "/"
    depth = app_path.lstrip("/").count("/")
    return "../" * depth if depth > 0 else "./"


_ASSET_FILES = ("css/app.css", "js/panel.js")


def _asset_version() -> str:
    """
    Panel CSS/JS adreslerine eklenen sürüm (?v=…): dosyalar değişince adres de değişir. Sayfa HTML'i her istekte taze
    gelir (no-store) ama statik dosyaları tarayıcı saatlerce önbellekten kullanabilir; sürümsüz adreste yeni sayfa eski
    CSS/JS ile bozuk görünür.
    """
    static_dir = TEMPLATES_DIR.parent / "static"
    stamps = []
    for rel in _ASSET_FILES:
        try:
            stamps.append(int((static_dir / rel).stat().st_mtime))
        except OSError:
            pass
    return str(max(stamps, default=0))


# ADR-43: panel uygulamaları (PWA). Müşteri kartından ayrı: kendi adı, başlangıç adresi ve dar kapsamı (müşteri kartının
# kapsamıyla çakışmaz). Servis çalışanı müşteri kartıyla ortaktır (aynı sw.js + aynı kapsam → tek kayıt).
PANEL_APPS = {
    "mudur": {"name": "Yönetim", "description": "müdür paneli: müşteriler, işlem kayıtları, kampanyalar, menü"},
    "personel": {"name": "Kasa", "description": "kasa paneli: damga, ödül teslimi, müşteri sorgulama"},
}


def _panel_app(request: Request, template: str) -> str:
    """Bu sayfa hangi panel uygulamasına ait? (/mudur → 'mudur', /personel → 'personel'; /giris, /yonetim → '')."""
    path = (request.scope.get("path") or "").rstrip("/")
    if template == "mudur.html" and path.endswith("/mudur"):
        return "mudur"
    if template == "personel.html" and path.endswith("/personel"):
        return "personel"
    return ""


def _render(request: Request, template: str, role: str, session: dict | None) -> HTMLResponse:
    with get_db() as conn:
        settings = get_all_settings(conn)
    pwa_app = _panel_app(request, template)
    ctx = {
        "pwa_app": pwa_app,
        "pwa_app_name": PANEL_APPS[pwa_app]["name"] if pwa_app else "",
        "pwa_icon": _panel_icon(settings, 192),
        "business_name": settings.get("business_name", DEFAULT_BUSINESS_NAME),
        "accent_color": settings.get("accent_color", "#1E4D3F"),
        "rel_base": _rel_base(request),
        "asset_v": _asset_version(),
        "role": role,
        "session_name": (session or {}).get("name", ""),
        "static_qr_stamps": (settings.get("static_qr_stamps", "1") or "1").strip() != "0",
        "campaign_optout_hint": CAMPAIGN_OPTOUT_HINT,   # ADR-36: kampanya bildirimi önizlemesi gönderilenle aynı satırı gösterir
    }
    page = template if session else "panel_login.html"
    html = jinja_env.get_template(page).render(ctx)
    resp = HTMLResponse(html, headers={"X-Robots-Tag": "noindex, nofollow"})
    auth.renew_remembered_session(resp, session)   # ADR-42: hatırlanan oturum panel açıldıkça 30 güne uzar
    return resp


@router.get("/mudur")
@router.get("/mudur/")
@router.get("/yonetim")
@router.get("/yonetim/")
@router.get("/yonetim/{subpath:path}")
def manager_page(request: Request, subpath: str = ""):
    """Müdür paneli (tam yetkili). Oturum yoksa giriş formu."""
    return _render(request, "mudur.html", "manager", auth.current_manager(request))


def _panel_icon(settings: dict, size: int) -> str:
    """Kafe logosu yüklüyse onu, yoksa varsayılan ikonu döner (uygulama köküne göre göreli yol)."""
    if (settings.get("logo_url") or "").strip():
        return f"static/uploads/logo-{size}.png"
    return f"static/img/icon-{size}.png"


@router.get("/mudur.webmanifest")
@router.get("/personel.webmanifest")
def panel_manifest(request: Request):
    """
    ADR-43: Yönetim (müdür) ve Kasa (personel) panelleri telefona / bilgisayara uygulama olarak yüklenir. Adresler
    manifestin kendi adresine göre görelidir (/k/ önekiyle de öneksiz de doğru çözülür). Kapsam yalnızca o panelin
    adresidir: müşteri kartı (/kart/, kapsam ./) ile çakışmaz.
    """
    app_key = "mudur" if (request.scope.get("path") or "").endswith("/mudur.webmanifest") else "personel"
    with get_db() as conn:
        settings = get_all_settings(conn)
    business = (settings.get("business_name") or DEFAULT_BUSINESS_NAME).strip()
    info = PANEL_APPS[app_key]
    icons = [{"src": _panel_icon(settings, s), "sizes": f"{s}x{s}", "type": "image/png", "purpose": "any"} for s in (192, 512)]
    manifest = {
        "name": f"{business} {info['name']}",
        "short_name": info["name"],
        "description": f"{business} {info['description']}",
        "lang": "tr",
        "dir": "ltr",
        "start_url": f"./{app_key}",
        "scope": f"./{app_key}",
        "display": "standalone",
        "background_color": "#FAF9F7",
        "theme_color": "#FAF9F7",
        "prefer_related_applications": False,
        "icons": icons,
    }
    return Response(
        content=json.dumps(manifest, ensure_ascii=False), media_type="application/manifest+json",
        headers={"X-Robots-Tag": "noindex, nofollow"},
    )


@router.get("/personel")
@router.get("/personel/")
def staff_page(request: Request):
    """Çalışan/kasiyer paneli (kısıtlı yetki). Oturum yoksa giriş formu."""
    return _render(request, "personel.html", "staff", auth.current_staff(request))


@router.get("/giris")
@router.get("/giris/")
def login_page(request: Request):
    """Ortak giriş adresi: oturum varsa rolüne uygun panel, yoksa giriş formu (hep 200 OK, yönlendirme yok)."""
    mgr = auth.current_manager(request)
    if mgr:
        return _render(request, "mudur.html", "manager", mgr)
    staff = auth.current_staff(request)
    if staff:
        return _render(request, "personel.html", "staff", staff)
    return _render(request, "panel_login.html", "manager", None)


@router.post("/panel/login")
def panel_login(req: LoginIn, request: Request, response: Response):
    ip = auth.client_ip(request)
    if not check_rate_limit(f"panel_login_{ip}", max_requests=10, window_seconds=900):
        response.status_code = 429
        return {"status": "error", "message": "Çok fazla deneme. 15 dakika sonra tekrar deneyin."}

    username = (req.username or "").strip()
    if not username or not req.password:
        response.status_code = 400
        return {"status": "error", "message": "Kullanıcı adı ve parolayı yazın."}

    # Dağıtık (çok IP'li) parola denemelerine karşı hesap bazlı üst sınır (G-04): YALNIZCA BAŞARISIZ denemeler sayılır
    # (başarılı girişler meşru müdürü kilitlemez, başarılı giriş sayacı sıfırlar). Hesap kilitliyken bile bu hesaba son
    # 12 saatte başarıyla girmiş IP'den gelen istek denenebilir: yabancı IP'lerden yapılan yanlış denemeler müdürü dışarıda bırakamaz.
    user_key = username.lower()[:60]
    fail_key = f"panel_login_fail_{user_key}"
    known_key = f"panel_login_known_{user_key}_{ip}"
    if rate_limit_blocked(fail_key, max_requests=10, window_seconds=900) and not rate_limit_blocked(known_key, 1, 12 * 3600):
        if check_rate_limit(f"panel_login_locklog_{user_key}", max_requests=1, window_seconds=900):
            security.log_event(
                "panel_login_locked", "medium", ip=ip, path="/panel/login", method="POST",
                user_agent=request.headers.get("user-agent", ""), actor="", detail="Hesap için çok sayıda hatalı deneme: geçici kilit.",
            )
        response.status_code = 429
        return {"status": "error", "message": "Bu hesap için çok fazla hatalı deneme. 15 dakika sonra tekrar deneyin."}

    with get_db(immediate=True) as conn:
        account = authenticate_user(conn, username, req.password)
    if not account:
        rate_limit_record(fail_key)
        security.record_failed_login(ip, "panel", user_agent=request.headers.get("user-agent", ""), path="/panel/login")
        response.status_code = 401
        return {"status": "error", "message": "Kullanıcı adı veya parola hatalı."}

    rate_limit_clear(fail_key)
    rate_limit_record(known_key)
    role = account["role"]
    # Önceki oturumdan kalan diğer rol çerezi temizlenir; yalnızca giriş yapılan rol geçerli olur.
    for other_role, cookie_name in auth.ROLE_COOKIES.items():
        if other_role != role:
            response.delete_cookie(key=cookie_name, path="/")
    auth.set_session_cookie(response, role, account["name"], account["id"], remember=bool(req.remember))
    user_agent = request.headers.get("user-agent", "")
    actor = f"{role}:{account['username']}"
    security.log_event(
        "panel_login", "info", ip=ip, path="/panel/login", method="POST",
        user_agent=user_agent, actor=actor, detail="Başarılı giriş",
    )
    out = {"status": "success", "role": role, "redirect": "mudur" if role == "manager" else "personel"}
    if is_legacy_default_password(req.password):
        # Eski sürümün kaynak kodunda yazılı parola: depoyu gören herkes bu hesaba girebilir. Giriş ekranı müdürü yeni
        # parola belirlemeye yönlendirir (personel için müdüre haber verir); Güvenlik sekmesinde olay açılır.
        security.log_event(
            "default_password_login", "high", ip=ip, path="/panel/login", method="POST", user_agent=user_agent, actor=actor,
            detail="Herkesçe bilinen eski varsayılan parolayla giriş yapıldı; parola hemen değiştirilmeli.",
            incident={
                "type": "default_password_in_use", "severity": "high",
                "summary": f"'{account['username']}' hesabı eski sürümün kaynak kodunda yazılı varsayılan parolayı kullanıyor. "
                           "Parolayı hemen değiştirin (müdür: Ayarlar › Hesabım, personel: Personel sekmesi).",
            },
        )
        out["weak_password"] = True
    return out


@router.post("/panel/logout")
def panel_logout(request: Request, response: Response):
    auth.revoke_request_sessions(request)
    auth.clear_session_cookies(response)
    return {"status": "success"}

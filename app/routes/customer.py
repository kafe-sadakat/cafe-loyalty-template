import json
from pathlib import Path
from urllib.parse import urlparse
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from jinja2 import Environment, FileSystemLoader
from app import auth
from app.config import DEFAULT_BUSINESS_NAME, PUBLIC_BASE_PATH, PUBLIC_BASE_URL, request_public_base
from app.db import REGISTER_SEAL_TOKEN, get_db, get_all_settings, get_kvkk_text, get_marketing_consent_text
from app.services import activity, nfc_sun, stamp_codes
from app.services.program_terms import build_program_terms
from app.services.guest_page import build_guest_page
from app.services.loyalty import (
    get_active_seal_by_token,
    get_customer_from_cookie,
    get_customer_card_data,
    static_qr_stamps_enabled,
    DEVICE_COOKIE_NAME,
)
from app.routes.api import set_device_cookie, resolve_device_token
from app.routes.panels import _asset_version
from app.utils import check_rate_limit

router = APIRouter()

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
jinja_env = Environment(loader=FileSystemLoader(TEMPLATES_DIR), autoescape=True)


def _compute_rel_base(request: Request) -> str:
    """İstek yolunun derinliğine göre './', '../', '../../' göreli taban yolunu otomatik hesaplar."""
    app_path = request.scope.get("path") or "/"
    depth = app_path.lstrip("/").count("/")
    return "../" * depth if depth > 0 else "./"


def get_common_context(
    conn,
    rel_base: str = "./",
    initial_claim_token: str = "",
    initial_staff_token: str = "",
    initial_tab: str = "home",
    guest_mode: bool = False,
    entry: str = "",
    official_host: str = "",
):
    """
    Aktif Kullanıcı Paneli (PWA) için ortak bağlamı hazırlar.
    `entry`: 'scan' (kasadaki sabit QR / NFC okutması: tek kullanımlık açılış) | 'stamp' (kasa ekranının canlı kodu) |
    'nfc' (NTAG 424 DNA dokunuşu, ADR-30) | 'register' (basılı QR'ın damgası kapalı: yalnızca kayıt, ADR-30) |
    'kayit' (Kayıt / Uygulama QR'ı /m/kayit: her zaman yalnızca kayıt, ADR-35) | ''.
    """
    settings = get_all_settings(conn)
    host = official_host or urlparse(PUBLIC_BASE_URL).netloc or "localhost:8000"
    return {
        "business_name": settings.get("business_name", DEFAULT_BUSINESS_NAME),
        "official_host": host,
        "accent_color": settings.get("accent_color", "#1E4D3F"),
        "reward_name": settings.get("reward_name", "Ücretsiz Kahve"),
        "stamp_target": int(settings.get("stamp_target", "5")),
        "daily_limit": int(settings.get("daily_limit", "2")),
        "kvkk_text": get_kvkk_text(settings),
        "marketing_consent_text": get_marketing_consent_text(settings),
        "program_terms_text": build_program_terms(settings),   # ADR-36: ayarlardan her çizilişte üretilir
        "rel_base": rel_base,
        "asset_v": _asset_version(),   # E-05: statik CSS adresi dosya değişince değişir (dağıtım sonrası eski CSS kalmasın)
        "initial_claim_token": initial_claim_token,
        "initial_staff_token": initial_staff_token,
        "initial_tab": initial_tab,
        "guest_mode": guest_mode,
        "entry": entry,
        "menu_note": settings.get("menu_note", ""),
    }


def _preview_bot(request: Request) -> str:
    """ADR-31: isteği bir bağlantı önizleyicisi (WhatsApp, Telegram, iMessage…) mi yaptı? Adı ya da boş."""
    return activity.preview_bot(request.headers.get("user-agent", ""))


def _note_preview(bot: str, path_kind: str, ip: str, code: str = "") -> None:
    """Kasa QR'ı / damga bağlantısı bir sohbete gönderildi: önizleyici sayfayı çekti → işlem kaydına not (ADR-31)."""
    try:
        with get_db(immediate=True) as conn:
            row = stamp_codes.find_code(conn, code) if code else None
            activity.record_link_preview(conn, bot, path_kind, ip=ip, code_row=row)
    except Exception:
        pass


def _render_pwa(
    request: Request,
    rel_base: str | None = None,
    initial_claim_token: str = "",
    initial_staff_token: str = "",
    initial_tab: str = "home",
    guest_mode: bool = False,
    entry: str = "",
) -> HTMLResponse:
    computed_rel = rel_base if rel_base is not None else _compute_rel_base(request)
    base_url, _ = request_public_base(request)
    official_host = urlparse(base_url).netloc or request.url.netloc or urlparse(PUBLIC_BASE_URL).netloc or "localhost:8000"
    device_token = resolve_device_token(request)
    with get_db() as conn:
        customer = get_customer_from_cookie(conn, device_token) if device_token else None
        customer_card = get_customer_card_data(conn, customer["id"]) if customer else None
        ctx = get_common_context(
            conn,
            rel_base=computed_rel,
            initial_claim_token=initial_claim_token,
            initial_staff_token=initial_staff_token,
            initial_tab=initial_tab,
            guest_mode=guest_mode,
            entry=entry,
            official_host=official_host,
        )
        ctx["claim_token"] = initial_claim_token
        ctx["has_device"] = customer is not None
        ctx["customer_card"] = customer_card
        template = jinja_env.get_template("app_pwa.html")
        resp = HTMLResponse(template.render(ctx), status_code=200)
        if initial_claim_token:
            resp.headers["X-Claim-Token"] = initial_claim_token
        if customer and device_token:
            set_device_cookie(resp, device_token)
        return resp


@router.get("/m/{seal_token}")
@router.get("/m/{seal_token}/")
def handle_seal_scan(seal_token: str, request: Request):
    """
    1. Kasadaki SABİT QR / NFC etiketi (/m/qr, önceden basılmış /m/{token}) — ADR-29: her okutuş TEK KULLANIMLIK bir
    açılıştır. Her GET yeni bir açılış kodu üretip sayfaya gömer; sayfa açılınca JS damgayı ister ve adres /kart/ olur.
    Sayfayı yenilemek, geri dönmek ya da ekranı/bağlantıyı başkasına göndermek ikinci damga vermez. QR'ı yeniden okutmak
    (sabit adresi yeniden açmak) yeni bir açılıştır; "aynı ziyarette tek QR damgası" beklemesi ve günlük limit sınırlar.
    Kayıtlı olmayan müşteri aynı akışta kayıt olur. Devre dışı bırakılmış mühür açılış üretmez. 302 yok; 200 OK (ADR-01).
    ADR-30: adres NTAG 424 DNA imzası taşıyorsa (?p=…&c=…) NFC dokunuşu olarak işlenir (bkz. /n). Basılı QR'ın damgası
    panelden kapatıldıysa (static_qr_stamps=0) açılış üretilmez; sayfa kayıt + uygulama için açılır (entry=register).
    ADR-35: /m/kayit = sistemi açan Kayıt / Uygulama QR'ı. static_qr_stamps ne olursa olsun, kamerayla, NFC ile ya da
    uygulama içi okutucuyla HİÇBİR ZAMAN damga vermez (açılış üretilmez, NFC imzası yok sayılır); yalnızca kayıt +
    uygulama (entry=kayit).
    ADR-41: Damga QR'ı /m/{gizli kod}'dur (stamp_codes.stamp_qr_token). Eski /m/qr ve /m/ altındaki başka her ad açılış
    üretmez; sayfa "bu QR artık kullanılmıyor" bilgisiyle açılır (entry=stale). NFC 424 imzalı adres bundan etkilenmez.
    """
    client_ip = auth.client_ip(request)
    if not check_rate_limit(f"m_scan_{client_ip}", max_requests=40, window_seconds=60):
        return HTMLResponse("Çok fazla istek yapıldı. Lütfen biraz bekleyiniz.", status_code=429)
    if seal_token.strip().lower() == REGISTER_SEAL_TOKEN:
        if _preview_bot(request):
            return _render_pwa(request=request, initial_tab="home")   # sohbet önizlemesi: not düşmeye gerek yok
        return _render_pwa(request=request, initial_tab="kartim", entry="kayit")
    nfc_token = nfc_sun.claim_token_for_page(request.url.query)
    bot = _preview_bot(request)
    if bot:
        # Bağlantı bir sohbette paylaşıldı (WhatsApp vb. önizlemesi): açılış üretilmez, olay not edilir (ADR-31)
        _note_preview(bot, "nfc" if nfc_token else "static", client_ip)
        return _render_pwa(request=request, initial_tab="home")
    if nfc_token:
        return _render_pwa(request=request, initial_claim_token=nfc_token, initial_tab="kartim", entry="nfc")
    register_only = stale = False
    with get_db(immediate=True) as conn:
        seal = get_active_seal_by_token(conn, seal_token)
        if not stamp_codes.is_stamp_qr_token(conn, seal_token):
            code, stale = "", True        # ADR-41: eski /m/qr ya da tahmin edilen ad — damga açılışı yok
        elif seal is not None and not seal["active"]:
            code = ""
        elif not static_qr_stamps_enabled(conn):
            code, register_only = "", True
        else:
            code = stamp_codes.create_stamp_code(conn, origin="scan", ttl_seconds=stamp_codes.SCAN_TTL_SECONDS)["code"]
    if stale:
        return _render_pwa(request=request, initial_tab="home", entry="stale")
    if register_only:
        return _render_pwa(request=request, initial_tab="kartim", entry="register")
    if not code:
        return _render_pwa(request=request, initial_tab="home")
    return _render_pwa(request=request, initial_claim_token=code, initial_tab="kartim", entry="scan")


@router.get("/n")
@router.get("/n/")
def handle_nfc_tap(request: Request):
    """
    NTAG 424 DNA etiketi (ADR-30): her dokunuşta adres yeni bir imza taşır (/k/n?p=…&c=…). GET hiçbir şeyi tüketmez
    (bağlantı önizleyicileri); imzalı adres sayfaya gömülür ve JS /api/claim ile ister. İmza, sayacın ilerlemesi ve tek
    kullanımlık açılış orada denetlenir; ardından adres /kart/ olur. Aynı adres yeniden açılınca damga vermez. 302 yok.
    """
    client_ip = auth.client_ip(request)
    if not check_rate_limit(f"n_scan_{client_ip}", max_requests=40, window_seconds=60):
        return HTMLResponse("Çok fazla istek yapıldı. Lütfen biraz bekleyiniz.", status_code=429)
    bot = _preview_bot(request)
    if bot:
        _note_preview(bot, "nfc", client_ip)
        return _render_pwa(request=request, initial_tab="home")
    token = nfc_sun.claim_token_for_page(request.url.query)
    if not token:
        return _render_pwa(request=request, initial_tab="home")
    return _render_pwa(request=request, initial_claim_token=token, initial_tab="kartim", entry="nfc")


@router.get("/d/{code}")
@router.get("/d/{code}/")
def handle_stamp_code_page(code: str, request: Request):
    """
    Kasa ekranındaki tek kullanımlık Damga QR'ı (ADR-28). 302 yok; 200 OK ile uygulama açılır, kod sayfaya gömülür.
    GET kodu tüketmez/ayırmaz (bağlantı önizleyicileri kodu yakamaz); sayfa açılınca JS damgayı /api/claim ile ister.
    """
    client_ip = auth.client_ip(request)
    if not check_rate_limit(f"d_scan_{client_ip}", max_requests=60, window_seconds=60):
        return HTMLResponse("Çok fazla istek yapıldı. Lütfen biraz bekleyiniz.", status_code=429)
    bot = _preview_bot(request)
    if bot:
        _note_preview(bot, "code", client_ip, code=(code or "")[:80])
        return _render_pwa(request=request, initial_tab="home")
    return _render_pwa(request=request, initial_claim_token=(code or "")[:80], initial_tab="kartim", entry="stamp")


@router.get("/indir/")
@router.get("/indir")
def handle_install_page(request: Request):
    """
    Masa QR'ı — Yalnızca menü + uygulama ekleme sayfası: damga BASMAZ. Menü ve kampanyaları açar,
    uygulamayı tek tıkla indirmeye yönlendirir. Kayıtsız ziyaretçi giriş ekranına zorlanmaz.
    """
    return _render_pwa(request=request, initial_tab="indir", guest_mode=True)


@router.get("/misafir")
@router.get("/misafir/")
def handle_guest_page(request: Request):
    """
    ADR-40: Sosyal medya profil bağlantısı — misafir sayfası (menü + kampanyalar). Damga / giriş / kayıt yok, çerez
    yazılmaz; uygulama kabuğu (manifest, yükleme, alt menü) yüklenmez. Sunucuda çizilir; 302 yok, 200 OK.
    """
    base_url, base_path = request_public_base(request)
    with get_db() as conn:
        ctx = build_guest_page(conn)
    ctx.update(
        rel_base=_compute_rel_base(request),
        asset_v=_asset_version(),
        page_url=f"{base_url}{base_path}/misafir",
        og_image=f"{base_url}{base_path}/{ctx['og_image_path']}",
    )
    return HTMLResponse(jinja_env.get_template("misafir.html").render(ctx), status_code=200)


@router.get("/c/{claim_token}")
@router.get("/c/{claim_token}/")
def handle_claim_page(claim_token: str, request: Request):
    """2. Eski c/{claim_token} bağlantıları (ADR-28): damga VERMEZ; kart sayfası açılır (200 OK)."""
    return _render_pwa(request=request, initial_tab="home")


@router.get("/qr-okut/")
@router.get("/qr-okut")
def handle_qr_okut_page(request: Request):
    """
    Ayrı QR Okut Sayfası / Sekmesi:
    Doğrudan kamera arayüzünü açar.
    """
    return _render_pwa(
        request=request,
        initial_tab="qrokut",
    )


@router.get("/kart/")
@router.get("/kart")
def handle_card_page(request: Request):
    """
    3. Ana Müşteri Paneli (PWA):
    Tek sayfa dijital sadakat müşteri uygulaması.
    `?tab=kampanyalar` (ödüllü kampanya bildirimi) gibi bir sekmeyle açılabilir; 302 yok, aynı sayfa 200 döner.
    """
    tab = request.query_params.get("tab") or "home"
    return _render_pwa(request=request, initial_tab=tab if tab in DEEP_LINK_TABS else "home")


DEEP_LINK_TABS = {"home", "kartim", "menu", "kampanyalar", "profil"}


@router.get("/r/{staff_token}")
@router.get("/r/{staff_token}/")
def handle_redeem_page(staff_token: str, request: Request):
    """
    4. Personel Ödül Teslim Sayfası (r/{staff_token}):
    Personel kartı okutulduğunda PWA içinde ödül kullandırma onayını açar.
    """
    return _render_pwa(
        request=request,
        initial_staff_token=staff_token,
        initial_tab="kartim",
    )


@router.get("/kvkk/")
@router.get("/kvkk")
def handle_kvkk_page(request: Request):
    """5. KVKK Aydınlatma Metni görünümü (PWA Profil sekmesi)."""
    return _render_pwa(request=request, initial_tab="profil")


@router.get("/kvkk/sil/")
@router.get("/kvkk/sil")
def handle_kvkk_delete_page(request: Request):
    """6. KVKK Verilerimi Sil görünümü (PWA Profil sekmesi)."""
    return _render_pwa(request=request, initial_tab="profil")


def _branding_paths(settings: dict, base_path: str | None = None) -> dict:
    """Kafe logosu varsa onu, yoksa varsayılan ikonları döner (alt yola göre mutlak yollar)."""
    base = PUBLIC_BASE_PATH if base_path is None else base_path
    if (settings.get("logo_url") or "").strip():
        return {
            "icon192": f"{base}/static/uploads/logo-192.png",
            "icon512": f"{base}/static/uploads/logo-512.png",
            "maskable512": "",
            "badge": f"{base}/static/uploads/badge-96.png",
        }
    return {
        "icon192": f"{base}/static/img/icon-192.png",
        "icon512": f"{base}/static/img/icon-512.png",
        "maskable512": f"{base}/static/img/icon-maskable-512.png",
        "badge": f"{base}/static/img/badge-96.png",
    }


@router.get("/sw.js")
@router.get("/kart/sw.js")
def handle_service_worker(request: Request):
    """Web Push ve PWA için Service Worker. Bildirimler kafenin logosu ve temasıyla gösterilir."""
    _, base_path = request_public_base(request)
    with get_db() as conn:
        settings = get_all_settings(conn)
    brand = _branding_paths(settings, base_path=base_path)
    defaults = {
        "title": settings.get("business_name", DEFAULT_BUSINESS_NAME),
        "icon": brand["icon192"],
        "badge": brand["badge"],
        "url": f"{base_path}/kart/",
        "base": f"{base_path}/",
    }
    sw_code = (
        "const DEFAULTS = " + json.dumps(defaults, ensure_ascii=False) + ";\n"
        + """
self.addEventListener('install', (event) => { self.skipWaiting(); });
self.addEventListener('activate', (event) => { event.waitUntil(self.clients.claim()); });
// Ağ isteklerini olduğu gibi geçirir (yüklenebilirlik için gerekli, önbellek yok).
self.addEventListener('fetch', () => {});

self.addEventListener('push', (event) => {
  let payload = { title: DEFAULTS.title, body: 'Yeni bir bildiriminiz var!', url: DEFAULTS.url };
  try {
    if (event.data) { payload = Object.assign(payload, event.data.json()); }
  } catch (e) {}

  const options = {
    body: payload.body,
    icon: payload.icon || DEFAULTS.icon,
    badge: payload.badge || DEFAULTS.badge,
    tag: payload.tag || undefined,
    renotify: !!payload.tag,
    lang: 'tr',
    data: { url: payload.url || DEFAULTS.url }
  };
  // ADR-27: hiçbir bildirim türü "kapat" düğmesi (action) taşımaz; bildirim yalnızca başlık + metin + logo ile gösterilir.
  if (payload.image) { options.image = payload.image; }
  event.waitUntil(self.registration.showNotification(payload.title || DEFAULTS.title, options));
});

// Yeni bildirimlerde düğme yok (ADR-27). Bu işleyici yalnızca ADR-27 öncesi gönderilip bildirim merkezinde duran eski
// kampanya bildirimlerindeki "Bildirimleri kapat" düğmesine basılırsa o ret talebini yerine getirir (yok saymaz).
async function optOut() {
  const sub = await self.registration.pushManager.getSubscription();
  if (!sub) return;
  try {
    await fetch(DEFAULTS.base + 'api/push/unsubscribe', {
      method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ endpoint: sub.endpoint })
    });
  } catch (e) {}
  try { await sub.unsubscribe(); } catch (e) {}
}

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  if (event.action === 'optout') { event.waitUntil(optOut()); return; }
  // Bildirime dokununca müşteri uygulaması hedef adrese gider. Yalnızca müşteri uygulamasının (…/kart/) penceresi öne
  // alınır: aynı tarayıcıda açık müdür / kasa / QR ekranı sekmesi "açık pencere" sayılmaz (yoksa dokunuş hiçbir yere
  // gitmez). Açık uygulamaya hedef adres iletilir (ör. ?tab=kampanyalar); pencere yoksa ya da öne alınamazsa yenisi açılır.
  let targetUrl = DEFAULTS.url;
  try {
    targetUrl = new URL((event.notification.data && event.notification.data.url) || DEFAULTS.url, self.location.origin).href;
  } catch (e) {}
  const appPath = new URL(DEFAULTS.url, self.location.origin).pathname.replace(/\\/$/, '');
  event.waitUntil((async () => {
    let apps = [];
    try {
      const all = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
      apps = all.filter((c) => {
        try { return new URL(c.url).pathname.replace(/\\/$/, '') === appPath; } catch (e) { return false; }
      });
    } catch (e) {}
    const client = apps.find((c) => c.visibilityState === 'visible') || apps[0];
    if (client && 'focus' in client) {
      try {
        client.postMessage({ type: 'notification-click', url: targetUrl });
        await client.focus();
        return;
      } catch (e) {}
    }
    if (self.clients.openWindow) return self.clients.openWindow(targetUrl);
  })());
});
"""
    )
    return Response(content=sw_code.strip(), media_type="application/javascript")


@router.get("/manifest.webmanifest")
def handle_webmanifest(request: Request):
    """7. PWA Manifest Belgesi (kafe logosuyla)."""
    base_url, base_path = request_public_base(request)
    with get_db() as conn:
        settings = get_all_settings(conn)
        business_name = settings.get("business_name", DEFAULT_BUSINESS_NAME)
        accent_color = settings.get("accent_color", "#003629")
    brand = _branding_paths(settings, base_path=base_path)

    icons = [
        {"src": brand["icon192"], "sizes": "192x192", "type": "image/png", "purpose": "any"},
        {"src": brand["icon512"], "sizes": "512x512", "type": "image/png", "purpose": "any"},
    ]
    if brand.get("maskable512"):
        icons.append({"src": brand["maskable512"], "sizes": "512x512", "type": "image/png", "purpose": "maskable"})

    # İlk kayıt (index 0) bu isteğin adresi; diğer önek (/k ya da kök) ikinci kayıt olarak eklenir (tekilleştirilmiş)
    related_apps = [{"platform": "webapp", "url": f"{base_url}{base_path}/manifest.webmanifest"}]
    other_url = f"{base_url}/manifest.webmanifest" if base_path else f"{base_url}/k/manifest.webmanifest"
    if other_url != related_apps[0]["url"]:
        related_apps.append({"platform": "webapp", "url": other_url})

    # Tamamen web tabanlı PWA: tarayıcının yerel "Uygulamayı yükle" akışıyla kurulur (APK/indirilebilir dosya YOKTUR).
    manifest = {
        "id": f"{base_path}/kart/",
        "name": business_name,
        "short_name": business_name if len(business_name) <= 16 else business_name[:16].rstrip(),
        "description": f"{business_name} dijital sadakat kartı ve damga takip sistemi",
        "lang": "tr",
        "dir": "ltr",
        "start_url": "./kart/",
        "scope": "./",
        "display": "standalone",
        "orientation": "portrait",
        "background_color": "#FCF9F8",
        "theme_color": accent_color,
        "categories": ["food", "lifestyle", "shopping"],
        "prefer_related_applications": False,
        # Yüklü mü? (Chrome/Android navigator.getInstalledRelatedApps): yükleme penceresi gelmezse "zaten yüklü" denebilsin
        "related_applications": related_apps,
        "icons": icons,
    }
    return Response(content=json.dumps(manifest, ensure_ascii=False), media_type="application/manifest+json")

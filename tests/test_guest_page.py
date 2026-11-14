"""
ADR-40: misafir sayfası (/misafir) — sosyal medya profil bağlantısı: menü + kampanyalar; damga / giriş / kayıt yok,
çerez yazmaz, uygulama kabuğu (manifest, yükleme, servis çalışanı, alt menü) yüklenmez. 302 yok.
"""
from starlette.testclient import TestClient

from app.db import get_db
from app.main import app
from app.services.guest_page import asset_path, format_price

MANAGER = {"username": "mudur", "password": "Test-Mudur-2026"}


def _login(creds):
    c = TestClient(app, base_url="https://testserver")
    assert c.post("/panel/login", json=creds).json()["status"] == "success"
    return c


def _seed(mgr):
    ok = lambda r: r.json()["status"] == "success"  # noqa: E731
    assert ok(mgr.post("/api/admin/campaigns/create", json={
        "title": "Hafta Sonu Kahve Saati", "description": "İkinci kahve %50", "badge": "HAFTA SONU"}))
    assert ok(mgr.post("/api/admin/campaigns/create", json={
        "title": "Instagram'da Takip Et", "description": "Takip et, +1 damga kazan", "badge": "ÖDÜLLÜ",
        "reward_stamps": 1, "action_url": "https://instagram.com/x", "action_label": "Takip Et"}))
    assert ok(mgr.post("/api/admin/products/create", json={
        "name": "Çikolatalı Sufle", "price": 200, "discount_price": 150, "discount_label": "%25 İndirim",
        "category": "Tatlılar", "is_new": 1}))
    assert ok(mgr.post("/api/admin/products/create", json={"name": "Flat White", "price": 127.5, "category": "Kahveler"}))
    assert ok(mgr.post("/api/admin/products/create", json={"name": "Gizli Tatlı Taslağı", "price": 90, "category": "Tatlılar"}))
    with get_db(immediate=True) as conn:
        conn.execute("UPDATE products SET active = 0 WHERE name = 'Gizli Tatlı Taslağı'")


def test_guest_page_shows_menu_and_campaigns_without_app_shell(client):
    _seed(_login(MANAGER))
    for path in ("/k/misafir", "/k/misafir/", "/misafir"):
        r = TestClient(app, base_url="https://testserver").get(path)
        assert r.status_code == 200, path                       # 302 yok (ADR-01)
        assert "set-cookie" not in r.headers, path               # çerez / cihaz kaydı yok
    html = client.get("/k/misafir").text
    # menü + kampanya
    for needle in ("Çikolatalı Sufle", "150 ₺", "200 ₺", "%25 İndirim", "Yeni", "Flat White", "127,50 ₺",
                   "Tatlılar", "Kahveler", "Hafta Sonu Kahve Saati", "İkinci kahve %50", 'id="menu"', 'id="kampanyalar"'):
        assert needle in html, needle
    # pasif ürün ve damga veren görev (giriş ister) misafirde yok
    assert "Gizli Tatlı Taslağı" not in html
    assert "Instagram&#39;da Takip Et" not in html and "+1 damga" not in html
    # uygulama gibi görünmez: manifest, servis çalışanı, yükleme penceresi, giriş / damga yok
    for absent in ("manifest.webmanifest", "serviceWorker", "beforeinstallprompt", "apple-mobile-web-app-capable",
                   "user-scalable=no", "/api/", "auth-screen", "Telefon", "QR"):
        assert absent not in html, absent
    # sosyal medya önizlemesi (Instagram / WhatsApp kartı)
    assert 'property="og:title"' in html and 'property="og:image"' in html
    from app.config import PUBLIC_BASE_URL, PUBLIC_BASE_PATH
    assert f'<meta property="og:url" content="{PUBLIC_BASE_URL}{PUBLIC_BASE_PATH}/misafir" />' in html


def test_guest_page_relative_assets_follow_subpath(client):
    html = client.get("/k/misafir/").text
    assert 'href="../static/css/fonts.css' in html                # /k/misafir/ → ../ = /k/
    html = client.get("/k/misafir").text
    assert 'href="./static/css/fonts.css' in html


def test_guest_page_empty_menu_and_no_campaigns(client):
    with get_db(immediate=True) as conn:
        conn.execute("UPDATE products SET active = 0")
        conn.execute("UPDATE campaigns SET active = 0")
    html = client.get("/k/misafir").text
    assert "Menü yakında burada." in html and 'id="kampanyalar"' not in html


def test_price_and_asset_helpers():
    assert format_price(125) == "125 ₺" and format_price(127.5) == "127,50 ₺" and format_price(1250) == "1.250 ₺"
    assert format_price(0) == "" and format_price("x") == "" and format_price(None) == ""
    assert asset_path("/static/uploads/a.jpg") == "static/uploads/a.jpg"
    assert asset_path("/k/static/uploads/a.jpg") == "static/uploads/a.jpg"
    for bad in ("https://evil.example/a.jpg", "javascript:alert(1)", "data:image/png;base64,xx", ""):
        assert asset_path(bad) == "", bad
    assert not asset_path("//evil.example/a.jpg").startswith("/")    # protokolsüz dış adres de göreli (yerel) kalır


def test_manager_panel_offers_guest_link_and_qr(client, monkeypatch):
    mgr = _login(MANAGER)
    ov = mgr.get("/api/admin/overview").json()
    assert ov["guest_url"].endswith("/misafir")
    page = mgr.get("/mudur").text
    for needle in ('id="card-guest-link"', "Sosyal Medya Bağlantısı", "rGuestLink", "kind=misafir"):
        assert needle in page, needle
    import app.routes.manager as manager_routes
    seen = {}
    monkeypatch.setattr(manager_routes, "generate_qr_png_bytes", lambda url: seen.setdefault("url", url) and b"png")
    r = mgr.get("/api/admin/qr-image?kind=misafir&download=1")
    assert r.status_code == 200 and seen["url"].endswith("/misafir")
    assert "qr-misafir-menu.png" in r.headers.get("content-disposition", "")

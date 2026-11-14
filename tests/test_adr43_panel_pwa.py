"""
ADR-43: Yönetim (müdür) ve Kasa (personel) panelleri uygulama (PWA) olarak yüklenir. Her birinin kendi manifesti, adı,
başlangıç adresi ve dar kapsamı vardır (müşteri kartı ile çakışmaz); servis çalışanı müşteri kartıyla ortaktır.
"""
import json

from starlette.testclient import TestClient

from app.main import app

MANAGER = {"username": "mudur", "password": "Test-Mudur-2026"}
STAFF = {"username": "kasa1", "password": "Test-Kasa-2026"}


def _login(creds):
    c = TestClient(app, base_url="https://testserver")
    assert c.post("/panel/login", json={**creds, "remember": True}).json()["status"] == "success"
    return c


def test_panel_manifests_are_separate_installable_apps(client):
    seen = {}
    for app_key, short in (("mudur", "Yönetim"), ("personel", "Kasa")):
        for path in (f"/k/{app_key}.webmanifest", f"/{app_key}.webmanifest"):
            r = client.get(path)
            assert r.status_code == 200 and r.headers["content-type"].startswith("application/manifest+json"), path
            m = json.loads(r.text)
            assert m["short_name"] == short and m["name"].endswith(short)
            assert m["start_url"] == f"./{app_key}" and m["scope"] == f"./{app_key}"     # müşteri kartının kapsamıyla çakışmaz
            assert m["display"] == "standalone" and {i["sizes"] for i in m["icons"]} == {"192x192", "512x512"}
            seen[app_key] = m
    customer = json.loads(client.get("/k/manifest.webmanifest").text)
    assert customer["scope"] == "./" and customer["start_url"] == "./kart/"
    assert seen["mudur"]["start_url"] != customer["start_url"]


def test_panels_link_their_manifest_and_install_button(client):
    pages = (
        ("/k/mudur", None, "mudur.webmanifest", "Yönetim"),          # giriş formu da yüklenebilir
        ("/k/personel", None, "personel.webmanifest", "Kasa"),
        ("/k/mudur", MANAGER, "mudur.webmanifest", "Yönetim"),
        ("/k/personel", STAFF, "personel.webmanifest", "Kasa"),
    )
    for path, creds, manifest, title in pages:
        c = _login(creds) if creds else TestClient(app, base_url="https://testserver")
        html = c.get(path).text
        assert f'<link rel="manifest" href="./{manifest}" />' in html, path
        assert f'<meta name="apple-mobile-web-app-title" content="{title}" />' in html, path
        assert 'rel="apple-touch-icon"' in html and "__panelInstallEvt" in html, path
        assert "data-pwa-install" in html and "panelInstall()" in html, path
    # ortak giriş /giris hangi panelin açılacağını bilmez: uygulama manifesti eklenmez
    assert 'rel="manifest"' not in TestClient(app, base_url="https://testserver").get("/k/giris").text


def test_panel_js_installs_and_shares_customer_service_worker(client):
    js = client.get("/k/static/js/panel.js").text
    assert "window.panelInstall" in js and "beforeinstallprompt" in js and "appinstalled" in js
    assert 'navigator.serviceWorker.register(APP_BASE + "sw.js")' in js      # müşteri kartıyla aynı kayıt
    assert "Ana Ekrana Ekle" in js                                           # iPhone / iPad talimatı
    assert "visibilitychange" in _login(MANAGER).get("/k/mudur").text        # öne gelince veriler tazelenir

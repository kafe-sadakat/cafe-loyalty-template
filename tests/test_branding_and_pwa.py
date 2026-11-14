"""Varsayılan marka, telefon biçimi, anti-fraud, personel hesapları, ürün etiketleri, PWA, tema."""
import sqlite3

from starlette.testclient import TestClient

from app.db import get_db, init_db
from app.main import app
from app.utils import format_phone, hash_token, phone_hash

CONSENT = {"kvkk_read": True, "explicit_consent": True}
PHONE = "532 800 11 22"
E164 = "+905328001122"


def _manager(client):
    r = client.post("/panel/login", json={"username": "mudur", "password": "Test-Mudur-2026"})
    assert r.json()["status"] == "success" and r.json()["redirect"] == "mudur", r.text


def _new_client():
    return TestClient(app, base_url="https://testserver")


def _login_customer(client, phone, device_token=None):
    body = {"phone": phone, **CONSENT}
    if device_token:
        body["device_token"] = device_token
    r = client.post("/api/auth/login", json=body).json()
    assert r["status"] == "success", r
    return r["device_token"]


# ------------------------------------------------------------------ marka + telefon biçimi
def test_phone_display_format_has_no_leading_zero():
    assert format_phone("+905321234567") == "532 123 45 67"


def test_phone_input_accepts_5xx_format_and_ui_uses_it(client):
    tok = _login_customer(client, PHONE)  # başında 0 olmadan
    card = client.get("/api/home-data", headers={"X-Device-Token": tok}).json()["customer"]
    assert card["phone_e164"] == E164 and card["formatted_phone"] == "532 800 11 22"
    page = client.get("/k/kart/").text
    assert 'placeholder="5XX XXX XX XX"' in page and "05XX" not in page


def test_brand_is_configured_default_everywhere(client):
    from app.config import DEFAULT_BUSINESS_NAME
    page = client.get("/k/kart/").text
    assert DEFAULT_BUSINESS_NAME in page and "Kahve Durağı" not in page and "Hakkımızda" not in page
    manifest = client.get("/manifest.webmanifest").json()
    assert manifest["name"] == DEFAULT_BUSINESS_NAME and "Kahve Durağı" not in str(manifest)
    assert "Kahve Durağı" not in client.get("/sw.js").text


def test_legacy_brand_name_is_migrated(client):
    from app.config import DEFAULT_BUSINESS_NAME
    with get_db(immediate=True) as conn:
        conn.execute("UPDATE settings SET value = 'Kahve Durağı' WHERE key = 'business_name'")
    init_db()
    with get_db() as conn:
        assert conn.execute("SELECT value FROM settings WHERE key = 'business_name'").fetchone()["value"] == DEFAULT_BUSINESS_NAME


# ------------------------------------------------------------------ anti-fraud
def test_deleted_account_returning_gets_clean_start_and_no_bonuses(client):
    tok = _login_customer(client, PHONE)
    hdr = {"X-Device-Token": tok}
    assert client.post("/api/claim-install-bonus", json={}, headers=hdr).json()["status"] == "success"
    # kayıt hediyesi (ADR-35) + uygulama hediyesi = 2 damga var; hesabı sil
    me = client.get("/api/me/card", headers=hdr).json()
    assert me["current_stamps"] == 2
    assert client.post("/api/delete-my-data", json={"phone": PHONE}, headers=hdr).json()["status"] == "success"

    # Kayıt defterinde numara DÜZ METİN değil, yalnızca özet var
    with get_db() as conn:
        rows = conn.execute("SELECT key_type, key_hash FROM deleted_customers_registry").fetchall()
        keys = {(r["key_type"], r["key_hash"]) for r in rows}
        assert ("phone", phone_hash(E164)) in keys and ("device", hash_token(tok)) in keys
        assert E164 not in str([tuple(r) for r in rows]) and "532800" not in str([tuple(r) for r in rows])

    # Aynı numarayla (yeni cihaz) tekrar kayıt: 0 damga, bonuslar kilitli
    fresh = _new_client()
    tok2 = _login_customer(fresh, PHONE)
    hdr2 = {"X-Device-Token": tok2}
    assert fresh.get("/api/me/card", headers=hdr2).json()["current_stamps"] == 0
    assert fresh.post("/api/claim-install-bonus", json={}, headers=hdr2).json()["status"] == "already_claimed"
    assert fresh.post("/api/push/subscribe", json={"endpoint": "https://push.example/ep1", **{"p256dh": "BNcRdreALRFXTkOOUHK1EtK2wtaz5Ry4YfYCA_0QTpQ", "auth": "tBHItJI5svbpez7KI4CCXg"}}, headers=hdr2).status_code == 200
    assert fresh.post("/api/claim-notif-bonus", headers=hdr2).json()["status"] in ("already_claimed", "no_subscription")
    with get_db() as conn:
        c = conn.execute("SELECT pwa_bonus_claimed, notif_bonus_claimed FROM customers WHERE phone_e164 = ?", (E164,)).fetchone()
        assert c["pwa_bonus_claimed"] == 1 and c["notif_bonus_claimed"] == 1


def test_same_device_token_with_new_phone_after_delete_is_also_locked(client):
    tok = _login_customer(client, PHONE)
    assert client.post("/api/delete-my-data", json={"phone": PHONE}, headers={"X-Device-Token": tok}).json()["status"] == "success"
    # Silen kişi yerel depodaki cihaz belirtecini koruyup BAŞKA numarayla kayıt olur
    fresh = _new_client()
    tok2 = _login_customer(fresh, "533 900 11 22", device_token=tok)
    assert fresh.post("/api/claim-install-bonus", json={}, headers={"X-Device-Token": tok2}).json()["status"] == "already_claimed"


def test_new_phone_on_same_device_inherits_used_bonus(client):
    tok = _login_customer(client, PHONE)
    assert client.post("/api/claim-install-bonus", json={}, headers={"X-Device-Token": tok}).json()["status"] == "success"
    tok2 = _login_customer(client, "534 111 22 33", device_token=tok)   # aynı cihaz, ikinci hesap
    assert client.post("/api/claim-install-bonus", json={}, headers={"X-Device-Token": tok2}).json()["status"] == "already_claimed"


def test_normal_new_customer_still_gets_bonus_once(client):
    tok = _login_customer(client, PHONE)
    h = {"X-Device-Token": tok}
    assert client.post("/api/claim-install-bonus", json={}, headers=h).json()["status"] == "success"
    assert client.post("/api/claim-install-bonus", json={}, headers=h).json()["status"] == "already_claimed"


def test_admin_delete_and_staff_register_also_reach_registry_guard(client):
    _manager(client)
    cid = client.post("/api/admin/customers/create", json={"phone": PHONE, "consent_confirmed": True}).json()["customer"]["id"]
    assert client.post(f"/api/admin/customers/{cid}/delete").json()["status"] == "success"
    new = client.post("/api/admin/customers/create", json={"phone": PHONE, "consent_confirmed": True}).json()["customer"]
    with get_db() as conn:
        c = conn.execute("SELECT pwa_bonus_claimed, notif_bonus_claimed FROM customers WHERE id = ?", (new["id"],)).fetchone()
        assert c["pwa_bonus_claimed"] == 1 and c["notif_bonus_claimed"] == 1


# ------------------------------------------------------------------ personel hesapları + tek giriş
def test_manager_manages_staff_accounts_and_staff_logs_in_with_username(client):
    _manager(client)
    r = client.post("/api/admin/staff/create", json={"name": "Ayşe Kasiyer", "username": "ayse", "password": "kasa-parola-1"}).json()
    assert r["status"] == "success" and r["staff"]["username"] == "ayse"
    sid = r["staff"]["id"]
    assert "password" not in str(r)
    # doğrulamalar
    assert client.post("/api/admin/staff/create", json={"name": "X", "username": "AYSE", "password": "kasa-parola-1"}).json()["status"] == "error"
    assert client.post("/api/admin/staff/create", json={"name": "Y", "username": "yeni.kasa", "password": "kisa"}).json()["status"] == "error"
    # parola düz metin saklanmaz
    with get_db() as conn:
        h = conn.execute("SELECT password_hash FROM admin_users WHERE id = ?", (sid,)).fetchone()["password_hash"]
        assert h.startswith("pbkdf2_sha256$") and "kasa-parola-1" not in h
        conn.execute("INSERT INTO customers (phone_e164, display_name, created_at) VALUES ('+905361112233','Müşteri','2026-11-04T10:00:00+03:00')")

    staff = _new_client()
    assert staff.post("/panel/login", json={"username": "ayse", "password": "yanlis-parola"}).status_code == 401
    ok = staff.post("/panel/login", json={"username": "ayse", "password": "kasa-parola-1"}).json()
    assert ok["status"] == "success" and ok["role"] == "staff" and ok["redirect"] == "personel"
    assert staff.get("/api/staff/session").json()["name"] == "Ayşe Kasiyer"
    # yetki sınırı: müdür uçları kapalı
    assert staff.get("/api/admin/overview").status_code == 401
    assert staff.get("/api/admin/staff").status_code == 401
    # kasa işlemi kişiye bağlı iz bırakır
    assert staff.post("/api/staff/stamp", json={"phone": "536 111 22 33"}).json()["status"] == "success"
    logs = client.get("/api/admin/staff-logs").json()["logs"]
    stamp = [l for l in logs if l["action"] == "stamp_added"][0]
    assert stamp["staff_name"] == "Ayşe Kasiyer" and stamp["staff_id"] == sid

    # devre dışı bırakılınca açık oturum anında düşer
    client.post(f"/api/admin/staff/{sid}/update", json={"active": False})
    assert staff.get("/api/staff/session").status_code == 401
    assert _new_client().post("/panel/login", json={"username": "ayse", "password": "kasa-parola-1"}).status_code == 401
    # yeniden etkinleştir, parola sıfırla, sonra sil → log kalır
    client.post(f"/api/admin/staff/{sid}/update", json={"active": True, "password": "yeni-parola-22"})
    again = _new_client()
    assert again.post("/panel/login", json={"username": "ayse", "password": "yeni-parola-22"}).json()["status"] == "success"
    assert client.post(f"/api/admin/staff/{sid}/delete").json()["status"] == "success"
    assert again.get("/api/staff/session").status_code == 401
    assert any(l["staff_name"] == "Ayşe Kasiyer" for l in client.get("/api/admin/staff-logs").json()["logs"])


def test_login_pages_are_single_screen_without_redirects(client):
    for path in ("/mudur", "/personel", "/yonetim"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 200
        # tek form: kullanıcı adı + parola; tema düğmesi korunur, rol seçici kalmadı
        assert 'id="lg-user"' in r.text and 'id="lg-pass"' in r.text and "data-theme-toggle" in r.text
        assert 'id="mode-switch"' not in r.text
    # rol hesaptan gelir: müdür → mudur, personel → personel
    assert client.post("/panel/login", json={"username": "mudur", "password": "Test-Mudur-2026"}).json()["redirect"] == "mudur"


# ------------------------------------------------------------------ ürünler: yeni + indirim etiketleri
def test_product_new_flag_and_discount_label(client):
    _manager(client)
    p = client.post("/api/admin/products/create", json={"name": "Cheesecake", "price": 200, "is_new": 1, "discount_price": 160, "discount_label": "%20 İndirim"}).json()["product"]
    assert p["is_new"] == 1 and p["discount_price"] == 160 and p["discount_label"] == "%20 İndirim"
    bad = client.post("/api/admin/products/create", json={"name": "Hata", "price": 100, "discount_price": 120}).json()
    assert bad["status"] == "error"
    plain = client.post("/api/admin/products/create", json={"name": "Düz", "price": 50}).json()["product"]
    assert plain["is_new"] == 0 and plain["discount_price"] is None
    client.cookies.clear()
    menu = {x["name"]: x for x in client.get("/api/home-data").json()["products"]}
    assert menu["Cheesecake"]["is_new"] == 1 and menu["Cheesecake"]["discount_price"] == 160
    page = client.get("/k/kart/").text
    assert "priceBadge" in page and "tag-new" in page and "line-through" in page


# ------------------------------------------------------------------ PWA
def test_manifest_has_192_512_and_maskable_icons_and_guide_replaces_chrome_warning(client):
    m = client.get("/manifest.webmanifest").json()
    sizes = {(i["sizes"], i["purpose"]) for i in m["icons"]}
    assert ("192x192", "any") in sizes and ("512x512", "any") in sizes and ("512x512", "maskable") in sizes
    assert m["display"] == "standalone" and m["start_url"] and m["scope"] and m["prefer_related_applications"] is False
    for icon in m["icons"]:
        assert client.get(icon["src"]).status_code == 200
    page = client.get("/k/indir").text
    assert "Chrome ile açın" not in page and "engage-install-guide" in page and "pwa-guide-modal" not in page and "beforeinstallprompt" in page
    assert ".apk" not in page.lower()


def test_push_title_carries_brand(client):
    from app.config import DEFAULT_BUSINESS_NAME
    from app.services.push import brand_title, get_push_branding
    with get_db() as conn:
        b = get_push_branding(conn)
    assert brand_title(b, "Hafta sonu keyfi") == f"{DEFAULT_BUSINESS_NAME} · Hafta sonu keyfi"
    assert brand_title(b, f"Günaydın {DEFAULT_BUSINESS_NAME} ailesi!") == f"Günaydın {DEFAULT_BUSINESS_NAME} ailesi!"
    assert b["icon"].endswith("icon-192.png")


# ------------------------------------------------------------------ menü tohumlama
def test_menu_seed_runs_once_and_respects_manager_deletions(monkeypatch):
    monkeypatch.setenv("MENU_SEED", "1")
    init_db()
    with get_db() as conn:
        n = conn.execute("SELECT COUNT(*) AS c FROM products").fetchone()["c"]
        assert n == 84   # 79 menü ürünü + 5 gizli tatlı taslağı
        assert conn.execute("SELECT price FROM products WHERE name = 'Espresso'").fetchone()["price"] == 130
        big = conn.execute("SELECT description FROM products WHERE name = 'Iced Latte'").fetchone()["description"]
        assert "Büyük boy: 220" in big
        drafts = conn.execute("SELECT active, image_url FROM products WHERE category = 'Tatlılar'").fetchall()
        assert len(drafts) == 5 and all(d["active"] == 0 and d["image_url"].startswith("static/uploads/seed-") for d in drafts)
        conn.execute("DELETE FROM products WHERE name = 'Espresso'")
    init_db()      # bayrak var: silinen ürün geri gelmez
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM products WHERE name = 'Espresso'").fetchone()["c"] == 0


# ------------------------------------------------------------------ tema
def test_theme_palettes_exist_in_compiled_css(client):
    css = client.get("/k/static/css/app.css").text
    flat = css.replace('"', "")
    assert "data-theme=soft" in flat and "data-theme=dark" in flat
    assert "26 23 20" in css and "212 175 55" in css and "250 249 247" in css   # #1A1714 espresso, #D4AF37 gold, #FAF9F7 canvas

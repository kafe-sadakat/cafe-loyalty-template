"""Müşteri paneli revizyonu (ADR-13): tek giriş, kategoriler, çağrı metinleri, analitik/drill-down, damga ayarı, kasada arama."""
from starlette.testclient import TestClient

from app.db import get_db
from app.main import app

MANAGER = {"username": "mudur", "password": "Test-Mudur-2026"}
STAFF = {"username": "kasa1", "password": "Test-Kasa-2026"}


def _new_client():
    return TestClient(app, base_url="https://testserver")


def _manager(client):
    r = client.post("/panel/login", json=MANAGER).json()
    assert r["status"] == "success" and r["role"] == "manager", r


def _customer(client, phone, name=None):
    body = {"phone": phone, "consent_confirmed": True}
    if name:
        body["display_name"] = name
    r = client.post("/api/admin/customers/create", json=body).json()
    assert r["status"] == "success", r
    return r["customer"]["id"]


# ------------------------------------------------------------------ tek giriş + roller
def test_seeded_accounts_route_by_role(client):
    m = client.post("/panel/login", json=MANAGER).json()
    assert m["role"] == "manager" and m["redirect"] == "mudur"
    s = _new_client().post("/panel/login", json=STAFF).json()
    assert s["role"] == "staff" and s["redirect"] == "personel"
    # kullanıcı adı büyük/küçük harf duyarsız
    assert _new_client().post("/panel/login", json={"username": "MUDUR", "password": "Test-Mudur-2026"}).json()["role"] == "manager"


def test_giris_page_is_200_and_role_aware(client):
    r = client.get("/giris", follow_redirects=False)
    assert r.status_code == 200 and 'id="lg-user"' in r.text
    _manager(client)
    assert "tab-overview" in client.get("/giris").text
    staff = _new_client()
    staff.post("/panel/login", json=STAFF)
    page = staff.get("/giris")
    assert page.status_code == 200 and "cust-search" in page.text and "tab-overview" not in page.text
    # personel müdür API'sine giremez
    assert staff.get("/api/admin/analytics").status_code == 401


def test_manager_changes_own_username_and_password(client):
    _manager(client)
    bad = client.post("/api/admin/account", json={"current_password": "yanlis", "username": "yeni.mudur"}).json()
    assert bad["status"] == "error"
    short = client.post("/api/admin/account", json={"current_password": "Test-Mudur-2026", "new_password": "kisa"}).json()
    assert short["status"] == "error"
    ok = client.post("/api/admin/account", json={
        "current_password": "Test-Mudur-2026", "username": "yeni.mudur", "new_password": "Yeni-Sifre-2026", "name": "Ahmet Bey",
    }).json()
    assert ok["status"] == "success" and ok["account"]["username"] == "yeni.mudur"
    assert client.get("/api/admin/session").json()["name"] == "Ahmet Bey"  # oturum çıkış gerektirmeden yenilendi
    assert _new_client().post("/panel/login", json=MANAGER).status_code == 401
    assert _new_client().post("/panel/login", json={"username": "yeni.mudur", "password": "Yeni-Sifre-2026"}).json()["role"] == "manager"
    # personel kullanıcı adıyla çakışma
    clash = client.post("/api/admin/account", json={"current_password": "Yeni-Sifre-2026", "username": "kasa1"}).json()
    assert clash["status"] == "error"


def test_staff_accounts_use_usernames_and_can_be_edited(client):
    _manager(client)
    created = client.post("/api/admin/staff/create", json={"name": "Deniz", "username": "kasa2", "password": "kasa-parola-2"}).json()
    assert created["status"] == "success"
    sid = created["staff"]["id"]
    assert client.post("/api/admin/staff/create", json={"name": "X", "username": "KASA2", "password": "kasa-parola-2"}).json()["status"] == "error"
    assert client.post("/api/admin/staff/create", json={"name": "Y", "username": "a b", "password": "kasa-parola-2"}).json()["status"] == "error"
    upd = client.post(f"/api/admin/staff/{sid}/update", json={"username": "kasa-deniz", "password": "Yeni-parola-3", "name": "Deniz K."}).json()
    assert upd["status"] == "success" and upd["staff"]["username"] == "kasa-deniz"
    assert _new_client().post("/panel/login", json={"username": "kasa2", "password": "kasa-parola-2"}).status_code == 401
    assert _new_client().post("/panel/login", json={"username": "kasa-deniz", "password": "Yeni-parola-3"}).json()["role"] == "staff"
    names = [s["username"] for s in client.get("/api/admin/staff").json()["staff"]]
    assert "kasa1" in names and "kasa-deniz" in names


# ------------------------------------------------------------------ kategoriler
def test_default_categories_and_crud(client):
    _manager(client)
    cats = client.get("/api/admin/categories").json()["categories"]
    assert [c["name"] for c in cats][:4] == ["Sıcak Kahveler", "Soğuk Kahveler", "Tatlılar", "Atıştırmalıklar"]
    assert client.post("/api/admin/categories/create", json={"name": "tatlılar"}).json()["status"] == "error"  # büyük/küçük harf duyarsız
    new = client.post("/api/admin/categories/create", json={"name": "Kahvaltı"}).json()
    assert new["status"] == "success"
    kid = new["category"]["id"]
    # sırala: Kahvaltı en başa
    order = [kid] + [c["id"] for c in cats]
    out = client.post("/api/admin/categories/reorder", json={"ids": order}).json()["categories"]
    assert out[0]["name"] == "Kahvaltı"
    # yeniden adlandır → ürünler de taşınır
    p = client.post("/api/admin/products/create", json={"name": "Menemen", "price": 90, "category": "Kahvaltı"}).json()["product"]
    assert p["category"] == "Kahvaltı"
    assert client.post(f"/api/admin/categories/{kid}/update", json={"name": "Sabah Menüsü"}).json()["status"] == "success"
    assert [x["category"] for x in client.get("/api/admin/products").json()["products"]] == ["Sabah Menüsü"]
    # sil → ürün 'Genel'e taşınır, silinmez
    assert client.post(f"/api/admin/categories/{kid}/delete").json()["status"] == "success"
    prods = client.get("/api/admin/products").json()["products"]
    assert len(prods) == 1 and prods[0]["category"] == "Genel"
    assert "Genel" in [c["name"] for c in client.get("/api/admin/categories").json()["categories"]]


def test_unknown_product_category_is_added_and_public_menu_is_ordered(client):
    _manager(client)
    client.post("/api/admin/products/create", json={"name": "Tiramisu", "price": 120, "category": "Tatlılar"})
    client.post("/api/admin/products/create", json={"name": "Latte", "price": 100, "category": "Sıcak Kahveler"})
    client.post("/api/admin/products/create", json={"name": "Pasif", "price": 10, "category": "Soğuk Kahveler", "active": 0})
    client.post("/api/admin/products/create", json={"name": "Özel", "price": 10, "category": "Yeni Kategori"})
    assert "Yeni Kategori" in [c["name"] for c in client.get("/api/admin/categories").json()["categories"]]
    client.cookies.clear()
    data = client.get("/api/home-data").json()
    # yalnızca aktif ürünü olan kategoriler, müdürün sırasıyla
    assert data["categories"] == ["Sıcak Kahveler", "Tatlılar", "Yeni Kategori"]
    assert [p["name"] for p in data["products"]] == ["Latte", "Tiramisu", "Özel"]


# ------------------------------------------------------------------ çağrı metinleri (ADR-21: kaldırıldı)
def test_cta_texts_are_removed_from_panel_and_customer_screen(client):
    _manager(client)
    ov = client.get("/api/admin/overview").json()["settings"]
    assert not any(k in ov for k in ("notif_cta_title", "notif_cta_text", "install_cta_title", "install_cta_text"))
    page = client.get("/mudur").text
    assert 'id="card-cta"' not in page and "Çağrı Metinleri" not in page
    client.post("/api/admin/settings", json={"notif_cta_title": "eski"})  # eski alan yok sayılır
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM settings WHERE key LIKE '%_cta_%'").fetchone()["c"] == 0
    client.cookies.clear()
    assert "cta" not in client.get("/api/home-data").json()


def test_customer_page_hides_brand_tagline_and_username_outside_profile(client):
    page = client.get("/k/kart/").text
    assert "Sadakat Kulübü" not in page
    assert "home-user-name" not in page and 'id="home-engage-card"' in page and 'id="home-gift-ready"' in page
    assert "Kasada ödeme yaparken personeli bilgilendirin" in page


def test_manager_page_has_no_greeting_and_clear_active_tab(client):
    _manager(client)
    page = client.get("/mudur").text
    assert "Bonjour" not in page
    assert page.count("side-link-active") >= 1 and 'id="crumb-title"' in page
    assert "metric-customers" not in page  # eski karşılama/KPI mozaiği yalnızca Genel sekmesinde yeni kartlarla


# ------------------------------------------------------------------ analitik + drill-down
def test_analytics_cards_and_segments(client):
    _manager(client)
    a = _customer(client, "0532 900 00 01", "ayse")
    b = _customer(client, "0532 900 00 02", "burak")
    _customer(client, "0532 900 00 03", "cem")
    for _ in range(5):  # 5 damga → hediye kazanır
        client.post(f"/api/admin/customers/{a}/stamp-add")
    client.post(f"/api/admin/customers/{b}/stamp-add")
    assert client.post(f"/api/admin/customers/{a}/redeem-reward").json()["status"] == "success"
    with get_db(immediate=True) as conn:
        conn.execute("UPDATE customers SET pwa_bonus_claimed = 1 WHERE id = ?", (a,))
        conn.execute("UPDATE customers SET push_opt_in = 1 WHERE id = ?", (b,))
        conn.execute(
            "INSERT INTO push_subscriptions (customer_id, endpoint, p256dh, auth, created_at, active) "
            "VALUES (?, 'https://push.example/1', 'k', 'a', '2026-11-05T10:00:00+03:00', 1)", (a,))
    an = client.get("/api/admin/analytics?days=7").json()
    c = an["cards"]
    assert c["customers_total"] == 3 and c["new_today"] == 3 and c["new_week"] == 3
    assert c["stamps_total"] == 6 and c["stamps_today"] == 6 and c["stamp_customers_today"] == 2
    assert c["gifts_earned"] == 1 and c["gifts_used"] == 1 and c["gifts_pending"] == 0
    assert c["push_on"] == 2 and c["pwa_on"] == 1
    assert len(an["series"]) == 7 and an["series"][-1]["stamps"] == 6 and an["series"][-1]["gifts"] == 1
    assert len(client.get("/api/admin/analytics?days=30").json()["series"]) == 30
    buckets = {x["key"]: x["count"] for x in an["distribution"]["stamp_buckets"]}
    assert buckets["bucket_0"] == 1 and buckets["bucket_1"] == 1 and buckets["bucket_5_7"] == 1

    def seg(kind):
        r = client.get(f"/api/admin/segment?kind={kind}").json()
        assert r["status"] == "success", r
        return r

    push = seg("push_on")
    assert push["total"] == 2 and {x["display_name"] for x in push["customers"]} == {"ayse", "burak"}
    row = [x for x in push["customers"] if x["display_name"] == "ayse"][0]
    assert row["phone"] == "532 900 00 01" and row["lifetime_stamps"] == 5 and row["last_activity"]
    assert [x["display_name"] for x in seg("pwa_on")["customers"]] == ["ayse"]
    assert [x["display_name"] for x in seg("gifts_used")["customers"]] == ["ayse"]
    assert {x["display_name"] for x in seg("stamps_today")["customers"]} == {"ayse", "burak"}
    assert seg("bucket_0")["total"] == 1 and seg("customers_all")["total"] == 3
    assert client.get("/api/admin/segment?kind=bilinmeyen").json()["status"] == "error"
    assert _new_client().get("/api/admin/segment?kind=push_on").status_code == 401


# ------------------------------------------------------------------ damga ayarı (+1 / -1 / belirli sayı)
def test_stamp_set_adjusts_card_to_exact_count(client):
    _manager(client)
    cid = _customer(client, "0532 910 00 01")
    r = client.post(f"/api/admin/customers/{cid}/stamp-set", json={"count": 3}).json()
    assert r["status"] == "success" and r["customer"]["current_stamps"] == 3
    r = client.post(f"/api/admin/customers/{cid}/stamp-set", json={"count": 1}).json()
    assert r["status"] == "success" and r["customer"]["current_stamps"] == 1
    assert client.post(f"/api/admin/customers/{cid}/stamp-set", json={"count": 5}).json()["status"] == "error"  # hedefe ulaşan = hediye
    assert client.post(f"/api/admin/customers/{cid}/stamp-set", json={"count": -1}).json()["status"] == "error"
    # hedef ayarı değişince üst sınır da değişir
    client.post("/api/admin/settings", json={"stamp_target": 8})
    assert client.post(f"/api/admin/customers/{cid}/stamp-set", json={"count": 7}).json()["customer"]["current_stamps"] == 7


# ------------------------------------------------------------------ kasada telefon / kullanıcı adı ile arama + hediye
def test_staff_search_by_phone_or_username_and_gift_flow(client):
    _manager(client)
    cid = _customer(client, "0532 920 00 01", "selin")
    _customer(client, "0532 920 00 02", "selim")
    for _ in range(5):
        client.post(f"/api/admin/customers/{cid}/stamp-add")

    staff = _new_client()
    staff.post("/panel/login", json=STAFF)
    assert staff.post("/api/staff/search", json={"q": "se"}).json()["status"] == "error"          # en az 3 karakter
    assert staff.post("/api/staff/search", json={"q": "532 920"}).json()["status"] == "error"      # telefon için en az 7 rakam
    by_phone = staff.post("/api/staff/search", json={"q": "532 920 00 01"}).json()
    assert by_phone["status"] == "success" and len(by_phone["customers"]) == 1
    cust = by_phone["customers"][0]
    assert cust["id"] == cid and cust["usable_rewards"] == 1 and "532 920" not in str(cust)    # telefon maskeli
    by_name = staff.post("/api/staff/search", json={"q": "selin"}).json()
    assert [c["id"] for c in by_name["customers"]] == [cid]
    prefix = staff.post("/api/staff/search", json={"q": "sel"}).json()
    assert len(prefix["customers"]) == 2
    assert staff.post("/api/staff/search", json={"q": "yokboyle"}).json()["status"] == "not_found"
    assert _new_client().post("/api/staff/search", json={"q": "selin"}).status_code == 401

    # hediyeyi arama sonucundaki müşteri kimliğiyle ver → personel loguna yazılır
    done = staff.post("/api/staff/redeem", json={"customer_id": cid}).json()
    assert done["status"] == "success" and done["customer"]["usable_rewards"] == 0
    assert staff.post("/api/staff/redeem", json={"customer_id": cid}).json()["status"] == "no_reward"
    logs = client.get("/api/admin/staff-logs").json()["logs"]
    gift = [l for l in logs if l["action"] == "reward_redeemed"][0]
    assert gift["staff_name"] == "Kasa 1" and gift["customer_id"] == cid


def test_manager_gift_is_written_to_staff_logs_too(client):
    _manager(client)
    cid = _customer(client, "0532 930 00 01", "gizem")
    for _ in range(5):
        client.post(f"/api/admin/customers/{cid}/stamp-add")
    assert client.post(f"/api/admin/customers/{cid}/redeem-reward").json()["status"] == "success"
    logs = client.get("/api/admin/staff-logs").json()["logs"]
    gift = [l for l in logs if l["action"] == "reward_redeemed" and l["customer_id"] == cid]
    assert gift and gift[0]["staff_name"] == "Müdür" and "Müdür panelinden" in gift[0]["detail"]

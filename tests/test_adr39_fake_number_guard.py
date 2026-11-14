"""
ADR-39: rastgele (uydurma) numarayla kayıt olup hediye damga toplamaya karşı koruma.
- Hediye damgaları (kayıt / uygulama yükleme / bildirim) ilk GERÇEK damgaya (kasa QR'ı, NFC, kasiyer) kadar bekletilir;
  evden açılan sahte hesap hiçbir damga toplayamaz. Gerçek damga gelince bekleyenler aynı anda karta eklenir.
- Uydurma görünen numara (5555555555, 532 111 11 11, 532 123 45 67) ile YENİ hesap açılamaz; mevcut müşteri girişi etkilenmez.
- Aynı telefonda ikinci numara kayıt hediyesi almaz (ama o ziyaretin damgasını alır).
- Aynı IP'den saatte en fazla NEW_ACCOUNT_IP_LIMIT yeni hesap.
- Panelden kapatılınca eski davranış (hemen verme) geri gelir.
"""
from starlette.testclient import TestClient

from app.db import get_db, set_setting
from app.main import app
from app.services import loyalty, stamp_codes
from app.services.program_terms import build_program_terms

CONSENT = {"kvkk_read": True}
MANAGER = {"username": "mudur", "password": "Test-Mudur-2026"}
STAFF = {"username": "kasa1", "password": "Test-Kasa-2026"}


def _client():
    return TestClient(app, base_url="https://testserver")


def _guard(on=True, daily=5):
    with get_db(immediate=True) as conn:
        set_setting(conn, "gift_requires_visit", "1" if on else "0")
        set_setting(conn, "daily_limit", str(daily))


def _login(c, phone, **extra):
    return c.post("/api/auth/login", json={"phone": phone, **CONSENT, **extra}).json()


def _sources(phone_e164):
    with get_db() as conn:
        return [r[0] for r in conn.execute(
            "SELECT s.source FROM stamps s JOIN customers c ON c.id = s.customer_id WHERE c.phone_e164 = ? "
            "AND s.voided_at IS NULL ORDER BY s.id", (phone_e164,))]


def _cid(phone_e164):
    with get_db() as conn:
        return conn.execute("SELECT id FROM customers WHERE phone_e164 = ?", (phone_e164,)).fetchone()[0]


def _screen_code():
    with get_db(immediate=True) as conn:
        return stamp_codes.create_stamp_code(conn, created_by="test")["code"]


def _panel(creds):
    c = _client()
    assert c.post("/panel/login", json=creds).json()["status"] == "success"
    return c


# ---------------------------------------------------------------- 1. evden sahte hesap: hiçbir damga yok
def test_home_registration_gets_no_stamps_until_first_real_stamp(client):
    _guard()
    c = _client()
    r = _login(c, "0532 980 39 01")
    assert r["status"] == "success" and r["welcome_gift"] is False and r["welcome_pending"] is True
    assert r["current_stamps"] == 0 and "ilk ziyaretinde" in r["welcome_message"]
    h = {"X-Device-Token": r["device_token"]}

    inst = c.post("/api/claim-install-bonus", json={}, headers=h).json()
    assert inst["status"] == "pending" and inst["pwa_bonus_claimed"] == 1 and inst["current_stamps"] == 0
    again = c.post("/api/claim-install-bonus", json={}, headers=h).json()
    assert again["status"] == "pending"                                   # ikinci istek damga üretmez
    n = c.post("/api/customer/notification-permission", json={"permission": "granted"}, headers=h).json()
    assert n["status"] == "success" and n["bonus_awarded"] is False and n["bonus_status"] == "pending"
    assert "ilk ziyaretinde" in n["message"]

    me = c.get("/api/home-data", headers=h).json()["customer"]
    assert me["current_stamps"] == 0 and me["pending_gifts"] == 3
    assert _sources("+905329803901") == []


def test_first_real_stamp_releases_all_pending_gifts(client):
    _guard(daily=1)
    c = _client()
    r = _login(c, "0532 980 39 02")
    h = {"X-Device-Token": r["device_token"]}
    c.post("/api/claim-install-bonus", json={}, headers=h)
    c.post("/api/customer/notification-permission", json={"permission": "granted"}, headers=h)

    s = c.post("/api/claim", json={"claim_token": "/d/" + _screen_code()}, headers=h).json()
    assert s["status"] == "success" and s["current_stamps"] == 4, s
    assert "Bekleyen 3 hediye" in s["message"] and s["welcome_gift"] is False
    assert sorted(_sources("+905329803902")) == sorted(["qr", "welcome", "bonus_pwa", "bonus_notif"])
    me = c.get("/api/home-data", headers=h).json()["customer"]
    # hediyeler günlük limite sayılmaz: bugün yalnızca 1 (QR) damga
    assert me["pending_gifts"] == 0 and me["today_stamps"] == 1
    with get_db() as conn:
        row = conn.execute("SELECT welcome_bonus_awarded, welcome_bonus_at, pwa_bonus_at, push_bonus_at FROM customers "
                           "WHERE phone_e164 = '+905329803902'").fetchone()
        assert row[0] == 1 and row[1] and row[2] and row[3]
    # ikinci gerçek damga bir şey daha eklemez
    with get_db(immediate=True) as conn:
        set_setting(conn, "daily_limit", "5")
    s2 = c.post("/api/claim", json={"claim_token": "/d/" + _screen_code()}, headers=h).json()
    assert s2["current_stamps"] == 5 or s2.get("reward_unlocked")
    assert "Bekleyen" not in s2["message"]


def test_cashier_stamp_also_releases_pending_gift(client):
    _guard()
    c = _client()
    _login(c, "0532 980 39 03")
    staff = _panel(STAFF)
    assert staff.post("/api/staff/stamp", json={"customer_id": _cid("+905329803903")}).json()["status"] == "success"
    assert sorted(_sources("+905329803903")) == ["staff", "welcome"]


def test_registering_at_the_till_gets_gift_and_stamp_at_once(client):
    _guard(daily=1)
    c = _client()
    r = c.post("/api/claim", json={"claim_token": "/d/" + _screen_code(), "phone": "0532 980 39 04", **CONSENT}).json()
    assert r["status"] == "success" and r["welcome_gift"] is True and r["current_stamps"] == 2, r
    assert "Kayıt hediyen +1" in r["message"] and "Bekleyen" not in r["message"]


def test_kayit_qr_registration_message_says_gift_waits(client):
    _guard()
    c = _client()
    r = c.post("/api/claim", json={"claim_token": "/m/kayit", "phone": "0532 980 39 05", **CONSENT}).json()
    assert r["status"] == "registered" and r["welcome_pending"] is True and r["current_stamps"] == 0
    assert "ilk ziyaretinde" in r["message"]


# ---------------------------------------------------------------- 2. uydurma numara
def test_implausible_numbers_cannot_open_new_accounts(client):
    _guard()
    for phone in ("0555 555 55 55", "0505 050 50 50", "0532 111 11 11", "0532 123 45 67", "0532 765 43 21"):
        r = _login(_client(), phone)
        assert r["status"] != "success", phone
        assert "geçerli görünmüyor" in r["message"], phone
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 0
    # uygulama içi kayıt yolu da aynı
    r = _client().post("/api/claim", json={"claim_token": "/m/kayit", "phone": "0555 555 55 55", **CONSENT}).json()
    assert r["status"] == "error" and "geçerli görünmüyor" in r["message"]


def test_existing_customer_with_patterned_number_can_still_log_in(client):
    with get_db(immediate=True) as conn:
        conn.execute("INSERT INTO customers (phone_e164, created_at, display_name) VALUES "
                     "('+905321111111', '2026-01-01T10:00:00+03:00', 'eski')")
    r = _login(_client(), "0532 111 11 11")
    assert r["status"] == "success" and r["display_name"] == "eski"


def test_plausibility_rule():
    assert loyalty.implausible_phone("+905555555555")
    assert loyalty.implausible_phone("+905321234567")
    assert not loyalty.implausible_phone("+905329803901")
    assert not loyalty.implausible_phone("+905051234568")


# ---------------------------------------------------------------- 3. aynı telefonda ikinci numara
def test_second_number_on_same_phone_gets_no_welcome_gift_but_keeps_visit_stamp(client):
    _guard(on=False, daily=5)                                    # hemen-verme modunda bile cihaz başına 1 hediye
    c = _client()
    first = _login(c, "0532 980 39 10")
    assert first["welcome_gift"] is True
    token = first["device_token"]
    second = _login(c, "0532 980 39 11", device_token=token)
    assert second["status"] == "success" and second["welcome_gift"] is False and second["welcome_locked"] is True
    assert second["current_stamps"] == 0 and _sources("+905329803911") == []


# ---------------------------------------------------------------- 4. aynı bağlantıdan seri kayıt
def test_new_account_rate_limit_per_ip(client):
    _guard()
    for i in range(loyalty.NEW_ACCOUNT_IP_LIMIT):
        assert _login(_client(), f"0532 98{i // 10} 4{i % 10} 0{i % 7 + 1}")["status"] == "success", i
    blocked = _login(_client(), "0533 870 12 98")
    assert blocked["status"] != "success" and "çok fazla yeni kayıt" in blocked["message"]
    # mevcut müşterinin girişi sınırdan etkilenmez
    assert _login(_client(), "0532 980 40 01")["status"] == "success"


# ---------------------------------------------------------------- 5. panel anahtarı ve koşullar
def test_manager_switch_and_terms(client):
    mgr = _panel(MANAGER)
    assert mgr.get("/api/admin/overview").json()["settings"]["gift_requires_visit"] is False  # testlerde kapalı
    r = mgr.post("/api/admin/settings", json={"gift_requires_visit": True}).json()
    assert r["status"] == "success" and r["settings"]["gift_requires_visit"] == "1"
    assert mgr.get("/api/admin/overview").json()["settings"]["gift_requires_visit"] is True
    assert 'id="gift-requires-visit"' in mgr.get("/mudur").text
    assert "ilk damgayla birlikte" in build_program_terms({})
    assert "ilk damgayla birlikte" not in build_program_terms({"gift_requires_visit": "0"})


def test_switch_off_restores_immediate_gifts(client):
    _guard(on=False)
    r = _login(_client(), "0532 980 39 20")
    assert r["welcome_gift"] is True and r["current_stamps"] == 1 and r["welcome_pending"] is False

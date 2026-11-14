"""
ADR-38: uygulama yükleme / bildirim açma hediye damgaları müdür panelinden açılıp kapatılır.
Kapalıyken damga verilmez, hak kullanılmış sayılmaz (yeniden açılınca alınır), uygulama ve koşullar hediye vaat etmez.
Profil'de bildirim / yükleme / "güvenli değil" sorunları için yardım bölümü vardır.
"""
from starlette.testclient import TestClient

from app.main import app
from app.services.program_terms import build_program_terms

CONSENT = {"kvkk_read": True}
MANAGER = {"username": "mudur", "password": "Test-Mudur-2026"}


def _new_client():
    return TestClient(app, base_url="https://testserver")


def _register(phone):
    c = _new_client()
    r = c.post("/api/auth/login", json={"phone": phone, **CONSENT}).json()
    assert r["status"] == "success", r
    return c, {"X-Device-Token": r["device_token"]}


def _manager():
    m = _new_client()
    assert m.post("/panel/login", json=MANAGER).json()["status"] == "success"
    return m


def _set(mgr, **flags):
    r = mgr.post("/api/admin/settings", json=flags).json()
    assert r["status"] == "success", r
    return r["settings"]


def test_disabled_gifts_give_no_stamp_and_reenable_still_grants(client):
    mgr = _manager()
    s = _set(mgr, bonus_install_enabled=False, bonus_notif_enabled=False)
    assert s["bonus_install_enabled"] == "0" and s["bonus_notif_enabled"] == "0"
    ov = mgr.get("/api/admin/overview").json()["settings"]
    assert ov["bonus_install_enabled"] is False and ov["bonus_notif_enabled"] is False

    c, h = _register("0532 980 00 01")
    me = c.get("/api/home-data", headers=h).json()["customer"]
    assert me["bonus_install_enabled"] is False and me["bonus_notif_enabled"] is False
    before = me["current_stamps"]

    r = c.post("/api/claim-install-bonus", json={}, headers=h).json()
    assert r["status"] == "disabled"
    n = c.post("/api/customer/notification-permission", json={"permission": "granted"}, headers=h).json()
    assert n["status"] == "success" and n["bonus_awarded"] is False and n["message"] == "✅ Bildirimler açık."
    me = c.get("/api/home-data", headers=h).json()["customer"]
    assert me["current_stamps"] == before and me["pwa_bonus_claimed"] == 0 and me["notif_bonus_claimed"] == 0

    # yeniden açılınca hak duruyor: ikisi de bir kez verilir
    _set(mgr, bonus_install_enabled=True, bonus_notif_enabled=True)
    assert c.post("/api/claim-install-bonus", json={}, headers=h).json()["status"] == "success"
    assert c.post("/api/customer/notification-permission", json={"permission": "granted"}, headers=h).json()["bonus_awarded"] is True
    assert c.get("/api/home-data", headers=h).json()["customer"]["current_stamps"] == before + 2


def test_one_gift_off_other_still_on(client):
    mgr = _manager()
    _set(mgr, bonus_notif_enabled=False)
    c, h = _register("0532 980 00 02")
    assert c.post("/api/claim-install-bonus", json={}, headers=h).json()["status"] == "success"
    assert c.post("/api/customer/notification-permission", json={"permission": "granted"}, headers=h).json()["bonus_awarded"] is False


def test_program_terms_follow_gift_switches():
    on = build_program_terms({})
    assert "ana ekranına eklediğinde +1" in on and "bildirimlerine izin verdiğinde +1" in on
    off = build_program_terms({"bonus_install_enabled": "0", "bonus_notif_enabled": "0"})
    assert "ana ekranına eklediğinde" not in off and "bildirimlerine izin verdiğinde" not in off
    assert "kayıt olduğunda +1 hediye damga" in off


def test_manager_panel_has_gift_switches(client):
    page = _manager().get("/mudur").text
    assert 'id="card-gift-bonus"' in page and 'id="gift-install-on"' in page and 'id="gift-notif-on"' in page


def test_customer_profile_has_help_section(client):
    from urllib.parse import urlparse
    from app.config import PUBLIC_BASE_URL
    expected_host = urlparse(PUBLIC_BASE_URL).netloc or "localhost:8000"
    page = client.get("/k/kart/").text
    for marker in ('id="help-section"', 'id="help-notif"', 'id="help-install"', 'id="help-warn"',
                   expected_host, "function openHelp(", "function renderHelp("):
        assert marker in page, marker

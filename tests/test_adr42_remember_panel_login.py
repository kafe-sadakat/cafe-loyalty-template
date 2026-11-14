"""
ADR-42: "Bu cihazda oturumum açık kalsın" — müdür ve personel paneli o cihazda 30 gün açık kalır, panel açıldıkça
(günde en çok bir kez) yeniden 30 güne uzar ve eski çerezin oturumu iptal edilir. İşaretsiz giriş eskisi gibi 12 saat.
"""
import time

from starlette.testclient import TestClient

from app import auth
from app.main import app

MANAGER = {"username": "mudur", "password": "Test-Mudur-2026"}
STAFF = {"username": "kasa1", "password": "Test-Kasa-2026"}
DAY = 86400


def _client():
    return TestClient(app, base_url="https://testserver")


def _login(creds, **extra):
    c = _client()
    r = c.post("/panel/login", json={**creds, **extra})
    assert r.json()["status"] == "success", r.text
    return c, r


def _set_cookie(resp, name):
    for h in resp.headers.get_list("set-cookie"):
        if h.startswith(name + "=") and not h.startswith(name + '=""'):
            return h
    return ""


def _aged_token(monkeypatch, role, sess, days_old, remember=True):
    """`days_old` gün önce açılmış gibi bir oturum çerezi."""
    real = time.time()
    with monkeypatch.context() as m:
        m.setattr(auth.time, "time", lambda: real - days_old * DAY)
        return auth.create_session(role, sess["name"], sess["user_id"], pv=sess["pv"], remember=remember)


def test_remembered_login_lasts_30_days_for_manager_and_staff(client):
    for creds, cookie, role in ((MANAGER, auth.MANAGER_COOKIE, "manager"), (STAFF, auth.STAFF_COOKIE, "staff")):
        c, r = _login(creds, remember=True)
        assert "Max-Age=2592000" in _set_cookie(r, cookie), role
        sess = auth.verify_session(c.cookies.get(cookie), role)
        assert sess["remember"] is True and abs(sess["exp"] - time.time() - 30 * DAY) < 60
    c, r = _login(MANAGER)                                   # işaretsiz (eski istemci de) → 12 saat
    assert "Max-Age=43200" in _set_cookie(r, auth.MANAGER_COOKIE)
    assert auth.verify_session(c.cookies.get(auth.MANAGER_COOKIE), "manager")["remember"] is False


def test_opening_panel_renews_remembered_session_and_revokes_old_cookie(client, monkeypatch):
    for creds, cookie, role, page, probe in (
        (MANAGER, auth.MANAGER_COOKIE, "manager", "/mudur", "/api/admin/session"),
        (STAFF, auth.STAFF_COOKIE, "staff", "/personel", "/api/staff/session"),
    ):
        c, _ = _login(creds, remember=True)
        sess = auth.verify_session(c.cookies.get(cookie), role)
        # yeni açılmış oturum: panel açılınca yeniden yazılmaz (günde en çok bir kez)
        assert _set_cookie(_client().get(page, headers={"cookie": f"{cookie}={c.cookies.get(cookie)}"}), cookie) == ""
        old = _aged_token(monkeypatch, role, sess, days_old=5)
        r = _client().get(page, headers={"cookie": f"{cookie}={old}"})
        assert r.status_code == 200
        fresh = _set_cookie(r, cookie)
        assert "Max-Age=2592000" in fresh, role
        new = fresh.split(";", 1)[0].split("=", 1)[1]
        new_sess = auth.verify_session(new, role)
        assert new_sess["remember"] and new_sess["exp"] - time.time() > 29 * DAY
        assert _client().get(probe, headers={"cookie": f"{cookie}={new}"}).status_code == 200
        assert _client().get(probe, headers={"cookie": f"{cookie}={old}"}).status_code == 401   # eski kopya iptal


def test_unremembered_session_is_not_extended(client, monkeypatch):
    c, _ = _login(MANAGER, remember=False)
    sess = auth.verify_session(c.cookies.get(auth.MANAGER_COOKIE), "manager")
    old = _aged_token(monkeypatch, "manager", sess, days_old=0.25, remember=False)   # 6 saatlik
    r = _client().get("/mudur", headers={"cookie": f"{auth.MANAGER_COOKIE}={old}"})
    assert r.status_code == 200 and _set_cookie(r, auth.MANAGER_COOKIE) == ""
    expired = _aged_token(monkeypatch, "manager", sess, days_old=0.6, remember=False)  # 14 saatlik → süresi dolmuş
    assert auth.verify_session(expired, "manager") is None


def test_login_form_offers_remember_checked_by_default(client):
    page = client.get("/giris").text
    assert 'id="lg-remember" type="checkbox" checked' in page and "Bu cihazda oturumum açık kalsın" in page
    assert 'remember: document.getElementById("lg-remember").checked' in page

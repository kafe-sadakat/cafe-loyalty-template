"""Güvenlik denetimi (yama doğrulama): yetki, RBAC, yükleme sertleştirme, başlıklar, brute-force, anti-fraud akışı."""
import io
import re

from PIL import Image
from starlette.testclient import TestClient

from app.main import app

CONSENT = {"kvkk_read": True, "explicit_consent": True}
MANAGER = {"username": "mudur", "password": "Test-Mudur-2026"}
STAFF = {"username": "kasa1", "password": "Test-Kasa-2026"}

ADMIN_GET = ["/api/admin/overview", "/api/admin/customers", "/api/admin/products", "/api/admin/staff",
             "/api/admin/security/summary", "/api/admin/reports/months", "/api/admin/consents"]
ADMIN_POST = ["/api/admin/settings", "/api/admin/staff/create", "/api/admin/products/create",
              "/api/admin/campaigns/create", "/api/admin/push/send", "/api/admin/upload"]


def _client():
    return TestClient(app, base_url="https://testserver")


def _stamp_code():
    """Kasa ekranının tek kullanımlık Damga QR kodu (ADR-28)."""
    from app.db import get_db
    from app.services.stamp_codes import create_stamp_code
    with get_db(immediate=True) as conn:
        return create_stamp_code(conn, created_by="test")["code"]


def _login(creds):
    c = _client()
    r = c.post("/panel/login", json=creds)
    assert r.json()["status"] == "success", r.text
    return c


def _png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (10, 120, 60)).save(buf, format="PNG")
    return buf.getvalue()


def test_admin_and_staff_endpoints_reject_anonymous(client):
    c = _client()
    for path in ADMIN_GET:
        assert c.get(path).status_code in (401, 403), path
    for path in ADMIN_POST:
        assert c.post(path, json={}).status_code in (401, 403), path
    assert c.get("/api/staff/session").status_code in (401, 403)
    assert c.post("/api/staff/stamp", json={"customer_id": 1}).status_code in (401, 403)


def test_staff_cannot_reach_manager_endpoints(client):
    staff = _login(STAFF)
    assert staff.get("/api/staff/session").status_code == 200       # kendi uçları açık
    for path in ADMIN_GET:
        assert staff.get(path).status_code in (401, 403), path
    for path in ADMIN_POST:
        assert staff.post(path, json={}).status_code in (401, 403), path
    # müdür ayarını değiştirmeye çalışan personel: değer değişmemeli
    assert staff.post("/api/admin/settings", json={"stamp_target": 2}).status_code in (401, 403)
    mgr = _login(MANAGER)
    assert mgr.get("/api/admin/overview").status_code == 200
    assert mgr.get("/api/admin/overview").json()["settings"]["stamp_target"] != "2"


def test_session_cookie_flags(client):
    r = _client().post("/panel/login", json=MANAGER)
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=strict" in cookie


def test_forged_session_cookie_is_rejected(client):
    c = _client()
    c.cookies.set("mgr_session", "eyJyIjoibWFuYWdlciIsImUiOjk5OTk5OTk5OTl9.deadbeef")
    assert c.get("/api/admin/overview").status_code in (401, 403)


def test_security_headers_present(client):
    h = client.get("/k/kart/").headers
    assert h["x-content-type-options"] == "nosniff"
    assert h["x-frame-options"] == "DENY"
    assert h["referrer-policy"] in ("no-referrer", "strict-origin-when-cross-origin")
    assert "frame-ancestors 'none'" in h["content-security-policy"]
    assert "strict-transport-security" in h
    # CORS: dış kaynaklara açık değil
    r = client.get("/k/kart/", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in r.headers


def test_upload_rejects_non_image_extensions_and_disguised_content(client):
    mgr = _login(MANAGER)
    bad = [
        ("evil.html", b"<script>alert(1)</script>", "text/html"),
        ("evil.svg", b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>", "image/svg+xml"),
        ("evil.py", b"import os", "text/x-python"),
        ("evil.sh", b"#!/bin/sh\nid", "application/x-sh"),
        ("notes.txt", b"hello", "text/plain"),
        ("evil.png", b"<html><script>alert(1)</script></html>", "image/png"),   # uzantı yalan söylüyor
        ("evil.jpg.html", _png_bytes(), "image/png"),                            # çift uzantı
        ("noext", _png_bytes(), "image/png"),
    ]
    for name, data, mime in bad:
        r = mgr.post("/api/admin/upload", data={"kind": "content"}, files={"file": (name, data, mime)})
        assert r.json()["status"] == "error", name


def test_upload_rejects_oversize_and_accepts_valid_png_with_random_name(client):
    mgr = _login(MANAGER)
    big = _png_bytes() + b"\x00" * (5 * 1024 * 1024 + 10)
    r = mgr.post("/api/admin/upload", data={"kind": "content"}, files={"file": ("big.png", big, "image/png")})
    assert r.json()["status"] == "error"
    ok = mgr.post("/api/admin/upload", data={"kind": "content"}, files={"file": ("../../etc/photo.PNG", _png_bytes(), "image/png")})
    body = ok.json()
    assert body["status"] == "success", body
    assert re.fullmatch(r"static/uploads/[0-9a-f]{32}\.jpg", body["url"])   # yeniden adlandırıldı, kullanıcı adı yola karışmadı


def test_panel_login_is_rate_limited_per_ip(client):
    c = _client()
    codes = [c.post("/panel/login", json={"username": "mudur", "password": "yanlis"}).status_code for _ in range(12)]
    assert codes[0] == 401 and 429 in codes[10:]


def test_delete_then_reregister_same_phone_is_locked(client):
    """5551112233: kayıt (hediye + 1 damga) → silme → yeniden kayıt: 0 damga, bonuslar kullanıldı, Yeniden Kayıt işareti."""
    phone = "555 111 22 33"
    c = _client()
    first = c.post("/api/claim", json={"claim_token": _stamp_code(), "phone": phone, **CONSENT}).json()
    assert first["status"] == "success" and first["current_stamps"] == 2      # kayıt hediyesi (ADR-35) + damga
    tok = first["device_token"]
    assert c.post("/api/delete-my-data", json={"phone": phone}, headers={"X-Device-Token": tok}).json()["status"] == "success"

    again = _client().post("/api/claim", json={"claim_token": _stamp_code(), "phone": "0555 111 2233", **CONSENT}).json()
    assert again["status"] == "success" and again["current_stamps"] == 0 and again.get("welcome_locked") is True

    staff = _login(STAFF)
    cust = staff.post("/api/staff/customer-lookup", json={"q": "5551112233"}).json()["customers"][0]
    assert cust["is_re_registered"] is True and cust["current_stamps"] == 0
    assert cust["welcome_bonus_status"] == "blocked" and cust["pwa_bonus_status"] == "blocked" and cust["push_bonus_status"] == "blocked"

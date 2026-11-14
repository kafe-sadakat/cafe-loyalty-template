"""
ADR-41: kasadaki Damga QR'ının adresi tahmin edilemez (/m/{12 karakterlik gizli kod}). Yalnızca bu adres damga açılışı
üretir; eski /m/qr, /m/ altındaki başka adlar ve uygulama içi okutucuya okutulan ham değerler damga vermez. Müdür
adresi panelden yenileyebilir (eskisi hemen geçersiz). Kayıt QR'ı (/m/kayit) ve NFC 424 imzası etkilenmez.
"""
import re

from starlette.testclient import TestClient

from app.db import get_db, set_setting
from app.main import app
from app.services import stamp_codes
from app.services.loyalty import STALE_QR_MESSAGE

CONSENT = {"kvkk_read": True}
MANAGER = {"username": "mudur", "password": "Test-Mudur-2026"}
STAFF = {"username": "kasa1", "password": "Test-Kasa-2026"}
SECRET = "k7p2x9qz4hmn"


def _new_client():
    return TestClient(app, base_url="https://testserver")


def _login(creds):
    c = _new_client()
    assert c.post("/panel/login", json=creds).json()["status"] == "success"
    return c


def _member(phone):
    c = _new_client()
    assert c.post("/api/auth/login", json={"phone": phone, **CONSENT}).json()["status"] == "success"
    return c


def _use_secret(token=SECRET):
    with get_db(immediate=True) as conn:
        set_setting(conn, stamp_codes.STAMP_QR_TOKEN_KEY, token)


def _stamps(c):
    return c.get("/api/home-data").json()["customer"]["current_stamps"]


def test_token_is_generated_once_and_unguessable(client):
    _use_secret("")
    with get_db(immediate=True) as conn:
        tok = stamp_codes.stamp_qr_token(conn)
        assert re.fullmatch(r"[a-hjkmnp-z2-9]{12}", tok), tok
        assert stamp_codes.stamp_qr_token(conn) == tok                       # sabit kalır
        assert stamp_codes.stamp_qr_path(conn) == "/m/" + tok
        assert stamp_codes.is_stamp_qr_token(conn, tok) and not stamp_codes.is_stamp_qr_token(conn, "qr")
    assert len({stamp_codes.new_stamp_qr_token() for _ in range(50)}) == 50


def test_only_secret_path_opens_a_stamp(client):
    _use_secret()
    for old in ("/k/m/qr", "/k/m/kasa_1", "/k/m/QR", "/m/qr", "/k/m/" + SECRET[:-1]):
        r = _new_client().get(old)
        assert r.status_code == 200, old                                    # 302 yok (ADR-01)
        assert "x-claim-token" not in r.headers, old                        # damga açılışı yok
        assert 'const ENTRY = "stale"' in r.text, old
    ok = _new_client().get("/k/m/" + SECRET)
    assert ok.status_code == 200 and ok.headers.get("x-claim-token")
    kayit = _new_client().get("/k/m/kayit")
    assert kayit.status_code == 200 and 'const ENTRY = "kayit"' in kayit.text   # kayıt QR'ı aynı


def test_in_app_scanner_rejects_old_and_guessed_content(client):
    _use_secret()
    c = _member("0532 410 00 01")
    before = _stamps(c)
    for raw in ("https://kafe.example.com/k/m/qr", "https://kafe.example.com/m/kasa_1", "qr", "kasa_1"):
        res = c.post("/api/claim", json={"claim_token": raw}).json()
        assert res["status"] == "error" and res["message"] == STALE_QR_MESSAGE, (raw, res)
    assert c.post("/api/claim", json={}).json()["message"] == STALE_QR_MESSAGE   # varsayılan "qr" de geçersiz
    assert _stamps(c) == before
    res = c.post("/api/claim", json={"claim_token": "https://kafe.example.com/k/m/" + SECRET}).json()
    assert res["status"] == "success", res
    assert _stamps(c) == before + 1


def test_manager_sees_secret_url_and_can_rotate(client, monkeypatch):
    _use_secret()
    mgr = _login(MANAGER)
    assert mgr.get("/api/admin/overview").json()["qr_register_url"].endswith("/m/" + SECRET)
    import app.routes.manager as manager_routes
    import app.routes.staff as staff_routes
    seen = []
    monkeypatch.setattr(manager_routes, "generate_qr_png_bytes", lambda url: seen.append(url) or b"png")
    monkeypatch.setattr(staff_routes, "generate_qr_png_bytes", lambda url: seen.append(url) or b"png")
    assert mgr.get("/api/admin/qr-image?kind=damga").status_code == 200 and seen[-1].endswith("/m/" + SECRET)
    assert _login(STAFF).get("/api/staff/qr-image?kind=damga").status_code == 200 and seen[-1].endswith("/m/" + SECRET)
    mgr.get("/api/admin/qr-image?kind=kayit")
    assert seen[-1].endswith("/m/kayit")
    # yalnızca müdür yenileyebilir
    assert _new_client().post("/api/admin/stamp-qr/rotate", json={}).status_code == 401
    assert _login(STAFF).post("/api/admin/stamp-qr/rotate", json={}).status_code in (401, 403)
    res = mgr.post("/api/admin/stamp-qr/rotate", json={}).json()
    assert res["status"] == "success"
    new = res["url"].rsplit("/m/", 1)[1]
    assert new != SECRET and len(new) == 12
    assert "x-claim-token" not in _new_client().get("/k/m/" + SECRET).headers       # eski adres hemen geçersiz
    assert _new_client().get("/k/m/" + new).headers.get("x-claim-token")
    page = mgr.get("/mudur").text
    assert 'id="qr-stamp-rotate"' in page and "rotateStampQr" in page and "api/admin/stamp-qr/rotate" in page

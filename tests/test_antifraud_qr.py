"""Suistimal korumaları (ADR-14, ADR-21): sabit kasa QR'ı, şüphe puanı + müdür incelemesi, hoş geldin damgası kilidi.
Dinamik/süreli QR (ADR-14'ün ilk hali) ADR-21 ile kaldırıldı; bu testler kaldırılmış olduğunu da doğrular."""
from starlette.testclient import TestClient

from app.db import get_db
from app.main import app
from app.services import fraud as fraud_svc, stamp_codes

CONSENT = {"kvkk_read": True}
MANAGER = {"username": "mudur", "password": "Test-Mudur-2026"}
STAFF = {"username": "kasa1", "password": "Test-Kasa-2026"}


def _new_client():
    return TestClient(app, base_url="https://testserver")


def _manager():
    c = _new_client()
    assert c.post("/panel/login", json=MANAGER).json()["status"] == "success"
    return c


def _code():
    """Kasa ekranının tek kullanımlık Damga QR kodu (ADR-28): damga ve hoş geldin damgası yalnızca bununla."""
    with get_db(immediate=True) as conn:
        return stamp_codes.create_stamp_code(conn, created_by="test")["code"]


# ------------------------------------------------------------------ sabit QR (dinamik QR kaldırıldı)
def test_dynamic_qr_feature_is_removed(client):
    mgr = _manager()
    s = mgr.get("/api/admin/overview").json()["settings"]
    assert "dynamic_qr_enabled" not in s and "dynamic_qr_ttl" not in s
    # eski ayar gönderilse bile yok sayılır; sabit QR çalışmaya devam eder
    assert mgr.post("/api/admin/settings", json={"dynamic_qr_enabled": 1}).json()["status"] == "success"
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM settings WHERE key LIKE 'dynamic_qr%'").fetchone()["c"] == 0
    r = client.get("/k/m/qr")
    # ADR-29: kasadaki sabit QR'ın her okutuşu tek kullanımlık bir açılış kodu taşır (süreli/imzalı QR yok)
    assert r.status_code == 200 and r.headers.get("X-Claim-Token") and "QR_EXPIRED" not in r.text
    page = mgr.get("/mudur").text
    assert 'id="card-dynamic-qr"' not in page and "dynqr" not in page


def test_fixed_qr_scan_stamps_once_and_legacy_nonce_is_ignored(client):
    token = client.get("/k/m/qr").headers["X-Claim-Token"]
    first = client.post("/api/claim", json={"claim_token": token, "phone": "0532 940 00 01", **CONSENT}).json()
    assert first["status"] == "success" and first["current_stamps"] == 2          # sabit QR okutması: kayıt hediyesi + damga (ADR-35)
    assert client.post("/api/claim", json={"claim_token": token}).json()["status"] == "code_used"   # yenileme
    # eski (dinamik QR dönemi) istemcilerin gönderdiği nonce zararsızdır
    other = _new_client().post("/api/claim", json={"claim_token": "qr", "nonce": "123.abc", "phone": "0532 941 00 01", **CONSENT}).json()
    assert other["status"] == "success" and other["current_stamps"] == 2      # kayıt hediyesi + damga (ADR-35)


def test_staff_qr_image_is_png_and_dynamic_endpoint_gone(client):
    staff = _new_client()
    staff.post("/panel/login", json=STAFF)
    assert staff.get("/api/staff/dynamic-qr").status_code == 404
    img = staff.get("/api/staff/qr-image?kind=stamp")
    assert img.status_code == 200 and img.content[:4] == b"\x89PNG"
    assert staff.get("/api/staff/qr-image?kind=install").status_code == 200
    page = staff.get("/personel").text
    assert "qr-dynamic-note" not in page and "api/staff/dynamic-qr" not in page
    assert "api/staff/stamp-code" in page          # ADR-28: canlı tek kullanımlık Damga QR'ı


# ------------------------------------------------------------------ şüphe puanı + müdür incelemesi
def test_repeated_suspicious_events_raise_flag_and_manager_can_review(client):
    mgr = _manager()
    cust = _new_client()
    tok = cust.post("/api/auth/login", json={"phone": "0532 950 00 01", **CONSENT}).json()["device_token"]
    cid = [c["id"] for c in mgr.get("/api/admin/customers").json()["customers"]][0]
    mgr.post(f"/api/admin/customers/{cid}/stamp-add")

    def scan():
        with get_db(immediate=True) as conn:
            fraud_svc.record_event(conn, "qr_expired", customer_id=cid, device_token=tok, ip="1.2.3.4")

    def level():
        return mgr.get("/api/admin/customers").json()["customers"][0]["fraud_level"]

    scan()
    assert level() is None                    # tek olay kimseyi şüpheli yapmaz
    scan()
    assert level() == "yellow"                # 2+2 = 4 → sarı
    scan(); scan()
    c = mgr.get("/api/admin/customers").json()["customers"][0]
    assert c["fraud_level"] == "red" and c["fraud_risk_score"] >= 6
    # engellenmedi: müşteri normal akışta hâlâ damga alabilir
    assert cust.post("/api/claim", json={"claim_token": _code(), "device_token": tok}).json()["status"] in ("success", "limit_reached")
    # müdür: uyarıyı kapat
    assert mgr.post(f"/api/admin/customers/{cid}/fraud-review", json={"action": "clear"}).json()["status"] == "success"
    assert level() is None
    # yeniden şüphe → müdür damgaları sıfırlar
    for _ in range(3):
        scan()
    assert level() == "red"
    before = mgr.get("/api/admin/customers").json()["customers"][0]["current_stamps"]
    assert before >= 1
    assert mgr.post(f"/api/admin/customers/{cid}/fraud-review", json={"action": "reset_stamps"}).json()["status"] == "success"
    after = mgr.get("/api/admin/customers").json()["customers"][0]
    assert after["current_stamps"] == 0 and after["fraud_level"] is None
    assert mgr.post(f"/api/admin/customers/{cid}/fraud-review", json={"action": "bilinmeyen"}).json()["status"] == "error"
    assert _new_client().post(f"/api/admin/customers/{cid}/fraud-review", json={"action": "clear"}).status_code == 401


def test_manager_panel_has_fraud_badge_without_dynamic_qr_card(client):
    page = _manager().get("/mudur").text
    assert "Şüpheli İstismar Girişimi" in page and 'id="card-dynamic-qr"' not in page and 'id="fraud-modal"' in page


# ------------------------------------------------------------------ hoş geldin damgası ömür boyu 1 kez
def test_welcome_stamp_is_not_given_again_after_account_delete(client):
    phone = "0532 960 00 01"
    first = client.post("/api/claim", json={"claim_token": _code(), "phone": phone, **CONSENT}).json()
    assert first["status"] == "success" and first["current_stamps"] == 2     # hediye + okutuş damgası (ADR-35)
    tok = first["device_token"]
    with get_db() as conn:
        assert conn.execute("SELECT welcome_bonus_awarded FROM customers WHERE phone_e164 = '+905329600001'").fetchone()[0] == 1
    assert client.post("/api/delete-my-data", json={"phone": phone}, headers={"X-Device-Token": tok}).json()["status"] == "success"

    again = _new_client()   # yeni cihaz, aynı numara
    r = again.post("/api/claim", json={"claim_token": _code(), "phone": phone, **CONSENT}).json()
    assert r["status"] == "success" and r["welcome_locked"] is True and r["current_stamps"] == 0
    assert "damga verilmedi" in r["message"]
    # sonraki normal ziyarette damga kazanır (hesap kilitli değil, yalnızca kayıt anı bonusu verilmez)
    nxt = again.post("/api/claim", json={"claim_token": _code(), "device_token": r["device_token"]}).json()
    assert nxt["status"] == "success" and nxt["current_stamps"] == 1


def test_welcome_gift_never_given_twice_even_through_login_after_delete(client):
    """ADR-35: giriş ekranından kayıt da hediye verir; hesabını silip dönen numara/cihaz ikinci hediyeyi almaz."""
    phone = "0532 963 00 01"
    first = client.post("/api/auth/login", json={"phone": phone, **CONSENT}).json()
    assert first["status"] == "success" and first["current_stamps"] == 1 and first["welcome_gift"] is True
    tok = first["device_token"]
    assert client.post("/api/delete-my-data", json={"phone": phone}, headers={"X-Device-Token": tok}).json()["status"] == "success"
    again = _new_client().post("/api/auth/login", json={"phone": phone, **CONSENT}).json()
    assert again["status"] == "success" and again["current_stamps"] == 0 and again["welcome_gift"] is False
    assert again["welcome_locked"] is True and "damga verilmedi" in again["welcome_message"]


def test_welcome_stamp_locked_by_device_even_with_new_phone(client):
    first = client.post("/api/claim", json={"claim_token": _code(), "phone": "0532 961 00 01", **CONSENT}).json()
    tok = first["device_token"]
    client.post("/api/delete-my-data", json={"phone": "0532 961 00 01"}, headers={"X-Device-Token": tok})
    r = _new_client().post("/api/claim", json={"claim_token": _code(), "phone": "0533 961 00 02", "device_token": tok, **CONSENT}).json()
    assert r["status"] == "success" and r.get("welcome_locked") is True and r["current_stamps"] == 0


def test_normal_new_customer_still_gets_welcome_stamp(client):
    r = client.post("/api/claim", json={"claim_token": _code(), "phone": "0532 962 00 01", **CONSENT}).json()
    assert r["status"] == "success" and r["current_stamps"] == 2 and not r.get("welcome_locked")     # hediye + damga


# ------------------------------------------------------------------ arayüz temizliği (ADR-14)
def test_cashier_screen_is_single_search_and_dynamic_slots(client):
    staff = _new_client()
    staff.post("/panel/login", json=STAFF)
    page = staff.get("/personel").text
    assert page.count('id="cust-search"') == 1 and "Fiş Ver" not in page and "printReceipt" not in page and "bb-receipt" not in page
    assert "2. Adım" not in page and "1. Adım" not in page and "0 / 8" not in page and "appendNum" not in page
    assert "HEDİYE KAHVEYİ VER (ONAYLA)" in page and "+1 DAMGA BAS" in page
    assert "stamp_target" in page or "c.stamp_target" in page   # kutu sayısı ayardan (müşteri JSON'undan) gelir


def test_customer_home_has_no_quick_access_box_or_username(client):
    page = client.get("/k/kart/").text
    assert "Hızlı Erişim" not in page and 'id="push-toggle-btn"' not in page and 'id="pwa-install-btn"' not in page
    assert 'id="home-engage-card"' in page and 'id="notif-bonus-card"' not in page and "home-user-name" not in page
    # Engelleyici tam ekran bildirim/yükleme pop-up'ı yok; kart damga vaadi vermez
    assert "pwa-install-popup-modal" not in page and "+1 Damga Al" not in page and "Fırsatları ve hediye kahveni kaçırmamak için" in page

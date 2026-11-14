"""
ADR-47: yeni müşteri kaydında ad-soyad + telefon zorunlu. Mevcut müşteri girişi etkilenmez; otomatik adlı (userN)
hesap girişte yazdığı adı alır, adı olan müşterinin adı değişmez. KVKK aydınlatma metni v7 ad-soyadı söyler.
"""
from starlette.testclient import TestClient

from app.db import KVKK_TEXT_V6, get_db, set_setting
from app.main import app
from app.services.loyalty import NAME_REQUIRED_MSG, clean_full_name
from app.services.stamp_codes import create_stamp_code

CONSENT = {"kvkk_read": True}


def _client():
    return TestClient(app, base_url="https://testserver")


def _require_name(on=True):
    with get_db(immediate=True) as conn:
        set_setting(conn, "registration_requires_name", "1" if on else "0")


def _name_of(phone_e164):
    with get_db() as conn:
        row = conn.execute("SELECT display_name FROM customers WHERE phone_e164 = ?", (phone_e164,)).fetchone()
    return row["display_name"] if row else None


def test_clean_full_name_rules():
    assert clean_full_name("  Ayşe   Yılmaz ") == "Ayşe Yılmaz"
    assert clean_full_name("Ömer Faruk Çelik") == "Ömer Faruk Çelik"
    assert clean_full_name("Mary O’Neil-Smith") == "Mary O’Neil-Smith"
    for bad in ("", None, "Ayşe", "A", "Ali 123", "user5 x", "<b>Ali</b> Veli", "Ali " + "x" * 40):
        assert clean_full_name(bad) is None, bad


def test_new_registration_requires_name_on_every_path(client):
    _require_name()
    # 1. Uygulama girişi
    r = _client().post("/api/auth/login", json={"phone": "0532 470 00 01", **CONSENT}).json()
    assert r["status"] == "error" and r["message"] == NAME_REQUIRED_MSG
    r = _client().post("/api/auth/login", json={"phone": "0532 470 00 01", "full_name": "Ali", **CONSENT}).json()
    assert r["status"] == "error" and r["message"] == NAME_REQUIRED_MSG
    assert _name_of("+905324700001") is None                                  # hesap açılmadı
    r = _client().post("/api/auth/login", json={"phone": "0532 470 00 01", "full_name": " Ayşe  Yılmaz ", **CONSENT}).json()
    assert r["status"] == "success", r
    assert _name_of("+905324700001") == "Ayşe Yılmaz"
    # 2. Kasadaki QR ile kayıt (+ damga)
    with get_db(immediate=True) as conn:
        code = create_stamp_code(conn, created_by="test")["code"]
    c = _client()
    r = c.post("/api/claim", json={"claim_token": code, "phone": "0532 470 00 02", **CONSENT}).json()
    assert r["status"] == "error" and r["message"] == NAME_REQUIRED_MSG and _name_of("+905324700002") is None
    r = c.post("/api/claim", json={"claim_token": code, "phone": "0532 470 00 02", "full_name": "Murat Kaya", **CONSENT}).json()
    assert r["status"] == "success", r
    assert _name_of("+905324700002") == "Murat Kaya"
    # 3. /api/me/identify
    r = _client().post("/api/me/identify", json={"phone": "0532 470 00 03", **CONSENT}).json()
    assert r["status"] == "error" and _name_of("+905324700003") is None
    assert _client().post("/api/me/identify", json={"phone": "0532 470 00 03", "full_name": "Zeynep Ak", **CONSENT}).json()["status"] == "success"


def test_existing_customers_log_in_without_name_and_keep_their_name(client):
    _require_name(False)
    assert _client().post("/api/auth/login", json={"phone": "0532 470 00 11", **CONSENT}).json()["status"] == "success"
    assert _name_of("+905324700011").startswith("user")                     # eski kayıt: otomatik ad
    assert _client().post("/api/auth/login", json={"phone": "0532 470 00 12", "full_name": "Can Demir", **CONSENT}).json()["status"] == "success"
    _require_name()
    # mevcut müşteri ad yazmadan girebilir
    assert _client().post("/api/auth/login", json={"phone": "0532 470 00 11", **CONSENT}).json()["status"] == "success"
    # otomatik adlı hesap girişte yazdığı adı alır
    assert _client().post("/api/auth/login", json={"phone": "0532 470 00 11", "full_name": "Ece Su", **CONSENT}).json()["status"] == "success"
    assert _name_of("+905324700011") == "Ece Su"
    # adı olan müşterinin adı başkasının yazdığıyla değişmez
    assert _client().post("/api/auth/login", json={"phone": "0532 470 00 12", "full_name": "Başka Biri", **CONSENT}).json()["status"] == "success"
    assert _name_of("+905324700012") == "Can Demir"


def test_audit_log_has_no_name_and_kvkk_v7(client):
    _require_name()
    assert _client().post("/api/auth/login", json={"phone": "0532 470 00 21", "full_name": "Gizli Kişi", **CONSENT}).json()["status"] == "success"
    with get_db() as conn:
        details = [r[0] for r in conn.execute("SELECT detail FROM audit_log WHERE action = 'customer_registered'")]
        kvkk = conn.execute("SELECT value FROM settings WHERE key = 'kvkk_text'").fetchone()[0]
        version = conn.execute("SELECT value FROM settings WHERE key = 'kvkk_version'").fetchone()[0]
    assert details and not any("Gizli" in d for d in details)
    assert "ad-soyad ve cep telefonu numarası (kayıtta istenir" in kvkk and version == "2026-11-v7"
    assert "isterseniz ad-soyad" in KVKK_TEXT_V6 and "isterseniz ad-soyad" not in kvkk


def test_form_has_required_name_field(client):
    page = client.get("/k/kart/").text
    assert 'id="login-name-input"' in page and 'autocomplete="name"' in page and "Adınız Soyadınız" in page
    assert "full_name: fullNameValue()" in page and "fullNameValid(consent.full_name)" in page
    assert "/^\\p{L}" not in page                       # \p{L} yalnızca çalışma anında kurulan RegExp içinde (eski Safari)

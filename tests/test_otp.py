"""
Yeni cihaz SMS doğrulaması (OTP, docs/OTP.md). OTP_MODE=new_device iken kayıtlı numara bağlı olmayan cihazdan girilirse
cihaz bağlanmaz, kod istenir; yeni kayıt ve bağlı cihaz kod istemez. OTP_MODE=off (varsayılan) davranışı değiştirmez.
"""
import hashlib
import hmac
import logging
import re
import time
from datetime import datetime, timedelta

import httpx
import pytest
from starlette.testclient import TestClient

from app.config import SECRET_KEY, validate_otp_config
from app.db import get_db
from app.main import app
from app.services import otp as otp_service
from app.services import otp_providers
from app.services.otp_providers import OtpProviderError, SmsGateProvider, sms_gate_endpoint
from app.services.stamp_codes import create_stamp_code
from app.utils import ISTANBUL_TZ, get_istanbul_now

CONSENT = {"kvkk_read": True}
PHONE = "0532 480 00 12"
PHONE_E164 = "+905324800012"


class FakeProvider:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    def send(self, phone_e164, text):
        if self.fail:
            raise OtpProviderError("test: gönderilemedi")
        self.sent.append((phone_e164, text))

    @property
    def last_code(self):
        return re.search(r"(\d{6})", self.sent[-1][1]).group(1)


class Clock:
    def __init__(self):
        # Günün ortası: testin gece yarısına denk gelip günlük sayaçların sıfırlanması sonucu bozmasın
        today = get_istanbul_now()
        self.now = datetime(today.year, today.month, today.day, 12, 0, tzinfo=ISTANBUL_TZ)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now = self.now + timedelta(seconds=seconds)


@pytest.fixture
def sms(monkeypatch):
    monkeypatch.setenv("OTP_MODE", "new_device")
    monkeypatch.setenv("OTP_PROVIDER", "console")
    monkeypatch.setenv("OTP_ALLOW_CONSOLE", "1")
    fake = FakeProvider()
    monkeypatch.setitem(otp_providers.PROVIDERS, "console", lambda: fake)
    return fake


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(otp_service, "_now", c)
    return c


def _device(ip="testclient"):
    return TestClient(app, base_url="https://testserver", client=(ip, 50000))


def _register(phone=PHONE):
    """İlk cihazdan kayıt (yeni kayıt: kod istenmez). Dönüş: (istemci, cihaz belirteci)."""
    c = _device()
    r = c.post("/api/auth/login", json={"phone": phone, **CONSENT}).json()
    assert r["status"] == "success", r
    return c, r["device_token"]


def _device_count(phone_e164=PHONE_E164):
    with get_db() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM devices d JOIN customers c ON c.id = d.customer_id WHERE c.phone_e164 = ?",
            (phone_e164,),
        ).fetchone()[0]


def _stamp_count(phone_e164=PHONE_E164):
    with get_db() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM stamps s JOIN customers c ON c.id = s.customer_id "
            "WHERE c.phone_e164 = ? AND s.voided_at IS NULL", (phone_e164,),
        ).fetchone()[0]


def _login(client, **extra):
    return client.post("/api/auth/login", json={"phone": PHONE, **extra})


# ------------------------------------------------------------------ kip kapalı / kod gerekmeyen durumlar
def test_off_mode_is_unchanged(monkeypatch):
    monkeypatch.delenv("OTP_MODE", raising=False)
    fake = FakeProvider()
    monkeypatch.setitem(otp_providers.PROVIDERS, "console", lambda: fake)
    _register()
    r = _login(_device(), otp_challenge_id="uydurma", otp_code="000000").json()
    assert r["status"] == "success" and r.get("device_token")       # bugünkü gibi: yeni cihaz bağlandı
    assert _device_count() == 2 and fake.sent == []
    assert _device().post("/api/auth/otp/resend", json={"challenge_id": "x"}).json()["status"] == "otp_expired"


def test_new_registration_and_linked_device_need_no_otp(sms):
    c, token = _register()
    assert sms.sent == []
    r = c.post("/api/auth/login", json={"phone": PHONE, "device_token": token}).json()
    assert r["status"] == "success" and r["device_token"] == token
    r = _device().post("/api/card-lookup", json={"phone": PHONE, "device_token": token}).json()
    assert r["status"] == "success"
    assert sms.sent == [] and _device_count() == 1


# ------------------------------------------------------------------ yeni cihaz
def test_existing_phone_new_device_requires_otp(sms, clock):
    _register()
    resp = _login(_device())
    r = resp.json()
    assert r["status"] == "otp_required", r
    assert set(r) == {"status", "challenge_id", "masked_phone", "expires_in", "resend_in", "message"}
    assert r["masked_phone"] == "+90 5** *** ** 12"
    assert r["expires_in"] == 300 and r["resend_in"] == 60 and len(r["challenge_id"]) >= 22
    assert "set-cookie" not in resp.headers and "device_token" not in r
    assert _device_count() == 1 and len(sms.sent) == 1
    assert sms.sent[0][0] == PHONE_E164
    code = sms.last_code
    assert sms.sent[0][1] == f"Kahve Dükkanım doğrulama kodunuz: {code}. Bu kodu kimseyle paylaşmayın."
    assert code not in resp.text


def test_valid_code_links_device_and_is_single_use(sms, clock):
    _register()
    dev = _device()
    ch = _login(dev).json()["challenge_id"]
    code = sms.last_code
    resp = _login(dev, otp_challenge_id=ch, otp_code=code)
    r = resp.json()
    assert r["status"] == "success" and r.get("device_token")
    assert _device_count() == 2
    assert code not in resp.text
    # Bağlanan cihaz artık kod istemez
    assert _login(dev, device_token=r["device_token"]).json()["status"] == "success"
    # Aynı kod başka cihazda yeniden kullanılamaz
    r2 = _login(_device(), otp_challenge_id=ch, otp_code=code).json()
    assert r2["status"] == "otp_expired" and _device_count() == 2


def test_wrong_code_decrements_attempts_then_locks(sms, clock):
    _register()
    dev = _device()
    ch = _login(dev).json()["challenge_id"]
    good = sms.last_code
    bad = "000000" if good != "000000" else "111111"
    for left in (2, 1):   # varsayılan OTP_MAX_ATTEMPTS=3
        r = _login(dev, otp_challenge_id=ch, otp_code=bad).json()
        assert r == {"status": "otp_invalid", "attempts_left": left, "message": r["message"]}
    r = _login(dev, otp_challenge_id=ch, otp_code="12ab").json()
    assert r["status"] == "otp_locked" and 0 < r["retry_after"] <= 300
    # Kilitliyken (bu IP için) doğru kod da geçmez, yeni kod da gönderilmez
    assert _login(dev, otp_challenge_id=ch, otp_code=good).json()["status"] == "otp_locked"
    assert _login(_device()).json()["status"] == "otp_locked"
    assert len(sms.sent) == 1 and _device_count() == 1
    # Kilit challenge süresi dolunca kalkar: yeni challenge + yeni SMS
    clock.advance(301)
    r = _login(_device()).json()
    assert r["status"] == "otp_required" and r["challenge_id"] != ch and len(sms.sent) == 2


def test_expired_code(sms, clock):
    _register()
    dev = _device()
    ch = _login(dev).json()["challenge_id"]
    clock.advance(301)
    r = _login(dev, otp_challenge_id=ch, otp_code=sms.last_code).json()
    assert r["status"] == "otp_expired" and r["message"]
    assert _device_count() == 1
    assert _login(dev, otp_challenge_id="yok-boyle-bir-challenge", otp_code="123456").json()["status"] == "otp_expired"


def test_reuse_within_cooldown_sends_no_sms(sms, clock):
    _register()
    first = _login(_device()).json()
    clock.advance(20)
    again = _login(_device()).json()
    assert again["status"] == "otp_required" and again["challenge_id"] == first["challenge_id"]
    assert again["expires_in"] == 280 and again["resend_in"] == 40
    assert len(sms.sent) == 1
    # Bekleme dolunca aynı challenge'ın AYNI kodu yeniden gider (kod döndürülmez, süre uzamaz)
    old = sms.last_code
    clock.advance(41)
    third = _login(_device()).json()
    assert third["challenge_id"] == first["challenge_id"] and third["resend_in"] == 60 and len(sms.sent) == 2
    assert sms.last_code == old and third["expires_in"] == 239
    r = _login(_device(), otp_challenge_id=first["challenge_id"], otp_code=old).json()
    assert r["status"] == "success"


def test_resend_endpoint(sms, clock):
    _register()
    dev = _device()
    ch = _login(dev).json()["challenge_id"]
    r = dev.post("/api/auth/otp/resend", json={"challenge_id": ch}).json()
    assert r["status"] == "otp_rate_limited" and 0 < r["retry_after"] <= 60 and len(sms.sent) == 1
    clock.advance(61)
    resp = dev.post("/api/auth/otp/resend", json={"challenge_id": ch})
    r = resp.json()
    assert r["status"] == "otp_sent" and r["expires_in"] == 239 and r["resend_in"] == 60   # süre uzamaz
    assert len(sms.sent) == 2 and sms.last_code not in resp.text
    assert sms.sent[0][1] == sms.sent[1][1]   # aynı kod yeniden gönderildi
    assert _login(dev, otp_challenge_id=ch, otp_code=sms.last_code).json()["status"] == "success"
    assert dev.post("/api/auth/otp/resend", json={"challenge_id": ch}).json()["status"] == "otp_expired"
    assert dev.post("/api/auth/otp/resend", json={"challenge_id": "yok"}).json()["status"] == "otp_expired"


def test_daily_cap_per_phone(sms, clock, monkeypatch):
    monkeypatch.setenv("OTP_DAILY_LIMIT_PER_PHONE", "2")
    _register()
    dev = _device()
    ch = _login(dev).json()["challenge_id"]
    clock.advance(61)
    assert dev.post("/api/auth/otp/resend", json={"challenge_id": ch}).json()["status"] == "otp_sent"
    clock.advance(61)
    r = dev.post("/api/auth/otp/resend", json={"challenge_id": ch}).json()
    assert r["status"] == "otp_rate_limited" and r["retry_after"] > 0
    clock.advance(400)   # challenge da süresi dolsa yeni challenge açılmaz: sınır numara başına
    r = _login(_device()).json()
    assert r["status"] == "otp_rate_limited" and len(sms.sent) == 2


def test_ip_send_limit(sms, clock, monkeypatch):
    monkeypatch.setattr(otp_service, "IP_SEND_LIMIT", 2)
    for i in range(3):
        _register(f"0532 480 01 0{i}")
    assert _device().post("/api/auth/login", json={"phone": "0532 480 01 00"}).json()["status"] == "otp_required"
    assert _device().post("/api/auth/login", json={"phone": "0532 480 01 01"}).json()["status"] == "otp_required"
    r = _device().post("/api/auth/login", json={"phone": "0532 480 01 02"}).json()
    assert r["status"] == "otp_rate_limited" and r["retry_after"] > 0 and len(sms.sent) == 2


def test_identify_and_card_lookup_paths_require_otp(sms, clock):
    _register()
    r = _device().post("/api/me/identify", json={"phone": PHONE}).json()
    assert r["status"] == "otp_required"
    dev = _device()
    r = dev.post("/api/card-lookup", json={"phone": PHONE}).json()
    assert r["status"] == "otp_required" and _device_count() == 1
    r = dev.post("/api/me/identify", json={"phone": PHONE, "otp_challenge_id": r["challenge_id"],
                                           "otp_code": sms.last_code}).json()
    assert r["status"] == "success" and r["identified"] is True and _device_count() == 2


def test_claim_with_registration_fields_requires_otp_before_stamp(sms, clock):
    _register()
    stamps_before = _stamp_count()
    with get_db(immediate=True) as conn:
        code = create_stamp_code(conn, created_by="test")["code"]
    dev = _device()
    body = {"claim_token": code, "phone": PHONE, **CONSENT}
    resp = dev.post("/api/claim", json=body)
    r = resp.json()
    assert r["status"] == "otp_required", r
    assert "device_token" not in r and "new_device_token" not in r
    assert _stamp_count() == stamps_before and _device_count() == 1
    # Kod bu tarayıcıya ayrıldı (başkası kullanamaz); kod doğrulanınca aynı damga işlenir
    assert dev.cookies.get("sadakat_stamp_hold")
    ch = r["challenge_id"]
    bad = "000000" if sms.last_code != "000000" else "111111"
    r = dev.post("/api/claim", json={**body, "otp_challenge_id": ch, "otp_code": bad}).json()
    assert r["status"] == "otp_invalid" and _stamp_count() == stamps_before
    r = dev.post("/api/claim", json={**body, "otp_challenge_id": ch, "otp_code": sms.last_code}).json()
    assert r["status"] == "success", r
    assert _stamp_count() == stamps_before + 1 and _device_count() == 2 and r.get("device_token")


# ------------------------------------------------------------------ sağlayıcılar
def test_sms_gate_endpoint_resolution():
    assert sms_gate_endpoint("http://192.168.1.50:8080") == "http://192.168.1.50:8080/message"
    assert sms_gate_endpoint("http://192.168.1.50:8080/") == "http://192.168.1.50:8080/message"
    assert sms_gate_endpoint("https://api.sms-gate.app") == "https://api.sms-gate.app/3rdparty/v1/messages"
    assert sms_gate_endpoint("https://api.sms-gate.app/3rdparty/v1") == "https://api.sms-gate.app/3rdparty/v1/messages"
    assert sms_gate_endpoint("https://sms.ornek.com/api/3rdparty/v1/") == "https://sms.ornek.com/api/3rdparty/v1/messages"
    assert sms_gate_endpoint("https://sms.ornek.com/api/3rdparty/v1/messages") == "https://sms.ornek.com/api/3rdparty/v1/messages"


def test_sms_gate_request_shape_and_errors(monkeypatch):
    calls = []

    def fake_post(url, json=None, auth=None, timeout=None):
        calls.append({"url": url, "json": json, "auth": auth, "timeout": timeout})
        return httpx.Response(202, json={"id": "x", "state": "Pending"})

    monkeypatch.setattr(otp_providers.httpx, "post", fake_post)
    SmsGateProvider("http://10.0.0.5:8080", "kullanici", "parola").send("+905321234567", "Kod: 123456")
    assert calls == [{
        "url": "http://10.0.0.5:8080/message",
        "json": {"textMessage": {"text": "Kod: 123456"}, "phoneNumbers": ["+905321234567"]},
        "auth": ("kullanici", "parola"),
        "timeout": otp_providers.SMS_GATE_TIMEOUT,
    }]
    assert otp_providers.SMS_GATE_TIMEOUT.read <= 10

    monkeypatch.setattr(otp_providers.httpx, "post", lambda *a, **k: httpx.Response(401))
    with pytest.raises(OtpProviderError) as exc:
        SmsGateProvider("http://10.0.0.5:8080", "u", "p").send("+905321234567", "Kod: 123456")
    assert "123456" not in str(exc.value)

    def boom(*a, **k):
        raise httpx.ConnectError("bağlantı yok")

    monkeypatch.setattr(otp_providers.httpx, "post", boom)
    with pytest.raises(OtpProviderError):
        SmsGateProvider("http://10.0.0.5:8080", "u", "p").send("+905321234567", "Kod: 123456")


def test_provider_failure_returns_unavailable_and_allows_retry(sms, clock, monkeypatch):
    _register()
    failing = FakeProvider(fail=True)
    monkeypatch.setitem(otp_providers.PROVIDERS, "console", lambda: failing)
    r = _login(_device()).json()
    assert r == {"status": "otp_unavailable", "message": otp_service.MSG_UNAVAILABLE}
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM otp_challenges").fetchone()[0] == 0   # gönderim geri alındı
    monkeypatch.setitem(otp_providers.PROVIDERS, "console", lambda: sms)
    assert _login(_device()).json()["status"] == "otp_required" and len(sms.sent) == 1   # bekleme engellemez


def test_code_never_in_responses_or_non_console_logs(monkeypatch, clock, caplog):
    monkeypatch.setenv("OTP_MODE", "new_device")
    monkeypatch.setenv("OTP_PROVIDER", "sms_gate")
    monkeypatch.setenv("SMS_GATE_URL", "http://192.168.1.50:8080")
    monkeypatch.setenv("SMS_GATE_USERNAME", "u")
    monkeypatch.setenv("SMS_GATE_PASSWORD", "p")
    bodies = []

    def fake_post(url, json=None, auth=None, timeout=None):
        bodies.append(json)
        return httpx.Response(202)

    monkeypatch.setattr(otp_providers.httpx, "post", fake_post)
    caplog.set_level(logging.DEBUG)
    _register()
    dev = _device()
    texts = []
    resp = _login(dev)
    texts.append(resp.text)
    ch = resp.json()["challenge_id"]
    code = re.search(r"(\d{6})", bodies[-1]["textMessage"]["text"]).group(1)
    clock.advance(61)
    resp = dev.post("/api/auth/otp/resend", json={"challenge_id": ch})
    texts.append(resp.text)
    code2 = re.search(r"(\d{6})", bodies[-1]["textMessage"]["text"]).group(1)
    texts.append(_login(dev, otp_challenge_id=ch, otp_code=code2).text)
    for t in texts:
        assert code not in t and code2 not in t
    assert code not in caplog.text and code2 not in caplog.text
    with get_db() as conn:
        row = conn.execute("SELECT * FROM otp_challenges WHERE id = ?", (ch,)).fetchone()
    assert code2 not in str(dict(row)) and row["consumed_at"]


def test_console_provider_logs_code_with_warning(caplog):
    caplog.set_level(logging.WARNING, logger="sadakat")
    otp_providers.ConsoleProvider().send("+905321234567", "Kod: 654321")
    assert "YALNIZCA GELİŞTİRME" in caplog.text and "654321" in caplog.text


# ------------------------------------------------------------------ açılış denetimi ve saklama
def test_startup_fails_when_new_device_without_provider(monkeypatch):
    monkeypatch.setenv("OTP_MODE", "new_device")
    monkeypatch.delenv("OTP_PROVIDER", raising=False)
    with pytest.raises(RuntimeError, match="OTP_PROVIDER"):
        with TestClient(app, base_url="https://testserver"):
            pass
    monkeypatch.setenv("OTP_PROVIDER", "sms_gate")
    for name in ("SMS_GATE_URL", "SMS_GATE_USERNAME", "SMS_GATE_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(RuntimeError, match="SMS_GATE_URL"):
        validate_otp_config()
    monkeypatch.setenv("SMS_GATE_URL", "192.168.1.50:8080")
    monkeypatch.setenv("SMS_GATE_USERNAME", "u")
    monkeypatch.setenv("SMS_GATE_PASSWORD", "p")
    with pytest.raises(RuntimeError, match="SMS_GATE_URL geçersiz"):
        validate_otp_config()
    monkeypatch.setenv("SMS_GATE_URL", "http://192.168.1.50:8080")
    validate_otp_config()
    monkeypatch.setenv("OTP_PROVIDER", "netgsm")
    with pytest.raises(RuntimeError, match="OTP_PROVIDER geçersiz"):
        validate_otp_config()
    monkeypatch.setenv("OTP_MODE", "acik")
    with pytest.raises(RuntimeError, match="OTP_MODE"):
        validate_otp_config()
    monkeypatch.delenv("OTP_MODE")
    monkeypatch.delenv("OTP_PROVIDER")
    validate_otp_config()   # varsayılan: off, sağlayıcı gerekmez


def test_retention_deletes_expired_challenges(sms, clock):
    from app.services.retention import run_retention
    _register()
    _login(_device())
    with get_db(immediate=True) as conn:
        conn.execute("UPDATE otp_challenges SET expires_at = ?",
                     ((get_istanbul_now() - timedelta(days=2)).isoformat(),))
        res = run_retention(conn)
        left = conn.execute("SELECT COUNT(*) FROM otp_challenges").fetchone()[0]
    assert res["otp_challenges_deleted"] == 1 and left == 0


def test_pwa_has_otp_step_for_login_and_claim(client):
    page = client.get("/kart/").text
    assert 'id="otp-modal"' in page and 'autocomplete="one-time-code"' in page and 'inputmode="numeric"' in page
    assert "function postWithOtp" in page and "otp_challenge_id: otpState.challengeId, otp_code: code" in page
    assert '/api/auth/otp/resend' in page and "Tekrar gönder (" in page
    assert 'postWithOtp("/api/auth/login"' in page and 'postWithOtp("/api/claim"' in page
    for status in ("otp_invalid", "otp_expired", "otp_locked", "otp_rate_limited", "otp_unavailable"):
        assert status in page, status


# ------------------------------------------------------------------ çerezsiz (yerel uygulama) istemci: ayrılmış damga
def _screen_code():
    with get_db(immediate=True) as conn:
        return create_stamp_code(conn, created_by="test")["code"]


def _hold_row(code):
    from app.utils import hash_token
    with get_db() as conn:
        return dict(conn.execute("SELECT * FROM stamp_codes WHERE code_hash = ?", (hash_token(code),)).fetchone())


def _other(code):
    return "000000" if code != "000000" else "111111"


def test_cookieless_client_claims_held_screen_code_after_otp(sms, clock):
    _register()
    before = _stamp_count()
    code = _screen_code()
    app_client = _device()
    body = {"claim_token": "/d/" + code, "phone": PHONE, **CONSENT}
    r = app_client.post("/api/claim", json=body).json()
    assert r["status"] == "otp_required"
    ch = r["challenge_id"]
    app_client.cookies.clear()          # yerel uygulama sadakat_stamp_hold çerezini saklamaz
    # Challenge olmadan kod hâlâ başkasına ayrılmış görünür (eski davranış)
    assert app_client.post("/api/claim", json=body).json()["status"] == "code_held"
    app_client.cookies.clear()
    r = app_client.post("/api/claim", json={**body, "otp_challenge_id": ch, "otp_code": _other(sms.last_code)}).json()
    assert r["status"] == "otp_invalid" and r["attempts_left"] == 2 and _stamp_count() == before
    app_client.cookies.clear()
    resp = app_client.post("/api/claim", json={**body, "otp_challenge_id": ch, "otp_code": sms.last_code})
    r = resp.json()
    assert r["status"] == "success", r
    assert r["device_token"] and r["new_device_token"] == r["device_token"]
    assert _stamp_count() == before + 1 and _device_count() == 2
    assert resp.cookies.get("sadakat_device_token")
    # Kod tek kullanımlık: aynı istek bir daha damga vermez
    app_client.cookies.clear()
    r = app_client.post("/api/claim", json={**body, "otp_challenge_id": ch, "otp_code": sms.last_code}).json()
    assert r["status"] == "code_used" and _stamp_count() == before + 1


def test_cookieless_client_claims_held_nfc_tap_after_otp(sms, clock):
    from app.db import set_setting
    from app.services import nfc_sun
    meta = bytes.fromhex("00112233445566778899AABBCCDDEEFF")
    file = bytes.fromhex("F0E1D2C3B4A5968778695A4B3C2D1E0F")
    with get_db(immediate=True) as conn:
        set_setting(conn, "nfc_sdm_meta_key", meta.hex().upper())
        set_setting(conn, "nfc_sdm_file_key", file.hex().upper())
    _register()
    before = _stamp_count()
    token = "/n?" + nfc_sun.build_query(meta, file, bytes.fromhex("04DE5F1EACC040"), 41)
    app_client = _device()
    body = {"claim_token": token, "phone": PHONE, **CONSENT}
    r = app_client.post("/api/claim", json=body).json()
    assert r["status"] == "otp_required", r
    app_client.cookies.clear()
    assert app_client.post("/api/claim", json=body).json()["status"] == "code_held"
    app_client.cookies.clear()
    r = app_client.post("/api/claim", json={**body, "otp_challenge_id": r["challenge_id"],
                                            "otp_code": sms.last_code}).json()
    assert r["status"] == "success", r
    assert r["device_token"] and _stamp_count() == before + 1 and _device_count() == 2


def test_cookieless_client_fixed_seal_still_works(sms, clock):
    _register()
    before = _stamp_count()
    app_client = _device()
    body = {"claim_token": "/m/qr", "phone": PHONE, **CONSENT}
    r = app_client.post("/api/claim", json=body).json()
    assert r["status"] == "otp_required"
    app_client.cookies.clear()
    r = app_client.post("/api/claim", json={**body, "otp_challenge_id": r["challenge_id"],
                                            "otp_code": sms.last_code}).json()
    assert r["status"] == "success" and r["device_token"] and _stamp_count() == before + 1


def test_foreign_challenge_cannot_take_held_code(sms, clock):
    attacker_phone = "0532 480 00 77"
    _register()
    _register(attacker_phone)
    before = _stamp_count()
    code = _screen_code()
    victim = _device("198.51.100.10")
    vbody = {"claim_token": "/d/" + code, "phone": PHONE, **CONSENT}
    ch_v = victim.post("/api/claim", json=vbody).json()["challenge_id"]
    victim_code = sms.last_code
    attacker = _device("203.0.113.66")
    abody = {"claim_token": "/d/" + code, "phone": attacker_phone, **CONSENT}
    assert attacker.post("/api/claim", json=abody).json()["status"] == "code_held"
    # Saldırganın kendi numarasının geçerli challenge'ı + doğru kodu: kurbana ayrılmış koda sahiplik vermez
    ch_a = attacker.post("/api/auth/login", json={"phone": attacker_phone}).json()["challenge_id"]
    attacker.cookies.clear()
    assert attacker.post("/api/claim", json={**abody, "otp_challenge_id": ch_a, "otp_code": sms.last_code}
                         ).json()["status"] == "code_held"
    # Kurbanın challenge kimliği başka bir numarayla kullanılamaz
    attacker.cookies.clear()
    assert attacker.post("/api/claim", json={**abody, "otp_challenge_id": ch_v, "otp_code": victim_code}
                         ).json()["status"] == "code_held"
    # Kurbanın numarası + challenge'ı (numarayı bilen herkes edinebilir) SMS kodu olmadan işe yaramaz
    attacker.cookies.clear()
    r = attacker.post("/api/claim", json={**vbody, "otp_challenge_id": ch_v, "otp_code": _other(victim_code)}).json()
    assert r["status"] == "otp_invalid" and _stamp_count() == before
    # Kurban kodu doğrular: damga kurbanın kartına
    victim.cookies.clear()
    r = victim.post("/api/claim", json={**vbody, "otp_challenge_id": ch_v, "otp_code": victim_code}).json()
    assert r["status"] == "success" and _stamp_count() == before + 1
    # Tüketilmiş challenge artık hiçbir ayrılmış koda sahiplik vermez
    code2 = _screen_code()
    other = _device("203.0.113.67")
    other.post("/api/claim", json={**vbody, "claim_token": "/d/" + code2})
    attacker.cookies.clear()
    assert attacker.post("/api/claim", json={**vbody, "claim_token": "/d/" + code2, "otp_challenge_id": ch_v,
                                             "otp_code": victim_code}).json()["status"] == "code_held"


def test_cookieless_otp_after_hold_expired_reports_code_expired_and_keeps_challenge(sms, clock):
    _register()
    before = _stamp_count()
    code = _screen_code()
    app_client = _device()
    body = {"claim_token": "/d/" + code, "phone": PHONE, **CONSENT}
    ch = app_client.post("/api/claim", json=body).json()["challenge_id"]
    held = _hold_row(code)
    assert held["hold_challenge_hash"] and held["held_until"]
    with get_db(immediate=True) as conn:   # ayrılma süresi doldu (ör. müşteri kodu çok geç yazdı)
        conn.execute("UPDATE stamp_codes SET held_until = ? WHERE id = ?",
                     ((get_istanbul_now() - timedelta(seconds=1)).isoformat(), held["id"]))
    app_client.cookies.clear()
    r = app_client.post("/api/claim", json={**body, "otp_challenge_id": ch, "otp_code": sms.last_code}).json()
    assert r["status"] == "code_expired" and set(r) == {"status", "message"}
    assert _stamp_count() == before and _device_count() == 1
    # Challenge tüketilmedi: aynı kodla cihaz /api/auth/login üzerinden bağlanabilir
    r = app_client.post("/api/auth/login", json={"phone": PHONE, "otp_challenge_id": ch,
                                                 "otp_code": sms.last_code}).json()
    assert r["status"] == "success" and r["device_token"] and _device_count() == 2


def test_hold_is_extended_to_challenge_lifetime(sms, monkeypatch):
    from app.services import stamp_codes
    from app.utils import parse_to_istanbul
    monkeypatch.setenv("OTP_TTL_SECONDS", "900")
    _register()
    code = _screen_code()
    _device().post("/api/claim", json={"claim_token": "/d/" + code, "phone": PHONE, **CONSENT})
    held_until = parse_to_istanbul(_hold_row(code)["held_until"])
    assert held_until - get_istanbul_now() > timedelta(seconds=stamp_codes.HOLD_SECONDS + 200)


# ------------------------------------------------------------------ M1: SMS sınırları veritabanında
def test_global_hourly_and_daily_caps(sms, clock, monkeypatch):
    monkeypatch.setenv("OTP_GLOBAL_HOURLY_LIMIT", "2")
    phones = [f"0532 480 02 0{i}" for i in range(4)]
    for p in phones:
        _register(p)
    for i in range(2):
        assert _device(f"198.51.100.{i}").post("/api/auth/login", json={"phone": phones[i]}).json()["status"] == "otp_required"
    for i in (2, 3):
        r = _device(f"198.51.100.{i}").post("/api/auth/login", json={"phone": phones[i]}).json()
        assert r["status"] == "otp_rate_limited" and 0 < r["retry_after"] <= 3600
    assert len(sms.sent) == 2
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'otp_global_limit'").fetchone()[0] == 1
    clock.advance(3601)    # saatlik pencere kaydı
    assert _device("198.51.100.2").post("/api/auth/login", json={"phone": phones[2]}).json()["status"] == "otp_required"
    # Günlük genel sınır (İstanbul günü)
    monkeypatch.setenv("OTP_GLOBAL_HOURLY_LIMIT", "30")
    monkeypatch.setenv("OTP_GLOBAL_DAILY_LIMIT", "3")
    r = _device("198.51.100.3").post("/api/auth/login", json={"phone": phones[3]}).json()
    assert r["status"] == "otp_rate_limited" and r["retry_after"] > 3600 and len(sms.sent) == 3


def test_ip_send_limit_is_db_backed_and_uses_ipv6_64(sms, clock, monkeypatch):
    from app.utils import _rate_limits
    monkeypatch.setattr(otp_service, "IP_SEND_LIMIT", 2)
    phones = [f"0532 480 03 0{i}" for i in range(3)]
    for p in phones:
        _register(p)
    assert _device("2001:db8:1:2::1").post("/api/auth/login", json={"phone": phones[0]}).json()["status"] == "otp_required"
    _rate_limits.clear()   # bellek sıfırlansa da (ör. yeniden başlatma) sayaç veritabanında
    assert _device("2001:db8:1:2::abcd").post("/api/auth/login", json={"phone": phones[1]}).json()["status"] == "otp_required"
    r = _device("2001:db8:1:2:ffff::9").post("/api/auth/login", json={"phone": phones[2]}).json()
    assert r["status"] == "otp_rate_limited" and 0 < r["retry_after"] <= 3600   # aynı /64
    assert _device("2001:db8:1:3::1").post("/api/auth/login", json={"phone": phones[2]}).json()["status"] == "otp_required"
    assert otp_service.ip_key("2001:db8:1:2::1") == otp_service.ip_key("2001:db8:1:2:ffff::9") == "2001:db8:1:2::/64"
    assert otp_service.ip_key("::ffff:203.0.113.5") == "203.0.113.5"


def test_daily_cap_writes_one_audit_row_per_phone_per_day(sms, clock, monkeypatch):
    monkeypatch.setenv("OTP_DAILY_LIMIT_PER_PHONE", "1")
    _register()
    assert _login(_device()).json()["status"] == "otp_required"
    clock.advance(301)
    for _ in range(4):
        assert _login(_device()).json()["status"] == "otp_rate_limited"
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'otp_daily_limit'").fetchone()[0] == 1


# ------------------------------------------------------------------ M2: hedefli kilitleme
def test_lockout_is_per_ip(sms, clock):
    _register()
    victim = _device("198.51.100.20")
    ch = _login(victim).json()["challenge_id"]
    good = sms.last_code
    attacker = _device("203.0.113.1")
    for _ in range(3):
        r = _login(attacker, otp_challenge_id=ch, otp_code=_other(good)).json()
    assert r["status"] == "otp_locked"
    assert _login(attacker, otp_challenge_id=ch, otp_code=good).json()["status"] == "otp_locked"
    assert attacker.post("/api/auth/otp/resend", json={"challenge_id": ch}).json()["status"] == "otp_rate_limited"
    # Kurbanın IP'si kilitli değil: aynı challenge döner ve doğru kod geçer
    again = _login(_device("198.51.100.20")).json()
    assert again["status"] == "otp_required" and again["challenge_id"] == ch
    r = _login(victim, otp_challenge_id=ch, otp_code=good).json()
    assert r["status"] == "success" and _device_count() == 2


def test_challenge_locks_for_everyone_at_ceiling(sms, clock):
    _register()
    ch = _login(_device("198.51.100.30")).json()["challenge_id"]
    good = sms.last_code
    results = [
        _login(_device(f"203.0.113.{100 + i // 2}"), otp_challenge_id=ch, otp_code=_other(good)).json()
        for i in range(otp_service.ATTEMPT_CEILING)
    ]
    assert results[-1]["status"] == "otp_locked"
    assert all(r["status"] == "otp_invalid" for r in results[:-1]) and results[-2]["attempts_left"] == 1
    r = _login(_device("198.51.100.30"), otp_challenge_id=ch, otp_code=good).json()
    assert r["status"] == "otp_locked" and _device_count() == 1
    assert _login(_device("198.51.100.31")).json()["status"] == "otp_locked"   # yeni SMS de gitmez


def test_resend_from_other_ip_does_not_invalidate_victims_code(sms, clock):
    _register()
    victim = _device("198.51.100.40")
    ch = _login(victim).json()["challenge_id"]
    victims_code = sms.last_code
    clock.advance(61)
    r = _device("203.0.113.9").post("/api/auth/otp/resend", json={"challenge_id": ch}).json()
    assert r["status"] == "otp_sent" and len(sms.sent) == 2 and sms.last_code == victims_code
    assert _login(victim, otp_challenge_id=ch, otp_code=victims_code).json()["status"] == "success"


# ------------------------------------------------------------------ kasiyer / müdür: doğrulama kilidini sıfırla
def _panel(creds):
    c = TestClient(app, base_url="https://testserver")
    assert c.post("/panel/login", json=creds).json()["status"] == "success"
    return c


STAFF_CREDS = {"username": "kasa1", "password": "Test-Kasa-2026"}
MANAGER_CREDS = {"username": "mudur", "password": "Test-Mudur-2026"}


def _customer_id(phone_e164=PHONE_E164):
    with get_db() as conn:
        return conn.execute("SELECT id FROM customers WHERE phone_e164 = ?", (phone_e164,)).fetchone()[0]


def test_staff_reset_clears_lock_and_daily_counter(sms, clock, monkeypatch):
    monkeypatch.setenv("OTP_DAILY_LIMIT_PER_PHONE", "1")
    _register()
    dev = _device()
    ch = _login(dev).json()["challenge_id"]
    good = sms.last_code
    for _ in range(3):
        _login(dev, otp_challenge_id=ch, otp_code=_other(good))
    assert _login(dev, otp_challenge_id=ch, otp_code=good).json()["status"] == "otp_locked"
    staff = _panel(STAFF_CREDS)
    card = staff.post("/api/staff/search", json={"q": "5324800012"}).json()["customers"][0]
    assert card["otp_enabled"] is True
    r = staff.post("/api/staff/otp-reset", json={"customer_id": card["id"]}).json()
    assert r["status"] == "success" and r["reset"] == {"challenges": 1, "sends": 1}
    # Aynı (değişmeyen) SMS kodu artık geçer
    assert _login(dev, otp_challenge_id=ch, otp_code=good).json()["status"] == "success"
    with get_db() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action = 'otp_reset' AND actor LIKE 'staff:%'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM staff_audit_logs WHERE action = 'otp_reset'").fetchone()[0] == 1
    # Günlük sayaç da sıfırlandı: başka yeni cihaz için SMS gidebilir
    clock.advance(301)
    assert _login(_device()).json()["status"] == "otp_required" and len(sms.sent) == 2


def test_manager_reset_and_auth(sms, clock):
    _register()
    dev = _device()
    ch = _login(dev).json()["challenge_id"]
    good = sms.last_code
    for _ in range(3):
        _login(dev, otp_challenge_id=ch, otp_code=_other(good))
    cid = _customer_id()
    assert _device().post(f"/api/admin/customers/{cid}/otp-reset").status_code == 401
    assert _device().post("/api/staff/otp-reset", json={"customer_id": cid}).status_code == 401
    assert _panel(STAFF_CREDS).post(f"/api/admin/customers/{cid}/otp-reset").status_code == 401
    mgr = _panel(MANAGER_CREDS)
    listing = mgr.get("/api/admin/customers").json()
    assert listing["otp_enabled"] is True and all(c["otp_enabled"] for c in listing["customers"])
    assert mgr.post(f"/api/admin/customers/{cid}/otp-reset").json()["status"] == "success"
    assert _login(dev, otp_challenge_id=ch, otp_code=good).json()["status"] == "success"
    with get_db() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action = 'otp_reset' AND actor LIKE 'mudur:%'").fetchone()[0] == 1


def test_reset_unavailable_when_otp_off(monkeypatch):
    monkeypatch.delenv("OTP_MODE", raising=False)
    _register()
    mgr = _panel(MANAGER_CREDS)
    assert mgr.post(f"/api/admin/customers/{_customer_id()}/otp-reset").json()["status"] == "error"
    assert mgr.get("/api/admin/customers").json()["otp_enabled"] is False
    staff = _panel(STAFF_CREDS)
    card = staff.post("/api/staff/search", json={"q": "5324800012"}).json()["customers"][0]
    assert card["otp_enabled"] is False
    assert staff.post("/api/staff/otp-reset", json={"customer_id": card["id"]}).json()["status"] == "error"


def test_panels_have_reset_action():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "app" / "templates"
    staff_html = (root / "personel.html").read_text(encoding="utf-8")
    mgr_html = (root / "mudur.html").read_text(encoding="utf-8")
    assert "Doğrulama kilidini sıfırla" in staff_html and "api/staff/otp-reset" in staff_html
    assert "Doğrulama kilidini sıfırla" in mgr_html and '"/otp-reset"' in mgr_html


# ------------------------------------------------------------------ M3 / LOW: yapılandırma ve sağlayıcı
def _sms_gate_env(monkeypatch, url):
    monkeypatch.setenv("OTP_MODE", "new_device")
    monkeypatch.setenv("OTP_PROVIDER", "sms_gate")
    monkeypatch.setenv("SMS_GATE_URL", url)
    monkeypatch.setenv("SMS_GATE_USERNAME", "u")
    monkeypatch.setenv("SMS_GATE_PASSWORD", "p")


@pytest.mark.parametrize("name,value", [
    ("OTP_MAX_ATTEMPTS", "11"), ("OTP_MAX_ATTEMPTS", "0"), ("OTP_TTL_SECONDS", "29"), ("OTP_TTL_SECONDS", "901"),
    ("OTP_DAILY_LIMIT_PER_PHONE", "11"), ("OTP_GLOBAL_HOURLY_LIMIT", "0"), ("OTP_GLOBAL_HOURLY_LIMIT", "1001"),
    ("OTP_GLOBAL_DAILY_LIMIT", "10001"), ("OTP_RESEND_SECONDS", "901"), ("OTP_TTL_SECONDS", "beş"),
])
def test_config_bounds_fail_fast(monkeypatch, name, value):
    _sms_gate_env(monkeypatch, "https://api.sms-gate.app")
    validate_otp_config()
    monkeypatch.setenv(name, value)
    with pytest.raises(RuntimeError, match=name):
        validate_otp_config()


def test_config_defaults_and_hourly_not_above_daily(monkeypatch):
    from app.config import get_otp_settings
    _sms_gate_env(monkeypatch, "https://api.sms-gate.app")
    cfg = get_otp_settings()
    assert cfg["max_attempts"] == 3 and cfg["global_hourly_limit"] == 30 and cfg["global_daily_limit"] == 200
    monkeypatch.setenv("OTP_GLOBAL_HOURLY_LIMIT", "300")
    monkeypatch.setenv("OTP_GLOBAL_DAILY_LIMIT", "100")
    with pytest.raises(RuntimeError, match="OTP_GLOBAL_HOURLY_LIMIT"):
        validate_otp_config()


@pytest.mark.parametrize("url,ok", [
    ("https://api.sms-gate.app", True), ("https://sms.ornek.com/api/3rdparty/v1", True),
    ("http://192.168.1.50:8080", True), ("http://10.0.0.5:8080", True), ("http://172.20.1.1:8080", True),
    ("http://127.0.0.1:8080", True), ("http://[::1]:8080", True), ("http://telefon.local:8080", True),
    ("http://100.101.102.103:8080", True), ("http://localhost:8080", True),
    ("http://api.sms-gate.app", False), ("http://sms.ornek.com:8080", False), ("http://8.8.8.8:8080", False),
    ("http://172.32.0.1:8080", False), ("http://100.128.0.1:8080", False), ("http://evil.local.ornek.com", False),
])
def test_sms_gate_url_requires_https_unless_private(monkeypatch, url, ok):
    _sms_gate_env(monkeypatch, url)
    if ok:
        validate_otp_config()
    else:
        with pytest.raises(RuntimeError, match="https://"):
            validate_otp_config()


def test_console_provider_requires_explicit_opt_in(monkeypatch):
    monkeypatch.setenv("OTP_MODE", "new_device")
    monkeypatch.setenv("OTP_PROVIDER", "console")
    monkeypatch.delenv("OTP_ALLOW_CONSOLE", raising=False)
    with pytest.raises(RuntimeError, match="OTP_ALLOW_CONSOLE"):
        validate_otp_config()
    monkeypatch.setenv("OTP_ALLOW_CONSOLE", "true")
    with pytest.raises(RuntimeError, match="OTP_ALLOW_CONSOLE"):
        validate_otp_config()
    monkeypatch.setenv("OTP_ALLOW_CONSOLE", "1")
    validate_otp_config()


def test_off_mode_ignores_other_otp_vars(monkeypatch):
    monkeypatch.delenv("OTP_MODE", raising=False)
    for name, value in (("OTP_TTL_SECONDS", "bozuk"), ("OTP_MAX_ATTEMPTS", "999"), ("OTP_PROVIDER", "yok"),
                        ("OTP_GLOBAL_DAILY_LIMIT", "-1"), ("SMS_GATE_URL", "http://8.8.8.8")):
        monkeypatch.setenv(name, value)
    validate_otp_config()
    with TestClient(app, base_url="https://testserver") as c:   # uygulama açılır
        _register()
        assert c.post("/api/auth/otp/resend", json={"challenge_id": "x"}).json()["status"] == "otp_expired"
        assert _device().post("/api/auth/login", json={"phone": PHONE}).json()["status"] == "success"


def test_code_hash_has_domain_separation(sms, clock):
    _register()
    ch = _login(_device()).json()["challenge_id"]
    code = sms.last_code
    with get_db() as conn:
        row = conn.execute("SELECT code_hash, code_seed FROM otp_challenges WHERE id = ?", (ch,)).fetchone()
    expected = hmac.new(SECRET_KEY.encode(), f"otp-code:{ch}:{code}".encode(), hashlib.sha256).hexdigest()
    legacy = hmac.new(SECRET_KEY.encode(), (ch + code).encode(), hashlib.sha256).hexdigest()
    assert row["code_hash"] == expected and row["code_hash"] != legacy and len(row["code_seed"]) == 32


def test_sms_gate_timeouts_and_total_deadline(monkeypatch):
    assert otp_providers.SMS_GATE_TIMEOUT.connect == 3.0 and otp_providers.SMS_TOTAL_TIMEOUT == 5.0

    def slow_post(*a, **k):
        time.sleep(0.5)
        return httpx.Response(202)

    monkeypatch.setattr(otp_providers.httpx, "post", slow_post)
    monkeypatch.setattr(otp_providers, "SMS_TOTAL_TIMEOUT", 0.05)
    started = time.monotonic()
    with pytest.raises(OtpProviderError, match="yanıt vermedi"):
        SmsGateProvider("http://10.0.0.5:8080", "u", "p").send("+905321234567", "Kod: 123456")
    assert time.monotonic() - started < 0.4

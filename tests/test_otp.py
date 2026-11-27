"""
Yeni cihaz SMS doğrulaması (OTP, docs/OTP.md). OTP_MODE=new_device iken kayıtlı numara bağlı olmayan cihazdan girilirse
cihaz bağlanmaz, kod istenir; yeni kayıt ve bağlı cihaz kod istemez. OTP_MODE=off (varsayılan) davranışı değiştirmez.
"""
import logging
import re
from datetime import timedelta

import httpx
import pytest
from starlette.testclient import TestClient

from app.config import validate_otp_config
from app.db import get_db
from app.main import app
from app.services import otp as otp_service
from app.services import otp_providers
from app.services.otp_providers import OtpProviderError, SmsGateProvider, sms_gate_endpoint
from app.services.stamp_codes import create_stamp_code
from app.utils import get_istanbul_now

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
        self.now = get_istanbul_now()

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now = self.now + timedelta(seconds=seconds)


@pytest.fixture
def sms(monkeypatch):
    monkeypatch.setenv("OTP_MODE", "new_device")
    monkeypatch.setenv("OTP_PROVIDER", "console")
    fake = FakeProvider()
    monkeypatch.setitem(otp_providers.PROVIDERS, "console", lambda: fake)
    return fake


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(otp_service, "_now", c)
    return c


def _device():
    return TestClient(app, base_url="https://testserver")


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
    for left in (4, 3, 2, 1):
        r = _login(dev, otp_challenge_id=ch, otp_code=bad).json()
        assert r == {"status": "otp_invalid", "attempts_left": left, "message": r["message"]}
    r = _login(dev, otp_challenge_id=ch, otp_code="12ab").json()
    assert r["status"] == "otp_locked" and 0 < r["retry_after"] <= 300
    # Kilitliyken doğru kod da geçmez, yeni kod da gönderilmez
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
    # Bekleme dolunca aynı challenge'a yeni kod gider (eski kod geçersiz)
    old = sms.last_code
    clock.advance(41)
    third = _login(_device()).json()
    assert third["challenge_id"] == first["challenge_id"] and third["resend_in"] == 60 and len(sms.sent) == 2
    if sms.last_code != old:
        assert _login(_device(), otp_challenge_id=first["challenge_id"], otp_code=old).json()["status"] == "otp_invalid"


def test_resend_endpoint(sms, clock):
    _register()
    dev = _device()
    ch = _login(dev).json()["challenge_id"]
    r = dev.post("/api/auth/otp/resend", json={"challenge_id": ch}).json()
    assert r["status"] == "otp_rate_limited" and 0 < r["retry_after"] <= 60 and len(sms.sent) == 1
    clock.advance(61)
    resp = dev.post("/api/auth/otp/resend", json={"challenge_id": ch})
    r = resp.json()
    assert r["status"] == "otp_sent" and r["expires_in"] == 300 and r["resend_in"] == 60
    assert len(sms.sent) == 2 and sms.last_code not in resp.text
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

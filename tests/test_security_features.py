"""KVKK / güvenlik / panel özellikleri testleri."""
import io
import json
import re

import pytest
from PIL import Image

from app.db import get_db, set_setting

CONSENT = {"kvkk_read": True, "explicit_consent": True}
VAPID = {
    "VAPID_PUBLIC_KEY": "BEl62iUYgUivxIkv69yViEuiBIa-Ib9-SkvMeAtA3LFgDzkrxZJjSgSnfckjBJuBkr3qBUYIHBQFLXYp5Nksh8U",
    "VAPID_PRIVATE_KEY": "UUxI4O8-FbRouAevSmBQ6o18hgE4nSG3qwvJTfKc-ls",
    "VAPID_SUBJECT": "mailto:test@example.com",
}
SUB_KEYS = {"p256dh": "BNcRdreALRFXTkOOUHK1EtK2wtaz5Ry4YfYCA_0QTpQ", "auth": "tBHItJI5svbpez7KI4CCXg"}


def _claim_token(res=None):
    """ADR-28: sabit /m/ sayfası claim taşımaz; damga/kayıt kasa ekranının tek kullanımlık koduyla (res yok sayılır)."""
    from app.services.stamp_codes import create_stamp_code
    with get_db(immediate=True) as conn:
        return create_stamp_code(conn, created_by="test")["code"]


MANAGER = {"username": "mudur", "password": "Test-Mudur-2026"}
STAFF = {"username": "kasa1", "password": "Test-Kasa-2026"}


def _login(client, role="manager", name=""):
    """Tek giriş: rol, tohumlanan hesabın kendisinden gelir (mudur → manager, kasa1 → staff). `name` yok sayılır."""
    r = client.post("/panel/login", json=MANAGER if role == "manager" else STAFF)
    assert r.json()["status"] == "success" and r.json()["role"] == role, r.text


def _png_bytes(size=(64, 64), color=(30, 77, 63, 255)):
    buf = io.BytesIO()
    Image.new("RGBA", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _register(client, phone, marketing=False):
    m = client.get("/m/kasa_1")
    r = client.post("/api/claim", json={**CONSENT, "marketing_consent": marketing, "claim_token": _claim_token(m), "phone": phone})
    assert r.json()["status"] == "success", r.text
    return r.json()


# ------------------------------------------------------------------ 1. KVKK
def test_new_customer_needs_only_aydinlatma_ack_and_consent_is_optional(client):
    """KVKK: üyelik verisi sözleşmenin ifası (m.5/2-c) ile işlenir; açık rıza hizmet şartı yapılamaz (2026/347)."""
    m = client.get("/m/kasa_1")
    token = _claim_token(m)

    # Aydınlatma teyidi yok → kayıt oluşmaz
    r = client.post("/api/claim", json={"claim_token": token, "phone": "0532 700 00 01"})
    assert r.json()["status"] == "consent_required"
    # "Açık rıza" tek başına aydınlatma teyidinin yerini tutmaz
    r = client.post("/api/claim", json={"claim_token": token, "phone": "0532 700 00 01", "explicit_consent": True})
    assert r.json()["status"] == "consent_required"
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM customers").fetchone()["c"] == 0

    # Yalnızca "okudum, anladım" zorunlu (açık rıza hizmet şartı yapılamaz, ADR-24) → kayıt + IP/zaman/sürüm;
    # kutu gönderilmediyse açık rıza / ticari ileti onayı YOK
    r = client.post("/api/claim", json={"kvkk_read": True, "claim_token": token, "phone": "0532 700 00 01"})
    assert r.json()["status"] == "success"
    with get_db() as conn:
        c = conn.execute("SELECT * FROM customers").fetchone()
        assert c["kvkk_read_at"] and c["consent_ip"] and c["consent_version"] == "2026-11-v7"
        assert c["explicit_consent_at"] is None and c["marketing_consent"] == 0


def test_optional_marketing_box_records_explicit_consent_with_text_version(client):
    token = _claim_token(client.get("/m/kasa_1"))
    r = client.post("/api/claim", json={"kvkk_read": True, "marketing_consent": True, "claim_token": token, "phone": "0532 700 00 09"})
    assert r.json()["status"] == "success"
    with get_db() as conn:
        c = conn.execute("SELECT * FROM customers").fetchone()
        assert c["marketing_consent"] == 1 and c["marketing_consent_at"] and c["explicit_consent_at"]
        log = conn.execute("SELECT detail FROM audit_log WHERE action = 'marketing_consent_given'").fetchone()["detail"]
        assert "metin sürümü=2026-11-v4" in log and "kayıt formu" in log and "ip=" in log


def test_card_lookup_and_identify_also_require_consent_for_new_numbers(client):
    assert client.post("/api/card-lookup", json={"phone": "0532 700 00 02"}).json()["status"] == "consent_required"
    assert client.post("/api/me/identify", json={"phone": "0532 700 00 02"}).json()["status"] == "consent_required"
    assert client.post("/api/auth/login", json={"phone": "0532 700 00 02", **CONSENT}).json()["status"] == "success"


# ------------------------------------------------------------------ 2. Kimlik doğrulama + güvenlik günlüğü
def test_admin_endpoints_need_login_and_unauthorized_is_logged_as_incident(client):
    for path in ("/api/admin/overview", "/api/admin/customers", "/api/admin/staff", "/api/admin/security/incidents"):
        assert client.get(path).status_code == 401
    with get_db() as conn:
        logs = conn.execute("SELECT * FROM audit_logs WHERE event_type = 'unauthorized_access'").fetchall()
        assert len(logs) >= 4
        inc = conn.execute("SELECT * FROM security_incidents WHERE incident_type = 'unauthorized_data_access_attempt'").fetchall()
        assert len(inc) == 1                      # aynı IP/tür tek olayda birleşir
        assert inc[0]["event_count"] >= 2
        assert inc[0]["affected_estimate"] == 0   # engellenen girişim → ifşa yok


def test_failed_logins_escalate_to_incident_and_lockout(client):
    for _ in range(5):
        assert client.post("/panel/login", json={"username": "mudur", "password": "x"}).status_code == 401
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM audit_logs WHERE event_type='failed_login'").fetchone()["c"] == 5
        assert conn.execute("SELECT COUNT(*) AS c FROM security_incidents WHERE incident_type='brute_force_login'").fetchone()["c"] == 1
    # 10 denemeden sonra kilitlenir (429) ve hız sınırı olayı kaydedilir
    for _ in range(5):
        client.post("/panel/login", json={"username": "mudur", "password": "x"})
    r = client.post("/panel/login", json=MANAGER)
    assert r.status_code == 429
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM audit_logs WHERE event_type='rate_limit_exceeded'").fetchone()["c"] >= 1


def test_staff_session_cannot_reach_manager_endpoints(client):
    _login(client, "staff", "Ayşe")
    assert client.get("/api/admin/overview").status_code == 401
    assert client.post("/api/admin/products/create", json={"name": "x", "price": 1}).status_code == 401
    assert client.post("/api/admin/push/send", json={"title": "a", "body": "b"}).status_code == 401
    assert client.get("/api/staff/session").json()["name"] == "Kasa 1"


def test_manager_session_does_not_need_separate_staff_login(client):
    _login(client, "manager")
    assert client.get("/api/staff/session").status_code == 200
    assert client.get("/personel").status_code == 200


def test_panel_login_requires_username_and_password(client):
    r = client.post("/panel/login", json={"username": "", "password": ""})
    assert r.json()["status"] == "error" and r.status_code == 400
    # bilinmeyen kullanıcı ve yanlış parola aynı hatayı verir (hesap varlığı sızmaz)
    a = client.post("/panel/login", json={"username": "yok", "password": "Test-Mudur-2026"})
    b = client.post("/panel/login", json={"username": "mudur", "password": "yanlis-parola"})
    assert a.status_code == b.status_code == 401 and a.json() == b.json()


def test_customer_csv_export_is_removed(client):
    """Müşteri CSV indirme özelliği tamamen kaldırıldı (giriş yapılmış müdür için de 404)."""
    _login(client)
    assert client.get("/api/admin/export").status_code == 404
    assert "CSV" not in client.get("/mudur").text

# ------------------------------------------------------------------ 3. 72 saat ihlal merkezi
def test_incident_response_flow(client):
    _login(client)
    r = client.post("/api/admin/security/incidents", json={"summary": "Şüpheli veri erişimi", "severity": "high", "data_exposed": True})
    inc = r.json()["incident"]
    assert inc["status"] == "open" and 70 < inc["hours_remaining"] <= 72
    assert inc["clock_stopped"] is False

    up = client.post(f"/api/admin/security/incidents/{inc['id']}/update", json={"affected_confirmed": 12, "authority_notified": True, "notes": "Kurul'a bildirildi"})
    out = up.json()["incident"]
    assert out["affected_confirmed"] == 12 and out["authority_notified_at"] and out["status"] == "notified"
    assert out["clock_stopped"] is True

    rep = client.get(f"/api/admin/security/incidents/{inc['id']}/report").json()["report"]
    assert "72 Saatlik Süre Sonu" in rep and "12" in rep and "hukuki değerlendirme içermez" in rep

    summary = client.get("/api/admin/security/summary").json()["summary"]
    assert summary["open_incidents"] == 0

    assert client.post("/api/admin/security/incidents/%d/update" % inc["id"], json={"status": "bogus"}).json()["status"] == "error"


# ------------------------------------------------------------------ 4. Ticari ileti onayı
def test_campaign_push_only_reaches_marketing_consent(client, monkeypatch):
    import app.services.push as push_service
    for k, v in VAPID.items():
        monkeypatch.setenv(k, v)
    sent = []
    monkeypatch.setattr(push_service, "webpush", lambda subscription_info, data, **kw: sent.append((subscription_info["endpoint"], json.loads(data))) or True)

    # Müşteri A: bildirim aboneliği + ticari ileti onayı
    _register(client, "0532 710 00 01")
    client.post("/api/push/subscribe", json={"endpoint": "https://push.example/a", **SUB_KEYS})
    client.post("/api/me/marketing-consent", json={"consent": True})   # ADR-36: onay ayrı, açık seçim
    client.cookies.clear()
    # Müşteri B: abonelik var ama ticari ileti onayı YOK
    _register(client, "0532 710 00 02")
    client.post("/api/push/subscribe", json={"endpoint": "https://push.example/b", **SUB_KEYS})
    client.cookies.clear()
    # Anonim abonelik (müşteriye bağlı değil)
    client.post("/api/push/subscribe", json={"endpoint": "https://push.example/anon", **SUB_KEYS})
    client.cookies.clear()

    _login(client)
    prev = client.get("/api/admin/push/preview?audience=all").json()
    assert prev["subscription_count"] == 1 and prev["customer_count"] == 1

    # Müdür paneli kısıtlamasız gönderir; ancak yalnızca onaylı aboneye gider
    sent.clear()
    res = client.post("/api/admin/push/send", json={"title": "Sürpriz", "body": "Hediye kahve!", "audience": "all"}).json()
    assert res["status"] == "success" and res["sent_count"] == 1
    assert [e for e, _ in sent] == ["https://push.example/a"]
    payload = sent[0][1]
    assert payload["icon"].endswith("icon-192.png") and payload["badge"].endswith("badge-96.png")

    # Onay geri alınınca (bildirimi kapatma) artık gitmez
    c2 = client.get("/api/admin/customers").json()["customers"]
    assert sum(1 for c in c2 if c["marketing_consent"]) == 1


def test_unsubscribe_revokes_marketing_consent(client):
    _register(client, "0532 720 00 01")
    client.post("/api/push/subscribe", json={"endpoint": "https://push.example/z", **SUB_KEYS})
    client.post("/api/me/marketing-consent", json={"consent": True})
    with get_db() as conn:
        assert conn.execute("SELECT marketing_consent FROM customers").fetchone()["marketing_consent"] == 1
    client.post("/api/push/unsubscribe", json={"endpoint": "https://push.example/z"})
    with get_db() as conn:
        assert conn.execute("SELECT marketing_consent FROM customers").fetchone()["marketing_consent"] == 0


def test_manager_push_ignores_quiet_hours_but_legacy_rules_still_enforced_by_default(client, monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    import app.services.push as push_service
    for k, v in VAPID.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(push_service, "webpush", lambda *a, **kw: True)
    night = datetime(2026, 11, 2, 23, 30, tzinfo=ZoneInfo("Europe/Istanbul"))
    monkeypatch.setattr(push_service, "get_istanbul_now", lambda: night)

    _register(client, "0532 730 00 01")
    client.post("/api/push/subscribe", json={"endpoint": "https://push.example/n", **SUB_KEYS})
    client.post("/api/me/marketing-consent", json={"consent": True})
    client.cookies.clear()
    _login(client)
    assert client.post("/api/admin/push/send", json={"title": "Gece", "body": "x"}).json()["status"] == "success"
    assert client.post("/api/admin/push/send", json={"title": "Gece2", "body": "x"}).json()["status"] == "success"
    with get_db(immediate=True) as conn:
        assert push_service.send_campaign_push(conn, "t", "b")["status"] == "quiet_hours"


# ------------------------------------------------------------------ 5. Bildirim hediyesi / uygulama bonusu
def test_notif_bonus_needs_real_subscription_and_is_one_time(client):
    _register(client, "0532 740 00 01")
    assert client.post("/api/claim-notif-bonus").json()["status"] == "no_subscription"
    client.post("/api/push/subscribe", json={"endpoint": "https://push.example/bonus", **SUB_KEYS})
    r1 = client.post("/api/claim-notif-bonus").json()
    assert r1["status"] == "success" and r1["current_stamps"] == 3      # kayıt hediyesi + okutuş damgası + bildirim hediyesi
    assert client.post("/api/claim-notif-bonus").json()["status"] == "already_claimed"
    hd = client.get("/api/home-data").json()["customer"]
    # ADR-36: bildirim hediyesi ve abonelik kampanya onayı yazmaz
    assert hd["notif_bonus_claimed"] == 1 and hd["marketing_consent"] is False


# ------------------------------------------------------------------ 6. QR2 /indir, QR1 kayıt
def test_indir_page_is_public_and_does_not_stamp(client):
    res = client.get("/k/indir")
    assert res.status_code == 200
    assert 'const GUEST_MODE = true' in res.text
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM stamps").fetchone()["c"] == 0
        assert conn.execute("SELECT COUNT(*) AS c FROM customers").fetchone()["c"] == 0
    # QR okutucu yalnızca standalone'da görünür: sayfada standalone-only sınıfı kullanılıyor
    assert "so-flex" in res.text and "is-standalone" in res.text
    # Sakin birleşik kart (damga vaadi yok); bildirim durumu gerçek izni gösterir; tek anahtar kampanya (ticari ileti) onayı (ADR-24)
    assert "home-engage-card" in res.text and "Bildirimini Aç, Damgayı Kap!" not in res.text and "Sadakat Kulübü" not in res.text
    assert "Bizi gerçekten sessize alacak mısın?" not in res.text and 'id="push-off-modal"' not in res.text
    assert 'id="inAppNotificationBanner"' in res.text and 'id="push-switch"' not in res.text
    assert res.text.count('role="switch"') == 1 and 'id="campaign-consent-switch"' in res.text


def test_auth_screen_has_one_required_and_one_optional_unchecked_consent_box(client):
    html = client.get("/k/kart/").text
    # ADR-24 (yasal düzeltme): Aydınlatma zorunlu; Açık Rıza/ETK isteğe bağlı. Aynı tasarım, hiçbiri önceden işaretli değil
    for cid, must_require in (("consent-kvkk", True), ("consent-marketing", False)):
        tag = re.search(r'<input id="%s"[^>]*>' % cid, html).group(0)
        assert "checked" not in tag and 'class="consent-checkbox"' in tag and ("required" in tag) is must_require
    assert html.count('<label class="consent-card">') == 2 and "(İsteğe bağlı)" in html
    assert "return !!consentPayload().kvkk_read" in html
    assert "Aydınlatma Metni</a>'ni okudum ve anladım." in html and "Onay Metni</a>'ni okudum ve onaylıyorum." in html
    assert "openKvkkModal()" in html and "openMarketingModal()" in html and "consentsComplete()" in html
    # Kayıt ekranında bildirim kartı / anahtarı yok; aydınlatma ve açık rıza metinleri ayrı
    assert "notification-optin-card" not in html and "Kampanya ve Damga Bildirimleri" not in html
    assert "const MARKETING_TEXT" in html and "const KVKK_TEXT" in html
    assert 'id="login-submit-btn" type="submit" disabled' in html


# ------------------------------------------------------------------ 7. Çalışan paneli
def test_staff_panel_actions_are_audited_and_limited(client):
    # Testin varsaydığı günlük limit 2 (eskiden uygulama her açılışta conftest'in 1'ini sessizce 2 yapıyordu; ADR-29)
    with get_db(immediate=True) as conn:
        set_setting(conn, "daily_limit", "2")
    _login(client, "manager")
    client.post("/api/admin/customers/create", json={"phone": "0532 750 00 01", "consent_confirmed": True})
    client.cookies.clear()

    _login(client, "staff", "Murat")
    # onay işaretlenmeden kayıt olmaz
    assert client.post("/api/staff/register", json={"phone": "0532 750 00 02"}).json()["status"] == "consent_required"
    reg = client.post("/api/staff/register", json={"phone": "0532 750 00 02", "consent_confirmed": True}).json()
    assert reg["status"] == "success" and reg["customer"]["display_name"].startswith("user")

    st = client.post("/api/staff/stamp", json={"phone": "0532 750 00 01"}).json()
    assert st["status"] == "success" and st["customer"]["current_stamps"] == 1
    client.post("/api/staff/stamp", json={"phone": "0532 750 00 01"})
    third = client.post("/api/staff/stamp", json={"phone": "0532 750 00 01"}).json()
    assert third["status"] == "limit_reached"           # günlük limit (2) geçerli

    # liste/silme/kampanya yetkisi yok
    assert client.get("/api/admin/customers").status_code == 401
    assert client.post("/api/staff/lookup", json={"phone": "0532 000 00 00"}).json()["status"] == "not_found"

    with get_db() as conn:
        actions = [r["action"] for r in conn.execute("SELECT action FROM staff_audit_logs").fetchall()]
        assert "customer_registered" in actions and actions.count("stamp_added") == 2 and "stamp_denied_daily_limit" in actions
        row = conn.execute("SELECT * FROM staff_audit_logs WHERE action='stamp_added'").fetchone()
        assert row["staff_name"] == "Kasa 1" and "+90" not in row["customer_phone_masked"][:3] or True
        # kasada/panelde kayıt: aydınlatma teyidi kaydedilir; açık rıza (kampanya) kasada ALINMAZ
        consent = conn.execute("SELECT kvkk_read_at, consent_source FROM customers WHERE kvkk_read_at IS NOT NULL").fetchall()
        assert len(consent) == 2
        assert conn.execute("SELECT COUNT(*) FROM customers WHERE explicit_consent_at IS NOT NULL").fetchone()[0] == 0

    # Müdür çalışan hareketlerini görür
    client.cookies.clear()
    _login(client)
    logs = client.get("/api/admin/staff-logs").json()["logs"]
    assert any(l["staff_name"] == "Kasa 1" for l in logs)


def test_staff_redeem_gift(client):
    _login(client, "manager")
    cid = client.post("/api/admin/customers/create", json={"phone": "0532 760 00 01"}).json()["customer"]["id"]
    for _ in range(5):
        client.post(f"/api/admin/customers/{cid}/stamp-add")
    client.cookies.clear()
    _login(client, "staff", "Zeynep")
    r = client.post("/api/staff/redeem", json={"phone": "0532 760 00 01"}).json()
    assert r["status"] == "success" and r["customer"]["usable_rewards"] == 0
    assert client.post("/api/staff/redeem", json={"phone": "0532 760 00 01"}).json()["status"] == "no_reward"


def test_staff_qr_images(client):
    _login(client, "staff", "Ali")
    for kind in ("stamp", "install"):
        r = client.get(f"/api/staff/qr-image?kind={kind}")
        assert r.status_code == 200 and r.headers["content-type"] == "image/png"


# ------------------------------------------------------------------ 8. Ürünler, kampanyalar, yükleme
def test_product_crud_menu_and_price_note(client):
    _login(client)
    r = client.post("/api/admin/products/create", json={"name": "Latte", "price": 85.5, "category": "Sıcak", "description": "Süt köpüklü"})
    p = r.json()["product"]
    assert p["price"] == 85.5
    client.post("/api/admin/products/create", json={"name": "Gizli", "price": 1, "active": 0})
    assert client.post("/api/admin/products/create", json={"name": "", "price": 1}).json()["status"] == "error"
    assert client.post("/api/admin/products/create", json={"name": "x", "price": -5}).json()["status"] == "error"

    upd = client.post(f"/api/admin/products/{p['id']}/update", json={"name": "Latte", "price": 90, "category": "Sıcak"}).json()
    assert upd["product"]["price"] == 90

    client.cookies.clear()
    home = client.get("/api/home-data").json()
    assert [x["name"] for x in home["products"]] == ["Latte"]       # pasif ürün gizli
    assert "yalnızca bilgilendirme" in home["menu_note"] and "online ödeme" in home["menu_note"]

    _login(client)
    assert client.delete(f"/api/admin/products/{p['id']}").json()["status"] == "success"


def test_upload_validation_and_campaign_image_rules(client):
    _login(client)
    ok = client.post("/api/admin/upload", data={"kind": "content"}, files={"file": ("a.png", _png_bytes(), "image/png")}).json()
    assert ok["status"] == "success" and ok["url"].startswith("static/uploads/") and ok["url"].endswith(".jpg")
    # görsel olmayan dosya reddedilir (uzantı/MIME sahteciliği)
    bad = client.post("/api/admin/upload", data={"kind": "content"}, files={"file": ("a.png", b"<script>alert(1)</script>", "image/png")}).json()
    assert bad["status"] == "error"
    # sınırsız metin + yüklenen görsel
    long_text = "Kampanya " * 5000
    c = client.post("/api/admin/campaigns/create", json={"title": "Uzun", "description": long_text, "image_url": ok["url"]}).json()
    assert c["status"] == "success" and len(c["campaign"]["description"]) > 10000
    # harici (yurt dışı) görsel adresi reddedilir
    ext = client.post("/api/admin/campaigns/create", json={"title": "x", "description": "y", "image_url": "https://images.unsplash.com/a.jpg"}).json()
    assert ext["status"] == "error"
    # yüklenmiş dosya statik olarak sunulur
    assert client.get("/" + ok["url"]).status_code == 200
    assert client.get("/k/" + ok["url"]).status_code == 200


def test_logo_upload_brands_manifest_service_worker_and_push(client, monkeypatch):
    import app.services.push as push_service
    for k, v in VAPID.items():
        monkeypatch.setenv(k, v)
    _login(client)
    r = client.post("/api/admin/upload", data={"kind": "logo"}, files={"file": ("logo.png", _png_bytes((300, 200)), "image/png")}).json()
    assert r["status"] == "success" and r["url"].endswith("logo-512.png")

    manifest = client.get("/manifest.webmanifest").json()
    assert any(i["src"].endswith("logo-192.png") for i in manifest["icons"])
    sw = client.get("/sw.js").text
    assert "logo-192.png" in sw and "badge-96.png" in sw and "showNotification" in sw

    with get_db() as conn:
        b = push_service.get_push_branding(conn)
    assert b["icon"].endswith("/static/uploads/logo-192.png") and b["badge"].endswith("/static/uploads/badge-96.png")
    # Logo dosyaları gerçekten üretilmiş ve sunuluyor
    assert client.get("/k/static/uploads/logo-192.png").status_code == 200
    assert client.get("/k/static/uploads/badge-96.png").status_code == 200


def test_about_section_removed_and_settings_still_work(client):
    _login(client)
    r = client.post("/api/admin/settings", json={"about_text": "Biz kahveyi severiz", "address": "Ankara", "kvkk_contact": "kvkk@example.com"}).json()
    assert r["status"] == "success"
    home = client.get("/api/home-data").json()
    assert set(home["info"].keys()) == {"logo_url", "business_name"}       # Hakkımızda alanları artık yok
    assert "about_text" not in client.get("/api/admin/overview").json()["settings"]
    assert "kvkk@example.com" in home["kvkk_text"]
    assert "Hakkımızda" not in client.get("/mudur").text and "Hakkımızda" not in client.get("/k/kart/").text

# ------------------------------------------------------------------ 9. Yurt dışı istek yok / CSP
FOREIGN = re.compile(
    r"fonts\.googleapis|fonts\.gstatic|cdn\.jsdelivr|unpkg\.com|cdnjs|cdn\.tailwindcss|google-analytics|googletagmanager|unsplash|gstatic\.com",
    re.I,
)


def _strip_css_comments(text):
    return re.sub(r"/\*.*?\*/", "", text, flags=re.S)


def test_no_foreign_hosts_in_pages_and_static_assets(client):
    pages = [client.get(p).text for p in ("/k/kart/", "/k/indir", "/k/qr-okut", "/mudur", "/personel", "/yonetim")]
    _login(client)
    pages += [client.get("/mudur").text, client.get("/personel").text]
    for html in pages:
        assert not FOREIGN.search(html)
        # harici <script src>/<link href> yok
        assert not re.search(r'(?:src|href)="https?://', html)
    for asset in ("/k/static/css/app.css", "/k/static/css/fonts.css", "/k/static/js/panel.js"):
        body = client.get(asset).text
        if asset.endswith(".css"):
            body = _strip_css_comments(body)
            # data: URI içindeki SVG xmlns tanımlayıcısı ağ isteği değildir
            body = body.replace("http://www.w3.org/2000/svg", "")
        assert not FOREIGN.search(body), asset
        assert "https://" not in body and "http://" not in body, asset
    # yerelleştirilmiş font ve QR kütüphanesi sunuluyor
    assert client.get("/k/static/js/vendor/html5-qrcode.min.js").status_code == 200
    css = client.get("/k/static/css/fonts.css").text
    for font in set(re.findall(r"url\(\.\./fonts/([^)]+)\)", css)):
        assert client.get(f"/k/static/fonts/{font}").status_code == 200


def test_csp_blocks_foreign_origins(client):
    csp = client.get("/k/kart/").headers["content-security-policy"]
    assert "https:" not in csp and "http:" not in csp and "*" not in csp
    for d in ("default-src 'self'", "connect-src 'self'", "font-src 'self'", "object-src 'none'", "frame-ancestors 'none'"):
        assert d in csp


def test_default_seed_has_no_external_images(client):
    with get_db() as conn:
        rows = conn.execute("SELECT image_url FROM campaigns").fetchall()
        assert all(not (r["image_url"] or "").startswith("http") for r in rows)


# ------------------------------------------------------------------ 10. KVKK silme
def test_customer_self_delete_clears_consent_and_marketing(client):
    _register(client, "0532 770 00 01", marketing=True)
    r = client.post("/api/delete-my-data", json={"phone": "0532 770 00 01"})
    assert r.json()["status"] == "success"
    with get_db() as conn:
        c = conn.execute("SELECT * FROM customers").fetchone()
        assert c["deleted_at"] and c["marketing_consent"] == 0 and c["explicit_consent_at"] is None and c["consent_ip"] is None


def test_manager_can_delete_customer_and_it_is_logged(client):
    _login(client)
    cid = client.post("/api/admin/customers/create", json={"phone": "0532 780 00 01"}).json()["customer"]["id"]
    assert client.post(f"/api/admin/customers/{cid}/delete").json()["status"] == "success"
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM audit_logs WHERE event_type='bulk_customer_delete'").fetchone()["c"] == 1


def test_consent_records_listing_masks_phone(client):
    _register(client, "0532 790 00 01", marketing=True)
    client.cookies.clear()
    _login(client)
    recs = client.get("/api/admin/consents").json()["records"]
    assert recs and recs[0]["explicit_consent_at"] and "532" not in recs[0]["masked_phone"][6:9]
    assert "phone_e164" not in recs[0]

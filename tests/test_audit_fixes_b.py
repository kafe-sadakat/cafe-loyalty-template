"""Denetim düzeltmeleri, Tier B (yasal, kodla çözülebilenler): veri sorumlusu kartı (Y-01/E-03/Y-15), silme bütünlüğü (Y-06),
onay ispat kaydı (Y-08), Verilerimi İndir (Y-09), aydınlatma v5 (Y-07), ölü WhatsApp listeleri (Y-18)."""
import json
import re

from app.db import get_db, init_db, set_setting

CONSENT = {"kvkk_read": True, "explicit_consent": True}
MANAGER = {"username": "mudur", "password": "Test-Mudur-2026"}
PHONE = "0532 900 11 22"
PHONE_E164 = "+905329001122"


def _js_const(html: str, name: str) -> str:
    m = re.search(r"const %s = (.*?);\n" % name, html, re.S)
    assert m, name
    return json.loads(m.group(1))


def _register(client, phone=PHONE, **extra):
    r = client.post("/api/me/identify", json={**CONSENT, "phone": phone, **extra}).json()
    assert r["status"] == "success", r
    return r


# ------------------------------------------------------------------ Y-01 / E-03 / Y-15 veri sorumlusu
def test_manager_panel_has_legal_info_card_and_notice_renders_it(client):
    client.post("/panel/login", json=MANAGER)
    page = client.get("/mudur").text
    for needle in ('id="card-legal-info"', 'id="legal-controller"', 'id="legal-address"', 'id="legal-contact"', "Yasal Bilgiler / Veri Sorumlusu"):
        assert needle in page, needle
    # earlier owner decision (tests/test_reports): the removed branding card words / ids stay out
    assert "İşletme" not in page and "card-business" not in page and "setting-business-name" not in page
    ok = client.post("/api/admin/settings", json={
        "kvkk_controller": "Örnek Gıda Ltd. Şti.", "kvkk_address": "Moda Cd. No:1 Kadıköy/İstanbul", "kvkk_contact": "kvkk@ornek.com",
    }).json()
    assert ok["status"] == "success" and ok["settings"]["kvkk_address"] == "Moda Cd. No:1 Kadıköy/İstanbul"
    overview = client.get("/api/admin/overview").json()["settings"]
    assert overview["kvkk_controller"] == "Örnek Gıda Ltd. Şti." and overview["kvkk_address"].startswith("Moda")
    client.cookies.clear()
    kvkk = _js_const(client.get("/k/kart/").text, "KVKK_TEXT")
    assert "Veri Sorumlusu (unvan ve adres): Örnek Gıda Ltd. Şti.\nAdres: Moda Cd. No:1 Kadıköy/İstanbul" in kvkk
    assert "Başvuru kanalı: kvkk@ornek.com" in kvkk


def test_notice_without_legal_info_adds_nothing_made_up(client):
    kvkk = _js_const(client.get("/k/kart/").text, "KVKK_TEXT")
    assert "Veri Sorumlusu (unvan ve adres):" not in kvkk and "Adres:" not in kvkk and "Başvuru kanalı:" not in kvkk


# ------------------------------------------------------------------ Y-07 aydınlatma v5
def test_notice_v5_describes_actual_processing(client):
    from app.db import KVKK_TEXT_V4, KVKK_TEXT_V5
    assert KVKK_TEXT_V5 != KVKK_TEXT_V4
    kvkk = _js_const(client.get("/k/kart/").text, "KVKK_TEXT")
    for needle in ("Metin sürümü: 2026-11-v7", "olası kötüye kullanım", "sadakat_stamp_hold", "m1_notif_offer_dismissed",
                   "loyalty_device_token", "Verilerimi İndir", "anahtarlı özeti (HMAC)"):
        assert needle in kvkk, needle
    assert "kabul ediyorum" not in kvkk.lower()
    # eski "geri döndürülemez özet" iddiası düzeltildi
    assert "geri döndürülemez özeti" not in kvkk
    # onay metni (ayrı belge) ve sürümü değişmedi
    assert "Metin sürümü: 2026-11-v4" in _js_const(client.get("/k/kart/").text, "MARKETING_TEXT")


# ------------------------------------------------------------------ Y-06 silme bütünlüğü
def test_delete_removes_name_from_audit_log_and_keeps_proof_rows(client):
    _register(client, marketing_consent=True)
    assert client.post("/api/profile/update", json={"display_name": "Ayşe Gizli"}).json()["status"] == "success"
    with get_db() as conn:
        cid = conn.execute("SELECT id FROM customers").fetchone()["id"]
        assert conn.execute("SELECT COUNT(*) AS n FROM audit_log WHERE detail LIKE '%Ayşe Gizli%'").fetchone()["n"] >= 1
    r = client.post("/api/delete-my-data", json={"phone": PHONE}).json()
    assert r["status"] == "success" and "silindi" in r["message"]
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM audit_log WHERE detail LIKE '%Ayşe Gizli%'").fetchone()["n"] == 0
        # ispat satırları (onay/ret) silinmez
        assert conn.execute("SELECT COUNT(*) AS n FROM audit_log WHERE action IN ('consent_recorded','marketing_consent_given')").fetchone()["n"] >= 1
        c = conn.execute("SELECT * FROM customers WHERE id = ?", (cid,)).fetchone()
        assert c["marketing_consent_version"] is None and c["marketing_consent_channel"] is None and c["marketing_consent_ip"] is None
        assert c["display_name"].startswith("silindi_") and c["phone_e164"].startswith("+9000")


def test_delete_also_clears_directus_stamp_claim_reasons(monkeypatch):
    """stamp_claims.reason'a yazılan 'ad (telefon) - ...' satırları silmede 'silindi' yapılır (SQL, sahte PG bağlantısıyla)."""
    import app.services.directus_sync as ds

    class Cur:
        def __init__(self, log):
            self.log = log
            self._last = ""

        def execute(self, sql, params=None):
            self._last = " ".join(sql.split())
            self.log.append((self._last, params))

        def fetchall(self):
            return [("11111111-1111-1111-1111-111111111111",)] if self._last.startswith("SELECT id FROM directus_users") else []

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class Conn:
        def __init__(self):
            self.log = []

        def cursor(self):
            return Cur(self.log)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def close(self):
            pass

    conn = Conn()
    monkeypatch.setattr(ds, "_is_sync_enabled", lambda: True)
    monkeypatch.setattr(ds, "_get_pg_conn", lambda: conn)
    monkeypatch.setattr(ds, "clear_directus_cache", lambda: True)

    class Immediate:
        def __init__(self, target, daemon=None, **kw):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(ds.threading, "Thread", Immediate)
    ds.anonymize_directus_customer("+905329001122")
    sqls = [s for s, _ in conn.log]
    claim = next(s for s in sqls if s.startswith("UPDATE stamp_claims"))
    assert "reason = 'silindi'" in claim and "ANY(%s::uuid[])" in claim
    assert any(s.startswith("UPDATE loyalty SET stamps = 0") for s in sqls)
    # kimlik alanları boşaltılmadan ÖNCE kullanıcı kimlikleri okunur
    assert sqls.index(next(s for s in sqls if s.startswith("SELECT id FROM directus_users"))) < sqls.index(
        next(s for s in sqls if s.startswith("UPDATE directus_users")))


# ------------------------------------------------------------------ Y-08 onay ispat kaydı
def test_marketing_consent_proof_is_stored_on_customer_row(client):
    _register(client, marketing_consent=True)
    with get_db() as conn:
        c = conn.execute("SELECT * FROM customers").fetchone()
        assert c["marketing_consent"] == 1 and c["marketing_consent_at"]
        assert c["marketing_consent_version"] == "2026-11-v4"            # onay METNİNİN sürümü
        assert c["marketing_consent_channel"].startswith("kayıt formu")
        assert c["marketing_consent_ip"]
    r = client.post("/api/me/marketing-consent", json={"consent": False}).json()
    assert r["status"] == "success"
    with get_db() as conn:
        c = conn.execute("SELECT * FROM customers").fetchone()
        assert c["marketing_consent"] == 0 and c["marketing_consent_channel"] == "site içi bildirim anahtarı"
        assert c["marketing_consent_version"] == "2026-11-v4"


def test_marketing_consent_columns_are_additive_migration():
    """Eski veritabanı (sütunsuz) açılışta veri kaybı olmadan yükseltilir."""
    with get_db(immediate=True) as conn:
        conn.execute("INSERT INTO customers (phone_e164, display_name, created_at) VALUES ('+905320000001', 'Eski', '2026-01-01T00:00:00+03:00')")
        conn.execute("ALTER TABLE customers DROP COLUMN marketing_consent_version")
        conn.execute("ALTER TABLE customers DROP COLUMN marketing_consent_channel")
        conn.execute("ALTER TABLE customers DROP COLUMN marketing_consent_ip")
    init_db()
    with get_db() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(customers)")}
        assert {"marketing_consent_version", "marketing_consent_channel", "marketing_consent_ip"} <= cols
        assert conn.execute("SELECT display_name FROM customers WHERE phone_e164 = '+905320000001'").fetchone()["display_name"] == "Eski"


# ------------------------------------------------------------------ Y-09 Verilerimi İndir
def test_export_my_data_returns_only_own_records(client):
    from app.services.stamp_codes import create_stamp_code
    _register(client, marketing_consent=True)
    with get_db(immediate=True) as conn:
        set_setting(conn, "daily_limit", "5")
        code = create_stamp_code(conn, created_by="t")["code"]
    assert client.post("/api/claim", json={"claim_token": code}).json()["status"] == "success"
    # başka bir müşteri (başka tarayıcı) — verisi bu dosyada çıkmamalı
    from starlette.testclient import TestClient
    from app.main import app
    other = TestClient(app, base_url="https://testserver")
    other.post("/api/me/identify", json={**CONSENT, "phone": "0532 900 33 44"})

    r = client.post("/api/me/export", json={"phone": PHONE}).json()
    assert r["status"] == "success"
    d = r["data"]
    assert d["profile"]["phone"] == PHONE_E164 and d["consents"]["marketing_consent"] is True
    assert d["consents"]["marketing_consent_text_version"] == "2026-11-v4" and d["consents"]["privacy_notice_version"] == "2026-11-v7"
    assert len(d["stamps"]) >= 1 and set(d["stamps"][0]) == {"created_at", "source", "note", "voided_at", "voided_reason"}
    blob = json.dumps(d, ensure_ascii=False)
    assert "905329003344" not in blob and "fraud" not in blob and "flag_note" not in blob and "device_hash" not in blob
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM audit_log WHERE action='customer_data_exported'").fetchone()["n"] == 1


def test_export_requires_matching_device_and_phone_and_is_rate_limited(client):
    from starlette.testclient import TestClient
    from app.main import app
    _register(client)
    # telefon eşleşmiyor
    assert client.post("/api/me/export", json={"phone": "0532 900 33 44"}).json()["status"] == "error"
    # cihaz belirteci yok (çerezsiz tarayıcı)
    stranger = TestClient(app, base_url="https://testserver")
    res = stranger.post("/api/me/export", json={"phone": PHONE}).json()
    assert res["status"] == "error" and "data" not in res
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM audit_logs WHERE event_type='customer_export_denied'").fetchone()["n"] >= 2
    # hız sınırı: dakikada 10
    from app.utils import _rate_limits
    _rate_limits.clear()
    statuses = [stranger.post("/api/me/export", json={"phone": PHONE}).json()["message"] for _ in range(12)]
    assert "Çok fazla istek" in statuses[-1]


def test_profile_page_has_export_button_and_valid_js(client):
    html = client.get("/k/kart/").text
    assert 'id="export-my-data-btn"' in html and "handleExportMyData" in html and "Verilerimi İndir (KVKK)" in html
    assert "Tüm verileriniz KVKK kapsamında silindi" not in html and "kalıcı olarak siler" not in html


# ------------------------------------------------------------------ Y-18 ölü WhatsApp listeleri
def test_unconsented_whatsapp_lists_removed(client):
    import app.services.admin as admin
    assert not hasattr(admin, "get_retention_lists")
    with get_db(immediate=True) as conn:
        set_setting(conn, "whatsapp_template", "x")
    init_db()
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM settings WHERE key = 'whatsapp_template'").fetchone()["n"] == 0
    client.post("/panel/login", json=MANAGER)
    client.post("/api/admin/settings", json={"menu_note": "m"})
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM settings WHERE key = 'whatsapp_template'").fetchone()["n"] == 0


# ------------------------------------------------------------------ E-05 / G-16
def test_pwa_static_css_is_versioned(client):
    html = client.get("/k/kart/").text
    assert re.search(r"static/css/app\.css\?v=\d+", html) and re.search(r"static/css/fonts\.css\?v=\d+", html)


def test_customer_log_lines_do_not_contain_name_or_phone(client, caplog):
    import logging
    with caplog.at_level(logging.INFO, logger="sadakat"):
        _register(client)
        client.post("/api/profile/update", json={"display_name": "Zeynep Logsuz"})
        client.cookies.clear()
        _register(client)   # mevcut müşteri tanınır
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "Zeynep" not in text and "532" not in text and "9001122" not in text

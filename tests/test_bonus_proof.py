"""Kasa "Kanıt Tablosu" (ADR-16): hoş geldin / uygulama indirme / bildirim bonus durumları, kara liste, son 5 damga, eski veri doldurma."""
from starlette.testclient import TestClient

from app.db import get_db, init_db
from app.main import app

CONSENT = {"kvkk_read": True, "explicit_consent": True}
MANAGER = {"username": "mudur", "password": "Test-Mudur-2026"}
STAFF = {"username": "kasa1", "password": "Test-Kasa-2026"}
SUB = {"p256dh": "BNcRdreALRFXTkOOUHK1EtK2wtaz5Ry4YfYCA_0QTpQ", "auth": "tBHItJI5svbpez7KI4CCXg"}
FIELDS = (
    "welcome_bonus_awarded", "welcome_bonus_at", "pwa_bonus_awarded", "pwa_bonus_at",
    "push_bonus_awarded", "push_bonus_at", "is_re_registered", "recent_stamps_log",
)


def _new_client():
    return TestClient(app, base_url="https://testserver")


def _login(creds):
    c = _new_client()
    assert c.post("/panel/login", json=creds).json()["status"] == "success"
    return c


def _stamp_code():
    from app.services.stamp_codes import create_stamp_code
    with get_db(immediate=True) as conn:
        return create_stamp_code(conn, created_by="test")["code"]


def _register(phone, client=None, device_token=None):
    c = client or _new_client()
    body = {"claim_token": _stamp_code(), "phone": phone, **CONSENT}
    if device_token:
        body["device_token"] = device_token
    r = c.post("/api/claim", json=body).json()
    assert r["status"] == "success", r
    return c, r


def _lookup(staff, q):
    r = staff.post("/api/staff/customer-lookup", json={"q": q}).json()
    assert r["status"] == "success", r
    return r["customers"][0]


def test_new_customer_shows_welcome_received_others_waiting(client):
    _register("0532 810 00 01")
    staff = _login(STAFF)
    cust = _lookup(staff, "532 810 00 01")
    assert all(f in cust for f in FIELDS)
    assert cust["welcome_bonus_status"] == "received" and cust["welcome_bonus_awarded"] is True
    assert len(cust["welcome_bonus_at"]) >= 16
    assert cust["pwa_bonus_status"] == "none" and cust["push_bonus_status"] == "none" and cust["is_re_registered"] is False
    # ADR-35: kayıt hediyesi ve okutuşun damgası ayrı kayıtlardır (aynı anda; yeni olan üstte)
    log = cust["recent_stamps_log"]
    assert [s["source"] for s in log] == ["qr", "welcome"]
    assert log[1]["label"] == "+1 Hoş Geldin Hediyesi"


def test_pwa_and_push_bonus_have_timestamps_and_recent_log(client):
    c, first = _register("0532 810 00 02")
    tok = first["device_token"]
    h = {"X-Device-Token": tok}
    assert c.post("/api/claim-install-bonus", json={}, headers=h).json()["status"] == "success"
    assert c.post("/api/push/subscribe", json={"endpoint": "https://push.example/proof1", **SUB}, headers=h).status_code == 200
    assert c.post("/api/claim-notif-bonus", headers=h).json()["status"] == "success"
    cust = _lookup(_login(STAFF), "532 810 00 02")
    assert cust["pwa_bonus_status"] == "received" and cust["push_bonus_status"] == "received" and cust["welcome_bonus_status"] == "received"
    assert cust["pwa_bonus_at"] and cust["push_bonus_at"]
    labels = [s["label"] for s in cust["recent_stamps_log"]]
    assert labels[:2] == ["+1 Bildirim Hediyesi", "+1 Uygulama İndirme Hediyesi"] and "+1 Hoş Geldin Hediyesi" in labels[2:]
    assert labels.count("+1 Hoş Geldin Hediyesi") == 1
    assert all(len(s["at"]) >= 16 for s in cust["recent_stamps_log"])


def test_two_indicator_groups_history_vs_live_status(client):
    """bonus_history (hak geçmişi) ile current_status (canlı cihaz) ayrı ayrı döner ve birbirinden bağımsızdır."""
    c, first = _register("0532 810 00 07")
    h = {"X-Device-Token": first["device_token"]}
    staff = _login(STAFF)
    cust = _lookup(staff, "532 810 00 07")
    assert cust["bonus_history"] == {"welcome": "claimed", "pwa": "none", "push": "none", "campaign": "none"}
    assert cust["current_status"] == {"push_active": False, "push_no_consent": False, "is_pwa": False, "account_recreated": False}

    assert c.post("/api/claim-install-bonus", json={}, headers=h).json()["status"] == "success"
    # ADR-36: abonelik onay yazmaz; kampanya onayı Profil anahtarıyla ayrıca verilir (onaysız abonelik "push_no_consent" sayılır)
    assert c.post("/api/push/subscribe", json={"endpoint": "https://push.example/two1", **SUB}, headers=h).status_code == 200
    assert c.post("/api/me/marketing-consent", json={"consent": True}, headers=h).json()["status"] == "success"   # ADR-36: ayrı, açık onay
    assert c.post("/api/claim-notif-bonus", headers=h).json()["status"] == "success"
    assert c.post("/api/me/client-state", json={"standalone": True}, headers=h).json()["status"] == "success"
    cust = _lookup(staff, "532 810 00 07")
    assert cust["bonus_history"] == {"welcome": "claimed", "pwa": "claimed", "push": "claimed", "campaign": "none"}
    assert cust["current_status"] == {"push_active": True, "push_no_consent": False, "is_pwa": True, "account_recreated": False}

    # Müşteri bildirimi kapatıp uygulamayı silse: canlı durum düşer, hak geçmişi ASLA değişmez
    assert c.post("/api/push/unsubscribe", json={"endpoint": "https://push.example/two1"}, headers=h).status_code == 200
    assert c.post("/api/me/client-state", json={"standalone": False}, headers=h).json()["status"] == "success"
    cust = _lookup(staff, "532 810 00 07")
    assert cust["bonus_history"] == {"welcome": "claimed", "pwa": "claimed", "push": "claimed", "campaign": "none"}
    assert cust["current_status"]["push_active"] is False and cust["current_status"]["is_pwa"] is False

    rows = _login(MANAGER).get("/api/admin/customers?search=5328100007").json()["customers"]
    assert rows[0]["bonus_history"] == cust["bonus_history"] and rows[0]["current_status"] == cust["current_status"]


def test_client_state_requires_known_device(client):
    r = _new_client().post("/api/me/client-state", json={"standalone": True}).json()
    assert r["status"] == "device_not_found"


def test_recent_log_is_limited_to_five_newest_with_staff_actor(client):
    mgr = _login(MANAGER)
    mgr.post("/api/admin/settings", json={"daily_limit": 10, "stamp_target": 30})
    cid = mgr.post("/api/admin/customers/create", json={"phone": "0532 810 00 03", "consent_confirmed": True}).json()["customer"]["id"]
    for _ in range(3):
        mgr.post(f"/api/admin/customers/{cid}/stamp-add")
    staff = _login(STAFF)
    for _ in range(4):
        assert staff.post("/api/staff/stamp", json={"customer_id": cid}).json()["status"] == "success"
    cust = _lookup(staff, "532 810 00 03")
    log = cust["recent_stamps_log"]
    assert len(log) == 5 and [s["source"] for s in log] == ["staff"] * 4 + ["admin"]
    assert log[0]["label"] == "+1 Normal Kasa Damgası" and log[0]["actor"] == "kasa1" and log[4]["actor"] == "mudur"
    assert cust["welcome_bonus_status"] == "none" and cust["pwa_bonus_status"] == "none"      # panelden açılan hesap: bonus yok


def test_deleted_and_re_registered_is_blocked_everywhere(client):
    phone = "0532 810 00 04"
    c, first = _register(phone)
    tok = first["device_token"]
    assert c.post("/api/claim-install-bonus", json={}, headers={"X-Device-Token": tok}).json()["status"] == "success"
    assert c.post("/api/delete-my-data", json={"phone": phone}, headers={"X-Device-Token": tok}).json()["status"] == "success"
    _register(phone)                                       # yeni cihaz, aynı numara
    cust = _lookup(_login(STAFF), "532 810 00 04")
    assert cust["is_re_registered"] is True
    assert cust["welcome_bonus_status"] == "blocked" and cust["pwa_bonus_status"] == "blocked" and cust["push_bonus_status"] == "blocked"
    assert cust["welcome_bonus_awarded"] is False and cust["current_stamps"] == 0


def test_same_device_second_phone_pwa_is_blocked_not_re_registered(client):
    c, first = _register("0532 810 00 05")
    tok = first["device_token"]
    assert c.post("/api/claim-install-bonus", json={}, headers={"X-Device-Token": tok}).json()["status"] == "success"
    r = _new_client().post("/api/auth/login", json={"phone": "0533 810 00 06", "device_token": tok, **CONSENT}).json()
    assert r["status"] == "success"
    cust = _lookup(_login(STAFF), "533 810 00 06")
    assert cust["pwa_bonus_status"] == "blocked" and cust["is_re_registered"] is False   # cihaz bonusu daha önce kullanmış
    # ADR-39 (ADR-35'i daraltır): aynı telefonda ikinci numara kayıt hediyesi de almaz (cihaz başına 1 hediye)
    assert cust["welcome_bonus_status"] == "none" and cust["current_stamps"] == 0


def test_lookup_variants_and_admin_list_expose_same_fields(client):
    _register("0532 810 00 07")
    staff = _login(STAFF)
    by_q = _lookup(staff, "532 810 00 07")
    by_id = staff.post("/api/staff/customer-lookup", json={"customer_id": by_q["id"]}).json()["customer"]
    by_phone = staff.post("/api/staff/customer-lookup", json={"phone": "0532 810 00 07"}).json()["customer"]
    assert by_id["welcome_bonus_at"] == by_phone["welcome_bonus_at"] == by_q["welcome_bonus_at"]
    old = staff.post("/api/staff/lookup", json={"customer_id": by_q["id"]}).json()["customer"]
    assert all(f in old for f in FIELDS)
    mgr = _login(MANAGER)
    row = [x for x in mgr.get("/api/admin/customers").json()["customers"] if x["id"] == by_q["id"]][0]
    assert all(f in row for f in FIELDS) and row["welcome_bonus_status"] == "received" and len(row["recent_stamps_log"]) == 2
    assert _new_client().post("/api/staff/customer-lookup", json={"q": "532 810 00 07"}).status_code == 401


def test_legacy_rows_are_backfilled_from_audit_log(client):
    """Alanlar eklenmeden önceki kayıtlar: bonus zamanları ve kara liste denetim günlüğünden doldurulur (bir kez)."""
    with get_db(immediate=True) as conn:
        conn.execute("DELETE FROM settings WHERE key = 'bonus_proof_backfill_v1'")
        t0 = "2026-10-01T10:00:00+03:00"
        conn.execute("INSERT INTO customers (id, phone_e164, display_name, created_at) VALUES (901, '+905328109001', 'eski1', ?)", (t0,))
        conn.execute("INSERT INTO customers (id, phone_e164, display_name, created_at) VALUES (902, '+905328109002', 'eski2', ?)", (t0,))
        # 901: kayıt anı damgası + indirme + bildirim bonusu (kaynak alanı yok)
        conn.execute("INSERT INTO stamps (customer_id, seal_id, created_at) VALUES (901, 1, '2026-10-01T10:00:03+03:00')")
        conn.execute("INSERT INTO stamps (customer_id, seal_id, created_at) VALUES (901, 1, '2026-10-02T14:20:00+03:00')")
        conn.execute("INSERT INTO stamps (customer_id, seal_id, created_at) VALUES (901, 1, '2026-10-03T09:05:00+03:00')")
        conn.execute("INSERT INTO stamps (customer_id, seal_id, created_at) VALUES (901, 1, '2026-10-04T12:00:00+03:00')")  # normal QR
        for at, actor, action, detail in (
            ("2026-10-01T10:00:01+03:00", "system", "customer_registered", "Yeni müşteri #901 (eski1) kaydedildi."),
            ("2026-10-02T14:20:01+03:00", "customer:901", "pwa_bonus_claimed", "x"),
            ("2026-10-03T09:05:01+03:00", "customer:901", "notif_bonus_claimed", "x"),
            ("2026-10-05T09:00:00+03:00", "customer:902", "returning_deleted_identity", "x"),
        ):
            conn.execute("INSERT INTO audit_log (at, actor, action, detail) VALUES (?, ?, ?, ?)", (at, actor, action, detail))
    init_db()
    with get_db() as conn:
        c = conn.execute("SELECT * FROM customers WHERE id = 901").fetchone()
        assert c["welcome_bonus_at"].startswith("2026-10-01T10:00:03") and c["welcome_bonus_awarded"] == 1
        assert c["pwa_bonus_at"].startswith("2026-10-02T14:20") and c["push_bonus_at"].startswith("2026-10-03T09:05")
        assert conn.execute("SELECT is_re_registered FROM customers WHERE id = 902").fetchone()[0] == 1
        src = [r["source"] for r in conn.execute("SELECT source FROM stamps WHERE customer_id = 901 ORDER BY id")]
        assert src == ["welcome", "bonus_pwa", "bonus_notif", None]
    cust = _lookup(_login(STAFF), "eski1")
    assert (cust["welcome_bonus_status"], cust["pwa_bonus_status"], cust["push_bonus_status"]) == ("received", "received", "received")
    assert cust["recent_stamps_log"][0]["label"] == "+1 Damga" and cust["recent_stamps_log"][1]["label"] == "+1 Bildirim Hediyesi"
    assert _lookup(_login(STAFF), "eski2")["welcome_bonus_status"] == "blocked"


def test_cashier_and_manager_screens_render_proof_ui(client):
    staff = _login(STAFF)
    page = staff.get("/personel").text
    assert "bonusProofHtml(c)" in page and "proofNoteStrip(c)" in page and "recentStampsHtml(c)" in page and "proofBeep" in page
    js = staff.get("/static/js/panel.js").text
    for text in (
        "Kazanılan Bonus Hakları", "Müşterinin Anlık Cihaz Durumu", "Cihaz: Yüklü PWA", "Cihaz: Tarayıcı (Web)",
        "Bildirim: Açık", "Bildirim: Kapalı", "Hesap: Yeniden Kayıt", "Kampanya: Alındı", "✅ ALINDI (Tarih: ", "🚫 HAKKI YOK (Daha Önce Hesap Sıfırlamış)",
        "🚫 BU CİHAZDA DAHA ÖNCE KULLANILDI", "⏳ Henüz Alınmadı", "⏳ Henüz İndirmedi", "⏳ Bildirimi Açık Değil",
        "💡 KASİYER NOTU: Müşteri", "kesinlikle ekstra manuel damga basmayınız!", "Son 5 İşlem Özeti",
    ):
        assert text in js, text
    mgr_page = _login(MANAGER).get("/mudur").text
    assert "bonusProofHtml(c)" in mgr_page and "proofMini(c)" in mgr_page


def test_manager_customer_list_is_two_row_layout_with_versioned_assets(client):
    """
    Müşteri listesi: üst satırda kimlik + işlemler, altında tablo genişliğini kullanan sabit sütunlu durum şeridi.
    (Şerit ilk hücreye konunca diğer sütunları ekran dışına itmişti.) Panel CSS/JS adresleri sürümlüdür: yeni sayfa,
    tarayıcının önbelleğindeki eski CSS/JS ile açılmaz.
    """
    import re

    mgr = _login(MANAGER)
    page = mgr.get("/mudur").text
    assert '<tr class="bb-cust-main">' in page and '<tr class="bb-cust-sub"><td colspan="6"' in page
    assert "bonusHistoryBadges(c, true)" in page and "liveStatusBadges(c, true)" in page and "campaignMiniBadges(c)" in page

    css_ref = re.search(r'static/css/app\.css\?v=(\d+)"', page)
    js_ref = re.search(r'static/js/panel\.js\?v=(\d+)"', page)
    assert css_ref and js_ref and int(css_ref.group(1)) > 0 and css_ref.group(1) == js_ref.group(1)

    css = mgr.get("/static/css/app.css?v=" + css_ref.group(1))
    assert css.status_code == 200
    for rule in (".bb-cust-sub", ".bb-mini-spec", "repeat(3, 7.75rem)", ".bb-col-date"):
        assert rule in css.text, rule
    js = mgr.get("/static/js/panel.js?v=" + js_ref.group(1)).text
    # kısa kip metinleri var; uzun (kasa kartı) metinleri de yerinde
    assert "Bildirim: Onay yok" in js and "Cihaz: Uygulama" in js and "Bildirim: İzinli, onay yok" in js

    # kasa ve giriş sayfaları da sürümlü adres kullanır
    assert re.search(r'panel\.js\?v=\d+"', _login(STAFF).get("/personel").text)
    assert re.search(r'app\.css\?v=\d+"', TestClient(app, base_url="https://testserver").get("/giris").text)

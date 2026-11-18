"""
ADR-35: iki AYRI QR + kayıt hediyesi.
- Kayıt / Uygulama QR'ı (/m/kayit): sistemi açar, yeni müşteriyi kaydeder; static_qr_stamps ne olursa olsun, kamera / NFC /
  uygulama içi okutucu fark etmeksizin HİÇBİR ZAMAN damga vermez.
- Damga QR'ı (/m/qr): ADR-29 / ADR-30 / ADR-33 davranışı aynen (tek kullanımlık açılış, bekleme, günlük limit, mod anahtarı).
- Yeni kayıt (hangi yoldan olursa olsun) ömür boyu TEK +1 kayıt hediyesi alır (source='welcome'): günlük limiti tüketmez,
  "aynı ziyarette tek QR damgası" beklemesini başlatmaz; hesabını silip dönen numara/cihaz almaz; eski müşteriye geriye
  dönük verilmez.
"""
from starlette.testclient import TestClient

from app.db import get_db, init_db, set_setting
from app.main import app
from app.services import stamp_codes
from test_nfc_sun import _keys, _open_tap, _tap

CONSENT = {"kvkk_read": True}
MANAGER = {"username": "mudur", "password": "Test-Mudur-2026"}
STAFF = {"username": "kasa1", "password": "Test-Kasa-2026"}
WHATSAPP_UA = "WhatsApp/2.23.20.0 A"
KAYIT_URL = "https://kafe.example.com/k/m/kayit"
DAMGA_URL = "https://kafe.example.com/k/m/qr"


def _client():
    return TestClient(app, base_url="https://testserver")


def _panel(creds):
    c = _client()
    assert c.post("/panel/login", json=creds).json()["status"] == "success"
    return c


def _static(on):
    with get_db(immediate=True) as conn:
        set_setting(conn, "static_qr_stamps", "1" if on else "0")


def _limits(daily=5, cooldown=0):
    with get_db(immediate=True) as conn:
        set_setting(conn, "daily_limit", str(daily))
        set_setting(conn, "min_interval_minutes", str(cooldown))


def _sources(phone_e164):
    with get_db() as conn:
        return [r[0] for r in conn.execute(
            "SELECT s.source FROM stamps s JOIN customers c ON c.id = s.customer_id WHERE c.phone_e164 = ? "
            "AND s.voided_at IS NULL ORDER BY s.id", (phone_e164,))]


def _login(c, phone, **extra):
    return c.post("/api/auth/login", json={"phone": phone, **CONSENT, **extra}).json()


def _purchase_stamps():
    with get_db() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM stamps WHERE voided_at IS NULL AND COALESCE(source, '') != 'welcome'").fetchone()[0]


def _screen_code():
    with get_db(immediate=True) as conn:
        return stamp_codes.create_stamp_code(conn, created_by="test")["code"]


# ------------------------------------------------------------------ 1. /m/kayit sayfası: 200, açılış yok, damga yok
def test_kayit_page_is_200_with_no_opening_in_both_modes(client):
    for static_on in (True, False):
        _static(static_on)
        for path in ("/k/m/kayit", "/k/m/kayit/", "/k/m/KAYIT", "/m/kayit"):
            r = client.get(path, follow_redirects=False)
            assert r.status_code == 200, path                       # asla 302 (ADR-01)
            assert not r.headers.get("x-claim-token"), path        # tek kullanımlık açılış üretilmez
            assert 'const ENTRY = "kayit"' in r.text
            assert "Kayıt hediyesi: +1 damga" in r.text
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM stamp_codes").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM stamps").fetchone()[0] == 0


def test_kayit_page_ignores_nfc_signature_and_preview_bots(client):
    _keys()
    r = client.get("/k/m/kayit?" + _tap(5))                        # etikete /m/kayit + SUN yazılsa bile damga yok
    assert r.status_code == 200 and not r.headers.get("x-claim-token") and 'const ENTRY = "kayit"' in r.text
    bot = client.get("/k/m/kayit", headers={"User-Agent": WHATSAPP_UA})
    assert bot.status_code == 200 and not bot.headers.get("x-claim-token") and 'const ENTRY = ""' in bot.text
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM nfc_tags").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM stamp_codes").fetchone()[0] == 0


def test_stamp_qr_page_is_unchanged(client):
    """/m/qr ADR-29: her GET tek kullanımlık açılış; static_qr_stamps=0 iken yalnızca kayıt (entry=register)."""
    page = client.get("/k/m/qr", follow_redirects=False)
    assert page.status_code == 200 and page.headers["x-claim-token"] and 'const ENTRY = "scan"' in page.text
    assert client.get("/k/m/qr").headers["x-claim-token"] != page.headers["x-claim-token"]
    _static(False)
    reg = client.get("/k/m/qr")
    assert reg.status_code == 200 and not reg.headers.get("x-claim-token") and 'const ENTRY = "register"' in reg.text


# ------------------------------------------------------------------ 2. uygulama içi okutucu + /api/claim: damga yok
def test_inapp_scanner_on_kayit_never_stamps_registered_customer_in_both_modes(client):
    _limits(daily=5, cooldown=0)
    c = _client()
    assert _login(c, "0532 700 10 01")["status"] == "success"          # kayıt hediyesi: 1 damga
    for static_on in (True, False):
        _static(static_on)
        for token in (KAYIT_URL, "/m/kayit", "kayit", "https://kafem.pages.dev/m/kayit",
                      KAYIT_URL + "?p=" + "0" * 32 + "&c=" + "0" * 16):
            r = c.post("/api/claim", json={"claim_token": token}).json()
            assert r["status"] == "registration_qr" and r["current_stamps"] == 1, (static_on, token)
            assert "damga vermez" in r["message"] and "Damga" in r["message"]
    assert _purchase_stamps() == 0
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM stamp_codes").fetchone()[0] == 0


def test_kayit_registers_unregistered_customer_with_gift_only(client):
    for i, static_on in enumerate((True, False)):
        _static(static_on)
        c = _client()
        phone = f"0532 700 11 0{i}"
        need = c.post("/api/claim", json={"claim_token": "/m/kayit"}).json()
        assert need["status"] == "need_auth"
        r = c.post("/api/claim", json={"claim_token": KAYIT_URL, "phone": phone, **CONSENT}).json()
        assert r["status"] == "registered" and r["welcome_gift"] is True and r["current_stamps"] == 1
        assert "+1 damga" in r["message"] and "damga vermez" in r["message"]
        assert _sources(f"+90532700110{i}") == ["welcome"]
    assert _purchase_stamps() == 0


def test_kayit_gives_no_stamp_even_with_a_valid_nfc_style_claim_token(client):
    _keys()
    _limits()
    c = _client()
    assert _login(c, "0532 700 12 01")["status"] == "success"
    r = c.post("/api/claim", json={"claim_token": "/m/kayit?" + _tap(9)}).json()
    assert r["status"] == "registration_qr"
    assert _purchase_stamps() == 0
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM nfc_tags").fetchone()[0] == 0


# ------------------------------------------------------------------ 3. mühür: kalıcı, idempotent, damga mührü olamaz
def test_kayit_seal_is_created_idempotently_and_cannot_be_disabled_or_used_for_stamps(client):
    from app.services.admin import toggle_seal_admin
    from app.services.loyalty import _get_fallback_active_seal
    with get_db() as conn:
        row = conn.execute("SELECT id, active FROM seals WHERE token = 'kayit'").fetchone()
    assert row is not None and row["active"] == 1
    # canlı veritabanında mühür yoksa ya da pasifse init_db yeniden kurar; tekrarlanınca çoğalmaz
    with get_db(immediate=True) as conn:
        conn.execute("DELETE FROM seals WHERE token = 'kayit'")
    init_db()
    init_db()
    with get_db(immediate=True) as conn:
        assert conn.execute("SELECT COUNT(*) FROM seals WHERE token = 'kayit'").fetchone()[0] == 1
        conn.execute("UPDATE seals SET active = 0 WHERE token = 'kayit'")
    init_db()
    with get_db(immediate=True) as conn:
        kid = conn.execute("SELECT id, active FROM seals WHERE token = 'kayit'").fetchone()
        assert kid["active"] == 1
        toggle_seal_admin(conn, kid["id"])                       # panelden devre dışı bırakılamaz
        assert conn.execute("SELECT active FROM seals WHERE token = 'kayit'").fetchone()[0] == 1
        # damga mührü olarak asla seçilmez (qr mührü pasif olsa bile)
        conn.execute("UPDATE seals SET active = 0 WHERE token != 'kayit'")
        fallback = _get_fallback_active_seal(conn)
        assert fallback["token"] != "kayit"
    # devre dışı sayılan başka mühürler kayıt QR'ını etkilemez
    assert client.get("/k/m/kayit").status_code == 200


# ------------------------------------------------------------------ 4. kayıt hediyesi: her kayıt yolunda tek +1
def test_welcome_gift_from_login_screen_once_and_not_for_returning_login(client):
    c = _client()
    first = _login(c, "0532 700 20 01")
    assert first["status"] == "success" and first["welcome_gift"] is True and first["current_stamps"] == 1
    assert "+1 damga" in first["welcome_message"]
    again = _login(c, "0532 700 20 01")                                # aynı müşteri yeniden giriş
    assert again["welcome_gift"] is False and again["current_stamps"] == 1
    other_device = _login(_client(), "0532 700 20 01")                 # başka cihazdan giriş
    assert other_device["welcome_gift"] is False and other_device["current_stamps"] == 1
    assert _sources("+905327002001") == ["welcome"]
    with get_db() as conn:
        assert conn.execute("SELECT welcome_bonus_awarded FROM customers WHERE phone_e164 = '+905327002001'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM customer_events WHERE kind = 'welcome_gift'").fetchone()[0] == 1


def test_welcome_gift_when_registering_through_stamp_qr_scan_plus_purchase_stamp(client):
    _limits(daily=1, cooldown=60)                                      # sıkı kurallarda bile hediye + damga
    c = _client()
    code = c.get("/k/m/qr").headers["x-claim-token"]
    r = c.post("/api/claim", json={"claim_token": "/d/" + code, "phone": "0532 700 21 01", **CONSENT}).json()
    assert r["status"] == "success" and r["welcome_gift"] is True and r["current_stamps"] == 2
    assert _sources("+905327002101") == ["welcome", "qr"]
    # aynı ziyarette ikinci QR damgası yok (bekleme QR damgasından sayılır); günlük limit yalnızca QR damgasını sayar
    again = c.post("/api/claim", json={"claim_token": "/d/" + c.get("/k/m/qr").headers["x-claim-token"]}).json()
    assert again["status"] == "limit_reached" and again["current_stamps"] == 2
    assert _purchase_stamps() == 1


def test_welcome_gift_through_display_screen_code_and_nfc_tap(client):
    _limits(daily=5)
    c = _client()
    r = c.post("/api/claim", json={"claim_token": "/d/" + _screen_code(), "phone": "0532 700 22 01", **CONSENT}).json()
    assert r["status"] == "success" and r["welcome_gift"] is True and r["current_stamps"] == 2
    assert _sources("+905327002201") == ["welcome", "qr"]
    # NFC dokunuşuyla kayıt
    _keys()
    n = _client()
    q = _tap(70)
    assert _open_tap(n, q)["status"] == "need_auth"
    reg = n.post("/api/claim", json={"claim_token": "/n?" + q, "phone": "0532 700 22 02", **CONSENT}).json()
    assert reg["status"] == "success" and reg["welcome_gift"] is True and reg["current_stamps"] == 2
    assert _sources("+905327002202") == ["welcome", "qr"]


def test_welcome_gift_when_stamp_qr_is_register_only_and_via_kayit(client):
    _static(False)
    a = _client().post("/api/claim", json={"claim_token": "/m/qr", "phone": "0532 700 23 01", **CONSENT}).json()
    assert a["status"] == "registered" and a["welcome_gift"] is True and a["current_stamps"] == 1
    b = _client().post("/api/claim", json={"claim_token": "/m/kayit", "phone": "0532 700 23 02", **CONSENT}).json()
    assert b["status"] == "registered" and b["welcome_gift"] is True and b["current_stamps"] == 1
    assert _sources("+905327002301") == ["welcome"] and _sources("+905327002302") == ["welcome"]
    assert _purchase_stamps() == 0


def test_no_second_gift_after_deletion_on_any_path(client):
    phone = "0532 700 24 01"
    c = _client()
    first = _login(c, phone)
    assert first["welcome_gift"] is True
    tok = first["device_token"]
    assert c.post("/api/delete-my-data", json={"phone": phone}, headers={"X-Device-Token": tok}).json()["status"] == "success"
    # (a) giriş ekranı, yeni cihaz
    a = _login(_client(), phone)
    assert a["welcome_gift"] is False and a["welcome_locked"] is True and a["current_stamps"] == 0
    assert "damga verilmedi" in a["welcome_message"]
    # silinen numara /m/kayit üzerinden de hediye almaz (hesap zaten açık; yeni telefon numarası aynı cihazda)
    c2 = _client()
    assert c2.post("/api/delete-my-data", json={"phone": phone}, headers={"X-Device-Token": a["device_token"]}).json()["status"] == "success"
    k = c2.post("/api/claim", json={"claim_token": "/m/kayit", "phone": phone, **CONSENT}).json()
    assert k["status"] == "registered" and k["welcome_gift"] is False and k["welcome_locked"] is True and k["current_stamps"] == 0
    # (b) cihaz kilidi: aynı cihazdan farklı numarayla kayıt
    k2 = c2.post("/api/claim", json={"claim_token": "/m/kayit", "phone": "0533 700 24 02", "device_token": a["device_token"], **CONSENT}).json()
    assert k2["status"] == "registered" and k2["welcome_gift"] is False
    assert _purchase_stamps() == 0


def test_no_retroactive_gift_for_existing_customers(client):
    """Bu değişiklikten ÖNCE kayıtlı müşteriye (hediyesini almamış olsa da) geriye dönük damga verilmez."""
    with get_db(immediate=True) as conn:
        conn.execute("INSERT INTO customers (id, phone_e164, display_name, created_at, welcome_bonus_awarded) "
                     "VALUES (801, '+905327002501', 'eski1', '2026-10-01T10:00:00+03:00', 0)")
        conn.execute("INSERT INTO customers (id, phone_e164, display_name, created_at, welcome_bonus_awarded, welcome_bonus_at) "
                     "VALUES (802, '+905327002502', 'eski2', '2026-10-01T10:00:00+03:00', 1, '2026-10-01T10:00:03+03:00')")
    init_db()                                                           # dağıtım / yeniden başlatma hediye vermez
    for phone in ("0532 700 25 01", "0532 700 25 02"):
        r = _login(_client(), phone)
        assert r["status"] == "success" and r["welcome_gift"] is False and r["current_stamps"] == 0
    c = _client()
    assert c.post("/api/claim", json={"claim_token": "/m/kayit", "phone": "0532 700 25 01", **CONSENT}).json()["welcome_gift"] is False
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM stamps WHERE customer_id IN (801, 802)").fetchone()[0] == 0
        assert conn.execute("SELECT welcome_bonus_awarded FROM customers WHERE id = 801").fetchone()[0] == 0


# ------------------------------------------------------------------ 5. hediye limiti / beklemeyi tüketmez
def test_gift_does_not_use_daily_limit_or_start_cooldown(client):
    _limits(daily=1, cooldown=60)
    c = _client()
    assert _login(c, "0532 700 30 01")["current_stamps"] == 1          # yalnızca hediye
    with get_db() as conn:
        from app.services.loyalty import count_today_stamps, _qr_cooldown_until
        from app.utils import get_istanbul_now
        cid = conn.execute("SELECT id FROM customers").fetchone()[0]
        assert count_today_stamps(conn, cid) == 0                       # hediye günlük limite sayılmaz
        assert _qr_cooldown_until(conn, cid, get_istanbul_now()) is None   # bekleme başlamaz
    # hemen ardından (günlük limit 1, bekleme 60 dk) ilk QR damgası verilir
    r = c.post("/api/claim", json={"claim_token": "/d/" + c.get("/k/m/qr").headers["x-claim-token"]}).json()
    assert r["status"] == "success" and r["current_stamps"] == 2
    # ikincisi bekleme / günlük limit kurallarına takılır (hediye bunları aşmaz)
    r2 = c.post("/api/claim", json={"claim_token": "/d/" + c.get("/k/m/qr").headers["x-claim-token"]}).json()
    assert r2["status"] == "limit_reached"
    # müşterinin kartındaki "bugün" sayacı da hediyeyi saymaz
    assert c.get("/api/my-card").json()["today_stamps"] == 1


def test_gift_stamp_can_unlock_reward_with_target_one_and_is_reported(client):
    with get_db(immediate=True) as conn:
        set_setting(conn, "stamp_target", "1")
    r = _login(_client(), "0532 700 31 01")
    assert r["welcome_gift"] is True and r["current_stamps"] == 0          # kart doldu → ödül bileti üretildi
    me = _panel(STAFF).post("/api/staff/customer-lookup", json={"q": "532 700 31 01"}).json()["customers"][0]
    assert me["welcome_bonus_status"] == "received" and me["current_stamps"] == 0


def test_reports_and_activity_log_show_the_gift(client):
    from openpyxl import load_workbook
    import io
    c = _client()
    _login(c, "0532 700 32 01")
    mgr = _panel(MANAGER)
    wb = load_workbook(io.BytesIO(mgr.get("/api/admin/reports/monthly-export?format=xlsx&month=all").content), data_only=True)
    acts = [r[4].value for r in wb["İşlem Geçmişi"].iter_rows(min_row=6)]
    assert "Hoş Geldin Hediyesi Aldı (+1 Damga)" in acts
    summary = {r[0].value: r[1].value for r in wb["Özet"].iter_rows(min_row=6, max_row=16)}
    assert summary["Hoş geldin hediyesi"] == 1
    ev = mgr.get("/api/admin/activity?days=0").json()["events"]
    assert [e["title"] for e in ev if e["kind"] == "welcome_gift"] == ["Kayıt hediyesi verildi"]
    s = mgr.get("/api/admin/activity/summary?days=7").json()["totals"]
    assert s["stamps_gift"] == 1 and s["stamps_qr"] == 0


# ------------------------------------------------------------------ 6. qr-image türleri doğru adresi kodlar
def test_qr_image_kinds_encode_the_right_urls(client, monkeypatch):
    import app.routes.manager as manager_routes
    import app.routes.staff as staff_routes

    seen: list[str] = []

    def fake_png(data: str) -> bytes:
        seen.append(data)
        return b"\x89PNG\r\n\x1a\n"

    monkeypatch.setattr(manager_routes, "generate_qr_png_bytes", fake_png)
    monkeypatch.setattr(staff_routes, "generate_qr_png_bytes", fake_png)
    expected = {"kayit": "/m/kayit", "damga": "/m/qr", "install": "/indir", "register": "/m/qr", "stamp": "/m/qr", "bilinmeyen": "/m/qr"}
    for who, prefix in ((_panel(MANAGER), "api/admin"), (_panel(STAFF), "api/staff")):
        for kind, path in expected.items():
            seen.clear()
            r = who.get(f"/{prefix}/qr-image?kind={kind}")
            assert r.status_code == 200 and r.content.startswith(b"\x89PNG")
            assert len(seen) == 1 and seen[0].endswith(path), (prefix, kind, seen)
            assert ("/m/kayit" in seen[0]) == (kind == "kayit")        # yalnızca kind=kayit kayıt adresini kodlar
    # varsayılan (kind verilmezse): eski davranış — damga QR'ı adresi
    seen.clear()
    _panel(MANAGER).get("/api/admin/qr-image")
    assert seen[0].endswith("/m/qr")
    # indirme dosya adları türe uyar
    dl = _panel(MANAGER)
    assert "qr-kayit-uygulama" in dl.get("/api/admin/qr-image?kind=kayit&download=1").headers["content-disposition"]
    assert "qr-kasa-damga" in dl.get("/api/admin/qr-image?kind=damga&download=1").headers["content-disposition"]
    assert "qr-masa-menu" in dl.get("/api/admin/qr-image?kind=install&download=1").headers["content-disposition"]


def test_qr_image_png_differs_between_kayit_and_damga(client):
    mgr = _panel(MANAGER)
    kayit = mgr.get("/api/admin/qr-image?kind=kayit").content
    damga = mgr.get("/api/admin/qr-image?kind=damga").content
    legacy = mgr.get("/api/admin/qr-image?kind=register").content
    assert kayit.startswith(b"\x89PNG") and kayit != damga          # iki QR asla aynı görsel değil
    assert damga == legacy                                            # eski tür geriye uyumlu: /m/qr


# ------------------------------------------------------------------ 7. paneller: etiketler görsellerle uyumlu
def test_manager_panel_labels_two_separate_qrs_and_keeps_forbidden_words_out(client):
    page = _panel(MANAGER).get("/mudur").text
    assert "Kayıt / Uygulama QR'ı" in page and "sistemi açar, damga vermez" in page
    assert "Damga QR'ı" in page and "kasadaki damga noktası" in page
    assert "api/admin/qr-image?kind=kayit" in page and "api/admin/qr-image?kind=damga" in page and "kind=install" in page
    assert 'id="qr-dual-register"' in page and 'id="qr-dual-stamp"' in page and 'id="qr-dual-install"' in page
    assert "kind=register" not in page                                # eski tür artık panelde kullanılmaz
    assert "İşletme" not in page and "card-business" not in page and "setting-business-name" not in page
    staff = _panel(STAFF).get("/personel").text
    assert "kind=damga" in staff and "kind=kayit" in staff and "kind=register" not in staff


def test_customer_app_texts_no_longer_say_one_qr_does_both(client):
    html = client.get("/k/m/kayit").text
    assert "KAYIT_QR_TEXT" in html and "damga vermez" in html and 'ENTRY === "kayit"' in html
    assert "WELCOME_GIFT_TEXT" in html and "res.welcome_gift" in html
    assert "Kayıt Ol ve İlk Damgamı Al" not in html


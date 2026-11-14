"""
ADR-36 (denetim Y-02, Y-03, Y-04, Y-10):
- "Bildirimleri Aç" kampanya (ticari ileti) onayı yazmaz; hediye damga yalnızca izne bağlıdır.
- "Bildirimleri Aç" ile paketli verilmiş eski onaylar bir kez geçersiz sayılır ve sakin kartla yeniden onaya alınır.
- Kampanya bildirimi göndereni tanıtır ve gövdede ret satırı taşır; işlemsel bildirimler taşımaz.
- Aydınlatma v6 Cloudflare'i anar; Sadakat Programı Koşulları ayarlardan üretilir.
"""
import json
import re

from starlette.testclient import TestClient

from app.db import get_db, init_db, reconfirm_bundled_marketing_consents, set_setting
from app.main import app

CONSENT = {"kvkk_read": True}
MANAGER = {"username": "mudur", "password": "Test-Mudur-2026"}
SUB = {"p256dh": "BNcRdreALRFXTkOOUHK1EtK2wtaz5Ry4YfYCA_0QTpQ", "auth": "tBHItJI5svbpez7KI4CCXg"}
VAPID = {
    "VAPID_PUBLIC_KEY": "BEl62iUYgUivxIkv69yViEuiBIa-Ib9-SkvMeAtA3LFgDzkrxZJjSgSnfckjBJuBkr3qBUYIHBQFLXYp5Nksh8U",
    "VAPID_PRIVATE_KEY": "UUxI4O8-FbRouAevSmBQ6o18hgE4nSG3qwvJTfKc-ls",
    "VAPID_SUBJECT": "mailto:test@example.com",
}
HINT = "Kapatmak için: Profil › Kampanya Bildirimleri"


def _new_client():
    return TestClient(app, base_url="https://testserver")


def _register(phone, **extra):
    c = _new_client()
    r = c.post("/api/auth/login", json={"phone": phone, **CONSENT, **extra}).json()
    assert r["status"] == "success", r
    return c, {"X-Device-Token": r["device_token"]}


def _js_const(html, name):
    m = re.search(r"const %s = (\".*?\");" % name, html)
    assert m, name
    return json.loads(m.group(1))


def _customer_row(phone_digits):
    with get_db() as conn:
        return conn.execute("SELECT * FROM customers WHERE phone_e164 LIKE ?", (f"%{phone_digits}",)).fetchone()


def _seed_consented(conn, phone, channel, audit_channel=None):
    """Doğrudan veritabanına onaylı müşteri yazar: channel = sütun değeri (None = v5 öncesi), audit_channel = günlükteki kanal."""
    cur = conn.execute(
        "INSERT INTO customers (phone_e164, created_at, display_name, marketing_consent, marketing_consent_at, "
        "marketing_consent_channel, kvkk_read_at) VALUES (?, '2026-11-01T10:00:00', ?, 1, '2026-11-02T10:00:00', ?, '2026-11-01T10:00:00')",
        (phone, f"u{phone[-4:]}", channel),
    )
    cid = cur.lastrowid
    if audit_channel is not None:
        conn.execute(
            "INSERT INTO audit_log (at, actor, action, detail) VALUES ('2026-11-02T10:00:00', ?, 'marketing_consent_given', ?)",
            (f"customer:{cid}", f"Açık rıza + ticari ileti onayı verildi (metin sürümü=2026-11-v3, kanal={audit_channel}, ip=1.1.1.1)."),
        )
    return cid


# ------------------------------------------------------------------ Y-02: "Bildirimleri Aç" onay yazmaz
def test_notification_open_gives_gift_but_no_marketing_consent(client):
    c, h = _register("0532 970 00 01")
    before = c.get("/api/home-data", headers=h).json()["customer"]["current_stamps"]
    assert c.post("/api/push/subscribe", json={"endpoint": "https://push.example/u1", **SUB, "marketing_consent": True},
                  headers=h).json()["marketing_consent"] is False
    r = c.post("/api/customer/notification-permission", json={"permission": "granted", "consent": True}, headers=h).json()
    assert r["status"] == "success" and r["bonus_awarded"] is True and r["marketing_consent"] is False
    me = c.get("/api/home-data", headers=h).json()["customer"]
    assert me["current_stamps"] == before + 1 and me["notif_bonus_claimed"] == 1 and me["marketing_consent"] is False
    row = _customer_row("5329700001")
    assert row["marketing_consent"] == 0 and row["marketing_consent_channel"] is None and row["explicit_consent_at"] is None
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action='marketing_consent_given'").fetchone()[0] == 0
    # once-only kuralı aynen: ikinci izin bildirimi ek damga vermez
    r2 = c.post("/api/customer/notification-permission", json={"permission": "granted"}, headers=h).json()
    assert r2["bonus_awarded"] is False


def test_page_has_no_bundled_consent_wording_and_gift_is_unconditional(client):
    page = client.get("/k/kart/").text
    assert "kabul etmiş olursun" not in page and "kampanya bildirimi almayı kabul edersin" not in page
    assert "kampanya bildirimi onayına bağlı değildir" in page
    assert "Yalnızca kampanya ve fırsat bildirimleri (isteğe bağlı)" in page and "Kampanya ve damga bildirimleri" not in page
    # kayıt kutusu hâlâ ayrı, işaretsiz ve isteğe bağlı
    assert 'id="consent-marketing" type="checkbox" class="consent-checkbox" onchange' in page


# ------------------------------------------------------------------ Y-02: eski paketli onayları yeniden onaya alma
def test_migration_only_reconfirms_consents_provably_given_through_the_bundled_button(client):
    with get_db(immediate=True) as conn:
        bundled_col = _seed_consented(conn, "+905320000101", "bildirim izni (Bildirimleri Aç)")
        explicit_reg = _seed_consented(conn, "+905320000102", "kayıt formu (self)")
        explicit_sw = _seed_consented(conn, "+905320000103", "site içi bildirim anahtarı")
        bundled_log = _seed_consented(conn, "+905320000104", None, audit_channel="bildirim izni")            # v5 öncesi, günlükten
        bundled_log2 = _seed_consented(conn, "+905320000105", None, audit_channel="bildirim izni (Bildirimleri Aç)")
        explicit_log = _seed_consented(conn, "+905320000106", None, audit_channel="kayıt formu (self)")
        unknown = _seed_consented(conn, "+905320000107", None)                                              # iz yok → dokunma
        old_toggle = _seed_consented(conn, "+905320000108", None, audit_channel="bildirim anahtarı")      # ADR-21/22 anahtarı = açık seçim
        no_consent = conn.execute(
            "INSERT INTO customers (phone_e164, created_at, marketing_consent) VALUES ('+905320000109', '2026-11-01T10:00:00', 0)"
        ).lastrowid
        result = reconfirm_bundled_marketing_consents(conn)
    assert result == {"checked": 8, "reconfirm": 3, "kept_explicit": 4, "unknown": 1}, result

    with get_db() as conn:
        row = lambda cid: conn.execute("SELECT * FROM customers WHERE id = ?", (cid,)).fetchone()  # noqa: E731
        for cid in (bundled_col, bundled_log, bundled_log2):
            r = row(cid)
            assert r["marketing_consent"] == 0 and r["marketing_reconfirm_pending"] == 1 and r["explicit_consent_at"] is None
            assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE actor=? AND action='marketing_consent_reconfirm_required'",
                                (f"customer:{cid}",)).fetchone()[0] == 1
        for cid in (explicit_reg, explicit_sw, explicit_log, old_toggle, unknown):
            r = row(cid)
            assert r["marketing_consent"] == 1 and r["marketing_reconfirm_pending"] == 0
        assert row(no_consent)["marketing_reconfirm_pending"] == 0
    # idempotent: ikinci çalıştırmada değişecek bir şey kalmaz
    with get_db(immediate=True) as conn:
        again = reconfirm_bundled_marketing_consents(conn)
    assert again["reconfirm"] == 0


def test_init_db_runs_the_migration_exactly_once():
    with get_db(immediate=True) as conn:
        cid = _seed_consented(conn, "+905320000201", "bildirim izni (Bildirimleri Aç)")
        conn.execute("DELETE FROM settings WHERE key = 'bundled_consent_reconfirm_v1'")
    init_db()
    with get_db() as conn:
        assert conn.execute("SELECT marketing_reconfirm_pending FROM customers WHERE id=?", (cid,)).fetchone()[0] == 1
        flag = json.loads(conn.execute("SELECT value FROM settings WHERE key='bundled_consent_reconfirm_v1'").fetchone()[0])
        assert flag["reconfirm"] == 1
    # sonradan (yeni sürümde) paketli kanal etiketiyle bir onay oluşsa bile açılış bir daha dokunmaz (bayrak kalıcı)
    with get_db(immediate=True) as conn:
        late = _seed_consented(conn, "+905320000202", "bildirim izni (Bildirimleri Aç)")
    init_db()
    with get_db() as conn:
        assert conn.execute("SELECT marketing_consent FROM customers WHERE id=?", (late,)).fetchone()[0] == 1


def test_reconfirm_pending_customers_are_excluded_from_audience_and_counted_separately(client, monkeypatch):
    import app.services.push as push_service
    for k, v in VAPID.items():
        monkeypatch.setenv(k, v)
    sent = []
    monkeypatch.setattr(push_service, "webpush",
                        lambda subscription_info, data, **kw: sent.append((subscription_info["endpoint"], json.loads(data))) or True)

    explicit, h1 = _register("0532 970 10 01", marketing_consent=True)      # açık kutu → alır
    pending, h2 = _register("0532 970 10 02", marketing_consent=True)       # paketli sayılıp yeniden onaya alınacak
    never, h3 = _register("0532 970 10 03")                                  # hiç onay yok
    for cl, h, ep in ((explicit, h1, "e"), (pending, h2, "p"), (never, h3, "n")):
        assert cl.post("/api/push/subscribe", json={"endpoint": f"https://push.example/aud-{ep}", **SUB}, headers=h).status_code == 200
    with get_db(immediate=True) as conn:
        conn.execute("UPDATE customers SET marketing_consent_channel = 'bildirim izni (Bildirimleri Aç)' WHERE phone_e164 LIKE '%5329701002'")
        reconfirm_bundled_marketing_consents(conn)

    mgr = _new_client()
    assert mgr.post("/panel/login", json=MANAGER).json()["status"] == "success"
    prev = mgr.get("/api/admin/push/preview?audience=all").json()
    assert prev["customer_count"] == 1 and prev["subscription_count"] == 1          # yalnızca açık onaylı
    assert prev["reconfirm_pending_count"] == 1 and prev["pending_consent_count"] == 2
    res = mgr.post("/api/admin/push/send", json={"title": "Fırsat", "body": "Bugün tatlı ikram!", "audience": "all"}).json()
    assert res["status"] == "success" and res["sent_count"] == 1
    assert [e for e, _ in sent] == ["https://push.example/aud-e"]


def test_reconfirm_card_flow_records_durable_proof(client):
    c, h = _register("0532 970 20 01", marketing_consent=True)
    with get_db(immediate=True) as conn:
        conn.execute("UPDATE customers SET marketing_consent_channel = 'bildirim izni (Bildirimleri Aç)'")
        reconfirm_bundled_marketing_consents(conn)
    me = c.get("/api/home-data", headers=h).json()["customer"]
    assert me["marketing_reconfirm_pending"] is True and me["marketing_consent"] is False

    # EVET: açık onay, kalıcı kanıt (kanal reconfirm_card + metin sürümü + IP), bekleyen bayrak kalkar
    r = c.post("/api/me/marketing-reconfirm", json={"answer": "yes"}, headers=h).json()
    assert r == {"status": "success", "answer": "yes", "marketing_consent": True}
    row = _customer_row("5329702001")
    assert row["marketing_consent"] == 1 and row["marketing_reconfirm_pending"] == 0
    assert row["marketing_consent_channel"] == "reconfirm_card" and row["marketing_consent_version"] == "2026-11-v4"
    assert row["marketing_consent_ip"] and row["explicit_consent_at"]
    with get_db() as conn:
        log = conn.execute("SELECT detail FROM audit_log WHERE action='marketing_consent_given' ORDER BY id DESC LIMIT 1").fetchone()[0]
        assert "kanal=reconfirm_card" in log
    me = c.get("/api/home-data", headers=h).json()["customer"]
    assert me["marketing_consent"] is True and me["marketing_reconfirm_pending"] is False
    # bekleyen yokken tekrar "yes" hiçbir şeyi değiştirmez
    assert c.post("/api/me/marketing-reconfirm", json={"answer": "no"}, headers=h).json()["status"] == "not_pending"
    assert _customer_row("5329702001")["marketing_consent"] == 1


def test_reconfirm_no_dismiss_and_profile_toggle(client):
    def pending_customer(phone):
        c, h = _register(phone, marketing_consent=True)
        with get_db(immediate=True) as conn:
            conn.execute("UPDATE customers SET marketing_consent_channel = 'bildirim izni (Bildirimleri Aç)' "
                         "WHERE marketing_reconfirm_pending = 0 AND marketing_consent = 1")
            reconfirm_bundled_marketing_consents(conn)
        return c, h

    c, h = pending_customer("0532 970 30 01")                       # HAYIR
    r = c.post("/api/me/marketing-reconfirm", json={"answer": "no"}, headers=h).json()
    assert r["status"] == "success" and r["marketing_consent"] is False
    row = _customer_row("5329703001")
    assert row["marketing_consent"] == 0 and row["marketing_reconfirm_pending"] == 0 and row["marketing_consent_channel"] == "reconfirm_card"
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action='marketing_consent_revoked'").fetchone()[0] == 1

    c, h = pending_customer("0532 970 30 02")                       # KAPAT (×): cevap yok, onay yok, kart bir daha gelmez
    r = c.post("/api/me/marketing-reconfirm", json={"answer": "dismiss"}, headers=h).json()
    assert r["status"] == "success" and r["marketing_consent"] is False
    row = _customer_row("5329703002")
    assert row["marketing_consent"] == 0 and row["marketing_reconfirm_pending"] == 0
    assert c.post("/api/me/marketing-reconfirm", json={"answer": "yes"}, headers=h).json()["status"] == "not_pending"
    assert _customer_row("5329703002")["marketing_consent"] == 0

    c, h = pending_customer("0532 970 30 03")                       # Profil anahtarı da yeniden onayı çözer
    assert c.post("/api/me/marketing-consent", json={"consent": True}, headers=h).json()["marketing_consent"] is True
    row = _customer_row("5329703003")
    assert row["marketing_consent"] == 1 and row["marketing_reconfirm_pending"] == 0
    assert row["marketing_consent_channel"] == "site içi bildirim anahtarı"

    assert c.post("/api/me/marketing-reconfirm", json={"answer": "bogus"}, headers=h).json()["status"] == "error"
    assert _new_client().post("/api/me/marketing-reconfirm", json={"answer": "yes"}).json()["status"] == "device_not_found"


def test_customer_page_has_calm_dismissible_reconfirm_card(client):
    page = client.get("/k/kart/").text
    assert 'id="home-reconfirm-card"' in page and "/api/me/marketing-reconfirm" in page
    assert "function answerReconfirm" in page and "function updateReconfirmCard" in page
    card = page.split('id="home-reconfirm-card"', 1)[1].split("</section>", 1)[0]
    for needle in ("Evet, gönderin", "Hayır, teşekkürler", "answerReconfirm('dismiss')", "ticari ileti"):
        assert needle in card, needle
    # pop-up değil: modal-overlay değil, ana ekranda satır içi kart
    assert "modal-overlay" not in card


def test_deleting_account_clears_pending_flag(client):
    c, h = _register("0532 970 40 01", marketing_consent=True)
    with get_db(immediate=True) as conn:
        conn.execute("UPDATE customers SET marketing_consent_channel = 'bildirim izni (Bildirimleri Aç)'")
        reconfirm_bundled_marketing_consents(conn)
    assert c.post("/api/delete-my-data", json={"phone": "0532 970 40 01"}).json()["status"] == "success"
    with get_db() as conn:
        r = conn.execute("SELECT marketing_consent, marketing_reconfirm_pending FROM customers").fetchone()
        assert r["marketing_consent"] == 0 and r["marketing_reconfirm_pending"] == 0


# ------------------------------------------------------------------ Y-04: kampanya bildirimi kimlik + ret satırı
def test_campaign_payload_identifies_sender_and_has_optout_line_but_transactional_does_not(client, monkeypatch):
    import app.services.push as push_service
    for k, v in VAPID.items():
        monkeypatch.setenv(k, v)
    sent = []
    monkeypatch.setattr(push_service, "webpush", lambda subscription_info, data, **kw: sent.append(json.loads(data)) or True)

    c, h = _register("0532 970 50 01", marketing_consent=True)
    assert c.post("/api/push/subscribe", json={"endpoint": "https://push.example/pay", **SUB}, headers=h).status_code == 200
    mgr = _new_client()
    assert mgr.post("/panel/login", json=MANAGER).json()["status"] == "success"
    res = mgr.post("/api/admin/push/send", json={"title": "Hafta Sonu", "body": "İkinci kahve %50", "audience": "all"}).json()
    assert res["status"] == "success" and res["sent_count"] == 1
    from app.config import DEFAULT_BUSINESS_NAME
    camp = sent[-1]
    assert camp["kind"] == "campaign" and camp["title"] == f"{DEFAULT_BUSINESS_NAME} · Hafta Sonu" and camp["brand"] == DEFAULT_BUSINESS_NAME
    assert camp["body"].splitlines() == ["İkinci kahve %50", HINT] and camp["body"].endswith(HINT)
    assert "actions" not in camp and "optout" not in camp
    # marka adı başlıkta zaten varsa tekrarlanmaz
    mgr.post("/api/admin/push/send", json={"title": f"{DEFAULT_BUSINESS_NAME} Haberi", "body": "x", "audience": "all"})
    assert sent[-1]["title"] == f"{DEFAULT_BUSINESS_NAME} Haberi"
    # hint iki kez eklenmez
    assert push_service.campaign_body("a" + chr(10) + HINT) == "a" + chr(10) + HINT

    # işlemsel (damga / ödül) ve test bildirimleri ret satırı taşımaz
    with get_db(immediate=True) as conn:
        cid = conn.execute("SELECT id FROM customers").fetchone()["id"]
        push_service.notify_customer_after_stamp(conn, cid, 2, 5, False, "Kahve")
        push_service.notify_customer_after_stamp(conn, cid, 5, 5, True, "Kahve")
    transactional = sent[-2:]
    assert len(transactional) == 2 and all(HINT not in p["body"] and p["kind"] != "campaign" for p in transactional)
    n = len(sent)
    assert mgr.post("/api/admin/push/test", json={"title": "T", "body": "B", "endpoint": "https://push.example/pay"}).json()["status"] == "success"
    assert len(sent) == n + 1 and HINT not in sent[-1]["body"]


def test_manager_preview_mockup_matches_what_is_sent(client):
    from app.config import DEFAULT_BUSINESS_NAME
    mgr = _new_client()
    assert mgr.post("/panel/login", json=MANAGER).json()["status"] == "success"
    page = mgr.get("/mudur").text
    assert _js_const(page, "PUSH_OPTOUT_HINT") == HINT and _js_const(page, "PUSH_BRAND") == DEFAULT_BUSINESS_NAME
    # sunucuda çizilen başlangıç önizlemesi gövdede ret satırını gösterir; İşletme sözcüğü/kimlikleri yasak
    mock = page.split('id="phone-push-body"', 1)[1].split("</p>", 1)[0]
    assert HINT in mock and "white-space: pre-line" in page
    assert "function renderPushPreview" in page and "İşletme" not in page
    assert 'id="card-business"' not in page and 'id="setting-business-name"' not in page


# ------------------------------------------------------------------ Y-03: aydınlatma v6
def test_notice_v6_mentions_cloudflare_without_claiming_legal_basis(client):
    kvkk = _js_const(client.get("/k/kart/").text, "KVKK_TEXT")
    assert "Metin sürümü: 2026-11-v7" in kvkk
    assert "Cloudflare, Inc. (ABD)" in kvkk
    assert "IP adresiniz" in kvkk and "yurt dışına aktarım içerebilir" in kvkk
    for forbidden in ("standart sözleşme", "sözleşme imzalan", "yeterlilik kararı", "m.9"):
        assert forbidden not in kvkk, forbidden
    # yeni teyit v6 olarak kayıt altına alınır
    _register("0532 970 60 01")
    assert _customer_row("5329706001")["consent_version"] == "2026-11-v7"


# ------------------------------------------------------------------ Y-10: Sadakat Programı Koşulları
def test_terms_modal_renders_configured_values_and_invents_nothing(client):
    with get_db(immediate=True) as conn:
        set_setting(conn, "stamp_target", "8")
        set_setting(conn, "reward_name", "Ücretsiz Latte")
        set_setting(conn, "daily_limit", "3")
        set_setting(conn, "min_interval_minutes", "45")
    page = client.get("/k/kart/").text
    terms = _js_const(page, "TERMS_TEXT")
    for needle in ("8 damgaya ulaştığında", '"Ücretsiz Latte" ödülü', "en fazla 3 damga", "45 dakika",
                   "+1 hediye damga", "kampanya (ticari ileti) bildirimlerini onaylamana bağlı değildir",
                   "personele bildirerek", "nakde çevrilemez, başkasına devredilemez",
                   "Hesabını silersen damgaların ve kullanılmamış ödüllerin iptal edilir",
                   "24 ay boyunca hiç işlem yapılmayan hesaplar",
                   "değiştirebilir veya programı sona erdirebilir. Değişiklikler uygulamada duyurulur",
                   "uygulamada gösterilir ve güncellenebilir"):
        assert needle in terms, needle
    for invented in ("₺", " TL", "ticaret unvan", "MERSİS", "adres:", "ay geçerli", "gün içinde kullan"):
        assert invented not in terms, invented
    # bekleme kapalıysa (0) metinde dakika yok; değerler değişince metin de değişir
    with get_db(immediate=True) as conn:
        set_setting(conn, "min_interval_minutes", "0")
        set_setting(conn, "stamp_target", "10")
    terms2 = _js_const(client.get("/k/kart/").text, "TERMS_TEXT")
    assert "dakika" not in terms2 and "10 damgaya ulaştığında" in terms2


def test_terms_linked_from_register_screen_and_profile_without_extra_checkbox(client):
    page = client.get("/k/kart/").text
    assert page.count("openTermsModal()") >= 2 and "function openTermsModal" in page
    assert "Sadakat Programı Koşulları</a>'nı kabul edersin." in page and "Kayıt olarak" in page
    # ek zorunlu kutu yok: yalnızca iki onay kutusu (aydınlatma + isteğe bağlı kampanya)
    assert page.count('type="checkbox" class="consent-checkbox"') == 2
    profile = page.split('id="campaign-consent-switch"', 1)[1]
    assert profile.index("KVKK Aydınlatma Metni") < profile.index("Sadakat Programı Koşulları")

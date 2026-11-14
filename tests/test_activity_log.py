"""
ADR-31: müşteri ve kasa işlem kayıtları (customer_events) + "olası kötüye kullanım" notları.

- Her damga isteğinin sonucu tek listede, sade cümleyle: "Kayıt oldu", "Hoş geldin damgası verildi", "Damga verilmedi —
  bekleme süresi dolmadı (son damga … önce, yeni QR damgası en erken saat …)".
- Bağlantıyı yeniden kullanarak (yenileme, geri tuşu, açık sekmeye yapıştırma, başkasının bağlantısı, süresi dolmuş /
  uydurma bağlantı, Instagram içinden açma, WhatsApp'ta paylaşma) damga almaya çalışanlar NEDENİ yazılarak not edilir.
- Kasiyer / müdür işlemleri de aynı listede; ana ekran özeti (summary) analizleri verir.
"""
from datetime import timedelta
from urllib.parse import urlencode

from starlette.testclient import TestClient

from app.db import get_db, set_setting
from app.main import app
from app.services import activity, stamp_codes
from app.utils import get_istanbul_now

CONSENT = {"kvkk_read": True}
MANAGER = {"username": "mudur", "password": "Test-Mudur-2026"}
STAFF = {"username": "kasa1", "password": "Test-Kasa-2026"}
NEW_TAB = {"nav": "navigate", "hist": 1, "standalone": False, "via": "page"}
INSTAGRAM_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
                "Mobile/15E148 Instagram 300.0.0.0")


def _new_client():
    return TestClient(app, base_url="https://testserver")


def _login(creds):
    c = _new_client()
    assert c.post("/panel/login", json=creds).json()["status"] == "success"
    return c


def _member(phone="0532 400 00 01"):
    c = _new_client()
    assert c.post("/api/auth/login", json={"phone": phone, **CONSENT}).json()["status"] == "success"
    return c


def _settings(daily=5, cooldown=60):
    with get_db(immediate=True) as conn:
        set_setting(conn, "daily_limit", str(daily))
        set_setting(conn, "min_interval_minutes", str(cooldown))


def _scan(c, client=None, headers=None, **extra):
    """Kasadaki sabit QR'ı okutmak: sayfa tek kullanımlık açılış taşır, uygulama onu açılış şekliyle birlikte ister."""
    page = c.get("/k/m/qr")
    assert page.status_code == 200
    body = {"claim_token": "/d/" + page.headers["x-claim-token"], "client": client or NEW_TAB, **extra}
    return c.post("/api/claim", json=body, headers=headers or {}).json()


def _events(mgr, **params):
    res = mgr.get("/api/admin/activity?" + urlencode({"days": 0, **params})).json()
    assert res["status"] == "success", res
    return res["events"]


def _cid(phone_digits):
    with get_db() as conn:
        return conn.execute("SELECT id FROM customers WHERE phone_e164 = ?", ("+90" + phone_digits,)).fetchone()[0]


def _fraud_reasons(cid):
    with get_db() as conn:
        return [r["reason"] for r in conn.execute("SELECT reason FROM fraud_events WHERE customer_id = ? ORDER BY id", (cid,))]


# ------------------------------------------------------------------ 1. QR okuttu → kayıt → damga → tekrar okuttu
def test_scan_register_stamp_and_cooldown_are_listed_clearly(client):
    _settings(daily=5, cooldown=60)
    reg = _scan(client, phone="0532 400 00 02", **CONSENT)
    assert reg["status"] == "success" and reg["current_stamps"] == 2     # kayıt hediyesi + okutuşun damgası (ADR-35)
    again = _scan(client)
    assert again["status"] == "interval_limit"
    mgr = _login(MANAGER)
    cid = _cid("5324000002")
    evs = _events(mgr, customer_id=cid)
    assert [e["kind"] for e in evs] == ["cooldown", "stamp", "welcome_gift", "registered"]       # yeniden eskiye
    cooldown, stamp, gift, registered = evs
    assert registered["title"] == "Kayıt oldu" and "Kasadaki QR / NFC'yi okutup" in registered["detail"]
    assert gift["title"] == "Kayıt hediyesi verildi" and gift["outcome"] == "ok" and "1/5" in gift["detail"]
    assert stamp["title"] == "Damga verildi" and stamp["outcome"] == "ok" and "2/5" in stamp["detail"]
    assert cooldown["title"] == "Damga verilmedi — bekleme süresi dolmadı" and cooldown["outcome"] == "denied"
    assert "son damgasını" in cooldown["detail"] and "bekleme 60 dk" in cooldown["detail"] and "en erken saat" in cooldown["detail"]
    # kasada yeniden okutmak (yeni sekme) tek başına kötüye kullanım sayılmaz
    assert cooldown["flagged"] is False and cooldown["opened_via"] == "new_tab"
    assert "Yeni sekmede" in cooldown["opened_via_label"] and cooldown["customer_phone"].endswith("02")
    assert "5324000002" not in str(evs)                                                 # telefon maskeli


# ------------------------------------------------------------------ 2. bağlantıyı yeniden kullanmak → not
def test_reusing_the_link_within_cooldown_is_noted_as_possible_abuse(client):
    _settings(daily=5, cooldown=60)
    c = _member("0532 400 00 03")
    assert _scan(c)["status"] == "success"
    # açık bir sekmeye yapıştırılmış adres (sekme geçmişi > 1)
    assert _scan(c, client={"nav": "navigate", "hist": 3})["status"] == "interval_limit"
    # sayfayı yenileyerek yeniden istemek
    assert _scan(c, client={"nav": "reload", "hist": 1})["status"] == "interval_limit"
    # kasada üçüncü kez okutmak (yeni sekme) → art arda deneme notu
    assert _scan(c)["status"] == "interval_limit"
    cid = _cid("5324000003")
    evs = _events(_login(MANAGER), customer_id=cid, group="flagged")
    assert len(evs) == 3 and all(e["kind"] == "cooldown" for e in evs)
    third, reload, pasted = evs
    assert "yapıştırılmış olabilir" in pasted["flag_note"] and pasted["opened_via"] == "existing_tab"
    assert "yenilenerek" in reload["flag_note"]
    assert "3. kez" in third["flag_note"] and third["opened_via"] == "new_tab"
    assert _fraud_reasons(cid) == ["link_retry", "link_retry", "link_retry"]
    assert _scan(c, client={"nav": "navigate", "hist": 1})["status"] == "interval_limit"   # damga yine verilmez


def test_shared_reused_expired_and_forged_links_are_explained(client):
    _settings(daily=5, cooldown=0)
    owner = _member("0532 400 00 04")
    with get_db(immediate=True) as conn:
        info = stamp_codes.create_stamp_code(conn, created_by="Kasa 1")
    assert owner.post("/api/claim", json={"claim_token": info["url"]}).json()["status"] == "success"
    assert owner.post("/api/claim", json={"claim_token": info["url"]}).json()["status"] == "code_used"   # hemen çift okutma
    other = _member("0532 400 00 05")
    assert other.post("/api/claim", json={"claim_token": info["url"]}).json()["status"] == "code_used"
    with get_db(immediate=True) as conn:
        old = (get_istanbul_now() - timedelta(minutes=10)).isoformat()
        conn.execute("UPDATE stamp_codes SET used_at = ? WHERE id = ?", (old, info["id"]))
    owner.post("/api/claim", json={"claim_token": info["url"]})
    other.post("/api/claim", json={"claim_token": "https://testserver/k/d/UYDURMAKOD1234567890"})
    with get_db(immediate=True) as conn:
        expired = stamp_codes.create_stamp_code(conn, created_by="Kasa 1",
                                                now=get_istanbul_now() - timedelta(seconds=stamp_codes.CODE_TTL_SECONDS + 30))
    other.post("/api/claim", json={"claim_token": expired["url"]})

    mgr = _login(MANAGER)
    owner_id, other_id = _cid("5324000004"), _cid("5324000005")
    mine = {e["kind"]: e for e in _events(mgr, customer_id=owner_id)}
    assert mine["stamp"]["actor"] == "Kasa 1" and mine["stamp"]["channel"] == "screen"
    assert mine["link_refresh"]["flagged"] is False and "İkinci damga verilmedi" in mine["link_refresh"]["detail"]
    assert mine["link_reused"]["flagged"] and "10 dk sonra" in mine["link_reused"]["flag_note"]
    # bağlantının ilk sahibinin listesinde de başkasının denemesi görünür (ilgili müşteri)
    assert mine["link_shared"]["customer_id"] == other_id and mine["link_shared"]["related_customer_id"] == owner_id
    theirs = {e["kind"]: e for e in _events(mgr, customer_id=other_id)}
    shared = theirs["link_shared"]
    assert shared["flagged"] and "başka bir müşterinin işleminde kullanılmıştı" in shared["detail"]
    assert "kopyalanıp" in shared["flag_note"] and shared["related_name"].startswith("user")
    assert theirs["link_invalid"]["flagged"] and "uydurulmuş" in theirs["link_invalid"]["flag_note"]
    assert theirs["link_expired"]["flagged"] and "süresi dolmuştu" in theirs["link_expired"]["detail"]


def test_social_app_browser_and_messaging_previews_are_flagged(client):
    _settings(daily=5, cooldown=0)
    c = _member("0532 400 00 06")
    res = _scan(c, headers={"user-agent": INSTAGRAM_UA})
    assert res["status"] == "success"                         # sistem engellemez, yalnızca not eder
    cid = _cid("5324000006")
    ev = _events(_login(MANAGER), customer_id=cid, group="flagged")[0]
    assert ev["kind"] == "stamp" and ev["opened_via"] == "social:Instagram"
    assert "Instagram uygulamasının içinden açıldı" in ev["flag_note"]
    assert _fraud_reasons(cid) == ["link_social"]

    with get_db() as conn:
        openings = conn.execute("SELECT COUNT(*) FROM stamp_codes WHERE origin = 'scan'").fetchone()[0]
    bot = _new_client()
    for _ in range(2):                                        # WhatsApp önizlemeyi birkaç kez çekebilir: tek kayıt
        page = bot.get("/k/m/qr", headers={"user-agent": "WhatsApp/2.23.20.0 A"})
        assert page.status_code == 200 and "x-claim-token" not in page.headers
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM stamp_codes WHERE origin = 'scan'").fetchone()[0] == openings
    previews = [e for e in _events(_login(MANAGER), group="flagged") if e["kind"] == "link_preview"]
    assert len(previews) == 1 and "WhatsApp" in previews[0]["detail"] and previews[0]["customer_name"] == "Kayıtsız ziyaretçi"
    # gerçek tarayıcılar önizleyici sayılmaz (uygulama içi WhatsApp tarayıcısı Mozilla/ ile başlar)
    assert activity.preview_bot("Mozilla/5.0 (Linux; Android 14) WhatsApp/2.24") == ""
    assert activity.preview_bot("Mozilla/5.0 (Linux; Android 13; Cubot X30) Chrome/120 Mobile") == ""
    assert activity.preview_bot("TelegramBot (like TwitterBot)") == "Telegram"


def test_waiting_out_the_cooldown_after_retries_is_noted(client):
    _settings(daily=5, cooldown=60)
    c = _member("0532 400 00 07")
    cid = _cid("5324000007")
    now = get_istanbul_now()
    with get_db(immediate=True) as conn:
        seal_id = conn.execute("SELECT id FROM seals ORDER BY id LIMIT 1").fetchone()[0]
        conn.execute("INSERT INTO stamps (customer_id, seal_id, created_at, source) VALUES (?, ?, ?, 'qr')",
                     (cid, seal_id, (now - timedelta(minutes=62)).isoformat()))
        activity.record(conn, "cooldown", "Damga verilmedi — bekleme süresi dolmadı", customer_id=cid,
                        outcome=activity.DENIED, channel="scan", at=(now - timedelta(minutes=40)).isoformat())
    assert _scan(c)["status"] == "success"
    ev = _events(_login(MANAGER), customer_id=cid)[0]
    assert ev["kind"] == "stamp" and ev["flagged"] and "süre dolar dolmaz" in ev["flag_note"]
    assert _fraud_reasons(cid) == ["cooldown_waiter"]


def test_same_phone_switching_accounts_is_noted(client):
    c = _new_client()
    assert c.post("/api/auth/login", json={"phone": "0532 400 00 08", **CONSENT}).json()["status"] == "success"
    assert c.post("/api/auth/login", json={"phone": "0532 400 00 09", **CONSENT}).json()["status"] == "success"
    first, second = _cid("5324000008"), _cid("5324000009")
    ev = _events(_login(MANAGER), customer_id=second, group="flagged")[0]
    assert ev["kind"] == "device_switch" and ev["related_customer_id"] == first and "birden fazla numarayla" in ev["flag_note"]
    assert _fraud_reasons(second) == ["device_switch"]


# ------------------------------------------------------------------ 3. kasa ve müdür işlemleri
def test_cashier_and_manager_actions_are_in_the_same_list(client):
    _settings(daily=1, cooldown=0)
    _member("0532 400 00 10")
    cid = _cid("5324000010")
    staff = _login(STAFF)
    assert staff.post("/api/staff/stamp", json={"customer_id": cid}).json()["status"] == "success"
    assert staff.post("/api/staff/stamp", json={"customer_id": cid}).json()["status"] == "limit_reached"
    mgr = _login(MANAGER)
    assert mgr.post(f"/api/admin/customers/{cid}/stamp-add").json()["status"] == "success"
    assert mgr.post(f"/api/admin/customers/{cid}/stamp-set", json={"count": 0}).json()["status"] == "success"
    kinds = [e["kind"] for e in _events(mgr, customer_id=cid)]
    assert kinds[:4] == ["admin_set", "admin_stamp", "staff_denied", "staff_stamp"]       # "Ayarla" tek kayıt
    kasa = {e["kind"]: e for e in _events(mgr, customer_id=cid, group="kasa")}
    assert kasa["staff_stamp"]["actor"] == "Kasa 1" and "Kasiyer Kasa 1 +1 damga bastı" in kasa["staff_stamp"]["detail"]
    assert "günlük üst sınır 1" in kasa["staff_denied"]["detail"] and kasa["staff_denied"]["outcome"] == "denied"
    assert "3 → 0" in kasa["admin_set"]["detail"]     # kayıt hediyesi (giriş) + kasiyer + müdür damgası


# ------------------------------------------------------------------ 4. ana ekran analizi + yetki
def test_dashboard_summary_and_manager_only_access(client):
    _settings(daily=5, cooldown=60)
    c = _member("0532 400 00 11")
    assert _scan(c)["status"] == "success"
    assert _scan(c, client={"nav": "reload", "hist": 1})["status"] == "interval_limit"
    mgr = _login(MANAGER)
    s = mgr.get("/api/admin/activity/summary?days=7").json()
    assert s["status"] == "success" and s["days"] == 7 and len(s["series"]) == 7
    t = s["totals"]
    assert t["stamps_qr"] == 1 and t["stamps_gift"] == 1 and t["denied_qr"] == 1 and t["flagged"] == 1 and t["flagged_customers"] == 1
    assert t["success_rate"] == 50 and t["registrations"] == 1
    assert s["reasons"] == [{"kind": "cooldown", "label": "Bekleme süresi dolmadı", "count": 1}]
    assert s["top_flagged"][0]["customer_id"] == _cid("5324000011") and s["recent_flagged"][0]["kind"] == "cooldown"
    assert s["series"][-1]["ok"] == 2 and s["series"][-1]["denied"] == 1 and s["recent"]     # kayıt hediyesi + QR damgası
    assert mgr.get("/api/admin/activity/summary?days=30").json()["days"] == 30
    assert _new_client().get("/api/admin/activity").status_code == 401
    assert _login(STAFF).get("/api/admin/activity").status_code in (401, 403)
    # arama: kullanıcı adı ya da telefonla
    found = _events(mgr, q="532 400 00 11")
    assert found and all(e["customer_id"] == _cid("5324000011") for e in found)


def test_opening_activity_tab_marks_notes_seen(client):
    """Menüdeki uyarı rozeti yalnızca okunmamış notları sayar; İşlem Kayıtları açılınca söner, yeni not gelince yanar."""
    _settings(daily=5, cooldown=60)
    c = _member("0532 400 00 12")
    assert _scan(c)["status"] == "success"
    assert _scan(c, client={"nav": "reload", "hist": 1})["status"] == "interval_limit"
    mgr = _login(MANAGER)
    totals = lambda: mgr.get("/api/admin/activity/summary?days=7").json()["totals"]
    assert totals()["unseen_flagged"] == 1
    assert _new_client().post("/api/admin/activity/seen", json={}).status_code == 401
    assert _login(STAFF).post("/api/admin/activity/seen", json={}).status_code in (401, 403)
    assert totals()["unseen_flagged"] == 1
    assert mgr.post("/api/admin/activity/seen", json={}).json()["status"] == "success"
    t = totals()
    assert t["unseen_flagged"] == 0 and t["flagged"] == 1          # okundu; analiz sayısı aynı kalır
    assert _scan(c, client={"nav": "reload", "hist": 1})["status"] == "interval_limit"
    assert totals()["unseen_flagged"] == 1                          # yeni not → rozet yeniden yanar
    page = mgr.get("/mudur").text
    assert "markActivitySeen" in page and "api/admin/activity/seen" in page


def test_manager_page_and_customer_app_are_wired(client):
    page = _login(MANAGER).get("/mudur").text
    for needle in ('id="tab-activity"', "İşlem Kayıtları", "Olası kötüye kullanım", 'id="card-activity-overview"',
                   "api/admin/activity", 'id="act-modal"'):
        assert needle in page, needle
    pwa = client.get("/k/kart/").text
    assert "CLAIM_CLIENT" in pwa and "getEntriesByType" in pwa


# ------------------------------------------------------------------ 5. geçmişin aktarılması + saklama süresi
def test_backfill_once_and_retention(client):
    from app.services.retention import run_retention
    _member("0532 400 00 12")
    cid = _cid("5324000012")
    now = get_istanbul_now()
    with get_db(immediate=True) as conn:
        seal_id = conn.execute("SELECT id FROM seals ORDER BY id LIMIT 1").fetchone()[0]
        conn.execute("INSERT INTO stamps (customer_id, seal_id, created_at, source, actor) VALUES (?, ?, ?, 'staff', 'kasa1')",
                     (cid, seal_id, (now - timedelta(days=3)).isoformat()))
        conn.execute("INSERT INTO fraud_events (at, customer_id, reason, points) VALUES (?, ?, 'claim_replay', 3)",
                     ((now - timedelta(days=2)).isoformat(), cid))
        conn.execute("DELETE FROM customer_events")
        conn.execute("DELETE FROM settings WHERE key = 'customer_events_backfill_v1'")
        assert activity.backfill_once(conn) >= 3
        assert activity.backfill_once(conn) == 0                                   # yalnızca bir kez
        rows = {r["kind"]: r for r in conn.execute("SELECT * FROM customer_events WHERE customer_id = ?", (cid,))}
        assert "geçmiş kayıt" in rows["staff_stamp"]["detail"] and "kasa1" in rows["staff_stamp"]["detail"]
        assert rows["link_shared"]["flagged"] == 1 and rows["registered"]["outcome"] == "info"
        activity.record(conn, "stamp", "Damga verildi", customer_id=cid, ip="203.0.113.7",
                        at=(now - timedelta(days=4 * 365)).isoformat())
        activity.record(conn, "stamp", "Damga verildi", customer_id=cid, ip="203.0.113.8", device_token="tok",
                        at=(now - timedelta(days=2 * 365)).isoformat())
        run_retention(conn)
        ips = [r["ip"] for r in conn.execute("SELECT ip FROM customer_events WHERE ip LIKE '203.0.113.%'")]
        old_left = conn.execute("SELECT COUNT(*) FROM customer_events WHERE at < ?",
                                ((now - timedelta(days=3 * 365)).isoformat(),)).fetchone()[0]
        scrubbed = conn.execute("SELECT ip, device_hash FROM customer_events WHERE at < ? AND at > ?",
                                ((now - timedelta(days=700)).isoformat(), (now - timedelta(days=800)).isoformat())).fetchone()
    assert ips == [] and old_left == 0 and scrubbed["ip"] == "" and scrubbed["device_hash"] == ""


def test_backfill_failure_never_blocks_startup(client, monkeypatch):
    from app.db import init_db
    with get_db(immediate=True) as conn:
        conn.execute("DELETE FROM settings WHERE key = 'customer_events_backfill_v1'")

    def broken_backfill(conn):
        conn.execute("INSERT INTO customer_events (at, kind) VALUES ('2026-01-01T00:00:00+03:00', 'yarim')")
        raise RuntimeError("bozuk veri")

    monkeypatch.setattr(activity, "backfill_once", broken_backfill)
    init_db()                                                    # uygulama yine açılır
    with get_db() as conn:
        assert conn.execute("SELECT 1 FROM settings WHERE key = 'customer_events_backfill_v1'").fetchone() is None
        assert conn.execute("SELECT COUNT(*) FROM customer_events WHERE kind = 'yarim'").fetchone()[0] == 0   # geri alındı

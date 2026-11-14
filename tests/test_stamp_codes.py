"""
ADR-28 + ADR-29: her damga tek kullanımlık bir koda bağlıdır.
- Kasadaki SABİT QR / NFC (/k/m/qr): her okutuş bir açılıştır; yenilemek, geri dönmek, bağlantıyı başkasına göndermek
  ikinci damga vermez. Sabit adresi yeniden açmak (kopyala-yapıştır) yeni açılıştır ama "aynı ziyarette tek QR damgası"
  beklemesine (min_interval_minutes) ve günlük limite takılır.
- Kasa ekranının canlı kodu (/k/d/{kod}, isteğe bağlı): tek kullanımlık, kısa ömürlü.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from starlette.testclient import TestClient

from app.db import get_db, init_db, set_setting
from app.main import app
from app.services import stamp_codes
from app.utils import get_istanbul_now, parse_to_istanbul

CONSENT = {"kvkk_read": True}
STAFF = {"username": "kasa1", "password": "Test-Kasa-2026"}


def _new_client():
    return TestClient(app, base_url="https://testserver")


def _staff():
    c = _new_client()
    assert c.post("/panel/login", json=STAFF).json()["status"] == "success"
    return c


def _code(**kw):
    with get_db(immediate=True) as conn:
        return stamp_codes.create_stamp_code(conn, created_by="Kasa 1", **kw)


def _stamps_total():
    """QR / kasa damgalarının sayısı. Kayıt hediyesi (source='welcome', ADR-35) ayrıdır: bu testler damga YOLUNU sınar."""
    with get_db() as conn:
        return conn.execute("SELECT COUNT(*) FROM stamps WHERE voided_at IS NULL AND COALESCE(source, '') != 'welcome'").fetchone()[0]


def _member(phone="0532 300 00 01"):
    c = _new_client()
    r = c.post("/api/auth/login", json={"phone": phone, **CONSENT}).json()
    assert r["status"] == "success", r
    return c


def _daily_limit(n):
    with get_db(immediate=True) as conn:
        set_setting(conn, "daily_limit", str(n))


def _cooldown(minutes):
    with get_db(immediate=True) as conn:
        set_setting(conn, "min_interval_minutes", str(minutes))


def _scan(c):
    """Kasadaki sabit QR'ı / NFC'yi okutmak: sayfa yeni bir tek kullanımlık açılış taşır, JS onu ister."""
    page = c.get("/k/m/qr")
    assert page.status_code == 200
    return c.post("/api/claim", json={"claim_token": "/d/" + page.headers["x-claim-token"]}).json()


# ------------------------------------------------------------------ 1. bildirilen açık (sabit QR / NFC)
def test_static_qr_scan_opens_once_and_refresh_or_share_gives_no_second_stamp(client):
    _daily_limit(5)
    _cooldown(60)
    page = client.get("/k/m/qr")
    code = page.headers["x-claim-token"]
    assert page.status_code == 200 and code and "Kayıt Ol ve Damgamı Al" in page.text
    reg = client.post("/api/claim", json={"claim_token": "/d/" + code, "phone": "0532 300 00 02", **CONSENT}).json()
    assert reg["status"] == "success" and reg["current_stamps"] == 2     # kayıt hediyesi + bu okutuşun damgası (ADR-35)
    # sayfayı yenilemek / geri dönmek: aynı açılış ikinci kez damga vermez
    again = client.post("/api/claim", json={"claim_token": "/d/" + code}).json()
    assert again["status"] == "code_used" and again["current_stamps"] == 2
    # ekranı başkasına göndermek: başka telefon da aynı açılışı kullanamaz
    assert _member("0532 300 00 03").post("/api/claim", json={"claim_token": "/d/" + code}).json()["status"] == "code_used"
    # QR'ı yeniden okutmak / sabit adresi kopyalayıp yeniden açmak: yeni açılış ama aynı ziyarette damga yok
    for _ in range(3):
        code2 = client.get("/k/m/qr").headers["x-claim-token"]
        assert code2 != code
        r = client.post("/api/claim", json={"claim_token": "/d/" + code2}).json()
        assert r["status"] == "interval_limit" and r["current_stamps"] == 2 and "en erken" in r["message"]
    # uygulama içi okutucu sabit QR'ı okusa da aynı kural
    inapp = client.post("/api/claim", json={"claim_token": "https://kafe.example.com/k/m/qr"}).json()
    assert inapp["status"] == "interval_limit"
    assert _stamps_total() == 1
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM stamp_codes WHERE origin = 'scan'").fetchone()[0] >= 5


def test_static_qr_next_stamp_needs_cooldown_and_respects_daily_limit(client):
    _daily_limit(2)
    _cooldown(60)
    c = _member()
    assert _scan(c)["status"] == "success"
    assert _scan(c)["status"] == "interval_limit"          # aynı ziyaret: ikinci damga yok
    _cooldown(0)                                            # bekleme kapatılırsa (panel) ikinci ziyaret damgası
    second = _scan(c)
    assert second["status"] == "success" and second["current_stamps"] == 3      # kayıt hediyesi + 2 QR damgası
    assert _scan(c)["status"] == "limit_reached"            # günlük üst sınır (panel) her durumda geçerli
    assert _stamps_total() == 2


def test_cooldown_counts_from_last_non_gift_stamp(client):
    from app.services.loyalty import _qr_cooldown_until
    ist = ZoneInfo("Europe/Istanbul")
    t0 = datetime(2026, 11, 8, 10, 0, tzinfo=ist)
    with get_db(immediate=True) as conn:
        set_setting(conn, "min_interval_minutes", "60")
        conn.execute("INSERT INTO customers (id, phone_e164, display_name, created_at) VALUES (77, '+905320000077', 'x', ?)",
                     (t0.isoformat(),))
        seal_id = conn.execute("SELECT id FROM seals ORDER BY id LIMIT 1").fetchone()[0]
        conn.execute("INSERT INTO stamps (customer_id, seal_id, created_at, source) VALUES (77, ?, ?, 'qr')",
                     (seal_id, t0.isoformat()))
        # tek seferlik hediye damgası (ör. uygulama indirme) beklemeyi yeniden başlatmaz
        conn.execute("INSERT INTO stamps (customer_id, seal_id, created_at, source) VALUES (77, ?, ?, 'bonus_pwa')",
                     (seal_id, (t0 + timedelta(minutes=30)).isoformat()))
        assert _qr_cooldown_until(conn, 77, t0 + timedelta(minutes=59)) == t0 + timedelta(minutes=60)
        assert _qr_cooldown_until(conn, 77, t0 + timedelta(minutes=61)) is None
        set_setting(conn, "min_interval_minutes", "0")
        assert _qr_cooldown_until(conn, 77, t0 + timedelta(minutes=1)) is None


def test_scan_right_after_cashier_stamp_is_the_same_visit(client):
    _daily_limit(5)
    _cooldown(60)
    c = _member("0532 300 00 08")
    staff = _staff()
    cid = staff.post("/api/staff/customer-lookup", json={"q": "532 300 00 08"}).json()["customers"][0]["id"]
    assert staff.post("/api/staff/stamp", json={"customer_id": cid}).json()["status"] == "success"
    assert _scan(c)["status"] == "interval_limit"           # kasiyer +1 verdi; müşteri aynı anda QR'dan ikinci almaz
    assert staff.post("/api/staff/stamp", json={"customer_id": cid}).json()["status"] == "success"   # kasiyer karar verir


def test_inactive_seal_opens_nothing(client):
    page = client.get("/k/m/kasa_pasif")
    assert page.status_code == 200 and "x-claim-token" not in page.headers
    r = _member().post("/api/claim", json={"claim_token": "https://kafe.example.com/k/m/kasa_pasif"}).json()
    assert r["status"] == "error" and "devre dışı" in r["message"]
    assert _stamps_total() == 0


def test_panel_limits_survive_restart_and_cooldown_default_applies_once(client):
    mgr = _new_client()
    assert mgr.post("/panel/login", json={"username": "mudur", "password": "Test-Mudur-2026"}).json()["status"] == "success"
    assert mgr.post("/api/admin/settings", json={"daily_limit": 1, "min_interval_minutes": 15}).json()["status"] == "success"
    init_db()   # yeniden başlatma / dağıtım: panelden girilen değerler geri alınmaz
    s = mgr.get("/api/admin/overview").json()["settings"]
    assert s["daily_limit"] == 1 and s["min_interval_minutes"] == 15
    # eski canlı veritabanı (bekleme 0, bayrak yok): varsayılan 60 dk yalnızca bir kez açılır
    with get_db(immediate=True) as conn:
        conn.execute("DELETE FROM settings WHERE key = 'qr_cooldown_default_v1'")
        set_setting(conn, "min_interval_minutes", "0")
    init_db()
    assert mgr.get("/api/admin/overview").json()["settings"]["min_interval_minutes"] == 60
    assert mgr.post("/api/admin/settings", json={"min_interval_minutes": 0}).json()["status"] == "success"
    init_db()
    assert mgr.get("/api/admin/overview").json()["settings"]["min_interval_minutes"] == 0


# ------------------------------------------------------------------ 2. tek kullanımlık kod
def test_stamp_code_is_single_use_and_copy_paste_gives_no_second_stamp(client):
    _daily_limit(5)
    c = _member()
    info = _code()
    page = c.get("/k/d/" + info["code"])
    assert page.status_code == 200 and page.headers["x-claim-token"] == info["code"]
    with get_db() as conn:   # sayfayı açmak (GET) kodu tüketmez / ayırmaz: bağlantı önizleyicileri kodu yakamaz
        row = stamp_codes.find_code(conn, info["code"])
        assert row["used_at"] is None and row["held_until"] is None
    first = c.post("/api/claim", json={"claim_token": "/d/" + info["code"]}).json()
    assert first["status"] == "success" and first["current_stamps"] == 2     # kayıt hediyesi (giriş) + damga
    for url in (info["url"], "/d/" + info["code"], info["code"]):   # kopyala-yapıştır / yeniden okutma
        again = c.post("/api/claim", json={"claim_token": url}).json()
        assert again["status"] == "code_used" and again["current_stamps"] == 2
    other = _member("0532 300 00 03")
    stolen = other.post("/api/claim", json={"claim_token": info["url"]}).json()
    assert stolen["status"] == "code_used" and stolen["current_stamps"] == 1      # yalnızca kendi kayıt hediyesi
    second = c.post("/api/claim", json={"claim_token": _code()["url"]}).json()   # kasadaki YENİ kod → yeni damga
    assert second["status"] == "success" and second["current_stamps"] == 3
    assert _stamps_total() == 2


def test_replay_fraud_points(client):
    _daily_limit(5)
    c = _member()
    info = _code()
    assert c.post("/api/claim", json={"claim_token": info["url"]}).json()["status"] == "success"
    c.post("/api/claim", json={"claim_token": info["url"]})                  # hemen çift okutma: puan yok
    other = _member("0532 300 00 07")
    other.post("/api/claim", json={"claim_token": info["url"]})              # başkasının kodu: claim_replay (3)
    with get_db(immediate=True) as conn:
        # 5 dakikadan sonra kendi kodunu yeniden açmak (kopyala-yapıştır): hafif puan
        old = (get_istanbul_now() - timedelta(minutes=10)).isoformat()
        conn.execute("UPDATE stamp_codes SET used_at = ? WHERE id = ?", (old, info["id"]))
    c.post("/api/claim", json={"claim_token": info["url"]})
    with get_db() as conn:
        rows = [(r["reason"], r["points"]) for r in conn.execute("SELECT reason, points FROM fraud_events ORDER BY id")]
    assert rows == [("claim_replay", 3), ("stamp_code_reused", 1)]


# ------------------------------------------------------------------ 3. süresi dolan / uydurma kod
def test_expired_and_forged_codes_give_no_stamp(client):
    c = _member()
    info = _code(now=get_istanbul_now() - timedelta(seconds=stamp_codes.CODE_TTL_SECONDS + 5))
    r = c.post("/api/claim", json={"claim_token": info["url"]}).json()
    assert r["status"] == "code_expired" and r["current_stamps"] == 1      # yalnızca kayıt hediyesi (giriş)
    assert c.post("/api/claim", json={"claim_token": "https://testserver/k/d/UYDURMAKOD1234567890"}).json()["status"] == "code_invalid"
    assert c.post("/api/claim", json={"claim_token": "/d/<script>"}).json()["status"] == "code_invalid"
    assert _stamps_total() == 0
    with get_db() as conn:
        reasons = [r["reason"] for r in conn.execute("SELECT reason FROM fraud_events ORDER BY id")]
    assert reasons == ["stamp_code_expired", "qr_invalid", "qr_invalid"]


# ------------------------------------------------------------------ 4. kayıtlı olmayan müşteri
def test_unregistered_customer_holds_code_while_registering(client):
    info = _code()
    first = client.post("/api/claim", json={"claim_token": info["url"]}).json()
    assert first["status"] == "need_auth" and "_hold_secret" not in first
    assert stamp_codes.HOLD_COOKIE in client.cookies
    thief = _member("0532 300 00 04")
    assert thief.post("/api/claim", json={"claim_token": info["url"]}).json()["status"] == "code_held"
    stranger = _new_client().post("/api/claim", json={"claim_token": info["url"], "phone": "0532 300 00 05", **CONSENT}).json()
    assert stranger["status"] == "code_held"
    # kodun 120 sn'si geçse bile koda ayrılan telefon kayıt formunu bitirebilir (10 dk)
    with get_db(immediate=True) as conn:
        past = (get_istanbul_now() - timedelta(seconds=1)).isoformat()
        conn.execute("UPDATE stamp_codes SET expires_at = ? WHERE id = ?", (past, info["id"]))
    reg = client.post("/api/claim", json={"claim_token": info["url"], "phone": "0532 300 00 06", **CONSENT}).json()
    assert reg["status"] == "success" and reg["current_stamps"] == 2 and reg["display_name"].startswith("user")
    assert client.post("/api/claim", json={"claim_token": info["url"]}).json()["status"] == "code_used"
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM customers WHERE phone_e164 = '+905323000005'").fetchone()[0] == 0
        assert [r[0] for r in conn.execute(
            "SELECT source FROM stamps WHERE customer_id = (SELECT id FROM customers WHERE phone_e164 = '+905323000006') ORDER BY id"
        )] == ["welcome", "qr"]


# ------------------------------------------------------------------ 5. kasa ekranı
def test_staff_screen_creates_polls_and_rotates_codes(client):
    anon = _new_client()
    assert anon.post("/api/staff/stamp-code", json={}).status_code == 401
    assert anon.get("/api/staff/stamp-code/status?ids=1").status_code == 401
    staff = _staff()
    a = staff.post("/api/staff/stamp-code", json={}).json()
    from app.config import PUBLIC_BASE_URL, PUBLIC_BASE_PATH
    assert a["status"] == "success" and a["qr"].startswith("data:image/png;base64,") and f"{PUBLIC_BASE_URL}{PUBLIC_BASE_PATH}/d/" in a["url"]
    assert a["rotate_after"] < a["ttl"]
    st = staff.get(f"/api/staff/stamp-code/status?ids={a['id']}").json()["codes"][0]
    assert st["state"] == "waiting"
    c = _member()
    assert c.post("/api/claim", json={"claim_token": a["url"]}).json()["status"] == "success"
    st = staff.get(f"/api/staff/stamp-code/status?ids={a['id']}").json()["codes"][0]
    assert st["state"] == "used" and st["result"] == "stamped" and st["customer"].startswith("user")
    # QR damgası kodu gösteren kasiyerin işlemi sayılır: "Bugün Basılan" + Son İşlemlerim + rapor (işlemi yapan)
    logs = staff.get("/api/staff/logs").json()
    assert logs["today_stamps"] == 1 and "Damga QR" in logs["logs"][0]["detail"]
    with get_db() as conn:
        assert tuple(conn.execute("SELECT actor, source FROM stamps WHERE source = 'qr'").fetchone()) == ("kasa1", "qr")
    # yeni kod istenince önceki (okutulmamış) kod RETIRE_GRACE_SECONDS içinde biter
    b = staff.post("/api/staff/stamp-code", json={}).json()
    nxt = staff.post("/api/staff/stamp-code", json={"replaces": b["id"]}).json()
    with get_db() as conn:
        row = conn.execute("SELECT expires_at FROM stamp_codes WHERE id = ?", (b["id"],)).fetchone()
    left = (parse_to_istanbul(row["expires_at"]) - get_istanbul_now()).total_seconds()
    assert left <= stamp_codes.RETIRE_GRACE_SECONDS + 1 and nxt["id"] != b["id"]
    # kayıt olan müşteriye ayrılan kod kasada "held" görünür
    held = staff.post("/api/staff/stamp-code", json={}).json()
    _new_client().post("/api/claim", json={"claim_token": held["url"]})
    assert staff.get(f"/api/staff/stamp-code/status?ids={held['id']}").json()["codes"][0]["state"] == "held"


def test_daily_limit_consumes_code_without_stamp(client):
    _daily_limit(1)
    c = _member()
    assert c.post("/api/claim", json={"claim_token": _code()["url"]}).json()["status"] == "success"
    info = _code()
    r = c.post("/api/claim", json={"claim_token": info["url"]}).json()
    assert r["status"] == "limit_reached" and r["current_stamps"] == 2     # kayıt hediyesi + 1 damga
    with get_db() as conn:
        st = stamp_codes.code_status_for_staff(conn, info["id"])
    assert st["state"] == "used" and st["result"] == "limit"


# ------------------------------------------------------------------ 6. saklama + sayfalar
def test_old_codes_are_purged_by_retention(client):
    from app.services.retention import run_retention
    _code(now=get_istanbul_now() - timedelta(days=31))
    _code()
    with get_db(immediate=True) as conn:
        res = run_retention(conn)
        left = conn.execute("SELECT COUNT(*) FROM stamp_codes").fetchone()[0]
    assert res["stamp_codes_deleted"] == 1 and left == 1


def test_pages_wire_the_new_flow(client):
    html = client.get("/k/kart/").text
    assert '"/d/" + appState.pendingClaimToken' in html           # açılış/kod sayfası açılınca damga istenir
    assert "Kayıt Ol ve İlk Damgamı Al" not in html and "Kasadaki bu QR kayıt içindir" not in html
    scan_page = client.get("/k/m/qr")
    assert 'const ENTRY = "scan"' in scan_page.text and scan_page.headers["x-claim-token"] in scan_page.text
    mgr = _new_client()
    mgr.post("/panel/login", json={"username": "mudur", "password": "Test-Mudur-2026"})
    mudur = mgr.get("/mudur").text
    assert 'id="setting-min-interval"' in mudur and "min_interval_minutes" in mudur
    m = client.get("/manifest.webmanifest").json()
    assert m["related_applications"][0]["platform"] == "webapp" and m["prefer_related_applications"] is False
    staff_page = _staff().get("/personel").text
    assert "api/staff/stamp-code" in staff_page and 'id="qr-modal-stamp"' in staff_page and "Damga QR'ı Göster" in staff_page
    assert "kind=damga" in staff_page and "kind=kayit" in staff_page and "toggleQrFullscreen" in staff_page

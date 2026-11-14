"""
ADR-30: her okutuşta imzası değişen damga — NTAG 424 DNA (SUN) NFC etiketi ve Kasa QR Ekranı.
- İmza doğrulaması NXP AN12196 test vektörleriyle.
- Aynı dokunuş adresi ikinci kez damga vermez (yenileme, kopyala-yapıştır, evden açma); sayacı geride kalan dokunuş ve
  sahte imza reddedilir. GET dokunuşu yakmaz.
- Basılı QR'ın damgası kapatılınca sabit QR yalnızca kayıt içindir; NFC ve QR ekranı damga vermeye devam eder.
- QR ekranı tek kullanımlık bağlantıyla eşleşir, yalnızca kendi kodlarını üretir/yoklar, müşteri adı göstermez.
"""
from starlette.testclient import TestClient

from app.db import get_db, set_setting
from app.main import app
from app.services import nfc_sun, stamp_codes

CONSENT = {"kvkk_read": True}
MANAGER = {"username": "mudur", "password": "Test-Mudur-2026"}
STAFF = {"username": "kasa1", "password": "Test-Kasa-2026"}
META = bytes.fromhex("00112233445566778899AABBCCDDEEFF")
FILE = bytes.fromhex("F0E1D2C3B4A5968778695A4B3C2D1E0F")
UID = bytes.fromhex("04DE5F1EACC040")
ZERO = bytes(16)


def _client():
    return TestClient(app, base_url="https://testserver")


def _keys(meta=META, file=FILE):
    with get_db(immediate=True) as conn:
        set_setting(conn, "nfc_sdm_meta_key", meta.hex().upper())
        set_setting(conn, "nfc_sdm_file_key", file.hex().upper())


def _limits(daily=5, cooldown=0):
    with get_db(immediate=True) as conn:
        set_setting(conn, "daily_limit", str(daily))
        set_setting(conn, "min_interval_minutes", str(cooldown))


def _static(on):
    with get_db(immediate=True) as conn:
        set_setting(conn, "static_qr_stamps", "1" if on else "0")


def _tap(counter, uid=UID, meta=META, file=FILE):
    """Etiketin bir dokunuşta adrese eklediği sorgu: p=<şifreli UID+sayaç>&c=<CMAC>."""
    return nfc_sun.build_query(meta, file, uid, counter)


def _member(phone="0532 400 00 01"):
    c = _client()
    assert c.post("/api/auth/login", json={"phone": phone, **CONSENT}).json()["status"] == "success"
    return c


def _manager():
    m = _client()
    assert m.post("/panel/login", json=MANAGER).json()["status"] == "success"
    return m


def _open_tap(c, query):
    """Telefon etikete dokunur: tarayıcı /k/n?… açar, sayfa imzalı adresi gömer, JS /api/claim ile ister."""
    page = c.get("/k/n?" + query)
    assert page.status_code == 200
    token = page.headers.get("x-claim-token")
    assert token == "/n?" + query
    return c.post("/api/claim", json={"claim_token": token}).json()


def _stamps():
    """Damga YOLUYLA verilen damgalar. Kayıt hediyesi (source='welcome', ADR-35) ayrıdır: kayıt (giriş) anında verilir."""
    with get_db() as conn:
        return conn.execute("SELECT COUNT(*) FROM stamps WHERE voided_at IS NULL AND COALESCE(source, '') != 'welcome'").fetchone()[0]


def _count(sql, *args):
    with get_db() as conn:
        return conn.execute(sql, args).fetchone()[0]


# ------------------------------------------------------------------ 1. imza (NXP AN12196)
def test_an12196_vector_decrypts_and_verifies():
    msg = nfc_sun.parse_query("picc_data=EF963FF7828658A599F3041510671E88&cmac=94EED9EE65337086")
    res = nfc_sun.verify(msg, ZERO, ZERO)
    assert res.ok and res.uid == "04DE5F1EACC040" and res.counter == 61


def test_an12196_vector_with_url_text_in_mac_input():
    # SDMMACInputOffset şifreli dosya verisinin başında: MAC girdisi "…&cmac=" metnidir
    msg = nfc_sun.parse_query(
        "picc_data=FD91EC264309878BE6345CBE53BADF40&enc=CEE9A53E3E463EF1F459635736738962&cmac=ECC1E7F6C6C73BF6"
    )
    res = nfc_sun.verify(msg, ZERO, ZERO)
    assert res.ok and res.uid == "04958CAA5C5E80" and res.counter == 8


def test_tampered_wrong_key_and_malformed_messages_fail():
    good = "EF963FF7828658A599F3041510671E88"
    assert not nfc_sun.verify(nfc_sun.parse_query(f"p={good}&c=94EED9EE65337087"), ZERO, ZERO).ok
    wrong = nfc_sun.verify(nfc_sun.parse_query(f"p={good}&c=94EED9EE65337086"), META, META)
    assert not wrong.ok and wrong.reason in ("bad_meta_key", "bad_mac")
    assert nfc_sun.parse_query("p=XYZ&c=94EED9EE65337086") is None
    assert nfc_sun.parse_claim_token("https://kafe.example.com/k/m/qr") is None
    assert nfc_sun.claim_token_for_page("p=" + good + "&c=94EED9EE65337086%0A<x>") == ""


def test_plain_mode_uid_and_counter_and_offsets():
    mac = nfc_sun.compute_sdmmac(FILE, UID, 300).hex().upper()
    res = nfc_sun.verify(nfc_sun.parse_query(f"uid={UID.hex().upper()}&ctr={300:06X}&c={mac}"), META, FILE)
    assert res.ok and res.counter == 300
    assert nfc_sun.tag_url_template().endswith("/n?p=" + "0" * 32 + "&c=" + "0" * 16)
    # AN12196 örnek adresi: PICCDataOffset 0x20, SDMMACOffset 0x43
    sample = nfc_sun.sdm_offsets("https://choose.url.com/ntag424?p=" + "0" * 32 + "&c=" + "0" * 16)
    assert sample["picc_offset"] == 0x20 and sample["mac_offset"] == 0x43 == sample["mac_input_offset"]


# ------------------------------------------------------------------ 2. NFC damga akışı
def test_each_tap_stamps_once_and_copied_link_never_stamps_again():
    _keys()
    _limits(daily=5)
    c = _member()
    q1 = _tap(10)
    first = _open_tap(c, q1)
    assert first["status"] == "success" and first["current_stamps"] == 2     # kayıt hediyesi (giriş) + dokunuş damgası
    # yenileme / geri dönme / kopyala-yapıştır / evden açma: aynı dokunuş ikinci kez damga vermez
    again = c.post("/api/claim", json={"claim_token": "/n?" + q1}).json()
    assert again["status"] == "code_used" and "dokunuşla işlemin zaten yapıldı" in again["message"]
    assert _member("0532 400 00 02").post("/api/claim", json={"claim_token": "/n?" + q1}).json()["status"] == "code_used"
    # yeni dokunuş (sayaç arttı) → yeni damga
    assert _open_tap(c, _tap(11))["status"] == "success"
    assert _stamps() == 2
    with get_db() as conn:
        tag = conn.execute("SELECT * FROM nfc_tags").fetchone()
        assert tag["uid"] == UID.hex().upper() and tag["last_counter"] == 11 and tag["taps"] == 2
    assert _count("SELECT COUNT(*) FROM stamp_codes WHERE origin = 'nfc'") == 2


def test_older_tap_after_newer_one_is_rejected():
    """Açılmadan saklanan (önceden toplanmış) dokunuş adresi, etikete sonradan dokunulunca geçersizleşir."""
    _keys()
    _limits(daily=5)
    c = _member()
    harvested = _tap(20)
    assert _open_tap(c, _tap(21))["status"] == "success"
    late = c.post("/api/claim", json={"claim_token": "/n?" + harvested}).json()
    assert late["status"] == "code_expired" and "artık geçerli değil" in late["message"]
    assert _stamps() == 1
    assert _count("SELECT COUNT(*) FROM fraud_events WHERE reason = 'nfc_stale'") == 1


def test_forged_signature_and_foreign_keys_are_rejected_and_flagged():
    _keys()
    _limits(daily=5)
    c = _member()
    picc = nfc_sun.encrypt_picc(META, UID, 30).hex().upper()
    assert c.post("/api/claim", json={"claim_token": f"/n?p={picc}&c=0011223344556677"}).json()["status"] == "nfc_invalid"
    # başka anahtarla (ör. fabrika anahtarı) programlanmış etiket de reddedilir
    assert c.post("/api/claim", json={"claim_token": "/n?" + _tap(31, meta=ZERO, file=ZERO)}).json()["status"] == "nfc_invalid"
    assert _stamps() == 0
    assert _count("SELECT COUNT(*) FROM fraud_events WHERE reason = 'nfc_invalid'") == 2
    assert _count("SELECT COUNT(*) FROM nfc_tags") == 0


def test_cooldown_and_daily_limit_still_apply_to_nfc():
    _keys()
    _limits(daily=2, cooldown=60)
    c = _member()
    assert _open_tap(c, _tap(40))["status"] == "success"
    assert _open_tap(c, _tap(41))["status"] == "interval_limit"     # aynı ziyarette ikinci dokunuş
    _limits(daily=1, cooldown=0)
    assert _open_tap(c, _tap(42))["status"] == "limit_reached"
    assert _stamps() == 1


def test_new_customer_registers_and_gets_stamp_from_one_tap():
    _keys()
    _limits(daily=5)
    c = _client()
    q = _tap(50)
    assert _open_tap(c, q)["status"] == "need_auth"                 # dokunuş bu tarayıcıya ayrıldı
    other = _member("0532 400 00 09").post("/api/claim", json={"claim_token": "/n?" + q}).json()
    assert other["status"] == "code_held"                          # başka telefon kullanamaz
    reg = c.post("/api/claim", json={"claim_token": "/n?" + q, "phone": "0532 400 00 03", **CONSENT}).json()
    assert reg["status"] == "success" and reg["current_stamps"] == 2      # kayıt hediyesi + dokunuşun damgası (ADR-35)
    assert reg["welcome_gift"] is True


def test_missing_keys_and_disabled_tag_give_no_stamp():
    _limits(daily=5)
    c = _member()
    assert _open_tap(c, _tap(60))["status"] == "nfc_unavailable"   # anahtar girilmemiş
    _keys()
    assert _open_tap(c, _tap(61))["status"] == "success"
    with get_db(immediate=True) as conn:
        tag_id = conn.execute("SELECT id FROM nfc_tags").fetchone()[0]
        assert nfc_sun.update_tag(conn, tag_id, active=False)
    assert _open_tap(c, _tap(62))["status"] == "nfc_unavailable"
    assert _stamps() == 1


def test_get_does_not_consume_tap_and_sun_on_static_address_is_nfc():
    _keys()
    _limits(daily=5)
    c = _member()
    q = _tap(80)
    for _ in range(3):                                             # bağlantı önizleyicisi / çift GET
        assert c.get("/k/n?" + q).status_code == 200
    assert _count("SELECT COUNT(*) FROM nfc_tags") == 0
    assert c.post("/api/claim", json={"claim_token": "/n?" + q}).json()["status"] == "success"
    page = c.get("/k/m/qr?" + _tap(81))                            # etikete kasa QR adresi + SUN yazılmışsa
    assert page.headers["x-claim-token"].startswith("/n?p=")


# ------------------------------------------------------------------ 3. basılı QR yalnızca kayıt (güvenli mod)
def test_static_qr_is_register_only_when_disabled_but_nfc_still_stamps():
    _keys()
    _limits(daily=5)
    _static(False)
    c = _member()
    page = c.get("/k/m/qr")
    assert page.status_code == 200 and not page.headers.get("x-claim-token")
    inapp = c.post("/api/claim", json={"claim_token": "https://kafe.example.com/k/m/qr"}).json()
    assert inapp["status"] == "registration_qr" and inapp["current_stamps"] == 1      # yalnızca kayıt hediyesi (giriş); damga yok
    assert _open_tap(c, _tap(90))["status"] == "success"
    assert _stamps() == 1


def test_static_opening_from_before_the_switch_gives_no_stamp():
    _limits(daily=5)
    c = _member()
    code = c.get("/k/m/qr").headers["x-claim-token"]
    _static(False)
    assert c.post("/api/claim", json={"claim_token": "/d/" + code}).json()["status"] == "registration_qr"
    assert _stamps() == 0


def test_static_qr_disabled_new_visitor_registers_without_stamp():
    _static(False)
    c = _client()
    r = c.post("/api/claim", json={"claim_token": "/m/qr", "phone": "0532 400 00 04", **CONSENT}).json()
    # basılı QR damga vermez ama yeni kayıt hediyesini alır (ADR-35): 1 damga = hediye
    assert r["status"] == "registered" and r["current_stamps"] == 1 and r["welcome_gift"] is True
    assert _stamps() == 0


# ------------------------------------------------------------------ 4. Kasa QR Ekranı
def _paired_display(m=None):
    m = m or _manager()
    created = m.post("/api/admin/qr-displays", json={"label": "Kasa"}).json()
    assert created["status"] == "success" and created["pairing"]["qr"].startswith("data:image/png")
    code = created["pairing"]["url"].rsplit("/", 1)[1]
    tablet = _client()
    page = tablet.get("/k/ekran/eslestir/" + code)
    assert page.status_code == 200 and "Bu Cihazı QR Ekranı Yap" in page.text
    assert tablet.post("/api/ekran/pair", json={"code": code}).json()["status"] == "success"
    return m, tablet, created["pairing"]["id"]


def test_display_shows_single_use_codes_and_hides_customer_name():
    _limits(daily=5)
    _static(False)                                                 # basılı QR kapalıyken ekran damga verir
    _m, tablet, display_id = _paired_display()
    assert "Damganı almak için QR'ı okut" in tablet.get("/k/ekran").text
    first = tablet.post("/api/ekran/stamp-code", json={}).json()
    assert first["status"] == "success" and "/d/" in first["url"] and first["qr"].startswith("data:image/png")
    with get_db() as conn:
        row = conn.execute("SELECT origin, display_id FROM stamp_codes WHERE id = ?", (first["id"],)).fetchone()
    assert row["origin"] == "display" and row["display_id"] == display_id
    code = first["url"].rsplit("/d/", 1)[1]
    c = _member()
    assert c.post("/api/claim", json={"claim_token": "/d/" + code}).json()["status"] == "success"
    st = tablet.get(f"/api/ekran/stamp-code/status?ids={first['id']}").json()["codes"][0]
    assert st["state"] == "used" and st["result"] == "stamped" and "customer" not in st
    assert c.post("/api/claim", json={"claim_token": "/d/" + code}).json()["status"] == "code_used"
    assert _stamps() == 1


def test_display_api_needs_paired_cookie_and_only_sees_own_codes():
    m, tablet, display_id = _paired_display()
    stranger = _client()
    assert stranger.post("/api/ekran/stamp-code", json={}).status_code == 401
    assert "Bu cihaz QR ekranı değil" in stranger.get("/k/ekran").text
    own = tablet.post("/api/ekran/stamp-code", json={}).json()["id"]
    with get_db(immediate=True) as conn:
        cashier_code = stamp_codes.create_stamp_code(conn, created_by="Kasa 1")["id"]
    seen = tablet.get(f"/api/ekran/stamp-code/status?ids={own},{cashier_code}").json()["codes"]
    assert [x["id"] for x in seen] == [own]
    assert m.post(f"/api/admin/qr-displays/{display_id}/revoke").json()["status"] == "success"
    assert tablet.post("/api/ekran/stamp-code", json={}).status_code == 401


def test_pairing_link_is_single_use_and_repair_moves_the_screen():
    m = _manager()
    created = m.post("/api/admin/qr-displays", json={"label": "Kasa"}).json()
    code = created["pairing"]["url"].rsplit("/", 1)[1]
    t1, t2 = _client(), _client()
    assert t1.post("/api/ekran/pair", json={"code": code}).json()["status"] == "success"
    assert t2.post("/api/ekran/pair", json={"code": code}).json()["status"] == "error"
    assert "geçersiz" in t2.get("/k/ekran/eslestir/" + code).text
    repaired = m.post(f"/api/admin/qr-displays/{created['pairing']['id']}/pair").json()
    new_code = repaired["pairing"]["url"].rsplit("/", 1)[1]
    assert t1.post("/api/ekran/stamp-code", json={}).json()["status"] == "success"   # yeni cihaz eşleşene kadar çalışır
    assert t2.post("/api/ekran/pair", json={"code": new_code}).json()["status"] == "success"
    assert t1.post("/api/ekran/stamp-code", json={}).status_code == 401
    assert t2.post("/api/ekran/stamp-code", json={}).json()["status"] == "success"


# ------------------------------------------------------------------ 5. müdür paneli
def test_manager_stamp_security_settings_and_staff_cannot_access():
    m = _manager()
    s = m.get("/api/admin/stamp-security").json()
    assert s["static_qr_stamps"] is True and s["nfc"]["configured"] is False and s["displays"] == []
    assert m.post("/api/admin/stamp-security", json={"nfc_meta_key": "1234"}).json()["status"] == "error"
    ok = m.post("/api/admin/stamp-security",
                json={"nfc_meta_key": META.hex(), "nfc_file_key": "", "static_qr_stamps": False}).json()
    assert ok["status"] == "success" and ok["nfc"]["configured"] and ok["static_qr_stamps"] is False
    assert ok["nfc"]["meta_key"] == META.hex().upper() and ok["nfc"]["zero_key"] is False
    _limits(daily=5)
    assert _open_tap(_member(), _tap(100, file=META))["status"] == "success"   # File anahtarı boş → Meta kullanılır
    after = m.get("/api/admin/stamp-security").json()["nfc"]
    assert len(after["tags"]) == 1 and after["tags"][0]["taps"] == 1 and after["last_event"]["result"] == "ok"
    zero = m.post("/api/admin/stamp-security", json={"nfc_meta_key": "00" * 16}).json()
    assert zero["nfc"]["zero_key"] is True
    staff = _client()
    assert staff.post("/panel/login", json=STAFF).json()["status"] == "success"
    assert staff.get("/api/admin/stamp-security").status_code == 401
    assert staff.post("/api/admin/qr-displays", json={"label": "x"}).status_code == 401

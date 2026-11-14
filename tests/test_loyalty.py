import pytest
from datetime import datetime, timedelta
from app.utils import normalize_phone, get_istanbul_now
from app.db import get_db, set_setting
from app.services.loyalty import DEVICE_COOKIE_NAME
from app.services.stamp_codes import create_stamp_code


def _stamp_code():
    """Kasa ekranının ürettiği tek kullanımlık Damga QR kodu (ADR-28): damga yalnızca bununla verilir."""
    with get_db(immediate=True) as conn:
        return create_stamp_code(conn, created_by="test")["code"]


CONSENT = {"kvkk_read": True, "explicit_consent": True}


def test_phone_normalization():
    """Türkiye cep telefonu normalizasyonu testi."""
    assert normalize_phone("0532 123 45 67") == "+905321234567"
    assert normalize_phone("532 123 45 67") == "+905321234567"
    assert normalize_phone("+90 532 123 45 67") == "+905321234567"
    assert normalize_phone("905321234567") == "+905321234567"
    assert normalize_phone("0532-123-45-67") == "+905321234567"
    assert normalize_phone("(0532) 123 45 67") == "+905321234567"
    assert normalize_phone("05051234567") == "+905051234567"

    with pytest.raises(ValueError):
        normalize_phone("0212 123 45 67")

    with pytest.raises(ValueError):
        normalize_phone("12345")


def test_static_seal_page_opens_single_use_scan_code(client):
    """Kasadaki sabit Damga QR'ı: 302 yok, 200 OK; her açılış tek kullanımlık bir kod taşır (ADR-29). Testte gizli kod
    "qr"dır (conftest); eski basılı mühür adlarının damga açmaması ADR-41 testlerinde."""
    from app.services.stamp_codes import find_code
    res = client.get("/m/qr", follow_redirects=False)
    assert res.status_code == 200
    code = res.headers["x-claim-token"]
    assert client.get("/m/qr").headers["x-claim-token"] != code     # her okutuş yeni bir açılış
    with get_db() as conn:
        row = find_code(conn, code)
        assert row["origin"] == "scan" and row["used_at"] is None


def test_stamp_code_single_use_and_daily_limit_two(client):
    """Damga kodu tek kullanımlıktır (aynı link tekrar damga vermez); günlük maksimum 2 damga limiti uygulanır."""
    with get_db(immediate=True) as conn:
        set_setting(conn, "daily_limit", "2")

    code1 = _stamp_code()
    first = client.post("/api/claim", json={"claim_token": code1, **CONSENT, "phone": "0533 111 22 33"}).json()
    # ADR-35: yeni kayıt = kayıt hediyesi +1 (günlük limite sayılmaz) + bu okutuşun damgası +1
    assert first["status"] == "success" and first["current_stamps"] == 2 and first["display_name"] == "user1"
    assert first["welcome_gift"] is True

    # Aynı link (kopyala-yapıştır / tekrar okutma) ikinci damgayı VERMEZ
    again = client.post("/api/claim", json={"claim_token": code1, **CONSENT, "phone": "0533 111 22 33"}).json()
    assert again["status"] == "code_used" and again["current_stamps"] == 2

    second = client.post("/api/claim", json={"claim_token": _stamp_code()}).json()
    assert second["status"] == "success" and second["current_stamps"] == 3

    # Aynı gün 3. kez (yeni kodla bile) günlük limit=2 devreye girer
    third = client.post("/api/claim", json={"claim_token": _stamp_code()}).json()
    assert third["status"] == "limit_reached"


def test_expired_stamp_code_gives_no_stamp(client):
    """Süresi dolan damga kodu damga vermez ve kayıt da açmaz (kasadaki güncel kod okutulmalı)."""
    code = _stamp_code()
    expired_time = (get_istanbul_now() - timedelta(minutes=30)).isoformat()
    with get_db(immediate=True) as conn:
        conn.execute("UPDATE stamp_codes SET expires_at = ?", (expired_time,))

    res = client.post("/api/claim", json={"claim_token": code, **CONSENT, "phone": "0535 222 33 44"}).json()
    assert res["status"] == "code_expired"
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM stamps").fetchone()["c"] == 0
        assert conn.execute("SELECT COUNT(*) AS c FROM customers").fetchone()["c"] == 0


def test_daily_limit_from_settings(client):
    """
    daily_limit koda gömülü olmasın, settings tablosundan okunsun (varsayılan 2, test için 1'den 2'ye değiştirilir).
    Ayar değiştiğinde anında yürürlüğe girer.
    """
    with get_db(immediate=True) as conn:
        set_setting(conn, "daily_limit", "1")

    phone = "0544 333 44 55"

    # 1. Damga: Başarılı
    claim1 = _stamp_code()
    res_claim1 = client.post("/api/claim", json={"claim_token": claim1, **CONSENT, "phone": phone})
    assert res_claim1.json()["status"] == "success"

    # 2. Damga (Aynı gün, yeni claim ile deneme): Varsayılan limit 1 olduğu için reddedilir
    claim2 = _stamp_code()
    res_claim2 = client.post("/api/claim", json={"claim_token": claim2, **CONSENT, "phone": phone})
    assert res_claim2.json()["status"] == "limit_reached"

    # Ayarı settings tablosundan dinamik olarak 2 yap
    with get_db(immediate=True) as conn:
        set_setting(conn, "daily_limit", "2")

    # 3. Damga (Aynı gün, yeni claim ile deneme): Artık izin verilmeli
    claim3 = _stamp_code()
    res_claim3 = client.post("/api/claim", json={"claim_token": claim3, **CONSENT, "phone": phone})
    assert res_claim3.json()["status"] == "success"
    assert res_claim3.json()["current_stamps"] == 3     # kayıt hediyesi + 2 QR damgası (hediye limite sayılmaz)


def test_httponly_device_cookie_flow(client):
    """
    Cihaz anahtarı tek yerde, tek mekanizmayla yönetilsin (HttpOnly cookie).
    İlk girişte cookie atanır, sonraki ziyaretlerde tarayıcı çerezi otomatik kullanır.
    """
    c1 = _stamp_code()

    # İlk claim: Telefon numarası verilir
    post_res = client.post("/api/claim", json={"claim_token": c1, **CONSENT, "phone": "0532 555 66 77"})
    assert post_res.status_code == 200
    assert post_res.json()["status"] == "success"

    # HttpOnly cookie'nin set edildiğini doğrula
    assert DEVICE_COOKIE_NAME in client.cookies
    cookie_header = post_res.headers.get("set-cookie", "")
    assert "HttpOnly" in cookie_header
    assert "SameSite=lax" in cookie_header or "samesite=lax" in cookie_header.lower()

    # /api/my-card çağrısı: Parametresiz, telefon numarasız, yalnızca HttpOnly çerez ile kartı getirmeli
    card_res = client.get("/api/my-card")
    assert card_res.status_code == 200
    card_data = card_res.json()
    assert card_data["status"] == "success"
    assert card_data["current_stamps"] == 2     # kayıt hediyesi + bu okutuşun damgası (ADR-35)
    assert card_data["stamp_target"] == 5

    # İkinci gün/damga: Telefon numarası göndermeden, sadece çerez ile damga alınabilmeli
    with get_db(immediate=True) as conn:
        set_setting(conn, "daily_limit", "5")

    c2 = _stamp_code()
    claim_no_phone = client.post("/api/claim", json={"claim_token": c2})
    assert claim_no_phone.json()["status"] == "success"
    assert claim_no_phone.json()["current_stamps"] == 3


def test_stamp_target_and_reward_issuance(client):
    """Hedefe ulaşınca (varsayılan 5) aynı transaction'da kart sıfırlanıp ödül bileti oluşur."""
    with get_db(immediate=True) as conn:
        set_setting(conn, "daily_limit", "10")

    phone = "0542 777 88 99"

    # İlk okutuş: kayıt hediyesi + damga = 2 (ADR-35); sonra 3, 4
    for i in range(2, 5):
        res = client.post("/api/claim", json={"claim_token": _stamp_code(), **CONSENT, "phone": phone})
        data = res.json()
        assert data["status"] == "success"
        assert data["current_stamps"] == i
        assert data["reward_unlocked"] is False

    # 5. Damga (Hedefe ulaşıldı!)
    c5 = _stamp_code()
    res5 = client.post("/api/claim", json={"claim_token": c5, **CONSENT, "phone": phone})
    data5 = res5.json()

    assert data5["status"] == "success"
    assert data5["reward_unlocked"] is True
    assert data5["current_stamps"] == 0
    assert data5["usable_rewards_count"] == 1


def test_staff_reward_redemption(client):
    """Ödül bileti personel kartı ile tek seferlik kullanılır ve teslim edilir."""
    with get_db(immediate=True) as conn:
        set_setting(conn, "daily_limit", "10")

    phone = "0551 888 99 00"
    for _ in range(4):   # kayıt hediyesi (ADR-35) + 4 QR damgası = 5 → ödül bileti
        client.post("/api/claim", json={"claim_token": _stamp_code(), **CONSENT, "phone": phone})

    # Müşteri telefonunda çerez var; personel kartı taranır
    check_res = client.post("/api/redeem-check", json={"staff_token": "barista_gizli_anahtar"})
    check_data = check_res.json()
    assert check_data["status"] == "ready"
    reward_id = check_data["reward_id"]

    # Personel ödülü onaylar
    confirm_res = client.post("/api/redeem-confirm", json={
        "staff_token": "barista_gizli_anahtar",
        "reward_id": reward_id
    })
    assert confirm_res.json()["status"] == "success"

    # Tekrar sorgulandığında kullanılabilir bilet kalmamış olmalı
    check_again = client.post("/api/redeem-check", json={"staff_token": "barista_gizli_anahtar"})
    assert check_again.json()["status"] == "no_reward"

    # İkinci kez onaylama reddedilir
    confirm_again = client.post("/api/redeem-confirm", json={
        "staff_token": "barista_gizli_anahtar",
        "reward_id": reward_id
    })
    assert confirm_again.json()["status"] == "error"


def test_card_lookup_no_pii(client):
    """Numarayla kart sorgulama kişisel veri sızdırmaz, yalnızca bakiye gösterir."""
    with get_db(immediate=True) as conn:
        set_setting(conn, "daily_limit", "10")

    phone = "0533 444 55 66"
    c = _stamp_code()
    client.post("/api/claim", json={"claim_token": c, **CONSENT, "phone": phone})

    # Başka bir cihazdan numara ile sorgulama
    new_client = client
    res = new_client.post("/api/card-lookup", json={**CONSENT, "phone": "0533 444 55 66"})
    data = res.json()
    assert data["status"] == "success"
    assert data["current_stamps"] == 2     # kayıt hediyesi + okutuşun damgası
    assert data["stamp_target"] == 5
    assert "phone" not in data
    assert "customer_id" not in data
    assert "device_token" not in data


def test_static_qr_registers_and_stamps_once_pasted_link_gives_nothing_same_visit(client):
    """
    Kasadaki sabit QR / NFC (/k/m/qr) — ADR-29:
    - İlk kez okutan numarasını girer → kayıt + bu okutmanın damgası (tek kullanımlık açılış).
    - Linki tekrar açmak / kopyalayıp yapıştırmak aynı ziyarette ikinci damga vermez (bildirilen açık).
    """
    with get_db(immediate=True) as conn:
        set_setting(conn, "daily_limit", "2")
        set_setting(conn, "min_interval_minutes", "60")

    first = client.get("/k/m/qr")
    assert first.status_code == 200 and "Kayıt Ol ve Damgamı Al" in first.text
    code = first.headers["x-claim-token"]

    # Numara girilmeden istenirse kayıt ekranı istenir (açılış bu tarayıcıya ayrılır)
    need = client.post("/api/claim", json={"claim_token": "/d/" + code})
    assert need.json()["status"] == "need_auth"

    # Numara girilince kayıt + bu okutmanın damgası
    reg = client.post("/api/claim", json={"claim_token": "/d/" + code, **CONSENT, "phone": "0532 222 33 44"}).json()
    # ADR-35: kayıt hediyesi (+1) + bu okutmanın damgası (+1)
    assert reg["status"] == "success" and reg["current_stamps"] == 2 and reg["display_name"] == "user1"
    assert reg["welcome_gift"] is True
    assert DEVICE_COOKIE_NAME in client.cookies

    # Linki tekrar açmak / kopyala-yapıştır: yeni açılış ama aynı ziyarette damga yok
    for _ in range(3):
        again_code = client.get("/k/m/qr").headers["x-claim-token"]
        again = client.post("/api/claim", json={"claim_token": "/d/" + again_code}).json()
        assert again["status"] == "interval_limit" and again["current_stamps"] == 2

    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM customers").fetchone()["c"] == 1
        assert conn.execute("SELECT COUNT(*) AS c FROM stamps").fetchone()["c"] == 2     # hediye + okutuş


def test_qr_okut_page_and_pwa_install_popup(client):
    """
    Ayrı 'QR Okut' sayfası (/k/qr-okut) ve PWA Kurulum Popup bileşenlerini doğrular:
    - Dedicated kamera alanı (dedicated-qr-reader)
    - Ana ekran birleşik durum kartı (home-engage-card); tam ekran indirme pop-up'ı YOK
    - html5-qrcode kütüphanesi
    """
    res = client.get("/k/qr-okut")
    assert res.status_code == 200
    assert "tab-qrokut" in res.text
    assert "dedicated-qr-reader" in res.text
    assert "home-engage-card" in res.text and "pwa-install-popup-modal" not in res.text
    assert "html5-qrcode" in res.text
    assert "nav-btn-qrokut" in res.text
    assert "Uygulamayı Yükle" in res.text

    res_direct = client.get("/qr-okut")
    assert res_direct.status_code == 200
    assert "tab-qrokut" in res_direct.text


def test_admin_stitch_panel_and_endpoints(client):
    """Müdür paneli: giriş yapmadan veri uçları kapalı; giriş sonrası JSON API çalışır."""
    # Oturumsuz: panel sayfası yalnızca giriş formu gösterir, API 401 döner
    res0 = client.get("/yonetim")
    assert res0.status_code == 200
    assert "Şifre" in res0.text and "Kullanıcı Adı" in res0.text
    assert client.get("/api/admin/overview").status_code == 401
    assert client.post("/api/admin/customers/create", json={"phone": "0532 111 22 33"}).status_code == 401

    # Giriş
    bad = client.post("/panel/login", json={"username": "mudur", "password": "yanlis"})
    assert bad.status_code == 401
    ok = client.post("/panel/login", json={"username": "mudur", "password": "Test-Mudur-2026"})
    assert ok.json()["status"] == "success"

    res1 = client.get("/yonetim")
    assert res1.status_code == 200
    # lucide kabuk: kenar çubuğu marka bloğu rolü gösterir
    assert "Yönetim Paneli" in res1.text

    # Yeni müşteri ekle (otomatik user1)
    create_res = client.post("/api/admin/customers/create", json={"phone": "0532 111 22 33"})
    assert create_res.json()["status"] == "success"
    cust = create_res.json()["customer"]
    assert cust["display_name"] == "user1"

    # +1 Damga ekle
    add_res = client.post(f"/api/admin/customers/{cust['id']}/stamp-add")
    assert add_res.json()["status"] == "success"
    assert add_res.json()["customer"]["current_stamps"] == 1

    # -1 Damga düşür
    rem_res = client.post(f"/api/admin/customers/{cust['id']}/stamp-remove")
    assert rem_res.json()["status"] == "success"
    assert rem_res.json()["customer"]["current_stamps"] == 0


def test_kvkk_delete_data(client):
    """KVKK Verilerimi Sil: Müşteri verilerini anonimleştirir ve HttpOnly çerezi temizler."""
    phone = "0530 123 99 88"
    c = _stamp_code()
    client.post("/api/claim", json={"claim_token": c, **CONSENT, "phone": phone})

    # Veri silme isteği gönder
    del_res = client.post("/api/delete-my-data", json={"phone": phone})
    assert del_res.json()["status"] == "success"

    # Çerez silinmiş olmalı veya kart sorgusu bulunamamalı
    card_res = client.get("/api/my-card")
    assert card_res.json()["status"] == "device_not_found"

    # SQLite'ta telefonun anonimleştiğini doğrula
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT phone_e164, deleted_at FROM customers")
        cust = cursor.fetchone()
        assert "+905301239988" not in cust["phone_e164"]
        assert "silindi" in cust["phone_e164"]
        assert cust["deleted_at"] is not None


def test_legacy_schema_migration_preserves_data(tmp_path, monkeypatch):
    """
    A) Eski şemaya sahip veritabanında init_db() çalıştığında:
    - Verileri silmeden eksik tablo, sütun ve indeksleri ekler.
    - Mevcut müşteri ve damgalar korunur, yeni damga/cihaz eklenebilir.
    """
    import sqlite3
    from app import config as app_config
    from app.db import init_db
    from app.main import app
    from starlette.testclient import TestClient

    legacy_db = tmp_path / "legacy_loyalty.db"
    conn = sqlite3.connect(legacy_db)
    # Eski şema: customers'ta created_at/deleted_at yok, stamps'ta seal_id/voided_at/reward_id yok
    conn.execute("CREATE TABLE customers (id INTEGER PRIMARY KEY AUTOINCREMENT, phone_e164 TEXT UNIQUE NOT NULL)")
    conn.execute("INSERT INTO customers (id, phone_e164) VALUES (1, '+905329998877')")
    conn.execute("CREATE TABLE stamps (id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id INTEGER NOT NULL, created_at TEXT NOT NULL)")
    conn.execute("INSERT INTO stamps (customer_id, created_at) VALUES (1, '2026-10-01T12:00:00+03:00')")
    conn.execute("CREATE TABLE seals (id INTEGER PRIMARY KEY AUTOINCREMENT, token TEXT UNIQUE NOT NULL)")
    conn.execute("INSERT INTO seals (token) VALUES ('eski_muhur')")
    conn.commit()
    conn.close()

    monkeypatch.setattr(app_config, "DB_PATH", legacy_db)
    # Migration çalışmalı ve hata vermemeli
    init_db()

    with TestClient(app, base_url="https://testserver") as tc:
        # Mevcut müşteri numarasıyla giriş yaptığında eski damgası korunmalı ve yeni damga alabilmeli
        res_m = tc.get("/m/eski_muhur", follow_redirects=False)
        assert res_m.status_code == 200          # eski basılı QR açılır (kayıt sayfası; damga vermez)
        claim_token = _stamp_code()              # damga kasa ekranındaki tek kullanımlık kodla (ADR-28)

        res_claim = tc.post("/api/claim", json={"claim_token": claim_token, **CONSENT, "phone": "0532 999 88 77"})
        assert res_claim.status_code == 200
        data = res_claim.json()
        assert data["status"] == "success"
        assert data["current_stamps"] == 2


def test_registration_error_turkish_and_masked_logging(client, caplog):
    """
    A) Kayıt hataları Türkçe ve anlaşılır döner, loga yazılır;
    telefon numarası loga ASLA açık yazılmaz (maskelenir).
    Ayrıca yeni kullanıcı /api/card-lookup üzerinden de kayıt olabilir.
    """
    import logging
    caplog.set_level(logging.WARNING)

    raw_invalid = "0212 555 44 33"
    claim_token = _stamp_code()

    res = client.post("/api/claim", json={"claim_token": claim_token, **CONSENT, "phone": raw_invalid})
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "error"
    assert "geçerli bir Türkiye cep telefonu" in body["message"]

    # Log kaydı oluşmalı ve ham numara kesinlikle logda yer almamalı
    assert len(caplog.text) > 0
    assert "0212 555 44 33" not in caplog.text
    assert "02125554433" not in caplog.text
    assert "5554433" not in caplog.text


def test_daily_limit_istanbul_midnight_and_next_day(client, monkeypatch):
    """
    B) Günlük limit Europe/Istanbul gününe göre çalışır:
    - Gece 23:58 (Istanbul) damga alan kullanıcı 23:59'da alamaz.
    - Gece yarısını geçince 00:01 (Istanbul) olduğunda (UTC günü değişmese bile) yeni damga alabilir.
    """
    from zoneinfo import ZoneInfo
    import app.utils
    import app.services.loyalty

    ist = ZoneInfo("Europe/Istanbul")
    current_time = datetime(2026, 11, 2, 23, 58, 0, tzinfo=ist)

    monkeypatch.setattr(app.utils, "get_istanbul_now", lambda: current_time)
    monkeypatch.setattr(app.services.loyalty, "get_istanbul_now", lambda: current_time)

    with get_db(immediate=True) as conn:
        set_setting(conn, "daily_limit", "1")

    # 1. Gün 23:58: İlk damga başarılı (kayıt hediyesi + damga; hediye günlük limite sayılmaz)
    c1 = _stamp_code()
    r1 = client.post("/api/claim", json={"claim_token": c1, **CONSENT, "phone": "0532 100 20 30"})
    assert r1.json()["status"] == "success"
    assert r1.json()["current_stamps"] == 2

    # Aynı gün 23:59: Günlük limit (1) nedeniyle reddedilmeli
    current_time = datetime(2026, 11, 2, 23, 59, 0, tzinfo=ist)
    c2 = _stamp_code()
    r2 = client.post("/api/claim", json={"claim_token": c2})
    assert r2.json()["status"] == "limit_reached"

    # Gece yarısı sonrası 00:01 (Europe/Istanbul: 3 Kasım 00:01, UTC: 2 Kasım 21:01):
    # Ertesi gün olduğu için damga alabilmeli!
    current_time = datetime(2026, 11, 3, 0, 1, 0, tzinfo=ist)
    c3 = _stamp_code()
    assert c3 != c2
    r3 = client.post("/api/claim", json={"claim_token": c3})
    assert r3.json()["status"] == "success"
    assert r3.json()["current_stamps"] == 3

    # Aynı gün (limit=1 iken) ikinci kez damga vermemeli (yeni kodla bile)
    r3_again = client.post("/api/claim", json={"claim_token": _stamp_code()})
    assert r3_again.json()["status"] == "limit_reached"

    # Eski veritabanından UTC olarak kaydedilmiş bir damga ('2026-11-03T21:30:00Z' = 4 Kasım 00:30 Istanbul)
    # 4 Kasım günü Istanbul saatinde günlük limite dahil edilmeli!
    with get_db(immediate=True) as conn:
        cust_id = conn.execute("SELECT id FROM customers LIMIT 1").fetchone()["id"]
        conn.execute(
            "INSERT INTO stamps (customer_id, seal_id, created_at) VALUES (?, 1, '2026-11-03T21:30:00Z')",
            (cust_id,)
        )

    current_time = datetime(2026, 11, 4, 10, 0, 0, tzinfo=ist)
    c4 = _stamp_code()
    r4 = client.post("/api/claim", json={"claim_token": c4})
    assert r4.json()["status"] == "limit_reached"


def test_device_cookie_attributes_renewal_and_multidevice(client):
    """
    C) Cihaz çerezi:
    - HttpOnly, Secure, SameSite=Lax, path="/", max_age=31536000 (1 yıl)
    - Her başarılı ziyarette yenilenir
    - Çerez silinse bile numara yazılınca mevcut müşteri bulunur ve yeni cihaz bağlanır (çok cihaz)
    - Cihaz tanınmıyorsa 'Numaranı yaz, seni tanıyalım' gösterilir
    """
    # Tanınmayan cihazda kart sayfasında 'Numaranı yaz, seni tanıyalım' görünmeli
    page_unauth = client.get("/kart/")
    assert "Numaranı yaz, seni tanıyalım" in page_unauth.text

    c1 = _stamp_code()
    claim_page = client.get(f"/d/{c1}")
    assert "Numaranı yaz, seni tanıyalım" in claim_page.text

    # İlk cihazdan kayıt ve damga
    res = client.post("/api/claim", json={"claim_token": c1, **CONSENT, "phone": "0532 600 70 80"})
    set_cookie = res.headers.get("set-cookie", "")
    assert "HttpOnly" in set_cookie
    assert "Secure" in set_cookie
    assert "samesite=lax" in set_cookie.lower()
    assert "Path=/" in set_cookie
    assert "Max-Age=31536000" in set_cookie

    # Başarılı ziyarette (/api/my-card) çerez yenilenmeli
    card_res = client.get("/api/my-card")
    assert card_res.json()["status"] == "success"
    renew_cookie = card_res.headers.get("set-cookie", "")
    assert "Max-Age=31536000" in renew_cookie
    assert "Secure" in renew_cookie

    # Çerez silindiğinde / başka cihazdan numara yazıldığında mevcut müşteri bulunmalı ve yeni cihaz bağlanmalı
    client.cookies.clear()
    lookup_res = client.post("/api/card-lookup", json={**CONSENT, "phone": "0532 600 70 80"})
    assert lookup_res.json()["status"] == "success"
    assert lookup_res.json()["current_stamps"] == 2     # kayıt hediyesi + okutuş damgası (ADR-35)
    assert lookup_res.json()["welcome_gift"] is False   # mevcut müşteri girişi hediye vermez
    assert DEVICE_COOKIE_NAME in client.cookies
    assert "Secure" in lookup_res.headers.get("set-cookie", "")

    # Artık yeni çerezle /api/my-card doğrudan çalışmalı ve müşteri 2 cihaza sahip olmalı
    card_res2 = client.get("/api/my-card")
    assert card_res2.json()["status"] == "success"
    assert card_res2.json()["current_stamps"] == 2

    with get_db() as conn:
        cnt = conn.execute("SELECT COUNT(*) as c FROM devices").fetchone()["c"]
        assert cnt == 2


def test_subpath_k_and_relative_urls(client):
    """
    D) Ön yüz adresleri göreli olmalı ve /k altında (Location başlıkları dahil) sorunsuz çalışmalı.
    """
    res_root = client.get("/k/", follow_redirects=False)
    assert res_root.status_code == 200

    res_seal = client.get("/k/m/kasa_1", follow_redirects=False)
    assert res_seal.status_code == 200

    claim_token = _stamp_code()
    res_claim_page = client.get(f"/k/d/{claim_token}")
    assert res_claim_page.status_code == 200 and res_claim_page.headers["x-claim-token"] == claim_token
    from app.config import DEFAULT_BUSINESS_NAME
    assert DEFAULT_BUSINESS_NAME in res_claim_page.text
    assert client.get("/k/c/eski-baglanti").status_code == 200   # eski claim bağlantısı: kayıt sayfası (damga yok)

    res_kvkk_del = client.get("/k/kvkk/sil/")
    assert res_kvkk_del.status_code == 200
    assert "/api/delete-my-data" in res_kvkk_del.text


def test_secret_key_required_no_default(monkeypatch):
    """
    E) SECRET_KEY için kodda varsayılan olmamalı; yoksa uygulama başlamayı reddetmeli.
    """
    from app.config import get_secret_key
    from app.main import app
    from starlette.testclient import TestClient

    monkeypatch.delenv("SECRET_KEY", raising=False)
    with pytest.raises(RuntimeError):
        get_secret_key()

    with pytest.raises(RuntimeError):
        with TestClient(app):
            pass


# ==========================================
# WEB PUSH BİLDİRİM & FLUTTER (/api/me/*) TESTLERİ
# ==========================================

def test_push_subscribe_and_duplicate_endpoint_update(client):
    """
    1. Abonelik kaydı ve aynı endpoint tekrar kaydolunca (INSERT yerine UPDATE ile) güncellenmesi.
    2. Unsubscribe çağrıldığında aboneliğin pasife (active=0) alınması.
    """
    endpoint = "https://fcm.googleapis.com/fcm/send/test-sub-1"

    # İlk kayıt
    res1 = client.post("/api/push/subscribe", json={
        "endpoint": endpoint,
        "p256dh": "BNcRdreALRFXTkOOUHK1EtK2wtaz5Ry4YfYCA_0QTpQ",
        "auth": "tBHItJI5svbpez7KI4CCXg"
    })
    assert res1.status_code == 200
    assert res1.json()["status"] == "success"
    sub_id_1 = res1.json()["subscription_id"]

    # Aynı endpoint ile farklı anahtar gönderildiğinde yeni satır açmamalı, mevcut kaydı güncellemeli
    res2 = client.post("/api/push/subscribe", json={
        "endpoint": endpoint,
        "p256dh": "UPDATED_P256DH_KEY_VALUE_1234567890",
        "auth": "UPDATED_AUTH_KEY_123"
    })
    assert res2.status_code == 200
    assert res2.json()["status"] == "success"
    assert res2.json()["subscription_id"] == sub_id_1

    with get_db() as conn:
        rows = conn.execute("SELECT id, p256dh, auth, active FROM push_subscriptions WHERE endpoint = ?", (endpoint,)).fetchall()
        assert len(rows) == 1
        assert rows[0]["p256dh"] == "UPDATED_P256DH_KEY_VALUE_1234567890"
        assert rows[0]["active"] == 1

    # Unsubscribe sonrası active=0 olmalı
    unsub_res = client.post("/api/push/unsubscribe", json={"endpoint": endpoint})
    assert unsub_res.json()["status"] == "success"

    with get_db() as conn:
        row = conn.execute("SELECT active FROM push_subscriptions WHERE endpoint = ?", (endpoint,)).fetchone()
        assert row["active"] == 0

    # Tekrar subscribe olunca tekrar active=1 olmalı
    res3 = client.post("/api/push/subscribe", json={
        "endpoint": endpoint,
        "p256dh": "UPDATED_P256DH_KEY_VALUE_1234567890",
        "auth": "UPDATED_AUTH_KEY_123"
    })
    assert res3.json()["status"] == "success"
    with get_db() as conn:
        row = conn.execute("SELECT active FROM push_subscriptions WHERE endpoint = ?", (endpoint,)).fetchone()
        assert row["active"] == 1


def test_anonymous_push_subscriber_linked_on_identify(client):
    """
    Müşteri henüz tanınmadan (anonim olarak) push aboneliği açarsa customer_id=NULL kaydedilir;
    sonrasında telefon numarasıyla (/api/me/identify veya damga ile) tanındığında abonelik
    otomatik olarak müşteriye bağlanır ve müşterinin push_opt_in=1 olur.
    """
    endpoint = "https://fcm.googleapis.com/fcm/send/anon-sub-99"

    # 1. Henüz telefon numarası yokken abone ol
    sub_res = client.post("/api/push/subscribe", json={
        "endpoint": endpoint,
        "keys": {
            "p256dh": "BNcRdreALRFXTkOOUHK1EtK2wtaz5Ry4YfYCA_0QTpQ",
            "auth": "tBHItJI5svbpez7KI4CCXg"
        }
    })
    assert sub_res.json()["status"] == "success"
    assert sub_res.json()["linked"] is False

    with get_db() as conn:
        sub_row = conn.execute("SELECT customer_id FROM push_subscriptions WHERE endpoint = ?", (endpoint,)).fetchone()
        assert sub_row["customer_id"] is None

    # 2. Şimdi /api/me/identify ile telefon numarasını gir
    id_res = client.post("/api/me/identify", json={**CONSENT, "phone": "0532 765 43 28"})  # ADR-39: düz sıra numara reddedilir
    id_data = id_res.json()
    assert id_data["status"] == "success"
    assert id_data["identified"] is True
    assert "phone" not in id_data
    assert "customer_id" not in id_data

    # 3. Aboneliğin otomatik olarak bu müşteriye bağlandığını doğrula
    with get_db() as conn:
        cust = conn.execute("SELECT id, push_opt_in, push_permission_at FROM customers WHERE phone_e164 = '+905327654328'").fetchone()
        assert cust is not None
        assert cust["push_opt_in"] == 1
        assert cust["push_permission_at"] is not None

        sub_row = conn.execute("SELECT customer_id, active FROM push_subscriptions WHERE endpoint = ?", (endpoint,)).fetchone()
        assert sub_row["customer_id"] == cust["id"]
        assert sub_row["active"] == 1


def test_me_card_no_pii(client):
    """
    /api/me/card uç noktası cihaz çerezine göre kart özetini döner ve
    kesinlikle PII (telefon numarası, customer_id, device_token) sızdırmaz.
    """
    # Tanınmayan cihazda
    unauth = client.get("/api/me/card").json()
    assert unauth["status"] == "device_not_found"
    assert unauth["identified"] is False

    # Müşteriyi tanıt ve 1 damga ekle
    phone = "0533 888 77 66"
    c = _stamp_code()
    client.post("/api/claim", json={"claim_token": c, **CONSENT, "phone": phone})

    card_res = client.get("/api/me/card")
    assert card_res.status_code == 200
    data = card_res.json()
    assert data["status"] == "success"
    assert data["identified"] is True
    assert data["current_stamps"] == 2     # kayıt hediyesi + damga
    assert data["stamp_target"] == 5
    assert data["usable_rewards_count"] == 0
    assert data["pending_rewards"] == 0
    assert "reward_name" in data

    # PII sızıntısı kontrolü
    raw_json_str = card_res.text
    assert "8887766" not in raw_json_str
    assert "+90" not in raw_json_str
    assert "phone" not in data
    assert "customer_id" not in data
    assert "device_token" not in data


def test_stamp_triggers_push_notification_and_reward_notification(client, monkeypatch):
    """
    Damga eklendiğinde 'Damgan eklendi, ödüle X kaldı',
    ödül kazanıldığında 'Ödülün hazır' Türkçe push bildirimi tetiklenir.
    """
    import json
    import app.services.push as push_service

    monkeypatch.setenv("VAPID_PUBLIC_KEY", "BEl62iUYgUivxIkv69yViEuiBIa-Ib9-SkvMeAtA3LFgDzkrxZJjSgSnfckjBJuBkr3qBUYIHBQFLXYp5Nksh8U")
    monkeypatch.setenv("VAPID_PRIVATE_KEY", "UUxI4O8-FbRouAevSmBQ6o18hgE4nSG3qwvJTfKc-ls")
    monkeypatch.setenv("VAPID_SUBJECT", "mailto:test@example.com")

    sent_payloads = []

    def fake_webpush(subscription_info, data, vapid_private_key, vapid_claims, timeout=10, **kwargs):
        sent_payloads.append({
            "endpoint": subscription_info["endpoint"],
            "payload": json.loads(data),
        })
        return True

    monkeypatch.setattr(push_service, "webpush", fake_webpush)

    with get_db(immediate=True) as conn:
        set_setting(conn, "daily_limit", "10")
        set_setting(conn, "stamp_target", "3")     # kayıt hediyesi (ADR-35) 1/3; ilk damga 2/3 → "ödüle 1 kaldı"

    # Müşteriyi tanıt ve push aboneliği ekle
    client.post("/api/me/identify", json={**CONSENT, "phone": "0532 444 11 22"})
    client.post("/api/push/subscribe", json={
        "endpoint": "https://fcm.googleapis.com/fcm/send/cust-stamp-1",
        "p256dh": "BNcRdreALRFXTkOOUHK1EtK2wtaz5Ry4YfYCA_0QTpQ",
        "auth": "tBHItJI5svbpez7KI4CCXg"
    })

    # 1. Damga -> "Damgan eklendi" ve "ödüle 1 kaldı"
    c1 = _stamp_code()
    r1 = client.post("/api/claim", json={"claim_token": c1})
    assert r1.json()["status"] == "success"
    assert len(sent_payloads) == 1
    assert "Damgan eklendi" in sent_payloads[0]["payload"]["title"] or "Damgan eklendi" in sent_payloads[0]["payload"]["body"]
    assert "ödüle 1 kaldı" in sent_payloads[0]["payload"]["body"]

    # 2. Damga (Hedef=3) -> "Ödülün hazır"
    c2 = _stamp_code()
    r2 = client.post("/api/claim", json={"claim_token": c2})
    assert r2.json()["status"] == "success"
    assert r2.json()["reward_unlocked"] is True
    assert len(sent_payloads) == 2
    assert "Ödülün hazır" in sent_payloads[1]["payload"]["title"] or "Ödülün hazır" in sent_payloads[1]["payload"]["body"]


def test_push_error_never_breaks_stamp_and_410_deactivates_subscription(client, monkeypatch):
    """
    1. webpush hata fırlattığında damga işlemi YİNE DE BAŞARILI olmalıdır.
    2. Push servisi HTTP 410 (Gone) döndüğünde abonelik derhal pasife (active=0) alınmalıdır.
    """
    import app.services.push as push_service

    monkeypatch.setenv("VAPID_PUBLIC_KEY", "BEl62iUYgUivxIkv69yViEuiBIa-Ib9-SkvMeAtA3LFgDzkrxZJjSgSnfckjBJuBkr3qBUYIHBQFLXYp5Nksh8U")
    monkeypatch.setenv("VAPID_PRIVATE_KEY", "UUxI4O8-FbRouAevSmBQ6o18hgE4nSG3qwvJTfKc-ls")
    monkeypatch.setenv("VAPID_SUBJECT", "mailto:test@example.com")

    class DummyResponse:
        status_code = 410

    def failing_webpush_410(*args, **kwargs):
        exc = Exception("410 Gone: Subscription expired")
        exc.response = DummyResponse()
        raise exc

    monkeypatch.setattr(push_service, "webpush", failing_webpush_410)

    endpoint = "https://fcm.googleapis.com/fcm/send/stale-410-endpoint"
    client.post("/api/me/identify", json={**CONSENT, "phone": "0535 999 00 11"})
    client.post("/api/push/subscribe", json={
        "endpoint": endpoint,
        "p256dh": "BNcRdreALRFXTkOOUHK1EtK2wtaz5Ry4YfYCA_0QTpQ",
        "auth": "tBHItJI5svbpez7KI4CCXg"
    })

    # Damga isteği: webpush 410 fırlatsa bile damga başarıyla kaydedilmeli!
    claim_res = client.post("/api/claim", json={"claim_token": _stamp_code()})
    assert claim_res.status_code == 200
    assert claim_res.json()["status"] == "success"
    assert claim_res.json()["current_stamps"] == 2     # kayıt hediyesi (identify) + damga

    # 410 dönen abonelik otomatik olarak active=0 yapılmış olmalı
    with get_db() as conn:
        sub = conn.execute("SELECT active, fail_count FROM push_subscriptions WHERE endpoint = ?", (endpoint,)).fetchone()
        assert sub["active"] == 0
        assert sub["fail_count"] >= 1


def test_campaign_quiet_hours_and_24h_rate_limit(client, monkeypatch):
    """
    1. Gece 22:00 - 09:00 (Europe/Istanbul) arasında kampanya bildirimi engellenir.
    2. Gündüz saatlerinde aynı kitleye 24 saatte en fazla 1 toplu bildirim gönderilebilir.
    3. Tüm gönderimler notification_log tablosuna yazılır.
    """
    from zoneinfo import ZoneInfo
    import app.services.push as push_service

    monkeypatch.setenv("VAPID_PUBLIC_KEY", "BEl62iUYgUivxIkv69yViEuiBIa-Ib9-SkvMeAtA3LFgDzkrxZJjSgSnfckjBJuBkr3qBUYIHBQFLXYp5Nksh8U")
    monkeypatch.setenv("VAPID_PRIVATE_KEY", "UUxI4O8-FbRouAevSmBQ6o18hgE4nSG3qwvJTfKc-ls")
    monkeypatch.setenv("VAPID_SUBJECT", "mailto:test@example.com")

    monkeypatch.setattr(push_service, "webpush", lambda *a, **kw: True)

    # Bir abone kaydet
    client.post("/api/me/identify", json={**CONSENT, "phone": "0532 111 00 99"})
    client.post("/api/push/subscribe", json={
        "endpoint": "https://fcm.googleapis.com/fcm/send/camp-sub-1",
        "p256dh": "BNcRdreALRFXTkOOUHK1EtK2wtaz5Ry4YfYCA_0QTpQ",
        "auth": "tBHItJI5svbpez7KI4CCXg",
    })
    client.post("/api/me/marketing-consent", json={"consent": True})   # ADR-36: kampanya onayı ayrı, açık seçim

    ist = ZoneInfo("Europe/Istanbul")

    class _R:
        def __init__(self, d):
            self.d = d

        def json(self):
            return self.d

    def _send(payload):
        with get_db(immediate=True) as conn:
            return _R(push_service.send_campaign_push(
                conn, title=payload["title"], body=payload["body"], audience=payload["audience"], enforce_limits=True
            ))

    # A) Gece 23:15 (Istanbul) -> Sessiz saat engeli!
    night_time = datetime(2026, 11, 2, 23, 15, 0, tzinfo=ist)
    monkeypatch.setattr(push_service, "get_istanbul_now", lambda: night_time)

    res_night = _send({
        "title": "Gece Kampanyası",
        "body": "Gece yarısı kahvesi",
        "audience": "all"
    })
    assert res_night.json()["status"] == "quiet_hours"

    # Sabah 08:30 (Istanbul) -> Hâlâ sessiz saat engeli!
    early_morning = datetime(2026, 11, 3, 8, 30, 0, tzinfo=ist)
    monkeypatch.setattr(push_service, "get_istanbul_now", lambda: early_morning)

    res_early = _send({
        "title": "Erken Kampanya",
        "body": "Sabah kahvesi",
        "audience": "all"
    })
    assert res_early.json()["status"] == "quiet_hours"

    # B) Gündüz 14:00 (Istanbul) -> Başarıyla gönderilmeli!
    day_time = datetime(2026, 11, 3, 14, 0, 0, tzinfo=ist)
    monkeypatch.setattr(push_service, "get_istanbul_now", lambda: day_time)
    monkeypatch.setattr(push_service, "get_istanbul_now_iso", lambda: day_time.isoformat())

    res_day1 = _send({
        "title": "Öğleden Sonra İkramı",
        "body": "Bugün filtre kahvelerde sürpriz!",
        "audience": "all"
    })
    data_day1 = res_day1.json()
    assert data_day1["status"] == "success"
    assert data_day1["sent_count"] == 1

    # C) Aynı kitleye ("all") 2 saat sonra (16:00) tekrar gönderim -> 24 saat limiti engellemeli!
    two_hours_later = datetime(2026, 11, 3, 16, 0, 0, tzinfo=ist)
    monkeypatch.setattr(push_service, "get_istanbul_now", lambda: two_hours_later)

    res_day2 = _send({
        "title": "İkinci Kampanya",
        "body": "Aynı gün ikinci bildirim",
        "audience": "all"
    })
    assert res_day2.json()["status"] == "rate_limited"

    # Farklı bir kitleye ("almost_there") gönderim 24 saat engeline takılmamalı
    res_other_audience = _send({
        "title": "Ödüle Yaklaşanlar",
        "body": "Ödülünüze az kaldı!",
        "audience": "almost_there"
    })
    assert res_other_audience.json()["status"] == "success"

    # notification_log kaydı kontrolü
    with get_db() as conn:
        logs = conn.execute("SELECT kind, title, audience, sent_count FROM notification_log WHERE kind = 'campaign'").fetchall()
        assert len(logs) == 2




def test_pwa_install_bonus_flow(client):
    # Testin varsaydığı günlük limit 2 (eskiden uygulama her açılışta conftest'in 1'ini sessizce 2 yapıyordu; ADR-29)
    with get_db(immediate=True) as conn:
        set_setting(conn, "daily_limit", "2")
    phone = "0536 777 88 99"
    claim_token = _stamp_code()

    # 1. Kayıt + 1. Damga
    res1 = client.post("/api/claim", json={"claim_token": claim_token, **CONSENT, "phone": phone})
    assert res1.status_code == 200
    assert res1.json()["status"] == "success"
    assert res1.json()["current_stamps"] == 2     # kayıt hediyesi + damga (ADR-35)

    # 2. PWA indirme hediyesi (+1 damga)
    bonus_res = client.post("/api/claim-install-bonus", json={})
    assert bonus_res.status_code == 200
    data_bonus = bonus_res.json()
    assert data_bonus["status"] == "success"
    assert data_bonus["current_stamps"] == 3
    assert data_bonus["pwa_bonus_claimed"] == 1
    assert "Uygulamayı indirdiğiniz için" in data_bonus["message"]

    # /api/home-data ve /api/my-card'da pwa_bonus_claimed = 1 görünmeli
    home_data = client.get("/api/home-data").json()
    assert home_data["customer"]["pwa_bonus_claimed"] == 1
    assert home_data["customer"]["current_stamps"] == 3

    # 3. Tekrar istendiğinde already_claimed dönmeli, damga artmamalı
    bonus_again = client.post("/api/claim-install-bonus", json={})
    assert bonus_again.status_code == 200
    assert bonus_again.json()["status"] == "already_claimed"
    assert bonus_again.json()["current_stamps"] == 3

    # 4. Günlük limit dolmuş başka bir kullanıcı için de indirme bonusunun çalıştığını doğrula
    phone2 = "0537 111 22 33"
    client.cookies.clear()
    c_a = _stamp_code()
    client.post("/api/claim", json={"claim_token": c_a, **CONSENT, "phone": phone2})

    c_b = _stamp_code()
    res_b = client.post("/api/claim", json={"claim_token": c_b})
    assert res_b.json()["current_stamps"] == 3      # hediye + 2 QR damgası

    # Günlük 2 damga hakkı doldu, 3. damga reddedilmeli
    c_c = _stamp_code()
    res_c = client.post("/api/claim", json={"claim_token": c_c})
    assert res_c.json()["status"] == "limit_reached"

    # Ancak PWA indirme bonusu günlük limite takılmadan verilmeli! (2 -> 3)
    bonus_res2 = client.post("/api/claim-install-bonus", json={})
    assert bonus_res2.status_code == 200
    assert bonus_res2.json()["status"] == "success"
    assert bonus_res2.json()["current_stamps"] == 4

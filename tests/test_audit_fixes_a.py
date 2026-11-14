"""Denetim düzeltmeleri, Tier A: kenar başlıklarına güven (G-01/G-10), olay döngüsü/kilit (E-01), giriş kilidi (G-04),
oturum iptali (G-06), müşteri kimliği taraması (G-07), push adresi (G-09), eğik çizgi (E-04), çift tıklama/bildirim (E-06/E-08)."""
import threading
import time

import pytest
from starlette.requests import Request
from starlette.testclient import TestClient

from app import auth, config
from app.db import get_db
from app.main import app

SECRET = "edge-test-secret-0123456789abcdef"
MANAGER = {"username": "mudur", "password": "Test-Mudur-2026"}
STAFF = {"username": "kasa1", "password": "Test-Kasa-2026"}
CONSENT = {"kvkk_read": True, "explicit_consent": True}


def _req(headers: dict | None = None, peer: str = "10.0.0.9") -> Request:
    return Request({
        "type": "http", "method": "GET", "path": "/", "query_string": b"", "client": (peer, 1234),
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
    })


@pytest.fixture
def edge_env(tmp_path, monkeypatch):
    """Her test: sıkı-kip işareti geçici klasörde, bellek önbelleği sıfır, sır tanımsız."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "_edge_strict_cache", None)
    monkeypatch.delenv("EDGE_SHARED_SECRET", raising=False)
    monkeypatch.setenv("EDGE_DEFAULT_HOST", "kafem.pages.dev")
    monkeypatch.setenv("EDGE_ALLOWED_HOSTS", "kafem.example.com,www.kafem.example.com")
    return monkeypatch


# ------------------------------------------------------------------ G-01 / G-10 kenar başlıkları
def test_edge_without_secret_known_host_still_works(edge_env):
    """Sır henüz yokken EDGE_DEFAULT_HOST ve EDGE_ALLOWED_HOSTS içindeki hostlar çalışır."""
    for host in ("kafem.pages.dev", "kafem.example.com", "www.kafem.example.com"):
        r = _req({"X-Edge-Host": host, "X-Edge-Client-IP": "198.51.100.7"})
        assert auth.client_ip(r) == "198.51.100.7"
        assert config.request_public_base(r) == (f"https://{host}", "")


def test_cloudflare_peer_uses_cf_connecting_ip_but_others_cannot_spoof(edge_env):
    """Cloudflare vekili arkasında gerçek IP CF-Connecting-IP'den okunur; Cloudflare dışı eşin sahte başlığı yok sayılır."""
    assert auth.client_ip(_req({"CF-Connecting-IP": "198.51.100.20"}, peer="188.114.97.5")) == "198.51.100.20"
    assert auth.client_ip(_req({"CF-Connecting-IP": "2001:db8::7"}, peer="2606:4700::1111")) == "2001:db8::7"
    assert auth.client_ip(_req({"CF-Connecting-IP": "198.51.100.20"}, peer="203.0.113.9")) == "203.0.113.9"
    assert auth.client_ip(_req({"CF-Connecting-IP": "bozuk"}, peer="188.114.97.5")) == "188.114.97.5"
    assert auth.client_ip(_req({}, peer="188.114.97.5")) == "188.114.97.5"


def test_edge_unknown_or_malformed_host_is_ignored(edge_env):
    for host in ("evil.com", "evil.com#.kafem.pages.dev", "x.kafem.pages.dev", "kafem.pages.dev.evil.com",
                 "kafem.pages.dev/../x", ""):
        r = _req({"X-Edge-Host": host, "X-Edge-Client-IP": "198.51.100.7"})
        assert auth.client_ip(r) == "10.0.0.9", host
        assert config.request_public_base(r) == (config.PUBLIC_BASE_URL, config.PUBLIC_BASE_PATH), host
    # X-Forwarded-Host artık hiç kullanılmaz (Caddy onu kendi adresiyle ezer)
    r = _req({"X-Forwarded-Host": "kafem.pages.dev", "X-Edge-Client-IP": "198.51.100.7"})
    assert auth.client_ip(r) == "10.0.0.9"


def test_edge_garbage_ip_header_falls_back_to_peer(edge_env):
    r = _req({"X-Edge-Host": "kafem.pages.dev", "X-Edge-Client-IP": "not-an-ip"})
    assert auth.client_ip(r) == "10.0.0.9"


def test_edge_allowed_hosts_env_extends_allow_list(edge_env):
    edge_env.setenv("EDGE_ALLOWED_HOSTS", "kart.ornek.com, bozuk host")
    r = _req({"X-Edge-Host": "kart.ornek.com", "X-Edge-Client-IP": "198.51.100.8"})
    assert auth.client_ip(r) == "198.51.100.8"
    assert config.request_public_base(r)[0] == "https://kart.ornek.com"


def test_edge_secret_valid_auth_trusted_and_switches_to_strict(edge_env, tmp_path):
    edge_env.setenv("EDGE_SHARED_SECRET", SECRET)
    # Geçiş dönemi: sır sunucuda var ama Cloudflare'de henüz yok → sırsız istek eskisi gibi (bilinen host) çalışır
    legacy = _req({"X-Edge-Host": "kafem.pages.dev", "X-Edge-Client-IP": "198.51.100.1"})
    assert auth.client_ip(legacy) == "198.51.100.1"
    # Doğru sır → güvenilir (önizleme alt alanı dahil) ve sıkı kip kalıcı işaretlenir
    ok = _req({"X-Edge-Auth": SECRET, "X-Edge-Host": "abc123.kafem.pages.dev", "X-Edge-Client-IP": "198.51.100.2"})
    assert auth.client_ip(ok) == "198.51.100.2"
    assert config.request_public_base(ok) == ("https://abc123.kafem.pages.dev", "")
    assert (tmp_path / ".edge_auth_enforced").exists()
    # Artık sırsız (sahte) başlıklar kabul edilmez
    assert auth.client_ip(legacy) == "10.0.0.9"
    assert config.request_public_base(legacy) == (config.PUBLIC_BASE_URL, config.PUBLIC_BASE_PATH)
    # Yanlış sır da kabul edilmez
    wrong = _req({"X-Edge-Auth": "yanlis", "X-Edge-Host": "kafem.pages.dev", "X-Edge-Client-IP": "198.51.100.3"})
    assert auth.client_ip(wrong) == "10.0.0.9"


def test_edge_strict_marker_survives_restart(edge_env, tmp_path):
    (tmp_path / ".edge_auth_enforced").write_text("1")
    edge_env.setenv("EDGE_SHARED_SECRET", SECRET)
    r = _req({"X-Edge-Host": "kafem.pages.dev", "X-Edge-Client-IP": "198.51.100.4"})
    assert auth.client_ip(r) == "10.0.0.9"


def test_edge_wrong_secret_rejected_even_before_strict(edge_env):
    edge_env.setenv("EDGE_SHARED_SECRET", SECRET)
    wrong = _req({"X-Edge-Auth": "yanlis", "X-Edge-Host": "kafem.pages.dev", "X-Edge-Client-IP": "198.51.100.3"})
    assert auth.client_ip(wrong) == "10.0.0.9"


def test_spoofed_edge_ip_does_not_bypass_login_rate_limit_for_unknown_host(client, edge_env):
    """Sahte host + her istekte farklı IP: IP bazlı sınır eş adresine göre uygulanır → 11. deneme 429."""
    codes = []
    for i in range(12):
        r = client.post("/panel/login", json={"username": "mudur", "password": "yanlis"},
                        headers={"X-Edge-Host": "evil.example", "X-Edge-Client-IP": f"198.51.100.{i + 1}"})
        codes.append(r.status_code)
    assert codes[0] == 401 and 429 in codes[10:]


def test_manifest_uses_pages_host_only_when_known(client, edge_env):
    ok = client.get("/manifest.webmanifest", headers={"X-Edge-Host": "kafem.pages.dev"}).json()
    assert ok["related_applications"][0]["url"].startswith("https://kafem.pages.dev/")
    bad = client.get("/manifest.webmanifest", headers={"X-Edge-Host": "x.com#.kafem.pages.dev"}).json()
    assert "x.com" not in bad["related_applications"][0]["url"]


# ------------------------------------------------------------------ E-01 olay döngüsü / yazma kilidi
def test_blocking_routes_are_sync_so_threadpool_runs_them():
    """Bloklayan işler async def içinde olay döngüsünü dondurmasın: ilgili rotalar düz def (iş parçacığı havuzu)."""
    import inspect
    from app.routes import api, manager, panels, staff
    for fn in (panels.panel_login, api.api_card_lookup, api.api_claim, api.api_auth_login, manager.push_send, manager.push_test,
               manager.customer_stamp_add, manager.customer_stamp_set, staff.stamp, staff.redeem):
        assert not inspect.iscoroutinefunction(fn), fn.__name__


def test_push_is_sent_after_write_lock_is_released(client, monkeypatch):
    """Damga sonrası push, yazma işlemi COMMIT edilip kilit bırakıldıktan SONRA gönderilir (yavaş push kilidi tutmaz)."""
    import app.services.push as push_service
    monkeypatch.setenv("VAPID_PUBLIC_KEY", "BEl62iUYgUivxIkv69yViEuiBIa-Ib9-SkvMeAtA3LFgDzkrxZJjSgSnfckjBJuBkr3qBUYIHBQFLXYp5Nksh8U")
    monkeypatch.setenv("VAPID_PRIVATE_KEY", "UUxI4O8-FbRouAevSmBQ6o18hgE4nSG3qwvJTfKc-ls")
    monkeypatch.setenv("VAPID_SUBJECT", "mailto:test@example.com")
    from app.services.stamp_codes import create_stamp_code
    state = {}

    def slow_webpush(subscription_info, data, **kw):
        # Gönderim sırasında başka bir bağlantı YAZABİLMELİ (kilit tutulmuyor)
        try:
            with get_db(immediate=True) as other:
                other.execute("UPDATE settings SET value = value WHERE key = 'stamp_target'")
            state["write_ok"] = True
        except Exception as exc:  # pragma: no cover
            state["write_ok"] = repr(exc)
        return True

    monkeypatch.setattr(push_service, "webpush", slow_webpush)
    client.post("/api/me/identify", json={**CONSENT, "phone": "0532 444 11 22"})
    client.post("/api/push/subscribe", json={
        "endpoint": "https://fcm.googleapis.com/fcm/send/lock-1",
        "p256dh": "BNcRdreALRFXTkOOUHK1EtK2wtaz5Ry4YfYCA_0QTpQ", "auth": "tBHItJI5svbpez7KI4CCXg"})
    with get_db(immediate=True) as conn:
        conn.execute("UPDATE settings SET value = '10' WHERE key = 'daily_limit'")
        code = create_stamp_code(conn, created_by="t")["code"]
    r = client.post("/api/claim", json={"claim_token": code})
    assert r.json()["status"] == "success"
    assert state.get("write_ok") is True, state
    with get_db() as conn:  # gönderim sonucu ve kayıt yine de yazıldı
        assert conn.execute("SELECT COUNT(*) AS n FROM notification_log WHERE kind LIKE 'stamp%'").fetchone()["n"] == 1
        assert conn.execute("SELECT last_success_at FROM push_subscriptions").fetchone()["last_success_at"]


def test_failed_transaction_discards_deferred_push(monkeypatch):
    """İşlem geri alınırsa COMMIT sonrası işler çalışmaz."""
    from app.db import run_after_commit
    ran = []
    with pytest.raises(RuntimeError):
        with get_db(immediate=True) as conn:
            assert run_after_commit(conn, lambda p: ran.append(p)) is True
            raise RuntimeError("geri al")
    assert ran == []
    with get_db(immediate=True) as conn:
        run_after_commit(conn, lambda p: ran.append(p))
    assert len(ran) == 1


def test_rate_limit_counter_is_thread_safe():
    from app.utils import _rate_limits, check_rate_limit
    _rate_limits.clear()
    results = []

    def worker():
        for _ in range(50):
            results.append(check_rate_limit("thread_key", 100, 60))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results.count(True) == 100 and results.count(False) == 300


# ------------------------------------------------------------------ G-04 giriş kilidi
def test_successful_logins_do_not_lock_the_manager(client):
    """Eskiden her deneme (başarılı olsa da) hesap sayacını artırıyordu; 31. başarılı giriş kilitleniyordu."""
    from app.utils import _rate_limits
    for _ in range(12):
        _rate_limits.clear()  # IP sınırı bu testin konusu değil
        assert client.post("/panel/login", json=MANAGER).status_code == 200
    # hesap sayacı yalnızca başarısızları sayar
    from app.utils import rate_limit_blocked
    assert not rate_limit_blocked("panel_login_fail_mudur", 10, 900)


def test_foreign_ip_failures_cannot_lock_out_a_known_ip(client):
    """10 yanlış denemeden sonra hesap yabancı IP'lere kilitlenir; daha önce başarıyla giren IP içeri girebilir."""
    from app.utils import _rate_limits
    assert client.post("/panel/login", json=MANAGER).status_code == 200  # bu IP artık "tanınan"
    _rate_limits.pop("panel_login_127.0.0.1", None)
    from app.utils import rate_limit_record
    for _ in range(10):
        rate_limit_record("panel_login_fail_mudur")
    # yabancı IP (kenar başlığı güvenilir değil: eş adresi aynı olduğundan tanınan IP'yi simüle etmek için known anahtarı sil)
    ip = auth.client_ip(_req(peer="testclient"))
    # tanınan IP: giriş geçer
    r = client.post("/panel/login", json=MANAGER)
    assert r.status_code == 200, r.text
    # tanınmayan IP: 429
    from app.utils import rate_limit_clear
    rate_limit_clear(f"panel_login_known_mudur_{ip}")
    for _ in range(10):
        rate_limit_record("panel_login_fail_mudur")
    r = client.post("/panel/login", json=MANAGER)
    assert r.status_code == 429


# ------------------------------------------------------------------ G-06 oturum iptali
def test_logout_revokes_session_server_side(client):
    assert client.post("/panel/login", json=MANAGER).status_code == 200
    stolen = dict(client.cookies)["mgr_session"]
    assert client.get("/api/admin/overview").status_code == 200
    client.post("/panel/logout")
    # Çerez sahibinde silinse de, çalınmış kopya sunucuda iptal edilmiştir
    attacker = TestClient(app, base_url="https://testserver")
    attacker.cookies.set("mgr_session", stolen)
    assert attacker.get("/api/admin/overview").status_code == 401
    assert attacker.get("/api/staff/session").status_code == 401


def test_logout_revokes_only_that_session(client):
    other = TestClient(app, base_url="https://testserver")
    assert client.post("/panel/login", json=MANAGER).status_code == 200
    assert other.post("/panel/login", json=MANAGER).status_code == 200
    client.post("/panel/logout")
    assert other.get("/api/admin/overview").status_code == 200


# ------------------------------------------------------------------ G-07 müşteri kimliği taraması
def _staff_with_customer(client, phone="0532 700 12 34"):
    mgr = TestClient(app, base_url="https://testserver")
    mgr.post("/panel/login", json=MANAGER)
    cid = mgr.post("/api/admin/customers/create", json={"phone": phone, "consent_confirmed": True}).json()["customer"]["id"]
    assert client.post("/panel/login", json=STAFF).status_code == 200
    return cid


def test_staff_id_enumeration_is_limited_but_normal_flow_is_not(client):
    cid = _staff_with_customer(client)
    # Normal akış: önce ara, sonra kimlikle işlem → sınırsız
    found = client.post("/api/staff/search", json={"q": "5327001234"}).json()
    assert found["status"] == "success" and found["customers"][0]["id"] == cid
    for _ in range(25):
        assert client.post("/api/staff/lookup", json={"customer_id": cid}).json()["status"] == "success"
    # Aramadan gelmeyen kimlikleri art arda deneme → 20'den sonra reddedilir ve olay kaydı açılır
    statuses = [client.post("/api/staff/lookup", json={"customer_id": 9000 + i}).json()["status"] for i in range(25)]
    assert statuses[:20] == ["not_found"] * 20 and statuses[20:] == ["error"] * 5
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM audit_logs WHERE event_type='staff_id_enumeration'").fetchone()["n"] >= 1


def test_staff_recent_logs_hide_search_noise(client):
    cid = _staff_with_customer(client)
    client.post("/api/staff/search", json={"q": "5327001234"})
    client.post("/api/staff/lookup", json={"customer_id": cid})
    client.post("/api/staff/stamp", json={"customer_id": cid})
    actions = {row["action"] for row in client.get("/api/staff/logs").json()["logs"]}
    assert "customer_search" not in actions and "customer_lookup" not in actions and "stamp_added" in actions


# ------------------------------------------------------------------ G-09 push adresi
def test_push_url_sanitized_to_own_site(monkeypatch):
    from app.services.push import sanitize_push_url
    monkeypatch.setenv("EDGE_DEFAULT_HOST", "kafem.pages.dev")
    monkeypatch.setenv("EDGE_ALLOWED_HOSTS", "kafem.example.com")
    d = "/k/kart/"
    assert sanitize_push_url("/k/kart/?tab=kampanyalar", d) == "/k/kart/?tab=kampanyalar"
    assert sanitize_push_url("https://kafem.example.com/k/kart/", d) == "https://kafem.example.com/k/kart/"
    assert sanitize_push_url("https://kafem.pages.dev/kart/", d) == "https://kafem.pages.dev/kart/"
    for bad in ("https://kotu.example/giris", "//kotu.example/x", "javascript:alert(1)", "http://kafem.example.com/",
                "https://kafem.example.com.kotu.example/", "https://kafem.example.com@kotu.example/", "/\\kotu.example",
                "/k/kart/\r\nX: y", "https://kafem.example.com:8443/", "", None):
        assert sanitize_push_url(bad, d) == d, bad


def test_campaign_push_with_foreign_url_goes_to_default(client, monkeypatch):
    import json as _json
    import app.services.push as push_service
    monkeypatch.setenv("VAPID_PUBLIC_KEY", "BEl62iUYgUivxIkv69yViEuiBIa-Ib9-SkvMeAtA3LFgDzkrxZJjSgSnfckjBJuBkr3qBUYIHBQFLXYp5Nksh8U")
    monkeypatch.setenv("VAPID_PRIVATE_KEY", "UUxI4O8-FbRouAevSmBQ6o18hgE4nSG3qwvJTfKc-ls")
    sent = []
    monkeypatch.setattr(push_service, "webpush", lambda subscription_info, data, **kw: sent.append(_json.loads(data)) or True)
    client.post("/api/me/identify", json={**CONSENT, "marketing_consent": True, "phone": "0532 800 00 77"})
    client.post("/api/push/subscribe", json={
        "endpoint": "https://fcm.googleapis.com/fcm/send/url-1", "marketing_consent": True,
        "p256dh": "BNcRdreALRFXTkOOUHK1EtK2wtaz5Ry4YfYCA_0QTpQ", "auth": "tBHItJI5svbpez7KI4CCXg"})
    client.cookies.clear()
    client.post("/panel/login", json=MANAGER)
    res = client.post("/api/admin/push/send", json={"title": "t", "body": "b", "url": "https://kotu.example/giris"}).json()
    assert res["status"] == "success" and res["sent_count"] == 1
    assert sent[0]["url"].endswith("/kart/") and "kotu.example" not in sent[0]["url"]


# ------------------------------------------------------------------ E-04 eğik çizgi
def test_trailing_slash_never_redirects(client):
    """307 Location'ı /k önekini kaybeder (Caddy kırpar): hiçbir adres yönlendirme yapmamalı; sayfalar iki biçimde 200."""
    for path in ("/k/api/home-data/", "/k/manifest.webmanifest/", "/k/sw.js/", "/k/static"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 404, (path, r.status_code)
    for path in ("/k/kart", "/k/kart/", "/k/giris", "/k/giris/", "/k/mudur", "/k/mudur/", "/k/personel", "/k/personel/",
                 "/k/m/qr", "/k/m/qr/", "/k/indir", "/k/indir/", "/k/kvkk", "/k/kvkk/", "/k", "/k/", "/kart", "/kart/"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 200, (path, r.status_code)


# ------------------------------------------------------------------ E-08 "Sayıya Ayarla" bildirimi
def test_set_stamps_does_not_send_one_push_per_stamp(client, monkeypatch):
    import json as _json
    import app.services.push as push_service
    monkeypatch.setenv("VAPID_PUBLIC_KEY", "BEl62iUYgUivxIkv69yViEuiBIa-Ib9-SkvMeAtA3LFgDzkrxZJjSgSnfckjBJuBkr3qBUYIHBQFLXYp5Nksh8U")
    monkeypatch.setenv("VAPID_PRIVATE_KEY", "UUxI4O8-FbRouAevSmBQ6o18hgE4nSG3qwvJTfKc-ls")
    sent = []
    monkeypatch.setattr(push_service, "webpush", lambda subscription_info, data, **kw: sent.append(_json.loads(data)) or True)
    client.post("/api/me/identify", json={**CONSENT, "phone": "0532 800 00 88"})
    client.post("/api/push/subscribe", json={
        "endpoint": "https://fcm.googleapis.com/fcm/send/set-1",
        "p256dh": "BNcRdreALRFXTkOOUHK1EtK2wtaz5Ry4YfYCA_0QTpQ", "auth": "tBHItJI5svbpez7KI4CCXg"})
    cid = _staff_with_customer(client, "0532 800 00 89")  # farklı müşteri; müdür oturumu için
    client.cookies.clear()
    client.post("/panel/login", json=MANAGER)
    with get_db() as conn:
        mine = conn.execute("SELECT id FROM customers WHERE phone_e164 = '+905328000088'").fetchone()["id"]
    r = client.post(f"/api/admin/customers/{mine}/stamp-set", json={"count": 3}).json()
    assert r["status"] == "success"
    assert sent == []   # düzeltme işlemi müşteriye sahte "Damgan eklendi" bildirimi göndermez
    r = client.post(f"/api/admin/customers/{mine}/stamp-add").json()
    assert r["status"] == "success" and len(sent) == 1   # tek tıkla +1 damga yine bildirir


def test_staff_register_then_act_by_id_is_not_counted_as_probe(client):
    _staff_with_customer(client)
    r = client.post("/api/staff/register", json={"phone": "0532 700 55 66", "consent_confirmed": True}).json()
    assert r["status"] == "success"
    new_id = r["customer"]["id"]
    for _ in range(25):
        assert client.post("/api/staff/lookup", json={"customer_id": new_id}).json()["status"] == "success"

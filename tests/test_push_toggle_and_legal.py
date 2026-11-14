"""
ADR-21: bildirim teslimatı (TTL/Urgency), abone sayacı = gerçek alıcı, KVKK metinleri ve saklama/imha görevi.
ADR-23: bildirim durumu gerçek tarayıcı izninden. ADR-24 (yasal düzeltme): Profil'de kampanya onayı anahtarı geri geldi;
açık rıza isteğe bağlı. ADR-27: bildirimlerde "Bildirimleri kapat" düğmesi yok (ADR-36: yalnızca kampanya bildirimi gövdesinde kısa ret satırı vardır).
ADR-36: abonelik / "Bildirimleri Aç" kampanya onayı yazmaz; onay yalnızca ayrı seçimle verilir.
"""
import json
import re
from datetime import timedelta

from app.db import get_db, set_setting
from app.utils import get_istanbul_now

VAPID = {
    "VAPID_PUBLIC_KEY": "BEl62iUYgUivxIkv69yViEuiBIa-Ib9-SkvMeAtA3LFgDzkrxZJjSgSnfckjBJuBkr3qBUYIHBQFLXYp5Nksh8U",
    "VAPID_PRIVATE_KEY": "UUxI4O8-FbRouAevSmBQ6o18hgE4nSG3qwvJTfKc-ls",
    "VAPID_SUBJECT": "mailto:test@example.com",
}
SUB = {"p256dh": "BNcRdreALRFXTkOOUHK1EtK2wtaz5Ry4YfYCA_0QTpQ", "auth": "tBHItJI5svbpez7KI4CCXg"}
MANAGER = {"username": "mudur", "password": "Test-Mudur-2026"}


def _js_const(html, name):
    """Sayfaya `| tojson` ile gömülen metni çözer (Jinja Türkçe karakterleri \\uXXXX olarak kaçırır)."""
    m = re.search(r"const %s = (\".*?\");" % name, html)
    assert m, name
    return json.loads(m.group(1))


def _register(client, phone):
    r = client.post("/api/auth/login", json={"phone": phone, "kvkk_read": True})
    assert r.json()["status"] == "success", r.text
    return {"X-Device-Token": r.json()["device_token"]}


def _consent(client, h, given=True):
    """ADR-36: kampanya onayı ayrı, açık seçimdir (Profil › Kampanya Bildirimleri anahtarı); abonelik onay yazmaz."""
    r = client.post("/api/me/marketing-consent", json={"consent": given}, headers=h).json()
    assert r["status"] == "success", r


def _vapid(monkeypatch):
    for k, v in VAPID.items():
        monkeypatch.setenv(k, v)


# ------------------------------------------------------------------ 1. teslimat: TTL + Urgency
def test_campaign_push_uses_ttl_urgency_and_has_no_close_hint(client, monkeypatch):
    import app.services.push as push_service
    _vapid(monkeypatch)
    calls = []
    monkeypatch.setattr(push_service, "webpush", lambda subscription_info, data, **kw: calls.append((json.loads(data), kw)) or True)

    h = _register(client, "0532 800 00 01")
    client.post("/api/push/subscribe", json={"endpoint": "https://push.example/t1", **SUB}, headers=h)
    _consent(client, h)
    client.cookies.clear()
    client.post("/panel/login", json=MANAGER)
    res = client.post("/api/admin/push/send", json={"title": "Fırsat", "body": "Bugün 2 damga!"}).json()
    assert res["status"] == "success" and res["sent_count"] == 1

    payload, kw = calls[-1]
    # ttl=0 (pywebpush varsayılanı) uyuyan telefona hiç ulaşmaz: kampanya 24 saat bekletilir, cihaz uyandırılır
    assert kw["ttl"] == push_service.TTL_CAMPAIGN and kw["headers"] == {"Urgency": "high"}
    # ADR-36 (Y-04): gönderici kimliği başlıkta (marka adı), gövdenin sonunda kısa ret satırı; düğme (action/optout) yok
    from app.config import DEFAULT_BUSINESS_NAME
    assert payload["title"].startswith(DEFAULT_BUSINESS_NAME)
    assert payload["body"] == "Bugün 2 damga!\nKapatmak için: Profil › Kampanya Bildirimleri"
    assert push_service.CAMPAIGN_OPTOUT_HINT == "Kapatmak için: Profil › Kampanya Bildirimleri"
    assert "optout" not in payload and "actions" not in payload and not hasattr(push_service, "OPTOUT_HINT")


def test_stamp_notification_also_has_ttl(client, monkeypatch):
    import app.services.push as push_service
    _vapid(monkeypatch)
    calls = []
    monkeypatch.setattr(push_service, "webpush", lambda subscription_info, data, **kw: calls.append(kw) or True)
    h = _register(client, "0532 800 00 02")
    client.post("/api/push/subscribe", json={"endpoint": "https://push.example/t2", **SUB}, headers=h)
    with get_db(immediate=True) as conn:
        cid = conn.execute("SELECT id FROM customers").fetchone()["id"]
        push_service.notify_customer_after_stamp(conn, cid, 2, 5, False, "Kahve")
    assert calls and calls[-1]["ttl"] == push_service.TTL_TRANSACTIONAL


def test_failed_send_reports_reason_to_manager(client, monkeypatch):
    import app.services.push as push_service
    _vapid(monkeypatch)

    class Resp:
        status_code = 403

    class Boom(Exception):
        response = Resp()

    def fail(*a, **kw):
        raise Boom("forbidden")

    monkeypatch.setattr(push_service, "webpush", fail)
    h = _register(client, "0532 800 00 03")
    client.post("/api/push/subscribe", json={"endpoint": "https://push.example/t3", **SUB}, headers=h)
    _consent(client, h)
    client.cookies.clear()
    client.post("/panel/login", json=MANAGER)
    res = client.post("/api/admin/push/send", json={"title": "x", "body": "y"}).json()
    assert res["failed_count"] == 1 and "HTTP 403" in res["message"] and "VAPID" in res["message"]


def test_empty_audience_message_explains_why(client, monkeypatch):
    import app.services.push as push_service
    _vapid(monkeypatch)
    monkeypatch.setattr(push_service, "webpush", lambda *a, **kw: True)
    client.post("/panel/login", json=MANAGER)
    res = client.post("/api/admin/push/send", json={"title": "x", "body": "y"}).json()
    assert res["sent_count"] == 0 and "abone yok" in res["message"]


# ------------------------------------------------------------------ 2. sayaç = gerçek alıcı
def test_permission_without_consent_is_counted_as_pending_not_subscriber(client):
    h = _register(client, "0532 810 00 01")
    # eski akış / bozuk durum: abonelik var ama ticari ileti onayı yok
    client.post("/api/push/subscribe", json={"endpoint": "https://push.example/p1", **SUB}, headers=h)
    client.cookies.clear()
    client.post("/panel/login", json=MANAGER)
    prev = client.get("/api/admin/push/preview?audience=all").json()
    assert prev["customer_count"] == 0 and prev["pending_consent_count"] == 1
    cust = client.get("/api/admin/customers").json()["customers"][0]
    # müdür listesinde "Bildirim: Açık" artık yalnızca gerçekten kampanya alabilecekler için
    assert cust["current_status"]["push_active"] is False and cust["current_status"]["push_no_consent"] is True


def test_turning_switch_on_fixes_pending_customer(client):
    h = _register(client, "0532 810 00 02")
    client.post("/api/push/subscribe", json={"endpoint": "https://push.example/p2", **SUB}, headers=h)
    # ADR-36: abonelik (eski istemci `marketing_consent: true` gönderse bile) onay yazmaz; Profil anahtarı açınca onay oluşur
    r = client.post("/api/push/subscribe", json={"endpoint": "https://push.example/p2", **SUB, "marketing_consent": True}, headers=h).json()
    assert r["status"] == "success" and r["marketing_consent"] is False
    _consent(client, h)
    client.cookies.clear()
    client.post("/panel/login", json=MANAGER)
    prev = client.get("/api/admin/push/preview?audience=all").json()
    assert prev["customer_count"] == 1 and prev["pending_consent_count"] == 0
    assert client.get("/api/admin/customers").json()["customers"][0]["current_status"]["push_active"] is True


def test_stale_local_token_does_not_make_subscription_anonymous(client):
    """localStorage'da eski bir token kalsa bile HttpOnly çerezdeki geçerli müşteriye bağlanır."""
    _register(client, "0532 810 00 03")   # çerez ayarlandı
    r = client.post("/api/push/subscribe", json={"endpoint": "https://push.example/p3", **SUB},
                    headers={"X-Device-Token": "eski-gecersiz-token"}).json()
    assert r["linked"] is True and r["marketing_consent"] is False


# ------------------------------------------------------------------ 3. aç/kapa anahtarı + sunucu durumu
def test_push_status_reflects_server_truth_and_toggle_cycle(client):
    h = _register(client, "0532 820 00 01")
    ep = "https://push.example/s1"
    assert client.post("/api/push/status", json={"endpoint": ep}, headers=h).json()["active"] is False
    client.post("/api/push/subscribe", json={"endpoint": ep, **SUB}, headers=h)
    _consent(client, h)
    st = client.post("/api/push/status", json={"endpoint": ep}, headers=h).json()
    assert st == {"status": "success", "active": True, "linked": True, "marketing_consent": True, "push_enabled": st["push_enabled"]}

    # KAPAT → abonelik pasif + onay geri alındı (ret, denetim kaydıyla)
    client.post("/api/push/unsubscribe", json={"endpoint": ep}, headers=h)
    st = client.post("/api/push/status", json={"endpoint": ep}, headers=h).json()
    assert st["active"] is False and st["marketing_consent"] is False
    with get_db() as conn:
        assert conn.execute("SELECT explicit_consent_at FROM customers").fetchone()[0] is None
        assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'marketing_consent_revoked'").fetchone()[0] == 1

    # TEKRAR AÇ → (izin zaten verilmiş) ayar sayfasına gitmeden aynı uçla yeniden aktif
    client.post("/api/push/subscribe", json={"endpoint": ep, **SUB}, headers=h)
    st = client.post("/api/push/status", json={"endpoint": ep}, headers=h).json()
    assert st["active"] is True and st["marketing_consent"] is False   # abonelik onayı geri vermez
    _consent(client, h)
    assert client.post("/api/push/status", json={"endpoint": ep}, headers=h).json()["marketing_consent"] is True


def test_customer_page_shows_real_permission_state_and_has_campaign_consent_switch(client):
    html = client.get("/k/kart/").text
    # ADR-23: durum kutusu + üst bar zili gerçek tarayıcı iznine bakar (tarayıcı iznini siteden kapatma yok)
    assert 'id="inAppNotificationBanner"' in html and 'id="enableNotifBtn"' in html and "js-notif-bell" in html
    assert "function isNotificationReallyEnabled" in html and 'notifPermission() === "granted"' in html
    assert 'id="push-switch"' not in html
    # ADR-24 (ETK m.8): ticari ileti onayını geri alma/verme anahtarı Profil'de; yalnızca kampanya onayını yönetir
    assert 'id="campaign-consent-switch" type="checkbox" role="switch"' in html and html.count('role="switch"') == 1
    assert "function handleCampaignConsentToggle" in html and "/api/me/marketing-consent" in html
    assert "api/push/status" in html and "subscriptionMatchesKey" in html   # VAPID anahtarı değişince yeniden abone olur
    assert "push-off-modal" not in html and "applyCta" not in html and "QR_EXPIRED" not in html
    # Tarayıcı/telefon ayarına yönlendirme yok; eski kapat/abonelikten çık akışı yok
    for gone in ("turnNotificationsOff", "handleNotificationToggle", "Abonelikten Çık", "Bildirimi Kapat",
                 "ayarlara gitmen", "tarayıcı ayarından"):
        assert gone not in html, gone


def test_service_worker_shows_no_close_button_on_any_notification(client):
    sw = client.get("/k/sw.js").text
    # ADR-27: hiçbir bildirim türüne düğme (action) eklenmez
    assert "options.actions" not in sw and "payload.optout" not in sw and "title: 'Bildirimleri kapat'" not in sw
    # ADR-27 öncesi bildirim merkezinde kalmış eski bir bildirimin düğmesine basılırsa ret yine işlenir (yok sayılmaz)
    assert "event.action === 'optout'" in sw and "api/push/unsubscribe" in sw and "sub.unsubscribe()" in sw
    from app.config import PUBLIC_BASE_PATH
    assert f'"base": "{PUBLIC_BASE_PATH}/"' in sw


# ------------------------------------------------------------------ 4. KVKK metinleri
def test_aydinlatma_v4_is_separate_from_consent_text_and_has_required_sections(client):
    html = client.get("/k/kart/").text
    kvkk = _js_const(html, "KVKK_TEXT")
    for section in ("1. Veri Sorumlusu", "Hukuki Sebepler", "4. Toplama Yöntemi", "5. Aktarım",
                    "6. Saklama Süreleri", "7. Çerezler", "8. Haklarınız (KVKK m.11)", "m.5/2-c", "Metin sürümü: 2026-11-v7"):
        assert section in kvkk, section
    assert "kabul ediyorum" not in kvkk.lower()   # aydınlatma metni rıza/kabul içermez (2026/347)
    # ADR-24: açık rıza hizmet şartı değil (yasal), ret yolu güncel anahtar adıyla yazılı
    assert "Bu onayı vermemeniz sadakat kartını kullanmanıza engel değildir." in kvkk
    assert 'Profil sekmesindeki "Kampanya Bildirimleri" anahtarıyla' in kvkk and "Profil › Bildirimler" not in kvkk
    marketing = _js_const(html, "MARKETING_TEXT")
    assert marketing.startswith("AÇIK RIZA VE TİCARİ ELEKTRONİK İLETİ ONAY METNİ") and "isteğe bağlıdır" in marketing
    assert "geri alabilirsiniz (6563 sayılı Kanun m.8)" in marketing and 'Profil › "Kampanya Bildirimleri"' in marketing
    # ADR-27: bildirimde düğme yok → metin de artık böyle bir düğmeden söz etmez
    assert "Bildirimleri kapat" not in marketing and "anahtarını kapatmanız yeterlidir; ret anında işlenir." in marketing
    assert "Metin sürümü: 2026-11-v4" in marketing


def test_controller_identity_appended_and_editable(client):
    client.post("/panel/login", json=MANAGER)
    ok = client.post("/api/admin/settings", json={"kvkk_controller": "Örnek Gıda Ltd. Şti. — Moda Cd. 1, Kadıköy/İstanbul",
                                                  "kvkk_contact": "kvkk@ornek.com"}).json()
    assert ok["status"] == "success"
    page = client.get("/mudur").text
    # Denetim Y-01/E-03: veri sorumlusu bilgileri müdür panelinden girilebilsin diye (eski kart kimlikleri yerine) YENİ bir
    # "Yasal Bilgiler / Veri Sorumlusu" kartı eklendi; "İşletme" sözcüğü ve eski kimlikler (card-legal, legal-form) kullanılmaz.
    assert 'id="card-legal-info"' in page and 'id="legal-controller"' in page and 'id="legal-contact"' in page
    assert 'id="card-legal"' not in page and "legal-form" not in page and "İşletme" not in page
    client.cookies.clear()
    kvkk = _js_const(client.get("/k/kart/").text, "KVKK_TEXT")
    assert "Veri Sorumlusu (unvan ve adres): Örnek Gıda Ltd. Şti." in kvkk and "Başvuru kanalı: kvkk@ornek.com" in kvkk


def test_old_default_texts_are_upgraded_but_custom_text_kept(client):
    from app.db import (_KVKK_TEXT_V1, _MARKETING_CONSENT_TEXT_V1, _MARKETING_CONSENT_TEXT_V2, _MARKETING_CONSENT_TEXT_V3,
                        KVKK_TEXT_V2, KVKK_TEXT_V3, KVKK_TEXT_V4, KVKK_TEXT_V5, KVKK_TEXT_V6, KVKK_TEXT_V7,
                        MARKETING_CONSENT_TEXT, init_db)
    assert KVKK_TEXT_V7 != KVKK_TEXT_V6 != KVKK_TEXT_V5 != KVKK_TEXT_V4 != KVKK_TEXT_V2 and KVKK_TEXT_V3 != KVKK_TEXT_V2   # .replace() zincirleri sessizce boşa düşmedi
    assert MARKETING_CONSENT_TEXT != _MARKETING_CONSENT_TEXT_V3   # v4 .replace() boşa düşmedi
    assert MARKETING_CONSENT_TEXT != _MARKETING_CONSENT_TEXT_V2 != _MARKETING_CONSENT_TEXT_V1
    for old_text, old_version, old_mkt in ((_KVKK_TEXT_V1, "2026-11-v1", _MARKETING_CONSENT_TEXT_V1),
                                           (KVKK_TEXT_V2, "2026-11-v2", _MARKETING_CONSENT_TEXT_V1),
                                           (KVKK_TEXT_V3, "2026-11-v3", _MARKETING_CONSENT_TEXT_V2),   # v3/v2 = ADR-23'te canlıya çıkan
                                           (KVKK_TEXT_V4, "2026-11-v4", _MARKETING_CONSENT_TEXT_V3),   # v4 + v3 = ADR-24/27 canlısı
                                           (KVKK_TEXT_V5, "2026-11-v5", _MARKETING_CONSENT_TEXT_V3),   # v5 = ADR-34 canlısı
                                           (KVKK_TEXT_V6, "2026-11-v6", _MARKETING_CONSENT_TEXT_V3)):  # v6 = ADR-36 canlısı
        with get_db(immediate=True) as conn:
            set_setting(conn, "kvkk_text", old_text)
            set_setting(conn, "kvkk_version", old_version)
            set_setting(conn, "marketing_consent_text", old_mkt)
            set_setting(conn, "marketing_consent_version", "2026-11-v2")
        init_db()
        with get_db() as conn:
            value = lambda k: conn.execute("SELECT value FROM settings WHERE key = ?", (k,)).fetchone()[0]  # noqa: E731
            assert value("kvkk_text") == KVKK_TEXT_V7 and value("kvkk_version") == "2026-11-v7"
            assert value("marketing_consent_text") == MARKETING_CONSENT_TEXT and value("marketing_consent_version") == "2026-11-v4"
    with get_db(immediate=True) as conn:
        set_setting(conn, "kvkk_text", "Özel metin")
        set_setting(conn, "marketing_consent_text", "Özel onay metni")
    init_db()
    with get_db() as conn:
        assert conn.execute("SELECT value FROM settings WHERE key='kvkk_text'").fetchone()[0] == "Özel metin"
        assert conn.execute("SELECT value FROM settings WHERE key='marketing_consent_text'").fetchone()[0] == "Özel onay metni"


# ------------------------------------------------------------------ 5. saklama / imha
def test_retention_anonymizes_inactive_and_purges_old_records(client):
    from app.services.retention import run_retention
    _register(client, "0532 830 00 01")   # eski müşteri
    client.cookies.clear()
    _register(client, "0532 830 00 02")   # aktif müşteri
    old = (get_istanbul_now() - timedelta(days=800)).isoformat()
    with get_db(immediate=True) as conn:
        ids = [r[0] for r in conn.execute("SELECT id FROM customers ORDER BY id")]
        conn.execute("UPDATE customers SET created_at = ?, pwa_seen_at = NULL WHERE id = ?", (old, ids[0]))
        conn.execute("UPDATE stamps SET created_at = ? WHERE customer_id = ?", (old, ids[0]))
        conn.execute("INSERT INTO push_subscriptions (customer_id, endpoint, p256dh, auth, created_at, active) "
                     "VALUES (NULL, 'https://push.example/old', 'x', 'y', ?, 0)", (old,))
        conn.execute("INSERT INTO audit_log (at, actor, action, detail) VALUES (?, 'x', 'eski', 'ip=1.1.1.1')",
                     ((get_istanbul_now() - timedelta(days=4 * 365)).isoformat(),))
        result = run_retention(conn)
    assert result["customers_anonymized"] == 1 and result["subscriptions_deleted"] == 1 and result["logs_deleted"] >= 1
    with get_db() as conn:
        rows = {r["id"]: r for r in conn.execute("SELECT id, deleted_at, phone_e164 FROM customers")}
        assert rows[ids[0]]["deleted_at"] and "silindi" in rows[ids[0]]["phone_e164"]
        assert rows[ids[1]]["deleted_at"] is None
        assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'eski'").fetchone()[0] == 0

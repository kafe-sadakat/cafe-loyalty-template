"""
Bildirimler (ADR-23): tek doğru tarayıcının gerçek izni. İzin 'granted' → ANINDA, tek seferlik +1 hediye damga
(Web Push aboneliği şart değil, ayrıca kurulur); 'denied'/'default' → damga yok. Rehber ve ayar yönlendirmesi yoktur.
ADR-24: yalnızca kampanya (ticari ileti) onayı Profil'den geri alınabilir/verilebilir (ETK m.8).
ADR-36 (Y-02): "Bildirimleri Aç" yalnızca tarayıcı iznini işler; kampanya onayı yazmaz (hediye damga izne bağlıdır).
"""
from starlette.testclient import TestClient

from app.db import get_db, set_setting
from app.main import app

CONSENT = {"kvkk_read": True, "explicit_consent": True}
STAFF = {"username": "kasa1", "password": "Test-Kasa-2026"}
SUB = {"p256dh": "BNcRdreALRFXTkOOUHK1EtK2wtaz5Ry4YfYCA_0QTpQ", "auth": "tBHItJI5svbpez7KI4CCXg"}


def _new_client():
    return TestClient(app, base_url="https://testserver")


def _stamp_code():
    from app.services.stamp_codes import create_stamp_code
    with get_db(immediate=True) as conn:
        return create_stamp_code(conn, created_by="test")["code"]


def _register(phone):
    c = _new_client()
    r = c.post("/api/claim", json={"claim_token": _stamp_code(), "phone": phone, **CONSENT}).json()
    assert r["status"] == "success", r
    return c, {"X-Device-Token": r["device_token"]}


def _login(creds):
    c = _new_client()
    assert c.post("/panel/login", json=creds).json()["status"] == "success"
    return c


def _perm(c, h, permission, consent=None):
    body = {"permission": permission}
    if consent is not None:
        body["consent"] = consent   # eski (önbellekteki) istemcilerin gönderdiği alan: ADR-36'dan beri sunucu YOK SAYAR
    return c.post("/api/customer/notification-permission", json=body, headers=h).json()


def _stamps(c, h):
    return c.get("/api/home-data", headers=h).json()["customer"]["current_stamps"]


def test_denied_or_default_permission_never_awards_a_stamp(client):
    c, h = _register("0532 920 00 01")
    before = _stamps(c, h)
    # Abonelik olsa bile tarayıcı izni 'denied' / 'default' ise 0 damga
    assert c.post("/api/push/subscribe", json={"endpoint": "https://push.example/perm1", **SUB}, headers=h).status_code == 200
    for perm in ("denied", "default", "bogus"):
        r = _perm(c, h, perm)
        assert r["status"] == "not_granted" and r["bonus_awarded"] is False, r
    me = c.get("/api/home-data", headers=h).json()["customer"]
    assert me["current_stamps"] == before and me["notif_bonus_claimed"] == 0 and me["marketing_consent"] is False


def test_granted_awards_once_instantly_even_without_subscription(client):
    """ADR-23: 'İzin Ver' → ANINDA +1; Web Push aboneliği arka planda ayrıca kurulur, damgayı bekletmez."""
    c, h = _register("0532 920 00 02")
    before = _stamps(c, h)
    r = _perm(c, h, "granted")
    assert r["status"] == "success" and r["bonus_awarded"] is True and r["push_active"] is False, r
    assert r["current_stamps"] == before + 1 and _stamps(c, h) == before + 1
    r2 = _perm(c, h, "granted")
    assert r2["status"] == "success" and r2["bonus_awarded"] is False and r2["bonus_status"] == "already_claimed", r2
    assert _stamps(c, h) == before + 1
    # İzin bilgisi taşımayan eski uç hâlâ gerçek abonelik ister (izin kanıtı yok)
    assert c.post("/api/claim-notif-bonus", headers=h).json()["status"] == "no_subscription"


def test_notification_permission_never_reopens_revoked_consent(client):
    """İzin eşitlemesi de, "Bildirimleri Aç" da (eski istemci `consent: true` yollasa bile) ret vermiş müşterinin onayını geri açmaz."""
    c, h = _register("0532 920 00 07")
    c.post("/api/me/marketing-consent", json={"consent": True}, headers=h)
    c.post("/api/push/unsubscribe", json={"endpoint": None}, headers=h)   # eski arayüzden verilmiş ret
    r = _perm(c, h, "granted")   # izin tarayıcıda hâlâ açık: damga verilir ama onay YAZILMAZ
    assert r["status"] == "success" and r["bonus_awarded"] is True and r["marketing_consent"] is False, r
    assert c.get("/api/home-data", headers=h).json()["customer"]["marketing_consent"] is False
    # ADR-36: "Bildirimleri Aç" (consent: true gönderilse bile) kampanya onayı YAZMAZ; yalnızca Profil anahtarı yazar
    assert _perm(c, h, "granted", consent=True)["marketing_consent"] is False
    assert c.get("/api/home-data", headers=h).json()["customer"]["marketing_consent"] is False
    assert c.post("/api/me/marketing-consent", json={"consent": True}, headers=h).json()["marketing_consent"] is True


def test_granted_with_subscription_awards_once_and_does_not_record_consent(client):
    c, h = _register("0532 920 00 03")
    before = _stamps(c, h)
    assert c.post("/api/push/subscribe", json={"endpoint": "https://push.example/perm3", **SUB}, headers=h).status_code == 200
    r = _perm(c, h, "granted", consent=True)   # eski istemci consent: true gönderse de ADR-36'da yok sayılır
    assert r["status"] == "success" and r["bonus_awarded"] is True and r["push_bonus_awarded"] is True, r
    assert r["current_stamps"] == before + 1 and r["marketing_consent"] is False
    me = c.get("/api/home-data", headers=h).json()["customer"]
    # hediye damga yalnızca izne bağlıdır; kampanya (ticari ileti) onayı yazılmadı
    assert me["notif_bonus_claimed"] == 1 and me["marketing_consent"] is False
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'marketing_consent_given'").fetchone()[0] == 0
        assert conn.execute("SELECT marketing_consent_channel FROM customers").fetchone()[0] is None
    assert "in_app_push_enabled" not in me

    # İkinci kez (push_bonus_awarded == true): başarılı ama ek damga yok
    r2 = _perm(c, h, "granted")
    assert r2["status"] == "success" and r2["bonus_awarded"] is False and r2["bonus_status"] == "already_claimed", r2
    assert _stamps(c, h) == before + 1

    staff = _login(STAFF)
    cust = staff.post("/api/staff/customer-lookup", json={"q": "532 920 00 03"}).json()["customers"][0]
    # abonelik var ama kampanya onayı yok → kasada "izinli, onay yok"; hediye yine de verilmiştir
    assert cust["current_status"]["push_active"] is False and cust["current_status"]["push_no_consent"] is True
    assert cust["push_bonus_status"] == "received"


def test_notification_permission_requires_identified_customer(client):
    r = _new_client().post("/api/customer/notification-permission", json={"permission": "granted"}).json()
    assert r["status"] == "device_not_found"


def test_marketing_consent_endpoint_kept_for_compat_and_never_awards_a_stamp(client):
    """/api/me/marketing-consent (Profil › Kampanya Bildirimleri anahtarı, ADR-24) onayı açar/kapatır,
    hediye damga vermez (damga yalnızca 'granted' izinle)."""
    c, h = _register("0532 920 00 04")
    before = _stamps(c, h)
    r = c.post("/api/me/marketing-consent", json={"consent": True}, headers=h).json()
    assert r == {"status": "success", "marketing_consent": True}
    me = c.get("/api/home-data", headers=h).json()["customer"]
    assert me["marketing_consent"] is True and me["current_stamps"] == before and me["notif_bonus_claimed"] == 0
    # Aynı durumu tekrar göndermek ikinci bir onay kaydı üretmez (ilk onayın ispat kaydı korunur)
    c.post("/api/me/marketing-consent", json={"consent": True}, headers=h)
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'marketing_consent_given'").fetchone()[0] == 1

    # Abonelik olmadığı için kasada "Bildirim: Açık (push)" sayılmaz
    staff = _login(STAFF)
    cust = staff.post("/api/staff/customer-lookup", json={"q": "532 920 00 04"}).json()["customers"][0]
    assert cust["current_status"]["push_active"] is False

    # KAPAT (anahtar): uç adresi olmadan tüm abonelikler pasif + onay geri alınır (ret)
    assert c.post("/api/push/unsubscribe", json={"endpoint": None}, headers=h).json()["status"] == "success"
    assert c.get("/api/home-data", headers=h).json()["customer"]["marketing_consent"] is False
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'marketing_consent_revoked'").fetchone()[0] == 1


def test_marketing_consent_requires_identified_customer(client):
    r = _new_client().post("/api/me/marketing-consent", json={"consent": True}).json()
    assert r["status"] == "device_not_found"


def test_fake_in_app_notification_endpoints_are_gone(client):
    c, h = _register("0532 920 00 05")
    assert c.post("/api/customer/enable-notifications", json={}, headers=h).status_code in (404, 405)
    assert c.get("/api/me/inbox", headers=h).status_code in (404, 405)


def test_no_subscription_means_notification_off_in_cashier_proof(client):
    _register("0532 920 00 06")
    staff = _login(STAFF)
    cust = staff.post("/api/staff/customer-lookup", json={"q": "532 920 00 06"}).json()["customers"][0]
    assert cust["current_status"]["push_active"] is False and cust["push_bonus_status"] == "none"


def test_customer_page_reflects_real_permission_and_has_no_off_controls(client):
    page = client.get("/k/kart/").text
    # Zorlamayan teklif kartı + tarayıcının kendi izin penceresi
    assert 'id="home-notif-offer"' in page
    assert "Bildirimleri aç, +1 Hediye Damga kazan" in page and "+1 Hediye Damga" in page
    # ADR-36: "Bildirimleri Aç" kampanya onayı DEĞİLDİR; "kabul edersin / kabul etmiş olursun" cümleleri yok, hediye onaydan bağımsız
    assert "kampanya bildirimi almayı kabul edersin" not in page and "kabul etmiş olursun" not in page
    assert "kampanya bildirimi onayına bağlı değildir" in page and "kampanya bildirimi onayı vermiş olmazsın" in page
    assert "/api/customer/notification-permission" in page and "Şimdi değil" in page
    # ADR-23: tek doğru tarayıcının gerçek izni; durum kutusu + üst bar zili; açıkken tıklanamaz "✅ Açık" rozeti
    assert "function isNotificationReallyEnabled" in page and 'notifPermission() === "granted"' in page
    assert "function syncNotificationStatusUI" in page and "function requestNotificationAndRewardStamp" in page
    assert 'id="inAppNotificationBanner"' in page and 'id="enableNotifBtn"' in page and "js-notif-bell" in page
    assert '"✅ Açık"' in page and "is-granted" in page and '"Bildirimler Açık"' in page and "btn.disabled = on" in page
    assert "function pollInAppNotifications" in page and "visibilitychange" in page and "watchNotificationPermission" in page
    # İzin → ANINDA damga → abonelik arka planda (süre sınırlı): abonelik askıda kalsa da damga gecikmez
    req = page.split("async function requestNotificationAndRewardStamp", 1)[1][:1500]
    assert req.index("requestNotifPermission()") < req.index("claimNotificationRewardStamp()") \
        < req.index("savePushSubscriptionInBackground()")
    assert "PUSH_SUBSCRIBE_TIMEOUT_MS" in page and "function withTimeout" in page
    # ADR-36: hiçbir bildirim yolu kampanya onayı göndermez (ne izin isteği, ne abonelik)
    assert "consent: !!userAction" not in page and "marketing_consent: !!withConsent" not in page
    assert "savePushSubscriptionInBackground(true)" not in page and "claimNotificationRewardStamp(true)" not in page
    # Karşılama bildirimi yalnızca damga mesajı (kapatma metni / düğmesi yok)
    assert "Tebrikler! Bildirimleri açtığınız için damganız hesabınıza tanımlandı." in page
    # Kapatma / abonelikten çıkma işlevi, eski aç-kapa anahtarı, rehberler ve ayar yönlendirmeleri tamamen kalktı
    for gone in ('id="notif-help-modal"', "Bildirimler Nasıl Açılır?", "İzni Verdim, Damgamı Kontrol Et",
                 "recheckNotifPermission", "openNotifHelp", "Olmadı mı?", "help-acc", "renderHelpAccordions",
                 "Site Ayarları", "Ayarlar → Safari", "kilit (🔒)", "(İsteğe bağlı)</em>", 'openIosGuide("notif")',
                 "awaitPermission", "handleNotificationToggle", "turnNotificationsOff", "notification-optin-card",
                 "js-notif-toggle", 'id="push-switch"', '"in_app_notifications"', "/api/push/unsubscribe",
                 "İsteğe Bağlı", "Abonelikten Çık", "Bildirimi Kapat"):
        assert gone not in page, gone
    # ADR-24 (ETK m.8): kampanya (ticari ileti) onayını geri alma anahtarı Profil'de; tarayıcı iznine ve damgaya dokunmaz
    assert 'id="campaign-consent-switch"' in page and "function handleCampaignConsentToggle" in page
    assert "/api/me/marketing-consent" in page and "geri alabilirsin" in page
    # Modallar <body> kökünde, Tailwind'e bağlı olmayan en üst katmanda; sadakat kartı kendi yığın bağlamında
    assert '.modal-overlay, [role="dialog"] { position: fixed !important; inset: 0 !important; z-index: 99999 !important; }' in page
    assert "isolation: isolate" in page
    assert page.count('class="modal-overlay') == 3 and "z-[125]" not in page.replace("z-[125] gibi", "")
    assert page.count('class="loyalty-card') == 2
    # Sahte/alternatif kanallar tamamen kalktı
    for gone in ("live-banner", "enable-notifications", "/api/me/inbox", "navigator.vibrate", "AudioContext",
                 "in_app_push_enabled", "uygulama içi bildirim", "Uygulama içi bildirim"):
        assert gone not in page, gone


def test_notification_ui_stays_green_even_if_accent_color_is_orange(client):
    """Canlıda accent_color turuncu (#C05621) ve yalnızca theme-color'da kullanılır: onay kutuları ve
    "Bildirimler Açık" göstergesi yine uygulamanın yeşilinde (#164e3f) kalır (ADR-23: "aktif/yeşil")."""
    with get_db(immediate=True) as conn:
        set_setting(conn, "accent_color", "#C05621")
    page = client.get("/k/kart/").text
    assert page.count("#C05621") == 1 and '<meta name="theme-color" content="#C05621" />' in page
    assert ".notif-status-box.is-active { background: #eaf5f0; border-color: #164e3f; }" in page
    assert "accent-color: #164e3f" in page and ".notif-action-btn { background: #164e3f;" in page


def test_customer_page_is_cross_browser_hardened(client):
    page = client.get("/k/indir").text
    assert '"serviceWorker" in navigator' in page and '"PushManager" in window' in page and '"Notification" in window' in page
    assert "function requestNotifPermission" in page and "function notifPermission" in page
    # Doğrudan (korumasız) izin çağrısı yalnızca sarmalayıcının içinde kalır
    assert page.count("Notification.requestPermission(") == 1
    assert "maxTouchPoints" in page and 'id="ios-guide-modal"' in page
    assert "new Uint8Array(raw.length)" in page
    # ADR-28: her tarayıcıda doğru yol — iPhone tarayıcı sekmesi → ana ekrana ekleme kılavuzu (Apple: web bildirimi yalnızca
    # ana ekran uygulamasında), uygulama içi tarayıcı → "Tarayıcıda Aç", engellenmiş izin → tek satır bilgi, Chrome'un sessiz
    # izin arayüzü → adres çubuğu ipucu; yükleme olayı <head>'de yakalanır, gecikirse beklenir, yüklüyse "zaten yüklü"
    assert "function browserEnv" in page and 'openInstallSheet("notif")' in page and 'openBrowserSheet("notif")' in page
    assert "notificationBlockedText()" in page and "NOTIF_QUIET_HINT" in page and "notifPromptPending" in page
    assert "beforeinstallprompt" in page.split("</head>", 1)[0] and "waitForInstallPrompt(2500)" in page
    assert "getInstalledRelatedApps" in page and "intent://" in page and 'openInstallSheet("install")' in page

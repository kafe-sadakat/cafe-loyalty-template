"""iPhone bildirim / "Uygulamayı Yükle" düzeltmesi: dokunuşta hiçbir şey beklenmeden kılavuz açılır, iPhone'da
sekme / uygulama içi tarayıcı / engelli izin için doğru yönlendirme gösterilir, manifest iki öneki de bilir."""


def _page(client):
    return client.get("/k/kart/").text


def test_install_app_shows_ui_before_any_wait(client):
    page = _page(client)
    assert "await waitForInstallPrompt" not in page
    assert "function openInstallGuide" in page
    assert "function runInstallPrompt" in page
    assert "Şimdi Yükle" in page
    assert "waitForInstallPrompt(2500)" in page
    body = page.split("function installApp", 1)[1]
    assert "openInstallGuide()" in body
    assert "await " not in body[: body.index("openInstallGuide()")]


def test_ios_guides_and_blocked_notification_flow(client):
    page = _page(client)
    assert '"x-safari-"' in page
    assert "function showNotificationBlocked" in page
    assert "function showNotificationNotGranted" in page
    assert "Bildirimlere İzin Ver" in page
    assert "listede yoksa" in page
    assert "Web Uygulaması Olarak Aç" in page
    assert "Uygulamada numaranla bir kez daha giriş yapman gerekebilir." in page
    assert "m1_ios_notif_nudge" in page
    assert page.count('class="modal-overlay') == 3   # yeni pencere eklenmez; mevcut kılavuz yeniden kullanılır


def test_manifest_lists_both_base_paths(client):
    apps = client.get("/k/manifest.webmanifest").json()["related_applications"]
    urls = [a["url"] for a in apps]
    assert len(urls) >= 2 and len(set(urls)) == len(urls)
    assert any(u.endswith("/k/manifest.webmanifest") for u in urls)
    assert any(u.endswith("/manifest.webmanifest") and not u.endswith("/k/manifest.webmanifest") for u in urls)

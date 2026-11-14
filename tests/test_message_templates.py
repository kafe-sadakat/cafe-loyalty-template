"""Kampanya ve bildirim şablonları (müdür paneli): tohumlama, ekle / düzenle / sil, doğrulama, yetki, denetim kaydı."""
from app.db import get_db, init_db
from app.services.templates import MAX_PER_KIND

MANAGER = {"username": "mudur", "password": "Test-Mudur-2026"}


def _manager(client):
    client.cookies.clear()
    assert client.post("/panel/login", json=MANAGER).json()["status"] == "success"
    return client


def _templates(client, kind=None):
    url = "/api/admin/templates" + (f"?kind={kind}" if kind else "")
    return client.get(url).json()["templates"]


def test_default_templates_are_seeded_once(client):
    _manager(client)
    tpls = _templates(client)
    assert [t["kind"] for t in tpls].count("campaign") == 3 and [t["kind"] for t in tpls].count("push") == 3
    assert any("Hafta Sonu" in t["label"] for t in tpls) and all(t["title"] and t["body"] for t in tpls)
    assert all("{{" not in t["body"] for t in tpls)   # işletme adı tohumlama anında gerçek metne işlenir
    # overview yanıtı da şablonları taşır (panel tek istekle çizer)
    assert len(client.get("/api/admin/overview").json()["templates"]) == 6


def test_deleted_default_template_does_not_come_back_after_restart(client):
    _manager(client)
    victim = _templates(client, "push")[0]
    assert client.post(f"/api/admin/templates/{victim['id']}/delete").json()["status"] == "success"
    init_db()   # uygulama yeniden başlar
    assert victim["id"] not in [t["id"] for t in _templates(client)] and len(_templates(client, "push")) == 2


def test_create_update_delete_cycle_with_audit(client):
    _manager(client)
    r = client.post("/api/admin/templates/create", json={
        "kind": "push", "label": "  Yağmurlu   gün  ", "title": " Kahve Dükkanım ", "body": " Yağmur var, sıcak bir latte iyi gider ☔ "}).json()
    assert r["status"] == "success"
    tpl = r["template"]
    assert tpl["label"] == "Yağmurlu gün" and tpl["title"] == "Kahve Dükkanım" and tpl["body"].startswith("Yağmur var")
    assert tpl["id"] in [t["id"] for t in r["templates"]]
    assert _templates(client, "push")[-1]["id"] == tpl["id"]   # sona eklenir

    upd = client.post(f"/api/admin/templates/{tpl['id']}/update", json={
        "kind": "campaign", "label": "", "title": "Yeni başlık", "body": "Yeni metin"}).json()
    assert upd["status"] == "success"
    # tür sonradan değişmez (kind istekten alınmaz); etiket boşsa başlıktan türer
    assert upd["template"]["kind"] == "push" and upd["template"]["label"] == "Yeni başlık" and upd["template"]["body"] == "Yeni metin"

    assert client.delete(f"/api/admin/templates/{tpl['id']}").json()["status"] == "success"
    assert tpl["id"] not in [t["id"] for t in _templates(client)]
    assert client.post(f"/api/admin/templates/{tpl['id']}/delete").json()["status"] == "error"   # artık yok
    assert client.post(f"/api/admin/templates/{tpl['id']}/update", json={"kind": "push", "title": "x", "body": "y"}).json()["status"] == "error"

    with get_db() as conn:
        actions = [r[0] for r in conn.execute("SELECT action FROM audit_log WHERE action LIKE 'template_%' ORDER BY id")]
    assert actions == ["template_created", "template_updated", "template_deleted"]


def test_template_validation(client):
    _manager(client)
    post = lambda **kw: client.post("/api/admin/templates/create", json=kw).json()   # noqa: E731
    assert post(kind="push", title="", body="x")["status"] == "error"
    assert post(kind="push", title="x", body="  ")["status"] == "error"
    assert post(kind="bogus", title="x", body="y")["status"] == "error"
    assert post(kind="push", title="t" * 81, body="y")["status"] == "error"      # bildirim başlığı ≤ 80
    assert post(kind="push", title="t", body="b" * 241)["status"] == "error"     # bildirim metni ≤ 240
    assert post(kind="campaign", title="t" * 121, body="y")["status"] == "error"
    assert post(kind="campaign", title="t" * 120, body="b" * 1000)["status"] == "success"
    long_label = post(kind="push", label="L" * 90, title="t", body="b")["template"]["label"]
    assert len(long_label) == 60 and long_label.endswith("…")


def test_template_count_is_limited_per_kind(client):
    _manager(client)
    for i in range(MAX_PER_KIND - 3):   # 3 hazır şablon zaten var
        assert client.post("/api/admin/templates/create", json={"kind": "push", "title": f"t{i}", "body": "b"}).json()["status"] == "success"
    full = client.post("/api/admin/templates/create", json={"kind": "push", "title": "fazla", "body": "b"}).json()
    assert full["status"] == "error" and str(MAX_PER_KIND) in full["message"]
    # kampanya şablonlarının sınırı ayrı sayılır
    assert client.post("/api/admin/templates/create", json={"kind": "campaign", "title": "ok", "body": "b"}).json()["status"] == "success"


def test_template_endpoints_require_manager(client):
    client.cookies.clear()
    assert client.get("/api/admin/templates").status_code == 401
    assert client.post("/api/admin/templates/create", json={"kind": "push", "title": "x", "body": "y"}).status_code == 401
    assert client.post("/api/admin/templates/1/delete").status_code == 401


def test_panel_renders_template_editor_without_hardcoded_templates(client):
    _manager(client)
    page = client.get("/mudur").text
    assert 'id="campaign-templates-bar"' in page and 'id="push-templates-list"' in page and 'id="tpl-modal"' in page
    assert "openTemplateEditor('campaign')" in page and "openTemplateEditor('push')" in page
    assert "btn-template" not in page and "btn-push-tpl" not in page   # sabit düğmeler kalktı
    for fn in ("function renderTemplates", "function saveTemplate", "function deleteTemplateById", "function onTemplateClick"):
        assert fn in page, fn

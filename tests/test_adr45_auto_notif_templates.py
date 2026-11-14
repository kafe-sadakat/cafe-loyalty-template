"""
ADR-45: otomatik bildirimlerin (damga eklendi / ödül hazır) başlık ve metni müdür panelinden düzenlenir; boş bırakılan
alan varsayılana döner. Kampanya / bildirim şablonlarında "Düzenle" düğmesi yazıyla görünür.
"""
import json

from starlette.testclient import TestClient

from app.db import get_db, set_setting
from app.main import app
from app.services import push as push_service
from app.services.push import AUTO_NOTIF_DEFAULTS, auto_notification_text
from app.services.stamp_codes import create_stamp_code

MANAGER = {"username": "mudur", "password": "Test-Mudur-2026"}
CONSENT = {"kvkk_read": True, "explicit_consent": True}
VALUES = {"isletme": "Kafe X", "damga": 2, "hedef": 5, "kalan": 3, "odul": "Bedava Latte"}


def _login():
    c = TestClient(app, base_url="https://testserver")
    assert c.post("/panel/login", json=MANAGER).json()["status"] == "success"
    return c


def test_placeholders_defaults_and_unknown_braces():
    assert auto_notification_text({}, "notif_stamp_body", VALUES) == "Damgan eklendi (2/5), ödüle 3 kaldı!"
    assert auto_notification_text({"notif_stamp_title": "   "}, "notif_stamp_title", VALUES) == "Kafe X - Damgan eklendi ☕"
    custom = {"notif_reward_body": "{odul} seni bekliyor {bilinmeyen} {damga}/{hedef}"}
    assert auto_notification_text(custom, "notif_reward_body", VALUES) == "Bedava Latte seni bekliyor {bilinmeyen} 2/5"


def test_manager_edits_texts_and_customer_receives_them(client, monkeypatch):
    mgr = _login()
    ov = mgr.get("/api/admin/overview").json()
    assert ov["auto_notif_defaults"] == AUTO_NOTIF_DEFAULTS
    assert ov["settings"]["notif_stamp_title"] == AUTO_NOTIF_DEFAULTS["notif_stamp_title"]     # boşken varsayılan
    r = mgr.post("/api/admin/settings", json={
        "notif_stamp_title": "☕ {isletme}: damgan geldi",
        "notif_stamp_body": "Kartında {damga}/{hedef}; {odul} için {kalan} kaldı " + "x" * 400,
        "notif_reward_title": "",                    # boş → varsayılan
        "notif_reward_body": "Ödülün {odul} kasada seni bekliyor 🎉",
    }).json()
    assert r["status"] == "success"
    ov = mgr.get("/api/admin/overview").json()["settings"]
    assert ov["notif_stamp_title"] == "☕ {isletme}: damgan geldi" and len(ov["notif_stamp_body"]) == 240
    assert ov["notif_reward_title"] == AUTO_NOTIF_DEFAULTS["notif_reward_title"]

    monkeypatch.setenv("VAPID_PUBLIC_KEY", "BEl62iUYgUivxIkv69yViEuiBIa-Ib9-SkvMeAtA3LFgDzkrxZJjSgSnfckjBJuBkr3qBUYIHBQFLXYp5Nksh8U")
    monkeypatch.setenv("VAPID_PRIVATE_KEY", "UUxI4O8-FbRouAevSmBQ6o18hgE4nSG3qwvJTfKc-ls")
    monkeypatch.setenv("VAPID_SUBJECT", "mailto:test@example.com")
    sent = []
    monkeypatch.setattr(push_service, "webpush", lambda subscription_info, data, **kw: sent.append(json.loads(data)) or True)
    with get_db(immediate=True) as conn:
        set_setting(conn, "daily_limit", "10")
        set_setting(conn, "stamp_target", "3")
        set_setting(conn, "reward_name", "Bedava Latte")
    client.post("/api/me/identify", json={**CONSENT, "phone": "0532 444 11 23"})
    client.post("/api/push/subscribe", json={
        "endpoint": "https://fcm.googleapis.com/fcm/send/adr45", "p256dh": "BNcRdreALRFXTkOOUHK1EtK2wtaz5Ry4YfYCA_0QTpQ",
        "auth": "tBHItJI5svbpez7KI4CCXg",
    })
    for _ in range(2):
        with get_db(immediate=True) as conn:
            code = create_stamp_code(conn, created_by="test")["code"]
        assert client.post("/api/claim", json={"claim_token": code}).json()["status"] == "success"
    assert sent[0]["title"].endswith(": damgan geldi") and sent[0]["body"].startswith("Kartında 2/3; Bedava Latte için 1 kaldı")
    assert sent[1]["title"].endswith("Ödülün hazır! 🎁")            # boş bırakılan başlık varsayılan
    assert sent[1]["body"] == "Ödülün Bedava Latte kasada seni bekliyor 🎉"


def test_panel_shows_auto_notif_editor_and_visible_edit_buttons(client):
    page = _login().get("/mudur").text
    for needle in ('id="auto-notif-form"', "Otomatik Bildirimler", 'id="an-notif_stamp_title"', 'id="an-notif_reward_body"',
                   "data-auto-notif-reset", "saveAutoNotif", "<span>Düzenle</span>"):
        assert needle in page, needle

"""
Ödüllü kampanyalar (ADR-26): hazır "Google'da yorum" görevi, doğrulama (onaysız damga yok), katılımcı listesi
(kampanya adıyla), aç / kapa, yapmayanlara bildirim, kasada onay, tek seferlik ödül, KVKK silme, rapor.
"""
import io
import json

from openpyxl import load_workbook
from starlette.testclient import TestClient

from app.db import get_db, init_db
from app.main import app
from app.services import push as push_service
from app.utils import get_istanbul_now

CONSENT = {"kvkk_read": True, "explicit_consent": True}
MANAGER = {"username": "mudur", "password": "Test-Mudur-2026"}
STAFF = {"username": "kasa1", "password": "Test-Kasa-2026"}
SUB = {"p256dh": "BNcRdreALRFXTkOOUHK1EtK2wtaz5Ry4YfYCA_0QTpQ", "auth": "tBHItJI5svbpez7KI4CCXg"}
VAPID = {
    "VAPID_PUBLIC_KEY": "BEl62iUYgUivxIkv69yViEuiBIa-Ib9-SkvMeAtA3LFgDzkrxZJjSgSnfckjBJuBkr3qBUYIHBQFLXYp5Nksh8U",
    "VAPID_PRIVATE_KEY": "UUxI4O8-FbRouAevSmBQ6o18hgE4nSG3qwvJTfKc-ls",
    "VAPID_SUBJECT": "mailto:test@example.com",
}
REVIEW_URL = "https://g.page/r/CbTestReview123/review"


def _new_client():
    return TestClient(app, base_url="https://testserver")


def _login(creds):
    c = _new_client()
    assert c.post("/panel/login", json=creds).json()["status"] == "success"
    return c


def _customer(phone, marketing=False):
    from app.services.stamp_codes import create_stamp_code
    with get_db(immediate=True) as conn:
        code = create_stamp_code(conn, created_by="test")["code"]
    c = _new_client()
    r = c.post("/api/claim", json={"claim_token": code, "phone": phone, **CONSENT, "marketing_consent": marketing}).json()
    assert r["status"] == "success", r
    c.headers["X-Device-Token"] = r["device_token"]
    return c, r


def _create(mgr, **kw):
    body = {"title": "Instagram'da Takip Et", "description": "Takip et, +1 damga kazan", "badge": "ÖDÜLLÜ",
            "reward_stamps": 1, "action_url": "https://instagram.com/kahvedukkanim", "action_label": "Takip Et", **kw}
    r = mgr.post("/api/admin/campaigns/create", json=body).json()
    assert r["status"] == "success", r
    return r["campaign"]


def _review(mgr, camp_id, customer_id, action="approve", reason=""):
    return mgr.post(f"/api/admin/campaigns/{camp_id}/participants/{customer_id}/review",
                    json={"action": action, "reason": reason}).json()


def _stamps(customer_id):
    with get_db() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM stamps WHERE customer_id = ? AND voided_at IS NULL", (customer_id,)
        ).fetchone()[0]


def _home_campaign(c, camp_id):
    return next((x for x in c.get("/api/home-data").json()["campaigns"] if x["id"] == camp_id), None)


def test_google_review_campaign_is_seeded_once_and_inactive(client):
    mgr = _login(MANAGER)
    seeded = [c for c in mgr.get("/api/admin/overview").json()["campaigns"] if c["reward_stamps"] > 0]
    assert len(seeded) == 1
    g = seeded[0]
    assert "Google" in g["title"] and g["active"] == 0 and g["reward_stamps"] == 1 and g["badge"] == "ÖDÜLLÜ"
    assert g["action_url"] is None and g["action_label"] == "Google'da Yorum Yap"
    assert "Puanın ne olursa olsun" in g["description"]          # ödül puana (olumlu yoruma) bağlı değil
    assert g["stats"] == {"joined": 0, "pending": 0, "approved": 0, "rejected": 0}
    assert all(c["id"] != g["id"] for c in _new_client().get("/api/home-data").json()["campaigns"])   # pasif: görünmez
    # müdür silerse yeniden başlatmada geri gelmez
    assert mgr.post(f"/api/admin/campaigns/{g['id']}/delete").json()["status"] == "success"
    init_db()
    assert not [c for c in mgr.get("/api/admin/overview").json()["campaigns"] if c["reward_stamps"] > 0]


def test_reward_field_validation_and_legacy_update(client):
    mgr = _login(MANAGER)
    post = lambda **kw: mgr.post("/api/admin/campaigns/create", json={"title": "x", "description": "y", **kw}).json()  # noqa: E731
    assert post(reward_stamps=1, action_url="http://g.page/r/x")["status"] == "error"        # yalnızca https
    assert post(reward_stamps=1, action_url="javascript:alert(1)")["status"] == "error"
    assert post(reward_stamps=1, action_url="https://g.page/a b")["status"] == "error"
    assert post(reward_stamps=6)["status"] == "error" and post(reward_stamps=-1)["status"] == "error"
    ok = post(reward_stamps=2, action_url=REVIEW_URL, action_label="  Google'da   Yorum Yap ")
    assert ok["status"] == "success"
    assert ok["campaign"]["reward_stamps"] == 2 and ok["campaign"]["action_label"] == "Google'da Yorum Yap"
    plain = post(reward_stamps=0, action_url=REVIEW_URL, action_label="x")["campaign"]          # düz duyuru: görev alanı tutulmaz
    assert plain["reward_stamps"] == 0 and plain["action_url"] is None and plain["action_label"] is None
    # ödül alanı göndermeyen (eski) istemcinin güncellemesi görevi bozmaz
    cid = ok["campaign"]["id"]
    upd = mgr.post(f"/api/admin/campaigns/{cid}/update", json={"title": "Yeni ad", "description": "y"}).json()
    assert upd["status"] == "success" and upd["campaign"]["reward_stamps"] == 2 and upd["campaign"]["action_url"] == REVIEW_URL
    # düz duyuruya katılım / gönderim yok
    c, _ = _customer("0532 900 00 20")
    assert c.post(f"/api/campaigns/{plain['id']}/join", json={}).json()["status"] == "error"


def test_join_and_submit_never_give_a_stamp_until_verified(client):
    mgr = _login(MANAGER)
    camp = _create(mgr, title="⭐ Google'da Yorum", action_url=REVIEW_URL, action_label="Google'da Yorum Yap")
    c, me = _customer("0532 900 00 01")
    cid = me["customer_id"]
    before = _stamps(cid)
    hc = _home_campaign(c, camp["id"])
    assert hc["my_status"] == "none" and hc["action_url"] == REVIEW_URL and hc["reward_stamps"] == 1
    assert "my_status" not in _home_campaign(_new_client(), camp["id"])       # misafire katılım durumu yok

    assert c.post(f"/api/campaigns/{camp['id']}/join", json={}).json()["my_status"] == "joined"
    assert c.post(f"/api/campaigns/{camp['id']}/submit", json={"proof_name": " "}).json()["status"] == "error"
    r = c.post(f"/api/campaigns/{camp['id']}/submit", json={"proof_name": "  Ayşe   K. "}).json()
    assert r["status"] == "success" and r["my_status"] == "pending" and r["my_proof_name"] == "Ayşe K."
    assert _stamps(cid) == before                                             # "Yaptım" demek damga VERMEZ

    part = mgr.get(f"/api/admin/campaigns/{camp['id']}/participants").json()  # kampanya adıyla katılımcı listesi
    assert part["campaign"]["title"] == "⭐ Google'da Yorum" and part["stats"]["pending"] == 1
    row = part["participants"][0]
    assert (row["customer_id"], row["status"], row["proof_name"], row["formatted_phone"]) == (cid, "pending", "Ayşe K.", "532 900 00 01")

    # onay → damga; günlük limit (testte 1, hoş geldin damgasıyla dolu) kampanya ödülünü engellemez
    ok = _review(mgr, camp["id"], cid)
    assert ok["status"] == "success" and ok["participant_status"] == "approved" and ok["stats"]["approved"] == 1
    assert _stamps(cid) == before + 1
    with get_db() as conn:
        s = conn.execute("SELECT source, actor, note FROM stamps WHERE customer_id = ? ORDER BY id DESC LIMIT 1", (cid,)).fetchone()
        actions = [r[0] for r in conn.execute("SELECT action FROM audit_log WHERE action LIKE 'campaign_%' ORDER BY id")]
    assert (s["source"], s["actor"], s["note"]) == ("campaign", "mudur", "⭐ Google'da Yorum")
    assert actions[-3:] == ["campaign_joined", "campaign_submitted", "campaign_reward_approved"]

    # ikinci onay / yeniden gönderim ikinci damga vermez
    assert _review(mgr, camp["id"], cid)["status"] == "already_approved"
    assert c.post(f"/api/campaigns/{camp['id']}/submit", json={"proof_name": "Ayşe K."}).json()["status"] == "already_approved"
    assert _stamps(cid) == before + 1
    assert _home_campaign(c, camp["id"])["my_status"] == "approved"


def test_rejected_customer_can_resubmit_and_gets_reminders_again(client):
    mgr = _login(MANAGER)
    camp = _create(mgr)
    c, me = _customer("0532 900 00 02", marketing=True)
    cid = me["customer_id"]
    aud = f"campaign:{camp['id']}"
    with get_db() as conn:
        assert cid in push_service.get_campaign_target_customer_ids(conn, aud)          # yapmadı → hatırlatılır
    assert c.post(f"/api/campaigns/{camp['id']}/submit", json={"proof_name": "Murat"}).json()["my_status"] == "pending"
    with get_db() as conn:
        assert cid not in push_service.get_campaign_target_customer_ids(conn, aud)      # bekleyen hatırlatılmaz
    assert _review(mgr, camp["id"], 999999, "reject")["status"] == "error"
    rej = _review(mgr, camp["id"], cid, "reject", "Yorum bulunamadı")
    assert rej["status"] == "success" and rej["participant_status"] == "rejected"
    hc = _home_campaign(c, camp["id"])
    assert hc["my_status"] == "rejected" and hc["my_reject_reason"] == "Yorum bulunamadı"
    with get_db() as conn:
        assert cid in push_service.get_campaign_target_customer_ids(conn, aud)          # reddedilen yeniden denenebilir
    before = _stamps(cid)
    assert c.post(f"/api/campaigns/{camp['id']}/submit", json={"proof_name": "Murat Y."}).json()["my_status"] == "pending"
    assert _stamps(cid) == before


def test_paused_campaign_takes_no_new_entries_but_pending_can_still_be_approved(client):
    mgr = _login(MANAGER)
    camp = _create(mgr)
    c, me = _customer("0532 900 00 03")
    cid = me["customer_id"]
    c.post(f"/api/campaigns/{camp['id']}/submit", json={"proof_name": "Zeynep"})
    assert mgr.post(f"/api/admin/campaigns/{camp['id']}/toggle").json()["campaign"]["active"] == 0
    assert _home_campaign(c, camp["id"]) is None                                        # pasif: uygulamada görünmez
    other, _ = _customer("0532 900 00 04")
    assert other.post(f"/api/campaigns/{camp['id']}/join", json={}).json()["status"] == "inactive"
    assert other.post(f"/api/campaigns/{camp['id']}/submit", json={"proof_name": "Can K"}).json()["status"] == "inactive"
    before = _stamps(cid)
    assert _review(mgr, camp["id"], cid)["status"] == "success" and _stamps(cid) == before + 1
    # yeniden açılınca daha önce yapmayan katılabilir
    assert mgr.post(f"/api/admin/campaigns/{camp['id']}/toggle").json()["campaign"]["active"] == 1
    assert other.post(f"/api/campaigns/{camp['id']}/join", json={}).json()["my_status"] == "joined"


def test_cashier_verifies_at_register_and_cards_show_campaign_name(client):
    mgr = _login(MANAGER)
    camp = _create(mgr, title="⭐ Google Yorumu", reward_stamps=2)
    _, me = _customer("0532 900 00 05")
    cid = me["customer_id"]
    staff = _login(STAFF)
    card = staff.post("/api/staff/customer-lookup", json={"q": "532 900 00 05"}).json()["customers"][0]
    item = next(x for x in card["campaigns"] if x["campaign_id"] == camp["id"])
    assert (item["status"], item["title"], item["reward_stamps"], item["active"]) == ("none", "⭐ Google Yorumu", 2, True)
    assert staff.post("/api/staff/campaign-review", json={"customer_id": cid, "campaign_id": camp["id"], "action": "reject"}).json()["status"] == "error"

    # müşteri uygulamada bir şey yapmadan kasada yorumunu gösterdi → kasiyer onaylar
    before = _stamps(cid)
    r = staff.post("/api/staff/campaign-review", json={"customer_id": cid, "campaign_id": camp["id"], "action": "approve"}).json()
    assert r["status"] == "success" and _stamps(cid) == before + 2
    item = next(x for x in r["customer"]["campaigns"] if x["campaign_id"] == camp["id"])
    assert item["status"] == "approved" and item["reviewed_by"] == "kasa1"
    assert r["customer"]["recent_stamps_log"][0]["label"] == "+1 Kampanya: ⭐ Google Yorumu"
    assert r["customer"]["bonus_history"]["campaign"] == "claimed"
    again = staff.post("/api/staff/campaign-review", json={"customer_id": cid, "campaign_id": camp["id"], "action": "approve"}).json()
    assert again["status"] == "already_approved" and _stamps(cid) == before + 2
    with get_db() as conn:
        logs = [row[0] for row in conn.execute("SELECT action FROM staff_audit_logs WHERE customer_id = ?", (cid,))]
    assert "campaign_approved" in logs
    # müdür müşteri listesinde kampanya adıyla durum görünür
    rows = mgr.get("/api/admin/customers?search=5329000005").json()["customers"]
    assert any(x["title"] == "⭐ Google Yorumu" and x["status"] == "approved" for x in rows[0]["campaigns"])


def test_reminder_push_reaches_only_consenting_customers_who_have_not_done_it(client, monkeypatch):
    for k, v in VAPID.items():
        monkeypatch.setenv(k, v)
    sent = []
    monkeypatch.setattr(push_service, "webpush",
                        lambda subscription_info, data, **kw: sent.append((subscription_info["endpoint"], json.loads(data))) or True)
    mgr = _login(MANAGER)
    camp = _create(mgr)
    a, _ = _customer("0532 900 00 06", marketing=True)    # yapmadı → hedef
    b, _ = _customer("0532 900 00 07", marketing=True)    # doğrulama bekliyor → hedef değil
    d, _ = _customer("0532 900 00 08", marketing=False)   # ticari ileti onayı yok (ETK) → hedef değil
    for cl, ep in ((a, "a"), (b, "b"), (d, "d")):
        assert cl.post("/api/push/subscribe", json={"endpoint": f"https://push.example/rc-{ep}", **SUB}).status_code == 200
    b.post(f"/api/campaigns/{camp['id']}/submit", json={"proof_name": "Burak"})
    aud = f"campaign:{camp['id']}"
    prev = mgr.get(f"/api/admin/push/preview?audience={aud}").json()
    assert prev["status"] == "success" and prev["customer_count"] == 1 and "yapmayanlar" in prev["audience_label"]
    sent.clear()
    res = mgr.post("/api/admin/push/send", json={"title": camp["title"], "body": "Hâlâ katılabilirsin!", "audience": aud}).json()
    assert res["status"] == "success" and res["sent_count"] == 1
    assert [e for e, _ in sent] == ["https://push.example/rc-a"]
    payload = sent[0][1]
    assert payload["url"].endswith("/kart/?tab=kampanyalar") and "optout" not in payload   # ADR-27: "kapat" düğmesi yok (ADR-36: yalnızca gövde satırı)
    assert payload["body"] == "Hâlâ katılabilirsin!\nKapatmak için: Profil › Kampanya Bildirimleri"   # ADR-36: ret satırı
    # pasif kampanyaya hatırlatma gönderilmez; bilinmeyen / bozuk kitle geçersiz
    mgr.post(f"/api/admin/campaigns/{camp['id']}/toggle")
    assert mgr.post("/api/admin/push/send", json={"title": "x", "body": "y", "audience": aud}).json()["status"] == "error"
    assert mgr.get("/api/admin/push/preview?audience=campaign:99999").json()["status"] == "error"
    assert mgr.get("/api/admin/push/preview?audience=campaign:abc").json()["status"] == "error"


def test_deleted_account_cannot_claim_the_same_campaign_again(client):
    mgr = _login(MANAGER)
    camp = _create(mgr)
    other = _create(mgr, title="Diğer görev")
    c, me = _customer("0532 900 00 09")
    cid = me["customer_id"]
    c.post(f"/api/campaigns/{camp['id']}/submit", json={"proof_name": "Elif"})
    assert _review(mgr, camp["id"], cid)["status"] == "success"
    c.post(f"/api/campaigns/{other['id']}/join", json={})
    assert c.post("/api/delete-my-data", json={"phone": "0532 900 00 09"}).json()["status"] == "success"
    with get_db() as conn:
        rows = conn.execute(
            "SELECT campaign_id, status, proof_name, phone_hash FROM campaign_participants WHERE customer_id = ?", (cid,)
        ).fetchall()
    # tamamlanmamış katılım silinir; onaylıda ad silinir, yalnızca geri döndürülemez telefon özeti kalır
    assert [(r["campaign_id"], r["status"]) for r in rows] == [(camp["id"], "approved")]
    assert rows[0]["proof_name"] is None and len(rows[0]["phone_hash"]) == 64

    c2, me2 = _customer("0532 900 00 09")          # aynı numarayla yeniden kayıt
    assert me2["customer_id"] != cid
    assert c2.post(f"/api/campaigns/{camp['id']}/join", json={}).json()["status"] == "error"
    assert _review(mgr, camp["id"], me2["customer_id"])["status"] == "error" and _stamps(me2["customer_id"]) == 0
    assert c2.post(f"/api/campaigns/{other['id']}/join", json={}).json()["status"] == "success"


def test_campaign_endpoints_require_identity(client):
    anon = _new_client()
    assert anon.post("/api/campaigns/1/join", json={}).json()["status"] == "device_not_found"
    assert anon.post("/api/campaigns/1/submit", json={"proof_name": "x y"}).json()["status"] == "device_not_found"
    assert anon.get("/api/admin/campaigns/1/participants").status_code == 401
    assert anon.post("/api/admin/campaigns/1/participants/1/review", json={"action": "approve"}).status_code == 401
    assert anon.post("/api/staff/campaign-review", json={"customer_id": 1, "campaign_id": 1, "action": "approve"}).status_code == 401
    assert _login(STAFF).get("/api/admin/campaigns/1/participants").status_code == 401     # kasiyer müdür listesine giremez


def test_monthly_report_lists_campaign_stamp_with_name_and_approver(client):
    mgr = _login(MANAGER)
    camp = _create(mgr, title="⭐ Google Yorum Ödülü")
    _, me = _customer("0532 900 00 10")
    assert _review(mgr, camp["id"], me["customer_id"])["status"] == "success"
    month = get_istanbul_now().strftime("%Y-%m")
    wb = load_workbook(io.BytesIO(mgr.get(f"/api/admin/reports/monthly-export?format=xlsx&month={month}").content))
    rows = [[c.value for c in r] for r in wb["İşlem Geçmişi"].iter_rows(min_row=6) if r[0].value]
    assert any(r[4] == "Kampanya Ödülü Aldı (+1 Damga) — ⭐ Google Yorum Ödülü" and r[5] == "mudur" for r in rows)
    summary = {r[0].value: r[1].value for r in wb["Özet"].iter_rows(min_row=6, max_row=20)}
    assert summary["Kampanya ödülü (damga)"] == 1


def test_panels_render_reward_ui_and_card_deep_link(client):
    page = _login(MANAGER).get("/mudur").text
    for marker in ('id="campaign-kind"', 'id="campaign-action-url"', 'id="campaign-reward-stamps"', 'id="camp-part-modal"',
                   'id="aud-campaign-list"', "function openParticipants", "function notifyNonParticipants",
                   "function reviewParticipant", "campaignBoxHtml(c)", "campaignMiniBadges(c)"):
        assert marker in page, marker
    staff_page = _login(STAFF).get("/personel").text
    assert "campaignBoxHtml(c)" in staff_page and "api/staff/campaign-review" in staff_page
    res = _new_client().get("/kart/?tab=kampanyalar")
    assert res.status_code == 200 and 'const INITIAL_TAB = "kampanyalar"' in res.text
    assert "function rewardTaskHtml" in res.text and "function campaignSubmit" in res.text
    assert 'const INITIAL_TAB = "home"' in _new_client().get("/kart/?tab=qrokut%3Cscript%3E").text

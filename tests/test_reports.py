"""Aylık geçmiş raporu (Excel/PDF) + kaldırılan yazılar/bölümler (ADR-15)."""
import io

from openpyxl import load_workbook
from starlette.testclient import TestClient

from app.db import get_db
from app.main import app
from app.utils import get_istanbul_now

MANAGER = {"username": "mudur", "password": "Test-Mudur-2026"}
STAFF = {"username": "kasa1", "password": "Test-Kasa-2026"}
EXPORT = "/api/admin/reports/monthly-export"


def _new_client():
    return TestClient(app, base_url="https://testserver")


def _login(creds):
    c = _new_client()
    assert c.post("/panel/login", json=creds).json()["status"] == "success"
    return c


def _seed(mgr, staff):
    """Müdür + kasiyer işlemleriyle bir ay verisi: kayıt, damga (kasa/müdür), hediye, kampanya, indirim, bildirim."""
    a = mgr.post("/api/admin/customers/create", json={"phone": "0532 700 00 01", "display_name": "Şule Çağlar", "consent_confirmed": True}).json()["customer"]["id"]
    b = mgr.post("/api/admin/customers/create", json={"phone": "0532 700 00 02", "display_name": "burak", "consent_confirmed": True}).json()["customer"]["id"]
    assert staff.post("/api/staff/stamp", json={"customer_id": b}).json()["status"] == "success"
    for _ in range(5):
        mgr.post(f"/api/admin/customers/{a}/stamp-add")
    assert staff.post("/api/staff/redeem", json={"customer_id": a}).json()["status"] == "success"
    mgr.post("/api/admin/campaigns/create", json={"title": "Hafta Sonu Çıtır Kampanya", "description": "İkinci kahve %50", "badge": "KAMPANYA"})
    mgr.post("/api/admin/products/create", json={"name": "Çikolatalı Sufle", "price": 200, "discount_price": 150, "discount_label": "%25 İndirim", "category": "Tatlılar"})
    with get_db(immediate=True) as conn:
        conn.execute(
            "INSERT INTO notification_log (kind, title, audience, sent_count, failed_count, created_at) VALUES ('campaign', 'Günaydın ☕ Şükür', 'all', 7, 1, ?)",
            (get_istanbul_now().isoformat(),),
        )
    return a, b


def _xlsx(res):
    return load_workbook(io.BytesIO(res.content))


def test_report_xlsx_contains_summary_history_campaigns_notifications(client):
    mgr, staff = _login(MANAGER), _login(STAFF)
    _seed(mgr, staff)
    month = get_istanbul_now().strftime("%Y-%m")
    res = mgr.get(f"{EXPORT}?format=xlsx&month={month}")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    assert res.headers["content-disposition"] == f'attachment; filename="Kahve_Dukkanim_Rapor_{month.replace("-", "_")}.xlsx"'
    assert res.content[:2] == b"PK"                       # gerçek zip/xlsx
    wb = _xlsx(res)
    assert wb.sheetnames == ["Özet", "İşlem Geçmişi", "Kampanya & İndirim", "Bildirimler"]

    summary = {r[0].value: r[1].value for r in wb["Özet"].iter_rows(min_row=6, max_row=16)}
    assert summary["Gelen toplam müşteri"] == 2 and summary["Yeni kayıt olan müşteri"] == 2
    assert summary["Basılan toplam damga"] == 6 and summary["Verilen (kullanılan) hediye kahve"] == 1
    assert summary["Kazanılan hediye hakkı"] == 1 and summary["Aktif kampanya sayısı"] >= 1
    assert summary["Gönderilen bildirim"] == 1

    rows = [[c.value for c in r] for r in wb["İşlem Geçmişi"].iter_rows(min_row=6) if r[0].value]
    actions = [(r[1], r[4], r[5]) for r in rows]
    assert ("burak", "+1 Damga Aldı (Kasa)", "kasa1") in actions                       # kim, ne aldı, kim bastı
    assert ("Şule Çağlar", "Hediye Kahve Kullandı", "kasa1") in actions                # Türkçe karakterler bozulmaz
    assert ("Şule Çağlar", "+1 Damga Eklendi (Müdür)", "mudur") in actions
    assert any(r[4] == "Yeni Kayıt Oldu" for r in rows)
    sule = [r for r in rows if r[1] == "Şule Çağlar"][0]
    assert sule[3] == "532 700 00 01" and sule[2].startswith("#") and "." in sule[0]    # 5XX biçimi, kod, GG.AA.YYYY

    camp = [[c.value for c in r] for r in wb["Kampanya & İndirim"].iter_rows(min_row=6)]
    flat = str(camp)
    assert "Hafta Sonu Çıtır Kampanya" in flat and "Çikolatalı Sufle" in flat and "150.00 ₺" in flat
    notif = [[c.value for c in r] for r in wb["Bildirimler"].iter_rows(min_row=6) if r[0].value]
    assert notif[0][1] == "Günaydın ☕ Şükür" and notif[0][4] == "7" and notif[0][5] == "1"


def test_report_pdf_is_valid_with_embedded_turkish_font(client):
    mgr, staff = _login(MANAGER), _login(STAFF)
    _seed(mgr, staff)
    month = get_istanbul_now().strftime("%Y-%m")
    res = mgr.get(f"{EXPORT}?format=pdf&month={month}")
    assert res.status_code == 200 and res.headers["content-type"] == "application/pdf"
    assert res.headers["content-disposition"] == f'attachment; filename="Kahve_Dukkanim_Rapor_{month.replace("-", "_")}.pdf"'
    assert res.content.startswith(b"%PDF-") and b"%%EOF" in res.content[-1024:]
    assert b"DejaVu" in res.content                      # Türkçe karakterli font gömülü
    assert len(res.content) > 5_000


def test_report_periods_and_validation_and_empty_months(client):
    mgr = _login(MANAGER)
    periods = mgr.get("/api/admin/reports/months").json()["periods"]
    values = [p["value"] for p in periods]
    assert values[0] == get_istanbul_now().strftime("%Y-%m") and values[-2:] == ["last30", "all"]
    assert periods[0]["label"].split()[0] in ("Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık")
    assert mgr.get(f"{EXPORT}?format=xlsx&month=last30").status_code == 200
    all_res = mgr.get(f"{EXPORT}?format=xlsx&month=all")
    assert all_res.status_code == 200 and "Tum_Zamanlar" in all_res.headers["content-disposition"]
    assert mgr.get(f"{EXPORT}?format=doc").status_code == 400
    assert mgr.get(f"{EXPORT}?format=xlsx&month=2026-13").status_code == 400
    assert mgr.get(f"{EXPORT}?format=xlsx&month=../etc").status_code == 400
    old = _xlsx(mgr.get(f"{EXPORT}?format=xlsx&month=2020-01"))          # veri olmayan ay: boş ama bozuk değil
    assert old["İşlem Geçmişi"]["A6"].value == "Bu dönemde işlem yok"
    assert mgr.get(f"{EXPORT}?format=pdf&month=2020-01").content.startswith(b"%PDF-")


def test_report_requires_manager_session(client):
    assert _new_client().get(f"{EXPORT}?format=xlsx").status_code == 401
    assert _login(STAFF).get(f"{EXPORT}?format=pdf").status_code == 401
    assert _login(STAFF).get("/api/admin/reports/months").status_code == 401


def test_report_marks_welcome_and_bonus_gifts(client):
    c = _new_client()
    from app.services.stamp_codes import create_stamp_code
    with get_db(immediate=True) as conn:
        code = create_stamp_code(conn, created_by="test")["code"]
    first = c.post("/api/claim", json={"claim_token": code, "phone": "0532 710 00 01", "kvkk_read": True, "explicit_consent": True}).json()
    tok = first["device_token"]
    assert c.post("/api/claim-install-bonus", json={}, headers={"X-Device-Token": tok}).json()["status"] == "success"
    mgr = _login(MANAGER)
    wb = _xlsx(mgr.get(f"{EXPORT}?format=xlsx&month=all"))
    acts = [r[4].value for r in wb["İşlem Geçmişi"].iter_rows(min_row=6)]
    assert "Hoş Geldin Hediyesi Aldı (+1 Damga)" in acts and "Uygulama İndirme Hediyesi Aldı (+1 Damga)" in acts
    summary = {r[0].value: r[1].value for r in wb["Özet"].iter_rows(min_row=6, max_row=16)}
    assert summary["Hoş geldin hediyesi"] == 1 and summary["Uygulama indirme hediyesi"] == 1


# ------------------------------------------------------------------ kaldırılan yazılar / bölümler
def test_removed_branding_business_and_backup_sections(client):
    mgr = _login(MANAGER)
    staff = _login(STAFF)
    pages = {
        "mudur": mgr.get("/mudur").text, "personel": staff.get("/personel").text,
        "giris": _new_client().get("/giris").text, "kart": client.get("/k/kart/").text, "indir": client.get("/k/indir").text,
    }
    for name, html in pages.items():
        for banned in ("Café Atelier", "Reserve", "Atelier"):
            assert banned not in html, f"{name}: '{banned}' kaldı"
    m = pages["mudur"]
    assert "Yedek Kütüğü" not in m and "card-business" not in m and "purgeBackups" not in m and "btn-purge-backups" not in m
    assert "İşletme" not in m and "setting-business-name" not in m
    assert 'id="card-report"' in m and "Excel Olarak İndir (.xlsx)" in m and "PDF Olarak İndir (.pdf)" in m
    assert "İşletme" not in pages["kart"]
    assert "backup" not in mgr.get("/api/admin/overview").json()
    assert mgr.post("/api/admin/purge-backups").status_code in (404, 405)

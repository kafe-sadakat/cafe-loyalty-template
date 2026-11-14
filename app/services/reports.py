"""
Aylık geçmiş raporu (Excel .xlsx + PDF .pdf).

İçerik: yönetici özeti, personel özeti, işlem geçmişi (kim ne almış / kim işlem yapmış),
kampanya & indirimli ürünler, bildirim geçmişi. Türkçe karakter uyumludur (xlsx UTF-8; PDF'te gömülü DejaVu Sans).
Tarihler İstanbul saatiyle ISO biçiminde saklanır; gün karşılaştırması ilk 10 karakter üzerinden yapılır.
"""
from __future__ import annotations

import io
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from xml.sax.saxutils import escape

from app.utils import format_phone, get_istanbul_now

TR_MONTHS = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"]
FONT_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"
MONTH_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")
BONUS_MATCH_SECONDS = 5

ACTIONS = {
    "qr": "+1 Damga Aldı (QR)",
    "welcome": "Hoş Geldin Hediyesi Aldı (+1 Damga)",
    "bonus_pwa": "Uygulama İndirme Hediyesi Aldı (+1 Damga)",
    "bonus_notif": "Bildirim Hediyesi Aldı (+1 Damga)",
    "staff": "+1 Damga Aldı (Kasa)",
    "admin": "+1 Damga Eklendi (Müdür)",
    "campaign": "Kampanya Ödülü Aldı (+1 Damga)",
    None: "+1 Damga Aldı",
}
# Damgayı bir panel kullanıcısının verdiği kaynaklar (rapor: "İşlemi Yapan" = o kullanıcı; kampanyada onaylayan)
PANEL_SOURCES = ("staff", "admin", "campaign")
SELF_ACTOR = "Müşteri (kendisi)"


@dataclass
class Period:
    key: str        # "2026-11" | "last30" | "all"
    label: str      # "Kasım 2026"
    start: str      # YYYY-MM-DD (dahil)
    end: str        # YYYY-MM-DD (hariç)
    file_part: str  # dosya adı parçası


def parse_period(value: Optional[str], now: Optional[datetime] = None) -> Optional[Period]:
    now = now or get_istanbul_now()
    v = (value or "").strip().lower()
    if not v:
        v = now.strftime("%Y-%m")
    if v == "all":
        return Period("all", "Tüm Zamanlar", "0000-01-01", "9999-12-31", "Tum_Zamanlar")
    if v == "last30":
        start = (now - timedelta(days=29)).strftime("%Y-%m-%d")
        end = (now + timedelta(days=1)).strftime("%Y-%m-%d")
        return Period("last30", "Son 30 Gün", start, end, "Son_30_Gun_" + now.strftime("%Y_%m_%d"))
    m = MONTH_RE.match(v)
    if not m:
        return None
    year, month = int(m.group(1)), int(m.group(2))
    end_year, end_month = (year + 1, 1) if month == 12 else (year, month + 1)
    return Period(v, f"{TR_MONTHS[month - 1]} {year}", f"{year:04d}-{month:02d}-01", f"{end_year:04d}-{end_month:02d}-01", f"{year}_{month:02d}")


def available_periods(conn: sqlite3.Connection) -> list[dict[str, str]]:
    """Seçenekler: ilk kayıttan bu aya kadar her ay (yeniden eskiye), Son 30 Gün ve Tüm Zamanlar."""
    row = conn.execute(
        "SELECT MIN(m) AS first FROM (SELECT MIN(substr(created_at, 1, 7)) AS m FROM customers "
        "UNION ALL SELECT MIN(substr(created_at, 1, 7)) FROM stamps)"
    ).fetchone()
    now = get_istanbul_now()
    first = row["first"] if row and row["first"] else now.strftime("%Y-%m")
    y, mth = int(first[:4]), int(first[5:7])
    months = []
    cy, cm = now.year, now.month
    while (cy, cm) >= (y, mth):
        months.append({"value": f"{cy}-{cm:02d}", "label": f"{TR_MONTHS[cm - 1]} {cy}"})
        cm -= 1
        if cm == 0:
            cy, cm = cy - 1, 12
    return months + [{"value": "last30", "label": "Son 30 Gün"}, {"value": "all", "label": "Tüm Zamanlar"}]


def fmt_dt(iso: Optional[str]) -> str:
    if not iso:
        return "—"
    try:
        return datetime.fromisoformat(iso).strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return str(iso)[:16]


def _in(col: str) -> str:
    return f"substr({col}, 1, 10) >= ? AND substr({col}, 1, 10) < ?"


def collect_report(conn: sqlite3.Connection, period: Period) -> dict[str, Any]:
    rng = (period.start, period.end)
    users = {r["id"]: r["username"] for r in conn.execute("SELECT id, username FROM admin_users")}
    customers = {
        r["id"]: r for r in conn.execute("SELECT id, display_name, phone_e164, created_at, deleted_at FROM customers")
    }

    def who(cid: int) -> tuple[str, str, str]:
        c = customers.get(cid)
        if not c:
            return "—", f"#{cid}", "—"
        if c["deleted_at"]:
            return "(silinmiş hesap)", f"#{cid}", "—"
        return (c["display_name"] or f"user{cid}"), f"#{cid}", format_phone(c["phone_e164"])

    # Eski (kaynak alanı olmayan) bonus damgalarını denetim kaydından eşleştir
    bonus_events: dict[int, list[tuple[datetime, str]]] = {}
    for r in conn.execute(
        "SELECT at, actor, action FROM audit_log WHERE action IN ('pwa_bonus_claimed', 'notif_bonus_claimed')"
    ):
        m = re.match(r"customer:(\d+)", r["actor"] or "")
        if m:
            try:
                bonus_events.setdefault(int(m.group(1)), []).append(
                    (datetime.fromisoformat(r["at"]), "bonus_pwa" if r["action"] == "pwa_bonus_claimed" else "bonus_notif")
                )
            except ValueError:
                pass

    def legacy_source(cid: int, created_at: str) -> Optional[str]:
        try:
            ts = datetime.fromisoformat(created_at)
        except ValueError:
            return None
        for at, kind in bonus_events.get(cid, []):
            if abs((at - ts).total_seconds()) <= BONUS_MATCH_SECONDS:
                return kind
        return None

    staff_logs = conn.execute(
        "SELECT customer_id, action, staff_name, staff_id, at FROM staff_audit_logs "
        "WHERE action IN ('reward_redeemed', 'customer_registered') ORDER BY id DESC"
    ).fetchall()

    def staff_for(cid: int, action: str, day: str) -> str:
        for r in staff_logs:
            if r["customer_id"] == cid and r["action"] == action and (r["at"] or "")[:10] == day:
                return users.get(r["staff_id"]) or r["staff_name"] or "—"
        return "—"

    tx: list[dict[str, Any]] = []

    for s in conn.execute(
        f"SELECT id, customer_id, created_at, source, actor, note FROM stamps WHERE {_in('created_at')} ORDER BY created_at",
        rng,
    ):
        name, code, phone = who(s["customer_id"])
        source = s["source"] or legacy_source(s["customer_id"], s["created_at"])
        if s["source"] in PANEL_SOURCES:
            actor = s["actor"] or "—"
        elif not s["source"] and source is None:
            actor = "—"
        else:
            actor = SELF_ACTOR
        action = ACTIONS.get(source, ACTIONS[None])
        if source == "campaign" and s["note"]:
            action = f"{action} — {s['note']}"
        tx.append({"ts": s["created_at"], "name": name, "code": code, "phone": phone, "action": action, "actor": actor, "kind": source or "stamp"})
    for s in conn.execute(
        f"SELECT customer_id, voided_at, voided_reason FROM stamps WHERE voided_at IS NOT NULL AND {_in('voided_at')}", rng
    ):
        name, code, phone = who(s["customer_id"])
        reason = (s["voided_reason"] or "").strip()
        tx.append({"ts": s["voided_at"], "name": name, "code": code, "phone": phone, "action": "Damga İptal Edildi" + (f" ({reason})" if reason else ""), "actor": "—", "kind": "void"})
    for r in conn.execute(
        f"SELECT customer_id, redeemed_at, redeemed_by FROM rewards WHERE redeemed_at IS NOT NULL AND voided_at IS NULL AND {_in('redeemed_at')} ORDER BY redeemed_at",
        rng,
    ):
        name, code, phone = who(r["customer_id"])
        actor = r["redeemed_by"] or staff_for(r["customer_id"], "reward_redeemed", r["redeemed_at"][:10])
        tx.append({"ts": r["redeemed_at"], "name": name, "code": code, "phone": phone, "action": "Hediye Kahve Kullandı", "actor": actor, "kind": "gift"})
    # Kaydı panelden açan kişi (müdür/kasiyer): denetim kaydındaki "customer_created" satırı
    by_display = {r["display_name"]: r["username"] for r in conn.execute("SELECT username, display_name FROM admin_users")}
    created_by: dict[int, str] = {}
    for r in conn.execute("SELECT actor, detail FROM audit_log WHERE action = 'customer_created'"):
        m = re.search(r"#(\d+)", r["detail"] or "")
        if m and r["actor"] and ":" in r["actor"]:
            label = r["actor"].split(":", 1)[1]
            created_by[int(m.group(1))] = by_display.get(label, label)
    for c in conn.execute(f"SELECT id, created_at FROM customers WHERE {_in('created_at')}", rng):
        name, code, phone = who(c["id"])
        actor = staff_for(c["id"], "customer_registered", c["created_at"][:10])
        if actor == "—":
            actor = created_by.get(c["id"], SELF_ACTOR)
        tx.append({"ts": c["created_at"], "name": name, "code": code, "phone": phone, "action": "Yeni Kayıt Oldu", "actor": actor, "kind": "register"})
    tx.sort(key=lambda r: r["ts"] or "")

    def one(sql: str, args: tuple = ()) -> int:
        return conn.execute(sql, args).fetchone()[0]

    visitors = one(
        f"SELECT COUNT(DISTINCT customer_id) FROM (SELECT customer_id FROM stamps WHERE voided_at IS NULL AND {_in('created_at')} "
        f"UNION ALL SELECT customer_id FROM rewards WHERE redeemed_at IS NOT NULL AND voided_at IS NULL AND {_in('redeemed_at')})",
        rng + rng,
    )
    notifications = [
        dict(r) for r in conn.execute(
            f"SELECT created_at, kind, title, audience, sent_count, failed_count FROM notification_log WHERE {_in('created_at')} ORDER BY created_at", rng
        )
    ]
    campaigns = [
        dict(r) for r in conn.execute(
            f"SELECT title, badge, active, created_at, description FROM campaigns WHERE {_in('created_at')} ORDER BY created_at", rng
        )
    ]
    discounts = [
        dict(r) for r in conn.execute(
            f"SELECT name, category, price, discount_price, discount_label, is_new, active, updated_at FROM products "
            f"WHERE discount_price IS NOT NULL AND {_in('updated_at')} ORDER BY updated_at", rng
        )
    ]
    kinds = [t["kind"] for t in tx]
    summary = {
        "visitors": visitors,
        "new_customers": one(f"SELECT COUNT(*) FROM customers WHERE {_in('created_at')}", rng),
        "stamps": one(f"SELECT COUNT(*) FROM stamps WHERE voided_at IS NULL AND {_in('created_at')}", rng),
        "gifts_used": one(f"SELECT COUNT(*) FROM rewards WHERE redeemed_at IS NOT NULL AND voided_at IS NULL AND {_in('redeemed_at')}", rng),
        "gifts_earned": one(f"SELECT COUNT(*) FROM rewards WHERE voided_at IS NULL AND {_in('issued_at')}", rng),
        "active_campaigns": one("SELECT COUNT(*) FROM campaigns WHERE active = 1"),
        "campaigns_published": len(campaigns),
        "notifications": len(notifications),
        "notifications_delivered": sum(int(n["sent_count"] or 0) for n in notifications),
        "welcome_gifts": kinds.count("welcome"),
        "pwa_gifts": kinds.count("bonus_pwa"),
        "notif_gifts": kinds.count("bonus_notif"),
        "campaign_gifts": kinds.count("campaign"),
    }
    staff: dict[str, dict[str, int]] = {}
    for t in tx:
        a = t["actor"]
        if a in ("—", SELF_ACTOR):
            continue
        row = staff.setdefault(a, {"stamps": 0, "gifts": 0, "registrations": 0})
        if t["kind"] in PANEL_SOURCES:
            row["stamps"] += 1
        elif t["kind"] == "gift":
            row["gifts"] += 1
        elif t["kind"] == "register":
            row["registrations"] += 1
    return {
        "period": period,
        "generated_at": get_istanbul_now().strftime("%d.%m.%Y %H:%M"),
        "summary": summary,
        "staff": [{"username": k, **v} for k, v in sorted(staff.items())],
        "transactions": tx,
        "campaigns": campaigns,
        "discounts": discounts,
        "notifications": notifications,
    }


SUMMARY_ROWS = [
    ("Gelen toplam müşteri", "visitors"), ("Yeni kayıt olan müşteri", "new_customers"),
    ("Basılan toplam damga", "stamps"), ("Verilen (kullanılan) hediye kahve", "gifts_used"),
    ("Kazanılan hediye hakkı", "gifts_earned"), ("Aktif kampanya sayısı", "active_campaigns"),
    ("Dönemde yayınlanan kampanya", "campaigns_published"), ("Gönderilen bildirim", "notifications"),
    ("Hoş geldin hediyesi", "welcome_gifts"), ("Uygulama indirme hediyesi", "pwa_gifts"), ("Bildirim hediyesi", "notif_gifts"),
    ("Kampanya ödülü (damga)", "campaign_gifts"),
]
TX_HEAD = ["Tarih-Saat", "Müşteri Adı", "Müşteri Kodu", "Telefon", "Yapılan İşlem", "İşlemi Yapan"]
CAMP_HEAD = ["Yayın Tarihi", "Başlık", "Rozet", "Durum", "Açıklama"]
DISC_HEAD = ["Güncelleme", "Ürün", "Kategori", "Normal Fiyat", "İndirimli Fiyat", "Etiket", "Yeni Ürün", "Durum"]
NOTIF_HEAD = ["Gönderim", "Başlık", "Tür", "Hedef Kitle", "Teslim", "Hatalı"]
STAFF_HEAD = ["Kullanıcı Adı", "Bastığı Damga", "Verdiği Hediye", "Açtığı Kayıt"]


def _tx_rows(data: dict[str, Any]) -> list[list[str]]:
    return [[fmt_dt(t["ts"]), t["name"], t["code"], t["phone"], t["action"], t["actor"]] for t in data["transactions"]]


def _camp_rows(data: dict[str, Any]) -> list[list[str]]:
    return [[fmt_dt(c["created_at"]), c["title"], c["badge"] or "", "Yayında" if c["active"] else "Pasif", c["description"] or ""] for c in data["campaigns"]]


def _disc_rows(data: dict[str, Any]) -> list[list[str]]:
    return [
        [fmt_dt(p["updated_at"]), p["name"], p["category"] or "", f"{p['price']:.2f} ₺", f"{p['discount_price']:.2f} ₺", p["discount_label"] or "", "Evet" if p["is_new"] else "", "Aktif" if p["active"] else "Pasif"]
        for p in data["discounts"]
    ]


def _notif_rows(data: dict[str, Any]) -> list[list[str]]:
    return [[fmt_dt(n["created_at"]), n["title"], n["kind"], n["audience"], str(n["sent_count"] or 0), str(n["failed_count"] or 0)] for n in data["notifications"]]


# ------------------------------------------------------------------ Excel
def build_xlsx(data: dict[str, Any], business_name: str) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    green, gold = "143328", "D4AF37"
    head_font = Font(name="Calibri", bold=True, color="FFFFFF")
    head_fill = PatternFill("solid", fgColor=green)
    thin = Side(style="thin", color="D6D1C7")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    period: Period = data["period"]

    wb = Workbook()

    def put(ws, row: int, column: int, value: Any):
        """Hücreye değer yazar. openpyxl '=' ile başlayan metni FORMÜL yapar; müşteri adı gibi dışarıdan gelen metin
        (ör. '=HYPERLINK(...)') müdür dosyayı açınca çalışmasın diye her metin düz metin (string) olarak yazılır."""
        c = ws.cell(row=row, column=column, value=value)
        if isinstance(value, str) and c.data_type == "f":
            c.data_type = "s"
        return c

    def write_table(ws, start_row: int, head: list[str], rows: list[list[Any]], widths: Optional[list[int]] = None) -> int:
        for ci, h in enumerate(head, 1):
            c = put(ws, start_row, ci, h)
            c.font, c.fill, c.border = head_font, head_fill, border
            c.alignment = Alignment(vertical="center", wrap_text=True)
        for ri, row in enumerate(rows, start_row + 1):
            for ci, v in enumerate(row, 1):
                c = put(ws, ri, ci, v)
                c.border = border
                c.alignment = Alignment(vertical="top", wrap_text=True)
        if widths:
            for ci, w in enumerate(widths, 1):
                letter = get_column_letter(ci)
                ws.column_dimensions[letter].width = max(ws.column_dimensions[letter].width or 0, w)
        return start_row + len(rows) + 1

    def title(ws, text: str, sub: str) -> None:
        put(ws, 1, 1, text).font = Font(name="Calibri", size=16, bold=True, color=green)
        put(ws, 2, 1, sub).font = Font(name="Calibri", italic=True, color="7D766F")
        ws["A3"].fill = PatternFill("solid", fgColor=gold)
        ws.row_dimensions[3].height = 3

    # 1. Özet
    ws = wb.active
    ws.title = "Özet"
    title(ws, f"{business_name} — Aylık Yönetici Özeti", f"Dönem: {period.label}  •  Oluşturulma: {data['generated_at']}")
    r = write_table(ws, 5, ["Gösterge", "Değer"], [[label, data["summary"][key]] for label, key in SUMMARY_ROWS], [40, 16])
    ws.cell(row=r + 1, column=1, value="Personel Özeti").font = Font(bold=True, size=13, color=green)
    staff_rows = [[s["username"], s["stamps"], s["gifts"], s["registrations"]] for s in data["staff"]] or [["Bu dönemde personel işlemi yok", "", "", ""]]
    write_table(ws, r + 2, STAFF_HEAD, staff_rows, [40, 16, 16, 16])

    # 2. İşlem geçmişi
    ws2 = wb.create_sheet("İşlem Geçmişi")
    title(ws2, "İşlem Geçmişi — Kim Ne Almış / Kim İşlem Yapmış", f"Dönem: {period.label}  •  {len(data['transactions'])} işlem")
    rows = _tx_rows(data) or [["Bu dönemde işlem yok", "", "", "", "", ""]]
    write_table(ws2, 5, TX_HEAD, rows, [18, 24, 14, 16, 44, 22])
    ws2.freeze_panes = "A6"
    ws2.auto_filter.ref = f"A5:{get_column_letter(len(TX_HEAD))}{5 + len(rows)}"

    # 3. Kampanya & indirim
    ws3 = wb.create_sheet("Kampanya & İndirim")
    title(ws3, "Kampanya & İndirimli Ürün Geçmişi", f"Dönem: {period.label}")
    ws3.cell(row=5, column=1, value="Yayınlanan Kampanyalar").font = Font(bold=True, size=13, color=green)
    nxt = write_table(ws3, 6, CAMP_HEAD, _camp_rows(data) or [["Bu dönemde kampanya yok", "", "", "", ""]], [18, 32, 14, 12, 60])
    ws3.cell(row=nxt + 1, column=1, value="İndirimli Ürünler").font = Font(bold=True, size=13, color=green)
    write_table(ws3, nxt + 2, DISC_HEAD, _disc_rows(data) or [["Bu dönemde indirimli ürün güncellemesi yok", "", "", "", "", "", "", ""]], [18, 32, 22, 16, 16, 18, 12, 10])

    # 4. Bildirimler
    ws4 = wb.create_sheet("Bildirimler")
    title(ws4, "Gönderilen Bildirimler", f"Dönem: {period.label}  •  {data['summary']['notifications']} gönderim, {data['summary']['notifications_delivered']} teslim")
    write_table(ws4, 5, NOTIF_HEAD, _notif_rows(data) or [["Bu dönemde bildirim yok", "", "", "", "", ""]], [18, 40, 14, 22, 10, 10])

    for sheet in wb.worksheets:
        sheet.sheet_view.showGridLines = False
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ------------------------------------------------------------------ PDF
_fonts_ready = False


def _register_fonts() -> None:
    global _fonts_ready
    if _fonts_ready:
        return
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    pdfmetrics.registerFont(TTFont("DejaVu", str(FONT_DIR / "DejaVuSans.ttf")))
    pdfmetrics.registerFont(TTFont("DejaVu-Bold", str(FONT_DIR / "DejaVuSans-Bold.ttf")))
    pdfmetrics.registerFontFamily("DejaVu", normal="DejaVu", bold="DejaVu-Bold", italic="DejaVu", boldItalic="DejaVu-Bold")
    _fonts_ready = True


def _pdf_text(value: Any) -> str:
    """Paragraph için güvenli metin: XML kaçışı + font dışı (emoji vb.) karakterler atılır."""
    return escape("".join(ch for ch in str(value if value is not None else "") if ord(ch) <= 0xFFFF))


def build_pdf(data: dict[str, Any], business_name: str) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    _register_fonts()
    period: Period = data["period"]
    green, gold, border = colors.HexColor("#143328"), colors.HexColor("#D4AF37"), colors.HexColor("#D6D1C7")
    h1 = ParagraphStyle("h1", fontName="DejaVu-Bold", fontSize=18, leading=22, textColor=green)
    h2 = ParagraphStyle("h2", fontName="DejaVu-Bold", fontSize=12, leading=16, textColor=green, spaceBefore=10, spaceAfter=4)
    sub = ParagraphStyle("sub", fontName="DejaVu", fontSize=9, leading=12, textColor=colors.HexColor("#7D766F"))
    cell = ParagraphStyle("cell", fontName="DejaVu", fontSize=7.5, leading=9.5)
    head = ParagraphStyle("head", fontName="DejaVu-Bold", fontSize=7.5, leading=9.5, textColor=colors.white)
    note = ParagraphStyle("note", fontName="DejaVu", fontSize=9, leading=12)

    def table(header: list[str], rows: list[list[Any]], widths: list[float]) -> Table:
        body = [[Paragraph(_pdf_text(h), head) for h in header]] + [[Paragraph(_pdf_text(v), cell) for v in r] for r in rows]
        t = Table(body, colWidths=widths, repeatRows=1)
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), green), ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("GRID", (0, 0), (-1, -1), 0.4, border), ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F7F5F1")]),
            ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        return t

    page_w = landscape(A4)[0] - 30 * mm
    s = data["summary"]
    story: list[Any] = [
        Paragraph(_pdf_text(f"{business_name} — Aylık Geçmiş & İşlem Raporu"), h1),
        Paragraph(_pdf_text(f"Dönem: {period.label}   •   Oluşturulma: {data['generated_at']}"), sub),
        Spacer(1, 6 * mm),
        Paragraph("Aylık Yönetici Özeti", h2),
    ]
    grid = []
    for i in range(0, len(SUMMARY_ROWS), 4):
        row = [
            Paragraph(f'<font name="DejaVu-Bold" size="16" color="#143328">{s[k]}</font><br/><font size="7.5" color="#7D766F">{_pdf_text(label)}</font>', note)
            for label, k in SUMMARY_ROWS[i:i + 4]
        ]
        grid.append(row + [""] * (4 - len(row)))
    cards = Table(grid, colWidths=[page_w / 4] * 4)
    cards.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.6, border), ("INNERGRID", (0, 0), (-1, -1), 0.4, border),
        ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8), ("LINEABOVE", (0, 0), (-1, 0), 2, gold),
    ]))
    story += [cards, Paragraph("Personel Özeti", h2)]
    staff_rows = [[x["username"], x["stamps"], x["gifts"], x["registrations"]] for x in data["staff"]]
    story.append(table(STAFF_HEAD, staff_rows, [page_w * 0.4, page_w * 0.2, page_w * 0.2, page_w * 0.2]) if staff_rows else Paragraph("Bu dönemde personel işlemi yok.", note))

    story += [PageBreak(), Paragraph(f"İşlem Geçmişi — Kim Ne Almış / Kim İşlem Yapmış ({len(data['transactions'])} işlem)", h2)]
    tx = _tx_rows(data)
    story.append(table(TX_HEAD, tx, [page_w * f for f in (0.14, 0.16, 0.08, 0.13, 0.31, 0.18)]) if tx else Paragraph("Bu dönemde işlem yok.", note))

    story += [PageBreak(), Paragraph("Yayınlanan Kampanyalar", h2)]
    camps = _camp_rows(data)
    story.append(table(CAMP_HEAD, camps, [page_w * f for f in (0.13, 0.22, 0.1, 0.08, 0.47)]) if camps else Paragraph("Bu dönemde kampanya yok.", note))
    story.append(Paragraph("İndirimli Ürünler", h2))
    disc = _disc_rows(data)
    story.append(table(DISC_HEAD, disc, [page_w * f for f in (0.13, 0.22, 0.16, 0.11, 0.11, 0.13, 0.07, 0.07)]) if disc else Paragraph("Bu dönemde indirimli ürün güncellemesi yok.", note))
    story.append(Paragraph(f"Gönderilen Bildirimler ({s['notifications']} gönderim, {s['notifications_delivered']} teslim)", h2))
    notif = _notif_rows(data)
    story.append(table(NOTIF_HEAD, notif, [page_w * f for f in (0.13, 0.35, 0.1, 0.2, 0.11, 0.11)]) if notif else Paragraph("Bu dönemde bildirim yok.", note))

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("DejaVu", 7.5)
        canvas.setFillColor(colors.HexColor("#7D766F"))
        canvas.drawString(15 * mm, 8 * mm, f"{business_name} • {period.label}")
        canvas.drawRightString(landscape(A4)[0] - 15 * mm, 8 * mm, f"Sayfa {doc.page}")
        canvas.setStrokeColor(gold)
        canvas.setLineWidth(1.2)
        canvas.line(15 * mm, 12 * mm, landscape(A4)[0] - 15 * mm, 12 * mm)
        canvas.restoreState()

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=15 * mm, rightMargin=15 * mm, topMargin=14 * mm, bottomMargin=17 * mm,
                            title=f"{business_name} Rapor {period.label}", author=business_name)
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()

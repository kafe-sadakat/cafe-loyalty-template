"""
Kasa "Kazanılan Tek Seferlik Bonuslar (Kanıt Tablosu)": hoş geldin / uygulama indirme / bildirim damgaları.

Kasiyerin "indirdim ama damgam gelmedi" itirazına karşı sunucunun kaydı: her bonus için durum
`received` (alındı, tarih-saat ile), `blocked` (hakkı yok: hesap sıfırlamış / cihaz daha önce kullanmış) veya `none` (henüz alınmadı).
Son 5 damganın kaynağı ve saati de döner. Tarihler İstanbul saatiyle ISO biçimindedir.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Iterable

from app.services.campaign_rewards import campaign_participation_map

SOURCE_LABELS = {
    "qr": "+1 Normal Damga (QR)",
    "welcome": "+1 Hoş Geldin Hediyesi",
    "bonus_pwa": "+1 Uygulama İndirme Hediyesi",
    "bonus_notif": "+1 Bildirim Hediyesi",
    "staff": "+1 Normal Kasa Damgası",
    "admin": "+1 Damga (Müdür)",
    "campaign": "+1 Kampanya Ödülü",
    None: "+1 Damga",
}


def stamp_label(source: Any, note: Any = None) -> str:
    """Damga kaynağının Türkçe etiketi; ödüllü kampanya damgasında kampanya adıyla ("+1 Kampanya: …")."""
    if source == "campaign" and note:
        return f"+1 Kampanya: {note}"
    return SOURCE_LABELS.get(source, SOURCE_LABELS[None])
RECENT_LIMIT = 5
# Eski `*_bonus_status` değerleri → yeni `bonus_history` değerleri
_HISTORY = {"received": "claimed", "blocked": "used", "none": "none"}


def _status(awarded_at: Any, locked: bool) -> str:
    if awarded_at:
        return "received"
    return "blocked" if locked else "none"


def bonus_proof_map(conn: sqlite3.Connection, customer_ids: Iterable[int]) -> dict[int, dict[str, Any]]:
    ids = sorted({int(i) for i in customer_ids})
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    recent: dict[int, list[dict[str, Any]]] = {i: [] for i in ids}
    for r in conn.execute(
        f"""
        SELECT customer_id, created_at, source, actor, note FROM (
            SELECT s.customer_id, s.created_at, s.source, s.actor, s.note,
                   ROW_NUMBER() OVER (PARTITION BY s.customer_id ORDER BY s.created_at DESC, s.id DESC) AS rn
            FROM stamps s WHERE s.voided_at IS NULL AND s.customer_id IN ({marks})
        ) WHERE rn <= {RECENT_LIMIT} ORDER BY customer_id, created_at DESC
        """,
        ids,
    ):
        recent[r["customer_id"]].append({
            "at": r["created_at"], "source": r["source"],
            "label": stamp_label(r["source"], r["note"]), "actor": r["actor"] or "",
        })
    campaigns = campaign_participation_map(conn, ids)

    campaign_ids = {
        r["customer_id"]
        for r in conn.execute(
            f"SELECT DISTINCT customer_id FROM stamps WHERE voided_at IS NULL AND source = 'campaign' AND customer_id IN ({marks})",
            ids,
        )
    }
    subscribed_ids = {
        r["customer_id"]
        for r in conn.execute(
            f"SELECT DISTINCT customer_id FROM push_subscriptions WHERE active = 1 AND customer_id IN ({marks})",
            ids,
        )
    }

    out: dict[int, dict[str, Any]] = {}
    for c in conn.execute(
        f"SELECT id, welcome_bonus_at, pwa_bonus_at, push_bonus_at, is_re_registered, pwa_bonus_claimed, notif_bonus_claimed, "
        f"COALESCE(pwa_standalone, 0) AS pwa_standalone, pwa_seen_at, COALESCE(marketing_consent, 0) AS marketing_consent, "
        f"COALESCE(pwa_bonus_pending, 0) AS pwa_pending, COALESCE(notif_bonus_pending, 0) AS notif_pending "
        f"FROM customers WHERE id IN ({marks})",
        ids,
    ):
        subscribed = c["id"] in subscribed_ids
        consented = bool(c["marketing_consent"])
        re_reg = bool(c["is_re_registered"])
        welcome = _status(c["welcome_bonus_at"], re_reg)
        # ADR-39: ilk kasa damgasını bekleyen hediye henüz verilmedi ama kullanılmış da sayılmaz ("none")
        pwa = _status(c["pwa_bonus_at"], re_reg or (bool(c["pwa_bonus_claimed"]) and not c["pwa_pending"]))
        push = _status(c["push_bonus_at"], re_reg or (bool(c["notif_bonus_claimed"]) and not c["notif_pending"]))
        out[c["id"]] = {
            # Gösterge 1 — hak kullanım geçmişi (uygulama silinse de değişmez): claimed | used | none
            "bonus_history": {
                "welcome": _HISTORY[welcome], "pwa": _HISTORY[pwa], "push": _HISTORY[push],
                "campaign": "claimed" if c["id"] in campaign_ids else "none",
            },
            # Gösterge 2 — şu anki cihaz/izin durumu
            # push_active = kampanya bildirimi gerçekten gidebilir (aktif abonelik + ticari ileti onayı);
            # müdürün "Tüm Abone Müşteriler" sayacıyla birebir aynı tanım.
            "current_status": {
                "push_active": subscribed and consented,
                "push_no_consent": subscribed and not consented,
                "is_pwa": bool(c["pwa_standalone"]),
                "account_recreated": re_reg,
            },
            "welcome_bonus_awarded": bool(c["welcome_bonus_at"]), "welcome_bonus_at": c["welcome_bonus_at"], "welcome_bonus_status": welcome,
            "pwa_bonus_awarded": bool(c["pwa_bonus_at"]), "pwa_bonus_at": c["pwa_bonus_at"], "pwa_bonus_status": pwa,
            "push_bonus_awarded": bool(c["push_bonus_at"]), "push_bonus_at": c["push_bonus_at"], "push_bonus_status": push,
            "is_re_registered": re_reg,
            "recent_stamps_log": recent.get(c["id"], []),
            # Ödüllü kampanyalar (ADR-26): aktif görevler + katıldıkları, kampanya adı ve durumla (kasada onay/ret)
            "campaigns": campaigns.get(c["id"], []),
        }
    return out


def bonus_proof(conn: sqlite3.Connection, customer_id: int) -> dict[str, Any]:
    return bonus_proof_map(conn, [customer_id]).get(customer_id, {})

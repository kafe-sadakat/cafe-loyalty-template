"""
Ödüllü kampanyalar (ADR-26): müşteri görevi yapar (ör. Google'da yorum), "Yaptım" der, doğrulama bekler;
müdür veya kasiyer kontrol edip ONAYLARSA damga verilir. Onaysız hiçbir damga verilmez.

Durumlar: joined (katıldı) -> pending (yaptığını bildirdi, doğrulama bekliyor) -> approved (damga verildi)
                                                                              -> rejected (bulunamadı; tekrar gönderebilir)
Kampanya pasifken yeni katılım / gönderim alınmaz; daha önce gönderilmiş başvurular yine onaylanabilir.
Aynı telefon bir kampanyanın ödülünü bir kez alır (hesap silip yeniden açmak dahil: onaylı satırda telefonun
geri döndürülemez HMAC özeti tutulur, ADR-09 ile aynı ilke).
"""
from __future__ import annotations

import logging
import re
import sqlite3
from typing import Any, Iterable, Optional
from urllib.parse import urlparse

from app.db import log_audit
from app.services import activity
from app.services.directus_sync import sync_customer_to_directus
from app.services.loyalty import _get_fallback_active_seal, get_customer_counts, issue_stamp
from app.services.push import notify_customer_after_stamp
from app.utils import format_phone, get_istanbul_now_iso, mask_phone, phone_hash

logger = logging.getLogger("sadakat")

STATUSES = ("joined", "pending", "approved", "rejected")
STATUS_LABELS = {
    "none": "Katılmadı",
    "joined": "Katıldı (henüz bildirmedi)",
    "pending": "Doğrulama bekliyor",
    "approved": "Onaylandı (damga verildi)",
    "rejected": "Reddedildi",
}
MAX_REWARD_STAMPS = 5
MAX_PROOF_LEN = 60
MIN_PROOF_LEN = 2
MAX_REASON_LEN = 120
MAX_URL_LEN = 500
MAX_ACTION_LABEL_LEN = 40
_CTRL = re.compile(r"[\x00-\x1f\x7f]")


def _clean_text(value: Any, limit: int) -> str:
    """Kontrol karakterlerini atar, boşlukları tekler, sınırda keser."""
    return _CTRL.sub("", " ".join(str(value or "").split()))[:limit].strip()


# ------------------------------------------------------------------ kampanya alanları (müdür formu)
def clean_reward_fields(
    reward_stamps: Any, action_url: Any, action_label: Any
) -> tuple[bool, dict[str, Any], Optional[str]]:
    """
    Ödüllü görev alanlarını doğrular. reward_stamps 0 = düz duyuru (görev alanları tutulmaz), 1-5 = ödüllü görev.
    Görev bağlantısı isteğe bağlıdır; verilirse https:// ile başlayan geçerli bir adres olmalıdır.
    """
    try:
        n = int(reward_stamps or 0)
    except (TypeError, ValueError):
        return False, {}, "Ödül damga sayısı geçersiz."
    if n < 0 or n > MAX_REWARD_STAMPS:
        return False, {}, f"Ödül 0 ile {MAX_REWARD_STAMPS} damga arasında olmalıdır."
    if n == 0:
        return True, {"reward_stamps": 0, "action_url": None, "action_label": None}, None
    url = str(action_url or "").strip()
    if url:
        parsed = urlparse(url)
        if len(url) > MAX_URL_LEN or _CTRL.search(url) or " " in url or parsed.scheme != "https" or not parsed.netloc:
            return False, {}, "Görev bağlantısı https:// ile başlayan geçerli bir adres olmalıdır (ör. Google yorum bağlantınız)."
    label = _clean_text(action_label, MAX_ACTION_LABEL_LEN)
    return True, {"reward_stamps": n, "action_url": url or None, "action_label": label or None}, None


def campaign_stats(conn: sqlite3.Connection, campaign_ids: Iterable[int]) -> dict[int, dict[str, int]]:
    """Kampanya başına katılımcı sayıları: {id: {joined, pending, approved, rejected}}."""
    ids = sorted({int(i) for i in campaign_ids})
    out = {i: {s: 0 for s in STATUSES} for i in ids}
    if not ids:
        return out
    marks = ",".join("?" for _ in ids)
    for r in conn.execute(
        f"SELECT campaign_id, status, COUNT(*) AS n FROM campaign_participants WHERE campaign_id IN ({marks}) "
        f"GROUP BY campaign_id, status",
        ids,
    ):
        if r["status"] in out[r["campaign_id"]]:
            out[r["campaign_id"]][r["status"]] = r["n"]
    return out


def _campaign(conn: sqlite3.Connection, campaign_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT id, title, active, COALESCE(reward_stamps, 0) AS reward_stamps FROM campaigns WHERE id = ?",
        (campaign_id,),
    ).fetchone()


def _participation(conn: sqlite3.Connection, campaign_id: int, customer_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT id, status, proof_name, reject_reason FROM campaign_participants WHERE campaign_id = ? AND customer_id = ?",
        (campaign_id, customer_id),
    ).fetchone()


def _state(conn: sqlite3.Connection, campaign_id: int, customer_id: int) -> dict[str, Any]:
    p = _participation(conn, campaign_id, customer_id)
    return {
        "campaign_id": campaign_id,
        "my_status": p["status"] if p else "none",
        "my_proof_name": p["proof_name"] if p else None,
        "my_reject_reason": p["reject_reason"] if p else None,
    }


def _rewarded_before(conn: sqlite3.Connection, campaign_id: int, customer_id: int) -> bool:
    """Bu müşterinin telefonu bu kampanyanın ödülünü başka (silinmiş) bir hesapla almış mı?"""
    row = conn.execute("SELECT phone_e164 FROM customers WHERE id = ?", (customer_id,)).fetchone()
    ph = phone_hash(row["phone_e164"]) if row and row["phone_e164"] else ""
    if not ph:
        return False
    return conn.execute(
        "SELECT 1 FROM campaign_participants WHERE campaign_id = ? AND status = 'approved' AND phone_hash = ? AND customer_id != ?",
        (campaign_id, ph, customer_id),
    ).fetchone() is not None


_ALREADY_REWARDED = "Bu kampanyanın ödülünü daha önce aldınız; her kampanyadan bir kez yararlanılabilir."


# ------------------------------------------------------------------ müşteri akışı
def join_campaign(conn: sqlite3.Connection, campaign_id: int, customer_id: int) -> dict[str, Any]:
    """Müşteri ödüllü kampanyaya katılır (kayıt). Aktif olmayan kampanyaya katılım alınmaz."""
    camp = _campaign(conn, campaign_id)
    if not camp or camp["reward_stamps"] <= 0:
        return {"status": "error", "message": "Kampanya bulunamadı."}
    if not camp["active"]:
        return {"status": "inactive", "message": "Bu kampanya şu anda aktif değil."}
    if _rewarded_before(conn, campaign_id, customer_id):
        return {"status": "error", "message": _ALREADY_REWARDED}
    cur = conn.execute(
        "INSERT OR IGNORE INTO campaign_participants (campaign_id, customer_id, status, joined_at) VALUES (?, ?, 'joined', ?)",
        (campaign_id, customer_id, get_istanbul_now_iso()),
    )
    if cur.rowcount:
        log_audit(conn, f"customer:{customer_id}", "campaign_joined", f"Ödüllü kampanyaya katıldı: #{campaign_id} '{camp['title']}'.")
    return {"status": "success", **_state(conn, campaign_id, customer_id)}


def submit_campaign(conn: sqlite3.Connection, campaign_id: int, customer_id: int, proof_name: Any) -> dict[str, Any]:
    """
    Müşteri görevi yaptığını bildirir ("Yorumumu yaptım") ve yorumda görünen adını yazar → 'pending'.
    Damga BURADA VERİLMEZ; yalnızca müdür / kasiyer onayıyla verilir. Reddedilen başvuru yeniden gönderilebilir.
    """
    camp = _campaign(conn, campaign_id)
    if not camp or camp["reward_stamps"] <= 0:
        return {"status": "error", "message": "Kampanya bulunamadı."}
    if not camp["active"]:
        return {"status": "inactive", "message": "Bu kampanya şu anda aktif değil."}
    name = _clean_text(proof_name, MAX_PROOF_LEN)
    if len(name) < MIN_PROOF_LEN:
        return {"status": "error", "message": "Yorumu hangi adla yaptığınızı yazın (doğrulama için gerekli)."}
    if _rewarded_before(conn, campaign_id, customer_id):
        return {"status": "error", "message": _ALREADY_REWARDED}
    now = get_istanbul_now_iso()
    p = _participation(conn, campaign_id, customer_id)
    if p and p["status"] == "approved":
        return {"status": "already_approved", "message": "Bu kampanyanın damgası zaten kartınıza eklendi. 🎉", **_state(conn, campaign_id, customer_id)}
    if p:
        conn.execute(
            "UPDATE campaign_participants SET status = 'pending', proof_name = ?, submitted_at = ?, reject_reason = NULL, "
            "reviewed_at = NULL, reviewed_by = NULL WHERE id = ?",
            (name, now, p["id"]),
        )
    else:
        conn.execute(
            "INSERT INTO campaign_participants (campaign_id, customer_id, status, proof_name, joined_at, submitted_at) "
            "VALUES (?, ?, 'pending', ?, ?, ?)",
            (campaign_id, customer_id, name, now, now),
        )
    log_audit(conn, f"customer:{customer_id}", "campaign_submitted", f"Kampanya #{campaign_id} '{camp['title']}' için doğrulama istendi.")
    return {
        "status": "success",
        "message": "Teşekkürler! Yorumun kontrol edildikten sonra damgan kartına eklenecek. ⏳",
        **_state(conn, campaign_id, customer_id),
    }


# ------------------------------------------------------------------ doğrulama (müdür / kasiyer)
def review_participant(
    conn: sqlite3.Connection,
    campaign_id: int,
    customer_id: int,
    action: str,
    reviewer: str,
    reason: str = "",
    audit_actor: str = "",
) -> dict[str, Any]:
    """
    action='approve': kampanyanın ödül damgaları verilir (günlük limite takılmaz, bir kez). Müşteri "Yaptım" demeden
    kasada yorumunu gösterdiyse de onaylanabilir. action='reject': damga verilmez; müşteri tekrar gönderebilir.
    `reviewer` damganın 'işlemi yapan' kullanıcı adıdır (rapor).
    """
    if action not in ("approve", "reject"):
        return {"status": "error", "message": "Geçersiz işlem."}
    camp = _campaign(conn, campaign_id)
    if not camp or camp["reward_stamps"] <= 0:
        return {"status": "error", "message": "Kampanya bulunamadı."}
    cust = conn.execute(
        "SELECT id, phone_e164, display_name FROM customers WHERE id = ? AND deleted_at IS NULL", (customer_id,)
    ).fetchone()
    if not cust:
        return {"status": "error", "message": "Müşteri bulunamadı."}
    p = _participation(conn, campaign_id, customer_id)
    if p and p["status"] == "approved":
        return {"status": "already_approved", "message": "Bu müşteri bu kampanyanın ödülünü zaten aldı."}
    now = get_istanbul_now_iso()
    who = audit_actor or reviewer or "panel"

    if action == "reject":
        if not p:
            return {"status": "error", "message": "Bu müşterinin bu kampanyada başvurusu yok."}
        clean_reason = _clean_text(reason, MAX_REASON_LEN) or "Yorum bulunamadı."
        conn.execute(
            "UPDATE campaign_participants SET status = 'rejected', reviewed_at = ?, reviewed_by = ?, reject_reason = ? WHERE id = ?",
            (now, reviewer or None, clean_reason, p["id"]),
        )
        log_audit(conn, who, "campaign_reward_rejected",
                  f"Kampanya #{campaign_id} '{camp['title']}': müşteri #{customer_id} reddedildi ({clean_reason}).")
        activity.record_campaign(conn, customer_id, camp["title"], False, who, reason=clean_reason,
                                 stamps_after=get_customer_counts(conn, customer_id)[0])
        return {"status": "success", "message": "Başvuru reddedildi; damga verilmedi.", "participant_status": "rejected"}

    if _rewarded_before(conn, campaign_id, customer_id):
        return {"status": "error", "message": "Bu telefon numarası bu kampanyanın ödülünü daha önce (silinmiş bir hesapla) aldı."}
    if not p:
        conn.execute(
            "INSERT INTO campaign_participants (campaign_id, customer_id, status, joined_at) VALUES (?, ?, 'joined', ?)",
            (campaign_id, customer_id, now),
        )
    seal = _get_fallback_active_seal(conn)
    seal_id = seal["id"] if seal else None
    n = int(camp["reward_stamps"])
    unlocked_any, stamp_target, reward_name = False, 5, ""
    for _ in range(n):
        unlocked, stamp_target, reward_name = issue_stamp(
            conn, customer_id, seal_id, now, source="campaign", actor=reviewer or "", note=camp["title"]
        )
        unlocked_any = unlocked_any or unlocked
    conn.execute(
        """
        UPDATE campaign_participants
        SET status = 'approved', reviewed_at = ?, reviewed_by = ?, reject_reason = NULL, stamps_awarded = ?,
            phone_hash = ?, submitted_at = COALESCE(submitted_at, ?)
        WHERE campaign_id = ? AND customer_id = ?
        """,
        (now, reviewer or None, n, phone_hash(cust["phone_e164"]), now, campaign_id, customer_id),
    )
    log_audit(conn, who, "campaign_reward_approved",
              f"Kampanya #{campaign_id} '{camp['title']}': müşteri #{customer_id} onaylandı, +{n} damga verildi.")

    current_stamps, usable = get_customer_counts(conn, customer_id)
    activity.record_campaign(conn, customer_id, camp["title"], True, who, stamps=n, stamps_after=current_stamps,
                             reward_unlocked=unlocked_any)
    display = cust["display_name"] or f"user{customer_id}"
    sync_customer_to_directus(
        phone_e164=cust["phone_e164"], display_name=display, current_stamps=current_stamps, usable_rewards=usable,
        add_stamp_code=f"CAMPAIGN_{campaign_id}",
        stamp_reason=f"{display} ({format_phone(cust['phone_e164'])}) - Kampanya: {camp['title']}",
    )
    try:
        notify_customer_after_stamp(
            conn=conn, customer_id=customer_id, current_stamps=current_stamps, stamp_target=stamp_target,
            reward_unlocked=unlocked_any, reward_name=reward_name,
        )
    except Exception as exc:  # bildirim hatası onayı asla bozmaz
        logger.warning("Kampanya onayı sonrası bildirim hatası yoksayıldı: %s", exc)
    return {
        "status": "success",
        "message": f"Onaylandı: +{n} damga verildi." + (" Ödül kahvesi hazır! 🎁" if unlocked_any else ""),
        "participant_status": "approved",
        "reward_unlocked": unlocked_any,
        "current_stamps": current_stamps,
        "usable_rewards_count": usable,
    }


# ------------------------------------------------------------------ listeler (panel)
def list_participants(conn: sqlite3.Connection, campaign_id: int) -> list[dict[str, Any]]:
    """Kampanyaya katılanlar (kampanya adıyla birlikte panelde): önce doğrulama bekleyenler."""
    rows = conn.execute(
        """
        SELECT p.customer_id, p.status, p.proof_name, p.joined_at, p.submitted_at, p.reviewed_at, p.reviewed_by,
               p.reject_reason, p.stamps_awarded, c.display_name, c.phone_e164, c.deleted_at,
               COALESCE(c.is_re_registered, 0) AS is_re_registered
        FROM campaign_participants p JOIN customers c ON c.id = p.customer_id
        WHERE p.campaign_id = ?
        ORDER BY CASE p.status WHEN 'pending' THEN 0 WHEN 'joined' THEN 1 WHEN 'rejected' THEN 2 ELSE 3 END,
                 COALESCE(p.submitted_at, p.joined_at) DESC
        LIMIT 500
        """,
        (campaign_id,),
    ).fetchall()
    name_counts: dict[str, int] = {}
    for r in rows:
        key = (r["proof_name"] or "").strip().lower()
        if key:
            name_counts[key] = name_counts.get(key, 0) + 1
    out = []
    for r in rows:
        deleted = bool(r["deleted_at"])
        key = (r["proof_name"] or "").strip().lower()
        out.append({
            "customer_id": r["customer_id"],
            "display_name": "(silinmiş hesap)" if deleted else ((r["display_name"] or "").strip() or f"user{r['customer_id']}"),
            "formatted_phone": "—" if deleted else format_phone(r["phone_e164"]),
            "masked_phone": "—" if deleted else mask_phone(r["phone_e164"]),
            "deleted": deleted,
            "status": r["status"],
            "status_label": STATUS_LABELS.get(r["status"], r["status"]),
            "proof_name": r["proof_name"],
            "duplicate_name": bool(key) and name_counts.get(key, 0) > 1,
            "re_registered": bool(r["is_re_registered"]),
            "joined_at": r["joined_at"],
            "submitted_at": r["submitted_at"],
            "reviewed_at": r["reviewed_at"],
            "reviewed_by": r["reviewed_by"],
            "reject_reason": r["reject_reason"],
            "stamps_awarded": r["stamps_awarded"],
        })
    return out


def campaign_participation_map(conn: sqlite3.Connection, customer_ids: Iterable[int]) -> dict[int, list[dict[str, Any]]]:
    """
    Müşteri kartları (kasa + müdür) için: her müşteride AKTİF ödüllü kampanyalar ve (pasif olsa da) katıldığı
    kampanyalar, kampanya adı ve durumla. Kasiyer buradan onaylar / reddeder.
    """
    ids = sorted({int(i) for i in customer_ids})
    if not ids:
        return {}
    camps = conn.execute(
        "SELECT id, title, active, COALESCE(reward_stamps, 0) AS reward_stamps FROM campaigns "
        "WHERE COALESCE(reward_stamps, 0) > 0 ORDER BY id DESC"
    ).fetchall()
    if not camps:
        return {i: [] for i in ids}
    marks = ",".join("?" for _ in ids)
    parts: dict[int, dict[int, sqlite3.Row]] = {}
    for p in conn.execute(
        f"SELECT campaign_id, customer_id, status, proof_name, submitted_at, reviewed_at, reviewed_by, reject_reason "
        f"FROM campaign_participants WHERE customer_id IN ({marks})",
        ids,
    ):
        parts.setdefault(p["customer_id"], {})[p["campaign_id"]] = p
    out: dict[int, list[dict[str, Any]]] = {}
    for cid in ids:
        items = []
        for camp in camps:
            p = parts.get(cid, {}).get(camp["id"])
            if not camp["active"] and not p:
                continue
            status = p["status"] if p else "none"
            items.append({
                "campaign_id": camp["id"],
                "title": camp["title"],
                "reward_stamps": camp["reward_stamps"],
                "active": bool(camp["active"]),
                "status": status,
                "status_label": STATUS_LABELS.get(status, status),
                "proof_name": p["proof_name"] if p else None,
                "submitted_at": p["submitted_at"] if p else None,
                "reviewed_at": p["reviewed_at"] if p else None,
                "reviewed_by": p["reviewed_by"] if p else None,
                "reject_reason": p["reject_reason"] if p else None,
            })
        out[cid] = items
    return out

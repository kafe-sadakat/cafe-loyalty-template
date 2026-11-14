"""
Çalışan (kasiyer) paneli işlemleri — dar yetkili: damga ekle, müşteri kaydet, hediye kullandır.
Müşteri silme, ürün/fiyat, kampanya ve bildirim yetkisi YOKTUR. Her işlem staff_audit_logs'a yazılır.
Müşteri araması yalnızca tam telefon numarasıyla yapılır (liste/toplu veri çekme yoktur).
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
from typing import Any, Optional

from app.db import get_all_settings, log_audit
from app.services import activity
from app.services.admin import admin_create_customer, admin_redeem_reward
from app.services.campaign_rewards import review_participant
from app.services.directus_sync import sync_customer_to_directus
from app.services.loyalty import (
    _get_fallback_active_seal,
    count_today_stamps,
    get_customer_counts,
    issue_stamp,
)
from app.services.proof import bonus_proof
from app.services.push import notify_customer_after_stamp
from app.utils import (
    format_phone,
    get_istanbul_now_iso,
    mask_phone,
    normalize_phone,
)


PBKDF2_ITERATIONS = 240_000
MIN_STAFF_PASSWORD_LEN = 8


# ------------------------------------------------------------------ personel hesapları (müdür yönetir)
def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ITERATIONS).hex()
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iters, salt, digest = stored.split("$")
        if scheme != "pbkdf2_sha256":
            return False
        calc = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), int(iters)).hex()
        return hmac.compare_digest(calc, digest)
    except Exception:
        return False


# Zamanlama farkından hesap varlığı sızmasın diye var olmayan hesapta da aynı maliyette hesap yapılır.
_DUMMY_HASH = hash_password("dummy-parola-zamanlama-korumasi")


def actor_username(conn: sqlite3.Connection, staff_id: Optional[int], staff_name: str) -> str:
    """İşlemi yapan kullanıcının kullanıcı adı (rapor için). Personel kimliği yoksa (müdür kasa modunda) müdür hesabı aranır."""
    if staff_id is not None:
        row = conn.execute("SELECT username FROM admin_users WHERE id = ?", (staff_id,)).fetchone()
        if row:
            return row["username"]
    row = conn.execute(
        "SELECT username FROM admin_users WHERE role = 'manager' AND display_name = ? ORDER BY id LIMIT 1", (staff_name,)
    ).fetchone()
    return row["username"] if row else (staff_name or "")


def log_staff_action(
    conn: sqlite3.Connection,
    staff_name: str,
    action: str,
    customer_id: Optional[int] = None,
    phone_masked: str = "",
    detail: str = "",
    ip: str = "",
    staff_id: Optional[int] = None,
) -> None:
    conn.execute(
        """
        INSERT INTO staff_audit_logs (at, staff_name, action, customer_id, customer_phone_masked, detail, ip, staff_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (get_istanbul_now_iso(), (staff_name or "?")[:40], action, customer_id, phone_masked, detail[:500], ip[:64], staff_id),
    )


def _find_customer(
    conn: sqlite3.Connection, raw_phone: Optional[str], customer_id: Optional[int] = None
) -> tuple[Optional[sqlite3.Row], Optional[str]]:
    """Müşteriyi tam telefon numarasıyla ya da (arama sonucundan gelen) müşteri kimliğiyle bulur."""
    if customer_id:
        row = conn.execute(
            "SELECT id, phone_e164, display_name FROM customers WHERE id = ? AND deleted_at IS NULL", (customer_id,)
        ).fetchone()
        if not row:
            return None, "Müşteri bulunamadı."
        return row, None
    try:
        phone = normalize_phone(raw_phone or "")
    except ValueError as exc:
        return None, str(exc)
    row = conn.execute(
        "SELECT id, phone_e164, display_name FROM customers WHERE phone_e164 = ? AND deleted_at IS NULL",
        (phone,),
    ).fetchone()
    if not row:
        return None, "Bu numaraya kayıtlı müşteri bulunamadı. Önce 'Yeni Müşteri' sekmesinden kaydedin."
    return row, None


def customer_view(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    settings = get_all_settings(conn)
    stamps, rewards = get_customer_counts(conn, row["id"])
    extra = conn.execute(
        "SELECT c.created_at, (SELECT MAX(created_at) FROM stamps s WHERE s.customer_id = c.id AND s.voided_at IS NULL) AS last_visit "
        "FROM customers c WHERE c.id = ?",
        (row["id"],),
    ).fetchone()
    return {
        **bonus_proof(conn, row["id"]),  # kanıt tablosu: bonus durumları + son 5 damga
        "id": row["id"],
        "display_name": (row["display_name"] or f"user{row['id']}"),
        "masked_phone": mask_phone(row["phone_e164"]),
        "created_at": extra["created_at"] if extra else None,
        "last_visit": extra["last_visit"] if extra else None,
        "current_stamps": stamps,
        "stamp_target": int(settings.get("stamp_target", "5") or 5),
        "usable_rewards": rewards,
        "today_stamps": count_today_stamps(conn, row["id"]),
        "daily_limit": int(settings.get("daily_limit", "2") or 2),
        "reward_name": settings.get("reward_name", ""),
    }


def staff_search(conn: sqlite3.Connection, query: str, staff_name: str, ip: str, staff_id: Optional[int] = None) -> dict[str, Any]:
    """
    Telefon numarası (5XX…) veya kullanıcı adı ile müşteri arar (en çok 5 sonuç).
    Liste/toplu veri çekmeyi önlemek için en az 3 karakter gerekir; telefon aramasında en az 7 rakam.
    """
    q = (query or "").strip()
    if len(q) < 3:
        return {"status": "error", "message": "Aramak için en az 3 karakter yazın."}
    like_escape = lambda s: s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    name_expr = "LOWER(COALESCE(NULLIF(TRIM(display_name), ''), 'user' || id))"
    rows: list[sqlite3.Row] = []
    if re.fullmatch(r"[\d\s()+.-]+", q):
        digits = re.sub(r"\D", "", q)
        if digits.startswith("90") and len(digits) > 10:
            digits = digits[2:]
        digits = digits.lstrip("0")
        if len(digits) < 7:
            return {"status": "error", "message": "Telefon için en az 7 rakam yazın."}
        rows = conn.execute(
            "SELECT id, phone_e164, display_name FROM customers WHERE deleted_at IS NULL "
            "AND phone_e164 LIKE ? ESCAPE '\\' ORDER BY id DESC LIMIT 5",
            (f"%{like_escape(digits)}%",),
        ).fetchall()
    else:
        needle = q.lower()
        rows = conn.execute(
            f"SELECT id, phone_e164, display_name FROM customers WHERE deleted_at IS NULL AND "
            f"({name_expr} = ? OR {name_expr} LIKE ? ESCAPE '\\') "
            f"ORDER BY ({name_expr} = ?) DESC, id DESC LIMIT 5",
            (needle, f"{like_escape(needle)}%", needle),
        ).fetchall()
    if not rows:
        return {"status": "not_found", "message": "Bu telefon numarası / kullanıcı adı ile kayıtlı müşteri bulunamadı."}
    customers = [customer_view(conn, r) for r in rows]
    log_staff_action(
        conn, staff_name, "customer_search", rows[0]["id"] if len(rows) == 1 else None,
        mask_phone(rows[0]["phone_e164"]) if len(rows) == 1 else "", f"{len(rows)} sonuç", ip, staff_id=staff_id,
    )
    return {"status": "success", "customers": customers}


def staff_lookup(
    conn: sqlite3.Connection, raw_phone: Optional[str], staff_name: str, ip: str,
    staff_id: Optional[int] = None, customer_id: Optional[int] = None,
) -> dict[str, Any]:
    row, err = _find_customer(conn, raw_phone, customer_id)
    if not row:
        return {"status": "not_found", "message": err}
    log_staff_action(conn, staff_name, "customer_lookup", row["id"], mask_phone(row["phone_e164"]), "", ip, staff_id=staff_id)
    return {"status": "success", "customer": customer_view(conn, row)}


def staff_add_stamp(
    conn: sqlite3.Connection, raw_phone: Optional[str], staff_name: str, ip: str,
    staff_id: Optional[int] = None, customer_id: Optional[int] = None,
) -> dict[str, Any]:
    """+1 damga (müşterinin günlük damga limiti geçerlidir)."""
    row, err = _find_customer(conn, raw_phone, customer_id)
    if not row:
        return {"status": "not_found", "message": err}

    masked = mask_phone(row["phone_e164"])
    settings = get_all_settings(conn)
    daily_limit = int(settings.get("daily_limit", "2") or 2)
    today = count_today_stamps(conn, row["id"])
    if today >= daily_limit:
        log_staff_action(conn, staff_name, "stamp_denied_daily_limit", row["id"], masked, f"Günlük limit {daily_limit}", ip, staff_id=staff_id)
        activity.record_staff_denied(conn, row["id"], staff_name, today, daily_limit,
                                     stamps_after=get_customer_counts(conn, row["id"])[0], ip=ip)
        return {
            "status": "limit_reached",
            "message": f"Bu müşteri bugünkü damga limitine ({daily_limit}) ulaştı.",
            "customer": customer_view(conn, row),
        }

    seal = _get_fallback_active_seal(conn)
    now_iso = get_istanbul_now_iso()
    reward_unlocked, stamp_target, reward_name = issue_stamp(
        conn, row["id"], seal["id"] if seal else None, now_iso,
        source="staff", actor=actor_username(conn, staff_id, staff_name),
    )
    log_audit(conn, f"staff:{staff_name}", "staff_stamp_added", f"Müşteri #{row['id']} için çalışan +1 damga ekledi.")
    log_staff_action(conn, staff_name, "stamp_added", row["id"], masked, "reward_unlocked" if reward_unlocked else "", ip, staff_id=staff_id)

    stamps, rewards = get_customer_counts(conn, row["id"])
    activity.record_staff_stamp(conn, row["id"], staff_name, stamps, reward_unlocked, ip=ip)
    display = row["display_name"] or f"user{row['id']}"
    sync_customer_to_directus(
        phone_e164=row["phone_e164"],
        display_name=display,
        current_stamps=stamps,
        usable_rewards=rewards,
        add_stamp_code=f"STAFF_{staff_name[:12]}",
        stamp_reason=f"{display} ({format_phone(row['phone_e164'])}) - Çalışan +1 damga",
    )
    try:
        notify_customer_after_stamp(
            conn=conn, customer_id=row["id"], current_stamps=stamps, stamp_target=stamp_target,
            reward_unlocked=reward_unlocked, reward_name=reward_name,
        )
    except Exception:
        pass

    return {
        "status": "success",
        "message": "Ödül kazanıldı! 🎁" if reward_unlocked else "+1 damga eklendi.",
        "reward_unlocked": reward_unlocked,
        "customer": customer_view(conn, row),
    }


def staff_register_customer(
    conn: sqlite3.Connection,
    raw_phone: str,
    display_name: Optional[str],
    consent_confirmed: bool,
    staff_name: str,
    ip: str,
    staff_id: Optional[int] = None,
) -> dict[str, Any]:
    """
    Yeni müşteri kaydı (kasada). Çalışan, müşterinin Aydınlatma Metni ile bilgilendirildiğini onaylamalıdır.
    Kampanya (ticari ileti) onayı kasada ALINMAZ: müşteri isterse kendi uygulamasındaki anahtarla açar.
    """
    try:
        phone = normalize_phone(raw_phone or "")
    except ValueError as exc:
        return {"status": "error", "message": str(exc)}

    existing = conn.execute(
        "SELECT id, phone_e164, display_name FROM customers WHERE phone_e164 = ? AND deleted_at IS NULL", (phone,)
    ).fetchone()
    if existing:
        return {"status": "exists", "message": "Bu numara zaten kayıtlı.", "customer": customer_view(conn, existing)}

    if not consent_confirmed:
        return {
            "status": "consent_required",
            "message": "Müşterinin KVKK Aydınlatma Metni ile bilgilendirildiğini onaylayın.",
        }

    ok, summary, err = admin_create_customer(
        conn, phone, display_name,
        consent={"kvkk_read": True, "ip": ip},
        actor=f"staff:{staff_name}",
    )
    if not ok or not summary:
        return {"status": "error", "message": err or "Müşteri kaydedilemedi."}
    row = conn.execute("SELECT id, phone_e164, display_name FROM customers WHERE id = ?", (summary["id"],)).fetchone()
    log_staff_action(conn, staff_name, "customer_registered", row["id"], mask_phone(phone), "Aydınlatma bilgilendirmesi çalışan onayıyla kaydedildi", ip, staff_id=staff_id)
    return {"status": "success", "message": "Müşteri kaydedildi.", "customer": customer_view(conn, row)}


def staff_redeem_reward(
    conn: sqlite3.Connection, raw_phone: Optional[str], staff_name: str, ip: str,
    staff_id: Optional[int] = None, customer_id: Optional[int] = None,
) -> dict[str, Any]:
    """🎁 Hediye kahveyi kullandırır (müşterinin en eski kullanılabilir ödül bileti)."""
    row, err = _find_customer(conn, raw_phone, customer_id)
    if not row:
        return {"status": "not_found", "message": err}
    masked = mask_phone(row["phone_e164"])
    ok, _summary, redeem_err = admin_redeem_reward(
        conn, row["id"], actor=f"staff:{staff_name}", redeemed_by=actor_username(conn, staff_id, staff_name)
    )
    if not ok:
        log_staff_action(conn, staff_name, "redeem_denied", row["id"], masked, redeem_err or "", ip, staff_id=staff_id)
        activity.record_redeem(conn, row["id"], f"staff:{staff_name}", ok=False,
                               stamps_after=get_customer_counts(conn, row["id"])[0], ip=ip)
        return {"status": "no_reward", "message": redeem_err or "Kullanılabilir ödül yok.", "customer": customer_view(conn, row)}
    log_staff_action(conn, staff_name, "reward_redeemed", row["id"], masked, "", ip, staff_id=staff_id)
    return {"status": "success", "message": "Hediye kahve kullandırıldı. Afiyet olsun! ☕", "customer": customer_view(conn, row)}


def staff_campaign_review(
    conn: sqlite3.Connection, customer_id: int, campaign_id: int, action: str, reason: str,
    staff_name: str, ip: str, staff_id: Optional[int] = None,
) -> dict[str, Any]:
    """
    Ödüllü kampanya doğrulaması kasada: kasiyer müşterinin telefonunda yorumunu görür → 'approve' (kampanya damgası,
    günlük limite takılmaz, kampanya başına bir kez) veya 'reject'. Her işlem personel günlüğüne yazılır.
    """
    row, err = _find_customer(conn, None, customer_id)
    if not row:
        return {"status": "not_found", "message": err}
    res = review_participant(
        conn, campaign_id, row["id"], action, reviewer=actor_username(conn, staff_id, staff_name),
        reason=reason or "", audit_actor=f"staff:{staff_name}",
    )
    if res.get("status") == "success":
        log_staff_action(
            conn, staff_name, "campaign_approved" if action == "approve" else "campaign_rejected", row["id"],
            mask_phone(row["phone_e164"]), f"Kampanya #{campaign_id}", ip, staff_id=staff_id,
        )
    res["customer"] = customer_view(conn, row)
    return res


def list_staff_logs(conn: sqlite3.Connection, limit: int = 200) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM staff_audit_logs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]

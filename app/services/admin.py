import io
import re
import shutil
import urllib.parse
import sqlite3
from pathlib import Path
import qrcode
from datetime import datetime, timedelta
from typing import Optional, Any
from app.utils import (
    normalize_phone,
    mask_phone,
    format_phone,
    hash_token,
    generate_token,
    get_istanbul_now,
    get_istanbul_now_iso,
    get_today_bounds_istanbul
)
from app.config import DEFAULT_BUSINESS_NAME
from app.db import get_all_settings, set_setting, log_audit
from app.services import activity
from app.services.directus_sync import (
    sync_customer_to_directus,
    sync_campaign_to_directus,
    delete_campaign_from_directus,
)
from app.services.campaign_rewards import campaign_stats, clean_reward_fields
from app.services.fraud import fraud_level
from app.services.proof import bonus_proof_map
from app.services.push import notify_customer_after_stamp
from app.services.uploads import UploadError, normalize_image_ref


def get_dashboard_metrics(conn: sqlite3.Connection) -> dict[str, Any]:
    """Yönetim paneli özet ölçümlerini hesaplar."""
    cursor = conn.cursor()
    now = get_istanbul_now()

    # 1. Toplam Müşteri
    cursor.execute("SELECT COUNT(*) as count FROM customers WHERE deleted_at IS NULL")
    total_customers = cursor.fetchone()["count"]

    # 2. Yeni Müşteriler (Bugün, 7 gün, 30 gün)
    today_start, today_end = get_today_bounds_istanbul()
    cursor.execute("""
        SELECT COUNT(*) as count FROM customers
        WHERE deleted_at IS NULL AND created_at >= ? AND created_at <= ?
    """, (today_start, today_end))
    new_customers_today = cursor.fetchone()["count"]

    seven_days_ago = (now - timedelta(days=7)).isoformat()
    cursor.execute("""
        SELECT COUNT(*) as count FROM customers
        WHERE deleted_at IS NULL AND created_at >= ?
    """, (seven_days_ago,))
    new_customers_7d = cursor.fetchone()["count"]

    thirty_days_ago = (now - timedelta(days=30)).isoformat()
    cursor.execute("""
        SELECT COUNT(*) as count FROM customers
        WHERE deleted_at IS NULL AND created_at >= ?
    """, (thirty_days_ago,))
    new_customers_30d = cursor.fetchone()["count"]

    # 3. Günlük Damga Grafiği (Son 14 gün)
    daily_chart = []
    month_names_tr = ["Oca", "Şub", "Mar", "Nis", "May", "Haz", "Tem", "Ağu", "Eyl", "Eki", "Kas", "Ara"]

    for i in range(13, -1, -1):
        target_day = now - timedelta(days=i)
        day_start = datetime(target_day.year, target_day.month, target_day.day, 0, 0, 0, tzinfo=now.tzinfo).isoformat()
        day_end = datetime(target_day.year, target_day.month, target_day.day, 23, 59, 59, 999999, tzinfo=now.tzinfo).isoformat()

        cursor.execute("""
            SELECT COUNT(*) as count FROM stamps
            WHERE voided_at IS NULL AND created_at >= ? AND created_at <= ?
        """, (day_start, day_end))
        stamp_count = cursor.fetchone()["count"]

        label = f"{target_day.day} {month_names_tr[target_day.month - 1]}"
        daily_chart.append({
            "date": target_day.strftime("%Y-%m-%d"),
            "label": label,
            "count": stamp_count
        })

    # 4. Tekrar Gelen Müşteri Oranı (2 veya daha fazla damga alanlar)
    cursor.execute("""
        SELECT COUNT(DISTINCT customer_id) as count
        FROM stamps
        WHERE voided_at IS NULL
    """)
    customers_with_stamps = cursor.fetchone()["count"]

    cursor.execute("""
        SELECT COUNT(*) as count FROM (
            SELECT customer_id FROM stamps
            WHERE voided_at IS NULL
            GROUP BY customer_id
            HAVING COUNT(*) >= 2
        )
    """)
    repeat_customers = cursor.fetchone()["count"]

    repeat_rate = 0.0
    if customers_with_stamps > 0:
        repeat_rate = round((repeat_customers / customers_with_stamps) * 100, 1)

    # 5. Üretilen ve Kullanılan Ödül Sayısı
    cursor.execute("SELECT COUNT(*) as count FROM rewards WHERE voided_at IS NULL")
    total_rewards_issued = cursor.fetchone()["count"]

    cursor.execute("SELECT COUNT(*) as count FROM rewards WHERE redeemed_at IS NOT NULL AND voided_at IS NULL")
    total_rewards_redeemed = cursor.fetchone()["count"]

    cursor.execute("SELECT COUNT(*) as count FROM stamps WHERE voided_at IS NULL")
    total_stamps_issued = cursor.fetchone()["count"]

    # 6. Ortalama Ziyaret Aralığı (gün cinsinden)
    cursor.execute("""
        SELECT customer_id, created_at
        FROM stamps
        WHERE voided_at IS NULL
        ORDER BY customer_id, created_at ASC
    """)
    all_stamps = cursor.fetchall()
    intervals = []
    last_seen: dict[int, datetime] = {}
    for row in all_stamps:
        cid = row["customer_id"]
        try:
            stamp_dt = datetime.fromisoformat(row["created_at"])
            if cid in last_seen:
                diff_days = (stamp_dt - last_seen[cid]).total_seconds() / 86400.0
                if diff_days > 0.05:
                    intervals.append(diff_days)
            last_seen[cid] = stamp_dt
        except Exception:
            pass

    avg_interval_days = round(sum(intervals) / len(intervals), 1) if intervals else 0.0

    return {
        "total_customers": total_customers,
        "new_customers_today": new_customers_today,
        "new_customers_7d": new_customers_7d,
        "new_customers_30d": new_customers_30d,
        "daily_chart": daily_chart,
        "repeat_customers": repeat_customers,
        "customers_with_stamps": customers_with_stamps,
        "repeat_rate": repeat_rate,
        "total_stamps_issued": total_stamps_issued,
        "total_rewards_issued": total_rewards_issued,
        "total_rewards_redeemed": total_rewards_redeemed,
        "avg_interval_days": avg_interval_days
    }


def get_all_customers_admin(conn: sqlite3.Connection, search: Optional[str] = None) -> list[dict[str, Any]]:
    """Yönetim paneli için müşterileri isim (user1 vb.), telefon, damga ve bilet durumlarıyla listeler."""
    cursor = conn.cursor()
    query = """
        SELECT
            c.id,
            c.phone_e164,
            c.display_name,
            c.created_at,
            COALESCE(c.marketing_consent, 0) as marketing_consent,
            COALESCE(c.fraud_risk_score, 0) as fraud_risk_score,
            c.explicit_consent_at,
            (SELECT COUNT(*) FROM stamps s WHERE s.customer_id = c.id AND s.voided_at IS NULL AND s.reward_id IS NULL) as current_stamps,
            (SELECT COUNT(*) FROM rewards r WHERE r.customer_id = c.id AND r.redeemed_at IS NULL AND r.voided_at IS NULL) as pending_rewards,
            (SELECT MIN(created_at) FROM stamps s WHERE s.customer_id = c.id AND s.voided_at IS NULL) as first_visit,
            (SELECT MAX(created_at) FROM stamps s WHERE s.customer_id = c.id AND s.voided_at IS NULL) as last_visit
        FROM customers c
        WHERE c.deleted_at IS NULL
    """
    params = []
    if search and search.strip():
        q = f"%{search.strip()}%"
        query += " AND (c.phone_e164 LIKE ? OR COALESCE(c.display_name, 'user' || c.id) LIKE ?)"
        params.extend([q, q])

    query += " ORDER BY c.id DESC LIMIT 300"

    cursor.execute(query, params)
    rows = cursor.fetchall()

    proofs = bonus_proof_map(conn, [r["id"] for r in rows])
    result = []
    for r in rows:
        cid = r["id"]
        display_name = (r["display_name"] or "").strip() or f"user{cid}"
        result.append({
            **proofs.get(cid, {}),
            "id": cid,
            "display_name": display_name,
            "phone_e164": r["phone_e164"],
            "masked_phone": mask_phone(r["phone_e164"]),
            "formatted_phone": format_phone(r["phone_e164"]),
            "current_stamps": r["current_stamps"],
            "pending_rewards": r["pending_rewards"],
            "first_visit": r["first_visit"],
            "last_visit": r["last_visit"],
            "created_at": r["created_at"],
            "marketing_consent": bool(r["marketing_consent"]),
            "explicit_consent_at": r["explicit_consent_at"],
            "fraud_risk_score": r["fraud_risk_score"],
            "fraud_level": fraud_level(r["fraud_risk_score"]),
        })
    return result


def _get_customer_sync_summary(conn: sqlite3.Connection, customer_id: int) -> Optional[dict[str, Any]]:
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT
            c.id,
            c.phone_e164,
            c.display_name,
            (SELECT COUNT(*) FROM stamps s WHERE s.customer_id = c.id AND s.voided_at IS NULL AND s.reward_id IS NULL) as current_stamps,
            (SELECT COUNT(*) FROM rewards r WHERE r.customer_id = c.id AND r.redeemed_at IS NULL AND r.voided_at IS NULL) as pending_rewards
        FROM customers c
        WHERE c.id = ? AND c.deleted_at IS NULL
        """,
        (customer_id,),
    )
    row = cursor.fetchone()
    if not row:
        return None
    display_name = (row["display_name"] or "").strip() or f"user{row['id']}"
    return {
        "id": row["id"],
        "phone_e164": row["phone_e164"],
        "display_name": display_name,
        "formatted_phone": format_phone(row["phone_e164"]),
        "current_stamps": row["current_stamps"],
        "pending_rewards": row["pending_rewards"],
    }


def admin_create_customer(
    conn: sqlite3.Connection,
    raw_phone: str,
    display_name: Optional[str] = None,
    consent: Optional[dict[str, Any]] = None,
    actor: str = "admin",
) -> tuple[bool, Optional[dict[str, Any]], Optional[str]]:
    """
    Panelden yeni müşteri ekler veya mevcut müşteriyi günceller ve Directus ile senkronize eder.
    `consent` (kvkk_read + explicit_consent + ip) verilirse onay kaydı kaynağı `actor` olarak saklanır.
    """
    try:
        phone_e164 = normalize_phone(raw_phone or "")
    except ValueError as e:
        return False, None, str(e)

    cursor = conn.cursor()
    now_iso = get_istanbul_now_iso()
    clean_name = (display_name or "").strip()[:50] or None

    cursor.execute("SELECT id, display_name, deleted_at FROM customers WHERE phone_e164 = ?", (phone_e164,))
    existing = cursor.fetchone()

    if existing:
        cid = existing["id"]
        final_name = clean_name or (existing["display_name"] or "").strip() or f"user{cid}"
        cursor.execute(
            """
            UPDATE customers
            SET deleted_at = NULL, display_name = ?
            WHERE id = ?
            """,
            (final_name, cid),
        )
        log_audit(conn, "admin", "customer_restored_or_updated", f"Müşteri #{cid} ({final_name}) yönetici tarafından güncellendi.")
    else:
        cursor.execute(
            """
            INSERT INTO customers (phone_e164, display_name, created_at)
            VALUES (?, ?, ?)
            """,
            (phone_e164, clean_name, now_iso),
        )
        cid = cursor.lastrowid
        final_name = clean_name or f"user{cid}"
        cursor.execute("UPDATE customers SET display_name = ? WHERE id = ?", (final_name, cid))
        log_audit(conn, actor, "customer_created", f"Yeni müşteri #{cid} ({final_name}) {actor} tarafından eklendi.")
        reg_channel, reg_who = activity.actor_parts(actor)
        activity.record_registration(conn, cid, reg_channel, ip=(consent or {}).get("ip") or "", actor=reg_who)
        # Anti-fraud: hesabını daha önce silmiş numara kasadan yeniden kaydedilse de bonussuz başlar.
        from app.services.loyalty import apply_returning_user_guard
        apply_returning_user_guard(conn, cid, phone_e164, [])

    if consent:
        from app.services.loyalty import record_consent
        record_consent(conn, cid, consent, source=f"panel:{actor}"[:40])

    summary = _get_customer_sync_summary(conn, cid)
    if summary:
        sync_customer_to_directus(
            customer_id=cid,
            phone_e164=summary["phone_e164"],
            display_name=summary["display_name"],
            current_stamps=summary["current_stamps"],
            usable_rewards=summary["pending_rewards"],
            record_claim=False,
        )
    return True, summary, None


def admin_update_customer_name(
    conn: sqlite3.Connection,
    customer_id: int,
    display_name: str,
) -> tuple[bool, Optional[dict[str, Any]], Optional[str]]:
    """Yönetici panelinden müşteri adını günceller ve Directus ile senkronize eder."""
    clean_name = (display_name or "").strip()
    if not clean_name or len(clean_name) < 2 or len(clean_name) > 50:
        return False, None, "Kullanıcı adı 2 ile 50 karakter arasında olmalıdır."

    cursor = conn.cursor()
    cursor.execute("SELECT id FROM customers WHERE id = ? AND deleted_at IS NULL", (customer_id,))
    if not cursor.fetchone():
        return False, None, "Müşteri bulunamadı."

    cursor.execute("UPDATE customers SET display_name = ? WHERE id = ?", (clean_name, customer_id))
    log_audit(conn, "admin", "customer_name_updated", f"Müşteri #{customer_id} adı '{clean_name}' olarak güncellendi.")

    summary = _get_customer_sync_summary(conn, customer_id)
    if summary:
        sync_customer_to_directus(
            customer_id=customer_id,
            phone_e164=summary["phone_e164"],
            display_name=summary["display_name"],
            current_stamps=summary["current_stamps"],
            usable_rewards=summary["pending_rewards"],
            record_claim=False,
        )
    return True, summary, None


def admin_add_stamp(
    conn: sqlite3.Connection,
    customer_id: int,
    actor_username: str = "",
    log_event: bool = True,
    notify: bool = True,
) -> tuple[bool, Optional[dict[str, Any]], Optional[str]]:
    """Yönetici panelinden müşteriye tek tıkla +1 damga ekler, hedef dolarsa ödül üretir ve Directus'a yazar.
    `log_event=False`: "Sayıya Ayarla" döngüsü tek bir işlem kaydı yazar (ADR-31).
    `notify=False`: "Sayıya Ayarla" düzeltmesinde her eklenen damga için ayrı "Damgan eklendi" bildirimi gitmez (E-08)."""
    cursor = conn.cursor()
    cursor.execute("SELECT id, phone_e164, display_name FROM customers WHERE id = ? AND deleted_at IS NULL", (customer_id,))
    cust = cursor.fetchone()
    if not cust:
        return False, None, "Müşteri bulunamadı."

    settings = get_all_settings(conn)
    stamp_target = int(settings.get("stamp_target", "5"))
    reward_name = settings.get("reward_name", "Ücretsiz Kahve")
    business_name = settings.get("business_name", DEFAULT_BUSINESS_NAME)
    now_iso = get_istanbul_now_iso()

    # Aktif mühür bul
    cursor.execute("SELECT id FROM seals WHERE active = 1 AND token != 'kayit' ORDER BY id ASC LIMIT 1")
    seal = cursor.fetchone()
    if seal:
        seal_id = seal["id"]
    else:
        cursor.execute("INSERT INTO seals (token, label, active) VALUES ('qr', 'Kasa Sabit QR Mühür', 1)")
        seal_id = cursor.lastrowid

    synthetic_token = f"admin_{generate_token(12)}"
    cursor.execute(
        """
        INSERT INTO stamps (customer_id, seal_id, created_at, source, actor)
        VALUES (?, ?, ?, 'admin', ?)
        """,
        (customer_id, seal_id, now_iso, actor_username or None),
    )

    # Güncel damga sayısını kontrol et
    cursor.execute(
        """
        SELECT id FROM stamps
        WHERE customer_id = ? AND voided_at IS NULL AND reward_id IS NULL
        ORDER BY id ASC
        """,
        (customer_id,),
    )
    unrewarded = cursor.fetchall()
    current_stamps = len(unrewarded)
    reward_earned = False

    if current_stamps >= stamp_target:
        cursor.execute(
            "INSERT INTO rewards (customer_id, issued_at) VALUES (?, ?)",
            (customer_id, now_iso),
        )
        new_reward_id = cursor.lastrowid
        stamp_ids_to_consume = [r["id"] for r in unrewarded[:stamp_target]]
        placeholders = ",".join(["?"] * len(stamp_ids_to_consume))
        cursor.execute(
            f"UPDATE stamps SET reward_id = ? WHERE id IN ({placeholders})",
            [new_reward_id] + stamp_ids_to_consume,
        )
        current_stamps -= stamp_target
        reward_earned = True
        log_audit(conn, "admin", "admin_reward_issued", f"Yönetici +1 damga ile Müşteri #{customer_id} ödül kazandı.")

    log_audit(conn, "admin", "admin_stamp_added", f"Müşteri #{customer_id} için yönetici +1 damga ekledi.")

    summary = _get_customer_sync_summary(conn, customer_id)
    if log_event:
        activity.record_admin_stamp(conn, customer_id, +1, actor_username,
                                    summary["current_stamps"] if summary else None, reward_earned)
    if summary:
        sync_customer_to_directus(
            customer_id=customer_id,
            phone_e164=summary["phone_e164"],
            display_name=summary["display_name"],
            current_stamps=summary["current_stamps"],
            usable_rewards=summary["pending_rewards"],
            record_claim=True,
            claim_code=synthetic_token,
            claim_reason="Admin +1 Damga",
        )
        try:
            if notify:
                notify_customer_after_stamp(
                    conn=conn,
                    customer_id=customer_id,
                    current_stamps=summary["current_stamps"],
                    stamp_target=stamp_target,
                    reward_unlocked=reward_earned,
                    reward_name=reward_name,
                )
        except Exception:
            pass

    return True, summary, None


def admin_remove_stamp(
    conn: sqlite3.Connection,
    customer_id: int,
    log_event: bool = True,
) -> tuple[bool, Optional[dict[str, Any]], Optional[str]]:
    """Yönetici panelinden müşterinin son damgasını (-1 Damga Düşür) geri alır ve Directus ile senkronize eder."""
    cursor = conn.cursor()
    # Önce ödüle bağlanmamış aktif damga var mı bak, yoksa en son aktif damgayı bul
    cursor.execute(
        """
        SELECT id FROM stamps
        WHERE customer_id = ? AND voided_at IS NULL AND reward_id IS NULL
        ORDER BY id DESC LIMIT 1
        """,
        (customer_id,),
    )
    row = cursor.fetchone()
    if not row:
        cursor.execute(
            """
            SELECT id FROM stamps
            WHERE customer_id = ? AND voided_at IS NULL
            ORDER BY id DESC LIMIT 1
            """,
            (customer_id,),
        )
        row = cursor.fetchone()

    if not row:
        return False, None, "Müşterinin düşürülebilecek aktif damgası bulunmuyor."

    ok, err = void_stamp(conn, row["id"], "Yönetici panelinden -1 damga düşürüldü", actor="admin")
    if not ok:
        return False, None, err

    summary = _get_customer_sync_summary(conn, customer_id)
    if log_event:
        activity.record_admin_stamp(conn, customer_id, -1, "", summary["current_stamps"] if summary else None)
    if summary:
        sync_customer_to_directus(
            customer_id=customer_id,
            phone_e164=summary["phone_e164"],
            display_name=summary["display_name"],
            current_stamps=summary["current_stamps"],
            usable_rewards=summary["pending_rewards"],
            record_claim=False,
        )
    return True, summary, None


def admin_set_stamps(
    conn: sqlite3.Connection,
    customer_id: int,
    target_count: int,
) -> tuple[bool, Optional[dict[str, Any]], Optional[str]]:
    """
    Müşterinin karttaki damga sayısını belirli bir sayıya ayarlar (0 … hedef-1).
    Artırma +1 damga akışını, azaltma -1 damga düşürme akışını tekrar eder (denetim izi ve ödül mantığı korunur).
    """
    settings = get_all_settings(conn)
    stamp_target = int(settings.get("stamp_target", "5"))
    if target_count < 0 or target_count >= stamp_target:
        return False, None, f"Damga sayısı 0 ile {stamp_target - 1} arasında olmalıdır (hedefe ulaşan damga hediyeye dönüşür)."
    summary = _get_customer_sync_summary(conn, customer_id)
    if not summary:
        return False, None, "Müşteri bulunamadı."
    current = int(summary["current_stamps"])
    before = current
    while current < target_count:
        ok, summary, err = admin_add_stamp(conn, customer_id, log_event=False, notify=False)
        if not ok:
            return False, None, err
        current = int(summary["current_stamps"])
    while current > target_count:
        ok, summary, err = admin_remove_stamp(conn, customer_id, log_event=False)
        if not ok:
            return False, None, err
        current = int(summary["current_stamps"])
    log_audit(conn, "admin", "admin_stamps_set", f"Müşteri #{customer_id} damga sayısı {target_count} olarak ayarlandı.")
    if before != target_count:
        activity.record_admin_set(conn, customer_id, before, target_count)
    return True, summary, None


def admin_redeem_reward(
    conn: sqlite3.Connection,
    customer_id: int,
    actor: str = "admin",
    redeemed_by: str = "",
) -> tuple[bool, Optional[dict[str, Any]], Optional[str]]:
    """Yönetici panelinden müşterinin 1 adet kullanılabilir ödülünü kullandırır ve Directus ile senkronize eder."""
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id FROM rewards
        WHERE customer_id = ? AND redeemed_at IS NULL AND voided_at IS NULL
        ORDER BY id ASC LIMIT 1
        """,
        (customer_id,),
    )
    reward = cursor.fetchone()
    if not reward:
        return False, None, "Müşterinin kullanılabilir ödül bileti bulunmuyor."

    now_iso = get_istanbul_now_iso()
    cursor.execute(
        "UPDATE rewards SET redeemed_at = ?, redeemed_by = ? WHERE id = ?",
        (now_iso, redeemed_by or None, reward["id"]),
    )
    log_audit(conn, actor, "admin_reward_redeemed", f"Müşteri #{customer_id} ödül #{reward['id']} {actor} tarafından kullandırıldı.")

    summary = _get_customer_sync_summary(conn, customer_id)
    activity.record_redeem(conn, customer_id, actor, ok=True, stamps_after=summary["current_stamps"] if summary else None)
    if summary:
        sync_customer_to_directus(
            customer_id=customer_id,
            phone_e164=summary["phone_e164"],
            display_name=summary["display_name"],
            current_stamps=summary["current_stamps"],
            usable_rewards=summary["pending_rewards"],
            record_claim=False,
        )
    return True, summary, None


_CAMPAIGN_COLS = (
    "id, title, description, image_url, badge, active, created_at, "
    "COALESCE(reward_stamps, 0) AS reward_stamps, action_url, action_label"
)


def list_campaigns_admin(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Tüm kampanyaları (aktif ve pasif) listeler; ödüllü görevlerde katılımcı sayıları (`stats`) da gelir."""
    rows = [dict(r) for r in conn.execute(f"SELECT {_CAMPAIGN_COLS} FROM campaigns ORDER BY id DESC")]
    stats = campaign_stats(conn, [r["id"] for r in rows if r["reward_stamps"] > 0])
    for r in rows:
        if r["reward_stamps"] > 0:
            r["stats"] = stats.get(r["id"])
    return rows


def create_campaign_admin(
    conn: sqlite3.Connection,
    title: str,
    description: str,
    image_url: str = "",
    badge: str = "KAMPANYA",
    active: int = 1,
    reward_stamps: Optional[int] = 0,
    action_url: Optional[str] = "",
    action_label: Optional[str] = "",
) -> tuple[bool, Optional[dict[str, Any]], Optional[str]]:
    """Yeni kampanya (düz duyuru veya ödüllü görev) oluşturur ve Directus PostgreSQL ile senkronize eder."""
    clean_title = (title or "").strip()
    clean_desc = (description or "").strip()
    if not clean_title:
        return False, None, "Kampanya başlığı boş bırakılamaz."

    try:
        clean_img = normalize_image_ref(image_url)
    except UploadError as exc:
        return False, None, str(exc)
    ok, reward, err = clean_reward_fields(reward_stamps, action_url, action_label)
    if not ok:
        return False, None, err
    clean_badge = (badge or "KAMPANYA").strip()[:30]
    active_int = 1 if active else 0
    now_iso = get_istanbul_now_iso()

    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO campaigns (title, description, image_url, badge, active, created_at, reward_stamps, action_url, action_label)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (clean_title, clean_desc, clean_img, clean_badge, active_int, now_iso,
         reward["reward_stamps"], reward["action_url"], reward["action_label"]),
    )
    cid = cursor.lastrowid
    kind = f"ödüllü görev, +{reward['reward_stamps']} damga" if reward["reward_stamps"] else "duyuru"
    log_audit(conn, "admin", "campaign_created", f"Kampanya #{cid} ('{clean_title}', {kind}) oluşturuldu.")

    sync_campaign_to_directus(cid, clean_title, clean_desc, clean_img, active_int)
    return True, {
        "id": cid,
        "title": clean_title,
        "description": clean_desc,
        "image_url": clean_img,
        "badge": clean_badge,
        "active": active_int,
        "created_at": now_iso,
        **reward,
    }, None


def update_campaign_admin(
    conn: sqlite3.Connection,
    campaign_id: int,
    title: str,
    description: str,
    image_url: str = "",
    badge: str = "KAMPANYA",
    active: int = 1,
    reward_stamps: Optional[int] = None,
    action_url: Optional[str] = None,
    action_label: Optional[str] = None,
) -> tuple[bool, Optional[dict[str, Any]], Optional[str]]:
    """
    Mevcut kampanyayı günceller ve Directus PostgreSQL ile senkronize eder.
    `reward_stamps` None ise (eski istemci) ödüllü görev alanlarına dokunulmaz.
    """
    clean_title = (title or "").strip()
    clean_desc = (description or "").strip()
    if not clean_title:
        return False, None, "Kampanya başlığı boş bırakılamaz."

    try:
        clean_img = normalize_image_ref(image_url)
    except UploadError as exc:
        return False, None, str(exc)
    clean_badge = (badge or "KAMPANYA").strip()[:30]
    active_int = 1 if active else 0

    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, created_at, COALESCE(reward_stamps, 0) AS reward_stamps, action_url, action_label FROM campaigns WHERE id = ?",
        (campaign_id,),
    )
    existing = cursor.fetchone()
    if not existing:
        return False, None, "Kampanya bulunamadı."
    if reward_stamps is None:
        reward = {k: existing[k] for k in ("reward_stamps", "action_url", "action_label")}
    else:
        ok, reward, err = clean_reward_fields(reward_stamps, action_url, action_label)
        if not ok:
            return False, None, err

    cursor.execute(
        """
        UPDATE campaigns
        SET title = ?, description = ?, image_url = ?, badge = ?, active = ?, reward_stamps = ?, action_url = ?, action_label = ?
        WHERE id = ?
        """,
        (clean_title, clean_desc, clean_img, clean_badge, active_int,
         reward["reward_stamps"], reward["action_url"], reward["action_label"], campaign_id),
    )
    log_audit(conn, "admin", "campaign_updated", f"Kampanya #{campaign_id} ('{clean_title}') güncellendi.")
    sync_campaign_to_directus(campaign_id, clean_title, clean_desc, clean_img, active_int)

    return True, {
        "id": campaign_id,
        "title": clean_title,
        "description": clean_desc,
        "image_url": clean_img,
        "badge": clean_badge,
        "active": active_int,
        "created_at": existing["created_at"],
        **reward,
    }, None


def toggle_campaign_admin(conn: sqlite3.Connection, campaign_id: int) -> tuple[bool, Optional[dict[str, Any]], Optional[str]]:
    """Kampanyanın aktif/pasif durumunu değiştirir ve Directus ile senkronize eder."""
    cursor = conn.cursor()
    cursor.execute(f"SELECT {_CAMPAIGN_COLS} FROM campaigns WHERE id = ?", (campaign_id,))
    row = cursor.fetchone()
    if not row:
        return False, None, "Kampanya bulunamadı."

    new_active = 0 if row["active"] else 1
    cursor.execute("UPDATE campaigns SET active = ? WHERE id = ?", (new_active, campaign_id))
    log_audit(conn, "admin", "campaign_toggled", f"Kampanya #{campaign_id} durumu {new_active} yapıldı.")

    sync_campaign_to_directus(campaign_id, row["title"], row["description"], row["image_url"] or "", new_active)
    updated = dict(row)
    updated["active"] = new_active
    return True, updated, None


def delete_campaign_admin(conn: sqlite3.Connection, campaign_id: int) -> tuple[bool, Optional[str]]:
    """Kampanyayı siler ve Directus'tan kaldırır."""
    cursor = conn.cursor()
    cursor.execute("SELECT id, title FROM campaigns WHERE id = ?", (campaign_id,))
    row = cursor.fetchone()
    if not row:
        return False, "Kampanya bulunamadı."

    cursor.execute("DELETE FROM campaigns WHERE id = ?", (campaign_id,))
    # Katılım geçmişi kampanyayla birlikte silinir; verilen damgalar kalır (damgada kampanya adı `note` olarak durur).
    cursor.execute("DELETE FROM campaign_participants WHERE campaign_id = ?", (campaign_id,))
    log_audit(conn, "admin", "campaign_deleted", f"Kampanya #{campaign_id} ('{row['title']}') silindi.")
    delete_campaign_from_directus(campaign_id)
    return True, None


def get_recent_stamps(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    """Son damga hareketlerini listeler."""
    cursor = conn.cursor()
    cursor.execute("""
        SELECT
            s.id,
            s.created_at,
            s.voided_at,
            s.voided_reason,
            s.reward_id,
            c.id as customer_id,
            c.display_name,
            c.phone_e164,
            seals.label as seal_label
        FROM stamps s
        JOIN customers c ON c.id = s.customer_id
        LEFT JOIN seals ON seals.id = s.seal_id
        ORDER BY s.id DESC
        LIMIT ?
    """, (limit,))
    rows = cursor.fetchall()
    result = []
    for r in rows:
        result.append({
            "id": r["id"],
            "customer_id": r["customer_id"],
            "display_name": (r["display_name"] or "").strip() or f"user{r['customer_id']}",
            "created_at": r["created_at"],
            "voided_at": r["voided_at"],
            "voided_reason": r["voided_reason"],
            "reward_id": r["reward_id"],
            "phone_masked": mask_phone(r["phone_e164"]),
            "phone_e164": r["phone_e164"],
            "formatted_phone": format_phone(r["phone_e164"]),
            "seal_label": r["seal_label"] or "Mühür"
        })
    return result


def void_stamp(conn: sqlite3.Connection, stamp_id: int, reason: str, actor: str = "admin") -> tuple[bool, Optional[str]]:
    """
    Damgayı geri alır (iptal eder).
    Eğer bu damga bir ödül biletinin oluşmasını sağlamışsa ve o ödül henüz kullanılmamışsa,
    ilgili ödül biletini iptal eder ve gruptaki diğer damgaları tekrar kart bakiyesine döndürür.
    """
    cursor = conn.cursor()
    now_iso = get_istanbul_now_iso()

    cursor.execute("SELECT id, customer_id, voided_at, reward_id FROM stamps WHERE id = ?", (stamp_id,))
    stamp = cursor.fetchone()
    if not stamp:
        return False, "Damga kaydı bulunamadı."
    if stamp["voided_at"]:
        return False, "Bu damga zaten daha önce geri alınmış."

    cursor.execute("""
        UPDATE stamps
        SET voided_at = ?, voided_reason = ?
        WHERE id = ?
    """, (now_iso, reason or "Yönetici tarafından geri alındı", stamp_id))

    reward_id = stamp["reward_id"]
    if reward_id:
        cursor.execute("SELECT id, redeemed_at, voided_at FROM rewards WHERE id = ?", (reward_id,))
        reward = cursor.fetchone()
        if reward and not reward["redeemed_at"] and not reward["voided_at"]:
            cursor.execute("UPDATE rewards SET voided_at = ? WHERE id = ?", (now_iso, reward_id))
            cursor.execute("UPDATE stamps SET reward_id = NULL WHERE reward_id = ? AND id != ?", (reward_id, stamp_id))
            log_audit(
                conn,
                actor,
                "reward_auto_voided",
                f"Damga #{stamp_id} geri alındığı için kullanılmamış ödül #{reward_id} iptal edildi ve kalan damgalar karta döndü."
            )

    log_audit(conn, actor, "stamp_voided", f"Damga #{stamp_id} geri alındı. Gerekçe: {reason}")
    return True, None


def get_seals_admin(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Tüm mühürleri listeler."""
    cursor = conn.cursor()
    cursor.execute("SELECT id, token, label, active FROM seals ORDER BY id DESC")
    return [dict(r) for r in cursor.fetchall()]


def create_seal_admin(conn: sqlite3.Connection, label: str) -> tuple[bool, str]:
    """Yeni bir mühür oluşturur."""
    token = generate_token(24)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO seals (token, label, active) VALUES (?, ?, 1)", (token, label.strip()))
    log_audit(conn, "admin", "seal_created", f"Yeni mühür '{label}' oluşturuldu.")
    return True, token


def toggle_seal_admin(conn: sqlite3.Connection, seal_id: int) -> bool:
    """Mührün aktif/pasif durumunu değiştirir. Kayıt QR mührü ('kayit', ADR-35) devre dışı bırakılamaz."""
    cursor = conn.cursor()
    cursor.execute("UPDATE seals SET active = 1 - active WHERE id = ? AND token != 'kayit'", (seal_id,))
    log_audit(conn, "admin", "seal_toggled", f"Mühür #{seal_id} durumu değiştirildi.")
    return True


def get_staff_cards_admin(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Tüm personel kartlarını listeler (Token hash'li saklanır)."""
    cursor = conn.cursor()
    cursor.execute("SELECT id, label, active FROM staff_cards ORDER BY id DESC")
    return [dict(r) for r in cursor.fetchall()]


def create_staff_card_admin(conn: sqlite3.Connection, label: str) -> tuple[bool, str]:
    """Yeni personel ödül kartı oluşturur."""
    raw_token = generate_token(32)
    token_hash = hash_token(raw_token)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO staff_cards (token_hash, label, active) VALUES (?, ?, 1)", (token_hash, label.strip()))
    log_audit(conn, "admin", "staff_card_created", f"Yeni personel kartı '{label}' oluşturuldu.")
    return True, raw_token


def toggle_staff_card_admin(conn: sqlite3.Connection, card_id: int) -> bool:
    """Personel kartının aktif/pasif durumunu değiştirir."""
    cursor = conn.cursor()
    cursor.execute("UPDATE staff_cards SET active = 1 - active WHERE id = ?", (card_id,))
    log_audit(conn, "admin", "staff_card_toggled", f"Personel kartı #{card_id} durumu değiştirildi.")
    return True


def is_campaign_locked(conn: sqlite3.Connection) -> bool:
    """Aktif en az 1 damga varsa True döner."""
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) as count FROM stamps WHERE voided_at IS NULL")
    return cursor.fetchone()["count"] > 0


_HEX_COLOR_RE = re.compile(r"^#(?:[0-9A-Fa-f]{3}|[0-9A-Fa-f]{6}|[0-9A-Fa-f]{8})$")


def update_settings_admin(
    conn: sqlite3.Connection,
    new_settings: dict[str, Any],
    respect_lock: bool = False,
) -> tuple[bool, Optional[str]]:
    """İşletme ayarlarını günceller."""
    locked = is_campaign_locked(conn) if respect_lock else False

    allowed_keys = (
        "business_name",
        "accent_color",
        "daily_limit",
        "min_interval_minutes",
        "kvkk_text",
        "stamp_target",
        "reward_name",
        "kvkk_contact",
        "kvkk_controller",
        "kvkk_address",
        "menu_note",
        "bonus_install_enabled",
        "bonus_notif_enabled",
        "gift_requires_visit",
        # ADR-45: otomatik bildirim metinleri
        "notif_stamp_title",
        "notif_stamp_body",
        "notif_reward_title",
        "notif_reward_body",
    )
    for key, value in new_settings.items():
        if value is None:
            continue
        if key in ("stamp_target", "reward_name") and locked:
            continue
        if key == "accent_color" and not _HEX_COLOR_RE.match(str(value).strip()):
            continue  # renk CSS'e (<style>) gömülür: yalnızca #RGB / #RRGGBB / #RRGGBBAA kabul edilir
        if key in allowed_keys:
            set_setting(conn, key, str(value).strip())

    log_audit(conn, "admin", "settings_updated", "İşletme ayarları güncellendi.")
    return True, None


def generate_qr_png_bytes(data: str) -> bytes:
    """Verilen metin/URL için PNG formatında QR kod görseli üretir."""
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=4,
    )
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#1E4D3F", back_color="#FCF9F8")
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()

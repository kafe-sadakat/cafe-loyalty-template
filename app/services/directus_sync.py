import logging
import os
import threading
import uuid
from datetime import datetime
from typing import Optional

import httpx
from app.utils import format_phone, ISTANBUL_TZ

logger = logging.getLogger("sadakat")

PG_HOST = os.getenv("PG_HOST", "db")
PG_PORT = int(os.getenv("PG_PORT", "5432"))
PG_DB = os.getenv("PG_DB", "directus")
PG_USER = os.getenv("PG_USER", "directus")
PG_PASSWORD = os.getenv("PG_PASSWORD", "")

CUSTOMER_ROLE_ID = os.getenv("DIRECTUS_CUSTOMER_ROLE_ID", "996d9d98-76b1-4af6-a1a2-e44f1e23f233")

DIRECTUS_INTERNAL_URL = os.getenv("DIRECTUS_INTERNAL_URL", "http://directus:8055")
# Gizli bilgiler yalnızca ortamdan (.env) okunur; kodda varsayılan parola/anahtar bulunmaz.
# ADMIN_PASSWORD / ADMIN_EMAIL adları, DIRECTUS_* adlarına geri dönüş (fallback) olarak desteklenir.
DIRECTUS_ADMIN_EMAIL = os.getenv("DIRECTUS_ADMIN_EMAIL") or os.getenv("ADMIN_EMAIL", "")
DIRECTUS_ADMIN_PASSWORD = os.getenv("DIRECTUS_ADMIN_PASSWORD") or os.getenv("ADMIN_PASSWORD", "")


def _is_sync_enabled() -> bool:
    if os.getenv("DISABLE_DIRECTUS_SYNC", "") == "1":
        return False
    # Bağımsız SQLite kurulumunda (PG_PASSWORD yoksa ve ENABLE_DIRECTUS_SYNC=1 değilse) bağlantı denemesi yapma.
    if not PG_PASSWORD and os.getenv("ENABLE_DIRECTUS_SYNC", "") != "1":
        return False
    # pytest altında çalışırken yerel ortamda 'db' konteyneri yoksa zaman kaybetmemek için kontrol
    if "PYTEST_CURRENT_TEST" in os.environ and os.getenv("ENABLE_DIRECTUS_IN_TESTS", "") != "1":
        return False
    return True


def _get_pg_conn():
    import psycopg2
    return psycopg2.connect(
        host=PG_HOST,
        port=PG_PORT,
        dbname=PG_DB,
        user=PG_USER,
        password=PG_PASSWORD,
        connect_timeout=2,
    )


def clear_directus_cache() -> bool:
    """Directus önbelleğini temizler (POST <DIRECTUS_INTERNAL_URL>/utils/cache/clear)."""
    if not _is_sync_enabled():
        return False
    try:
        with httpx.Client(base_url=DIRECTUS_INTERNAL_URL, timeout=2.5) as client:
            login_res = client.post(
                "/auth/login",
                json={"email": DIRECTUS_ADMIN_EMAIL, "password": DIRECTUS_ADMIN_PASSWORD},
            )
            if login_res.status_code in (200, 201):
                token = login_res.json().get("data", {}).get("access_token")
                if token:
                    clear_res = client.post(
                        "/utils/cache/clear",
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    return clear_res.status_code in (200, 204)
    except Exception as exc:
        logger.debug("Directus önbellek temizleme atlandı: %s", str(exc))
    return False


def _sync_customer_worker(
    phone_e164: str,
    display_name: str,
    current_stamps: int,
    usable_rewards: int,
    add_stamp_code: Optional[str] = None,
    stamp_reason: Optional[str] = None,
) -> None:
    if not _is_sync_enabled():
        return
    try:
        formatted_phone = format_phone(phone_e164)
        first_name_val = f"{display_name} ({formatted_phone})"
        last_name_val = formatted_phone
        digits = "".join(ch for ch in phone_e164 if ch.isdigit())
        synthetic_email = f"{digits}@kahveduragi.local"
        now_dt = datetime.now(ISTANBUL_TZ)

        conn = _get_pg_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT id FROM directus_users
                        WHERE external_identifier = %s
                           OR last_name = %s
                           OR email = %s
                        LIMIT 1
                        """,
                        (phone_e164, formatted_phone, synthetic_email),
                    )
                    row = cur.fetchone()
                    if row:
                        user_uuid = str(row[0])
                        cur.execute(
                            """
                            UPDATE directus_users
                            SET first_name = %s,
                                last_name = %s,
                                title = %s,
                                location = %s,
                                external_identifier = %s,
                                role = COALESCE(role, %s::uuid),
                                status = 'active'
                            WHERE id = %s::uuid
                            """,
                            (
                                first_name_val,
                                last_name_val,
                                formatted_phone,
                                display_name,
                                phone_e164,
                                CUSTOMER_ROLE_ID,
                                user_uuid,
                            ),
                        )
                    else:
                        user_uuid = str(uuid.uuid4())
                        cur.execute(
                            """
                            INSERT INTO directus_users
                                (id, first_name, last_name, email, location, title, status, role, provider, external_identifier)
                            VALUES
                                (%s::uuid, %s, %s, %s, %s, %s, 'active', %s::uuid, 'default', %s)
                            """,
                            (
                                user_uuid,
                                first_name_val,
                                last_name_val,
                                synthetic_email,
                                display_name,
                                formatted_phone,
                                CUSTOMER_ROLE_ID,
                                phone_e164,
                            ),
                        )

                    # Loyalty kaydı güncelle veya ekle
                    cur.execute("SELECT id FROM loyalty WHERE \"user\" = %s::uuid LIMIT 1", (user_uuid,))
                    loy_row = cur.fetchone()
                    if loy_row:
                        cur.execute(
                            """
                            UPDATE loyalty
                            SET stamps = %s,
                                free_rewards = %s,
                                last_stamp_at = %s
                            WHERE id = %s
                            """,
                            (current_stamps, usable_rewards, now_dt, loy_row[0]),
                        )
                    else:
                        cur.execute(
                            """
                            INSERT INTO loyalty ("user", stamps, free_rewards, last_stamp_at)
                            VALUES (%s::uuid, %s, %s, %s)
                            """,
                            (user_uuid, current_stamps, usable_rewards, now_dt),
                        )

                    # Eğer damga/ödül hareketi varsa stamp_claims tablosuna kaydet
                    if add_stamp_code:
                        cur.execute(
                            """
                            INSERT INTO stamp_claims (code, type, status, reason, user_created, date_created)
                            VALUES (%s, 'stamp', 'approved', %s, %s::uuid, %s)
                            """,
                            (
                                add_stamp_code[:64],
                                stamp_reason or f"{display_name} ({formatted_phone})",
                                user_uuid,
                                now_dt,
                            ),
                        )
        finally:
            conn.close()

        clear_directus_cache()
    except Exception as exc:
        logger.debug("PostgreSQL/Directus müşteri senkronizasyonu atlandı: %s", str(exc))


def anonymize_directus_customer(phone_e164: str) -> None:
    """
    KVKK silme/unutulma: PostgreSQL (Directus) tarafındaki müşteri kopyasını anonimleştirir.
    Satır silinmez (başka tablolardan referans olabilir); kişisel alanlar boşaltılır, kullanıcı arşivlenir.
    """
    if not _is_sync_enabled() or not phone_e164:
        return

    def _worker():
        try:
            formatted_phone = format_phone(phone_e164)
            digits = "".join(ch for ch in phone_e164 if ch.isdigit())
            synthetic_email = f"{digits}@kahveduragi.local"
            conn = _get_pg_conn()
            try:
                with conn:
                    with conn.cursor() as cur:
                        # Her damgada stamp_claims.reason'a "ad (telefon) - ..." yazılmıştı: silme sonrası bu satırlarda kimlik
                        # kalmasın (Y-06). Kullanıcı kimlikleri, kimlik alanları boşaltılmadan ÖNCE okunur.
                        cur.execute(
                            """
                            SELECT id FROM directus_users
                            WHERE external_identifier = %s OR last_name = %s OR email = %s
                            """,
                            (phone_e164, formatted_phone, synthetic_email),
                        )
                        user_ids = [str(r[0]) for r in cur.fetchall()]
                        if user_ids:
                            cur.execute(
                                "UPDATE stamp_claims SET reason = 'silindi' WHERE user_created = ANY(%s::uuid[])",
                                (user_ids,),
                            )
                            cur.execute(
                                'UPDATE loyalty SET stamps = 0, free_rewards = 0 WHERE "user" = ANY(%s::uuid[])',
                                (user_ids,),
                            )
                        cur.execute(
                            """
                            UPDATE directus_users
                            SET first_name = 'silindi', last_name = NULL, title = NULL, location = NULL,
                                external_identifier = NULL, email = 'silindi-' || id::text || '@silindi.invalid',
                                status = 'archived'
                            WHERE external_identifier = %s OR last_name = %s OR email = %s
                            """,
                            (phone_e164, formatted_phone, synthetic_email),
                        )
            finally:
                conn.close()
            clear_directus_cache()
        except Exception as exc:
            # Başarısızlık sessiz kalmasın: silme talebinin Directus kopyasına yansımadığı günlükte görünür (Y-06)
            logger.warning("PostgreSQL (Directus) müşteri anonimleştirme BAŞARISIZ: %s", str(exc))

    threading.Thread(target=_worker, daemon=True).start()


def sync_customer_to_directus(
    phone_e164: str,
    display_name: str,
    current_stamps: int,
    usable_rewards: int,
    add_stamp_code: Optional[str] = None,
    stamp_reason: Optional[str] = None,
    async_mode: bool = True,
    customer_id: Optional[int] = None,
    record_claim: bool = False,
    claim_code: Optional[str] = None,
    claim_reason: Optional[str] = None,
) -> None:
    """Müşteri, profil adı ve damga/ödül bilgisini PostgreSQL Directus veritabanına senkronize eder."""
    if not _is_sync_enabled():
        return
    final_code = add_stamp_code or (claim_code if record_claim else None)
    final_reason = stamp_reason or claim_reason
    if async_mode:
        t = threading.Thread(
            target=_sync_customer_worker,
            args=(phone_e164, display_name, current_stamps, usable_rewards, final_code, final_reason),
            daemon=True,
        )
        t.start()
    else:
        _sync_customer_worker(phone_e164, display_name, current_stamps, usable_rewards, final_code, final_reason)


def sync_campaign_to_directus(
    *args,
    title: str = "",
    description: str = "",
    active: bool = True,
    image_url: str = "",
) -> None:
    """Kampanyayı PostgreSQL campaigns tablosuna senkronize eder."""
    if not _is_sync_enabled():
        return
    # Hem (title, description, active) hem de (campaign_id, title, description, image_url, active) çağrılarını destekle
    if len(args) == 3:
        title, description, active = args[0], args[1], bool(args[2])
    elif len(args) >= 4:
        title, description = str(args[1]), str(args[2])
        active = bool(args[4]) if len(args) >= 5 else True

    def _worker():
        try:
            conn = _get_pg_conn()
            try:
                with conn:
                    with conn.cursor() as cur:
                        cur.execute("SELECT id FROM campaigns WHERE title = %s LIMIT 1", (title,))
                        row = cur.fetchone()
                        if row:
                            cur.execute(
                                "UPDATE campaigns SET description = %s, active = %s WHERE id = %s",
                                (description, bool(active), row[0]),
                            )
                        else:
                            cur.execute(
                                "INSERT INTO campaigns (title, description, active, start_date) VALUES (%s, %s, %s, CURRENT_DATE)",
                                (title, description, bool(active)),
                            )
            finally:
                conn.close()
            clear_directus_cache()
        except Exception as exc:
            logger.debug("PostgreSQL kampanya senkronizasyonu atlandı: %s", str(exc))

    threading.Thread(target=_worker, daemon=True).start()


def delete_campaign_from_directus(title_or_id) -> None:
    """Kampanyayı PostgreSQL campaigns tablosundan siler."""
    if not _is_sync_enabled():
        return

    def _worker():
        try:
            conn = _get_pg_conn()
            try:
                with conn:
                    with conn.cursor() as cur:
                        if isinstance(title_or_id, int):
                            cur.execute("DELETE FROM campaigns WHERE id = %s", (title_or_id,))
                        else:
                            cur.execute("DELETE FROM campaigns WHERE title = %s", (str(title_or_id),))
            finally:
                conn.close()
            clear_directus_cache()
        except Exception as exc:
            logger.debug("PostgreSQL kampanya silme atlandı: %s", str(exc))

    threading.Thread(target=_worker, daemon=True).start()


def sync_all_sqlite_to_directus(sqlite_conn) -> None:
    """Açılışta mevcut tüm SQLite müşterilerini ve kampanyalarını PostgreSQL Directus ile eşitler."""
    if not _is_sync_enabled():
        return
    try:
        cursor = sqlite_conn.cursor()
        cursor.execute(
            """
            SELECT
                c.id,
                c.phone_e164,
                c.display_name,
                (SELECT COUNT(*) FROM stamps s WHERE s.customer_id = c.id AND s.voided_at IS NULL AND s.reward_id IS NULL) as current_stamps,
                (SELECT COUNT(*) FROM rewards r WHERE r.customer_id = c.id AND r.redeemed_at IS NULL AND r.voided_at IS NULL) as usable_rewards
            FROM customers c
            WHERE c.deleted_at IS NULL
            """
        )
        rows = [dict(r) for r in cursor.fetchall()]

        def _bulk_worker():
            for r in rows:
                disp = r["display_name"] or f"user{r['id']}"
                _sync_customer_worker(
                    phone_e164=r["phone_e164"],
                    display_name=disp,
                    current_stamps=r["current_stamps"] or 0,
                    usable_rewards=r["usable_rewards"] or 0,
                )

        threading.Thread(target=_bulk_worker, daemon=True).start()
    except Exception as exc:
        logger.debug("Toplu Directus senkronizasyonu atlandı: %s", str(exc))

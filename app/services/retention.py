"""
Kişisel veri saklama ve imha (KVKK m.4/2-d "gerektiği süre kadar muhafaza", m.7 silme/anonimleştirme).

Aydınlatma Metni'nin 6. bölümünde müşteriye taahhüt edilen süreleri uygular; uygulama açılışında ve sonra
günde bir kez çalışır (RETENTION_JOB=0 ile kapatılabilir):
- 24 ay boyunca hiçbir işlem (kayıt, damga, ödül, uygulama açılışı) olmayan müşteri hesabı anonimleştirilir.
- Kapatılmış (pasif) bildirim abonelikleri 90 gün sonra silinir (endpoint + şifreleme anahtarları).
- Onay/ret, güvenlik ve işlem günlükleri 3 yıl sonra silinir (ETK + KVKK ispat süresi dolunca).
- Şüphe (fraud) olayları 1 yıl, süresi geçmiş damga bağlantıları (claims) ve kasa damga kodları (stamp_codes,
  ADR-28) 30 gün sonra silinir.
- Tek kullanımlık SMS kodu kayıtları (otp_challenges ve deneme sayaçları otp_attempts) süreleri dolduktan 1 gün sonra,
  SMS gönderim kayıtları (otp_sends; günlük / saatlik sınırlar bunlardan sayılır) ve tek-kayıt işaretleri
  (otp_audit_marks) 2 gün sonra silinir.
Tarihler İstanbul saatiyle ISO-8601 (+03:00) saklandığından metin karşılaştırması güvenlidir.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Optional

from app.db import log_audit
from app.utils import get_istanbul_now

logger = logging.getLogger("sadakat")

INACTIVE_CUSTOMER_DAYS = 730
INACTIVE_SUBSCRIPTION_DAYS = 90
LOG_RETENTION_DAYS = 3 * 365
FRAUD_EVENT_DAYS = 365
CLAIM_TOKEN_DAYS = 30
STAMP_CODE_DAYS = 30
OTP_CHALLENGE_DAYS = 1
OTP_SEND_DAYS = 2

_LOG_TABLES = (
    ("audit_log", "at"),
    ("audit_logs", "at"),
    ("staff_audit_logs", "at"),
    ("notification_log", "created_at"),
    ("customer_events", "at"),   # ADR-31: müşteri + kasa işlem kayıtları (IP / cihaz özeti 1 yıl sonra silinir)
)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (name,)).fetchone() is not None


def inactive_customers(conn: sqlite3.Connection, cutoff_iso: str) -> list[sqlite3.Row]:
    """Son işlemi (kayıt/damga/ödül/uygulama açılışı) cutoff'tan eski, silinmemiş müşteriler."""
    return conn.execute(
        """
        SELECT c.id, c.phone_e164 FROM customers c
        WHERE c.deleted_at IS NULL
          AND MAX(
                COALESCE(c.created_at, ''),
                COALESCE(c.pwa_seen_at, ''),
                COALESCE((SELECT MAX(s.created_at) FROM stamps s WHERE s.customer_id = c.id), ''),
                COALESCE((SELECT MAX(COALESCE(r.redeemed_at, r.issued_at)) FROM rewards r WHERE r.customer_id = c.id), '')
              ) < ?
        """,
        (cutoff_iso,),
    ).fetchall()


def run_retention(conn: sqlite3.Connection, now: Optional[datetime] = None) -> dict[str, int]:
    """Saklama sürelerini uygular; ne kadar kaydın anonimleştirildiğini/silindiğini döner."""
    from app.services.loyalty import anonymize_customer  # döngüsel içe aktarmayı önler

    now = now or get_istanbul_now()

    def cutoff(days: int) -> str:
        return (now - timedelta(days=days)).isoformat()

    result = {"customers_anonymized": 0, "subscriptions_deleted": 0, "logs_deleted": 0,
              "fraud_events_deleted": 0, "claims_deleted": 0, "stamp_codes_deleted": 0,
              "otp_challenges_deleted": 0, "otp_sends_deleted": 0}

    for row in inactive_customers(conn, cutoff(INACTIVE_CUSTOMER_DAYS)):
        anonymize_customer(conn, row["id"], row["phone_e164"], actor="system:retention",
                           reason="24 ay hareketsizlik — KVKK saklama süresi doldu")
        result["customers_anonymized"] += 1

    result["subscriptions_deleted"] = conn.execute(
        "DELETE FROM push_subscriptions WHERE active = 0 AND COALESCE(last_success_at, created_at) < ?",
        (cutoff(INACTIVE_SUBSCRIPTION_DAYS),),
    ).rowcount

    for table, column in _LOG_TABLES:
        if _table_exists(conn, table):
            result["logs_deleted"] += conn.execute(
                f"DELETE FROM {table} WHERE {column} < ?", (cutoff(LOG_RETENTION_DAYS),)
            ).rowcount

    if _table_exists(conn, "fraud_events"):
        result["fraud_events_deleted"] = conn.execute(
            "DELETE FROM fraud_events WHERE at < ?", (cutoff(FRAUD_EVENT_DAYS),)
        ).rowcount

    if _table_exists(conn, "customer_events"):
        # İşlem kaydı 3 yıl kalır (yukarıda); IP ve cihaz özeti şüphe olaylarıyla aynı sürede (1 yıl) silinir.
        conn.execute(
            "UPDATE customer_events SET ip = '', device_hash = '' WHERE at < ? AND (ip != '' OR device_hash != '')",
            (cutoff(FRAUD_EVENT_DAYS),),
        )

    if _table_exists(conn, "claims"):
        result["claims_deleted"] = conn.execute(
            "DELETE FROM claims WHERE expires_at < ?", (cutoff(CLAIM_TOKEN_DAYS),)
        ).rowcount

    if _table_exists(conn, "stamp_codes"):
        result["stamp_codes_deleted"] = conn.execute(
            "DELETE FROM stamp_codes WHERE created_at < ?", (cutoff(STAMP_CODE_DAYS),)
        ).rowcount

    if _table_exists(conn, "otp_challenges"):
        result["otp_challenges_deleted"] = conn.execute(
            "DELETE FROM otp_challenges WHERE expires_at < ?", (cutoff(OTP_CHALLENGE_DAYS),)
        ).rowcount
    if _table_exists(conn, "otp_attempts"):
        conn.execute("DELETE FROM otp_attempts WHERE challenge_id NOT IN (SELECT id FROM otp_challenges)")
    if _table_exists(conn, "otp_sends"):
        result["otp_sends_deleted"] = conn.execute(
            "DELETE FROM otp_sends WHERE sent_at < ?", (cutoff(OTP_SEND_DAYS),)
        ).rowcount
    if _table_exists(conn, "otp_audit_marks"):
        conn.execute("DELETE FROM otp_audit_marks WHERE created_at < ?", (cutoff(OTP_SEND_DAYS),))

    if any(result.values()):
        log_audit(conn, "system:retention", "retention_run",
                  "Saklama/imha: " + ", ".join(f"{k}={v}" for k, v in result.items()))
        logger.info("Saklama/imha görevi: %s", result)
    return result

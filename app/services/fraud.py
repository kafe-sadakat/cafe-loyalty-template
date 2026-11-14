"""
Şüpheli istismar sayacı (`customers.fraud_risk_score`).

Sistem emin olmadan kimseyi ENGELLEMEZ: yalnızca puan biriktirir. Puan eşiği aşılınca müdür panelinde
sarı/kırmızı "⚠️ Şüpheli İstismar Girişimi" rozeti görünür; müdür incelemeyi kapatabilir veya damgaları sıfırlayabilir.
Puan, yalnızca son WINDOW_DAYS günün ve son incelemeden sonraki olayların toplamıdır (eski olaylar kendiliğinden düşer).
"""
from __future__ import annotations

import sqlite3
from datetime import timedelta
from typing import Any, Optional

from app.db import log_audit
from app.utils import get_istanbul_now, get_istanbul_now_iso, hash_token

WINDOW_DAYS = 7
YELLOW_AT = 3   # şüphe: izlemeye değer
RED_AT = 6      # kesinleşmiş şüpheli durum

# Olay nedeni → şüphe puanı (ADR-28, ADR-30)
REASON_POINTS = {
    "qr_expired": 2,    # süresi dolmuş dinamik QR ile damga isteği
    "qr_missing": 2,    # dinamik QR açıkken zaman damgasız (eski/fotoğraf) bağlantı
    "qr_invalid": 3,    # imzası bozuk / sahte zaman damgası; ADR-28: tabloda olmayan (uydurma) damga kodu
    "claim_replay": 3,  # tek kullanımlık damga bağlantısının tekrar kullanılması (başkasının kodu / başka telefonda açılmış kod)
    "stamp_code_expired": 1,  # ADR-28: süresi dolmuş kasa damga kodu (yavaş açılış da olabilir: hafif puan)
    "stamp_code_reused": 1,   # ADR-28: müşterinin kendi kullandığı kodu 5 dk sonra yeniden açması (kopyala-yapıştır)
    "nfc_invalid": 3,   # ADR-30: imzası tutmayan NTAG 424 DNA adresi (uydurma / bozulmuş p, c)
    "nfc_stale": 1,     # ADR-30: sayacı geride kalmış dokunuş (önceden toplanmış ya da sonra geçersizleşmiş eski adres)
    # ADR-31 (işlem kayıtlarındaki "olası kötüye kullanım" notları; activity.py yazar)
    "link_retry": 1,       # bekleme / günlük limit içinde bağlantıyı yeniden kullanma (yenileme, geri tuşu, açık sekme) ya da 3+ deneme
    "link_social": 2,      # damga bağlantısı Instagram / Facebook / TikTok gibi bir uygulamanın içinden açıldı (paylaşılan bağlantı)
    "cooldown_waiter": 1,  # bekleme içinde deneyip süre dolar dolmaz sabit bağlantıdan damga alma
    "device_switch": 2,    # aynı telefonda farklı numarayla hesap (olası çoklu hesap)
}


def fraud_level(score: int) -> Optional[str]:
    if score >= RED_AT:
        return "red"
    if score >= YELLOW_AT:
        return "yellow"
    return None


def recompute(conn: sqlite3.Connection, customer_id: int) -> int:
    row = conn.execute("SELECT fraud_reviewed_at FROM customers WHERE id = ?", (customer_id,)).fetchone()
    if not row:
        return 0
    since = (get_istanbul_now() - timedelta(days=WINDOW_DAYS)).isoformat()
    reviewed = row["fraud_reviewed_at"] or ""
    score = conn.execute(
        "SELECT COALESCE(SUM(points), 0) AS s FROM fraud_events WHERE customer_id = ? AND at >= ? AND at > ?",
        (customer_id, since, reviewed),
    ).fetchone()["s"]
    conn.execute("UPDATE customers SET fraud_risk_score = ? WHERE id = ?", (score, customer_id))
    if score >= YELLOW_AT:
        conn.execute(
            "UPDATE customers SET fraud_flagged_at = COALESCE(fraud_flagged_at, ?) WHERE id = ?",
            (get_istanbul_now_iso(), customer_id),
        )
    return score


def record_event(
    conn: sqlite3.Connection, reason: str, customer_id: Optional[int] = None,
    device_token: Optional[str] = None, ip: str = "", points: Optional[int] = None,
) -> int:
    """Olayı kaydeder; müşteri biliniyorsa puanını günceller ve yeni puanı döner (bilinmiyorsa 0)."""
    pts = points if points is not None else REASON_POINTS.get(reason, 1)
    conn.execute(
        "INSERT INTO fraud_events (at, customer_id, device_hash, ip, reason, points) VALUES (?, ?, ?, ?, ?, ?)",
        (get_istanbul_now_iso(), customer_id, hash_token(device_token) if device_token else "", (ip or "")[:64], reason, pts),
    )
    if customer_id is None:
        return 0
    score = recompute(conn, customer_id)
    if score >= YELLOW_AT:
        log_audit(conn, f"customer:{customer_id}", "fraud_risk", f"Şüphe puanı {score} ({reason}).")
    return score


def review_customer(conn: sqlite3.Connection, customer_id: int, action: str, actor: str = "mudur") -> tuple[bool, Optional[str]]:
    """action='clear' → incelendi, puan sıfırlanır; action='reset_stamps' → tüm damgalar + bekleyen hediyeler iptal edilir."""
    if not conn.execute("SELECT 1 FROM customers WHERE id = ? AND deleted_at IS NULL", (customer_id,)).fetchone():
        return False, "Müşteri bulunamadı."
    now_iso = get_istanbul_now_iso()
    if action == "reset_stamps":
        conn.execute(
            "UPDATE stamps SET voided_at = ?, voided_reason = 'Şüpheli istismar incelemesi: damgalar sıfırlandı' "
            "WHERE customer_id = ? AND voided_at IS NULL",
            (now_iso, customer_id),
        )
        conn.execute(
            "UPDATE rewards SET voided_at = ? WHERE customer_id = ? AND voided_at IS NULL AND redeemed_at IS NULL",
            (now_iso, customer_id),
        )
        log_audit(conn, actor, "fraud_reset_stamps", f"Müşteri #{customer_id} damgaları şüpheli istismar nedeniyle sıfırlandı.")
    elif action != "clear":
        return False, "Bilinmeyen işlem."
    else:
        log_audit(conn, actor, "fraud_cleared", f"Müşteri #{customer_id} şüpheli istismar uyarısı incelendi ve kapatıldı.")
    conn.execute(
        "UPDATE customers SET fraud_reviewed_at = ?, fraud_risk_score = 0, fraud_flagged_at = NULL WHERE id = ?",
        (now_iso, customer_id),
    )
    from app.services import activity  # döngüsel içe aktarmayı önler (activity → fraud)
    activity.record_fraud_review(conn, customer_id, action, actor=actor)
    return True, None

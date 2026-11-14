"""
Güvenlik olay günlüğü ve veri ihlali (72 saat) yönetimi.

- `audit_logs`          : ham güvenlik olayları (yetkisiz erişim, hatalı giriş, hız sınırı aşımı...)
- `security_incidents`  : eşik aşıldığında otomatik açılan veya yönetici tarafından elle açılan olaylar

Bu modül hukuki değerlendirme yapmaz; yalnızca teknik kaydı tutar ve yönetici paneline
zaman damgalı, 72 saat geri sayımlı bir olay müdahale ekranı için veri sağlar.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Optional

from app.db import get_db
from app.utils import ISTANBUL_TZ, get_istanbul_now, get_istanbul_now_iso, parse_to_istanbul

logger = logging.getLogger("sadakat")

BREACH_NOTIFICATION_HOURS = 72
VALID_STATUSES = ("open", "investigating", "notified", "closed")
STATUS_LABELS = {
    "open": "Açık",
    "investigating": "İnceleniyor",
    "notified": "Kurul'a bildirildi",
    "closed": "Kapatıldı",
}
SEVERITIES = ("low", "medium", "high", "critical")

# Yetkisiz denendiğinde "toplu veri çekme girişimi" sayılan yönetim yolları
SENSITIVE_PATH_MARKERS = (
    "/api/admin/overview",
    "/api/admin/customers",
    "/api/admin/export",
    "/api/admin/consents",
    "/api/admin/security",
    "/api/admin/staff-logs",
    "/api/push/campaign",
)


def _client_ip_from_key(key: str) -> str:
    """'claim_api_1.2.3.4' gibi hız sınırı anahtarından IP kısmını ayıklar."""
    return key.rsplit("_", 1)[-1] if "_" in key else key


def _window_start_iso(minutes: int) -> str:
    return (get_istanbul_now() - timedelta(minutes=minutes)).isoformat()


def _insert_event(
    conn: sqlite3.Connection,
    event_type: str,
    severity: str,
    ip: str,
    path: str,
    method: str,
    user_agent: str,
    actor: str,
    detail: str,
) -> None:
    conn.execute(
        """
        INSERT INTO audit_logs (at, event_type, severity, ip, path, method, user_agent, actor, detail)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            get_istanbul_now_iso(),
            event_type,
            severity,
            (ip or "")[:64],
            (path or "")[:300],
            (method or "")[:10],
            (user_agent or "")[:300],
            (actor or "")[:80],
            (detail or "")[:1000],
        ),
    )


def _count_customers(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) AS c FROM customers WHERE deleted_at IS NULL").fetchone()["c"]


def open_or_bump_incident(
    conn: sqlite3.Connection,
    incident_type: str,
    severity: str,
    source_ip: str,
    path: str,
    summary: str,
    data_exposed: bool = False,
    dedupe_minutes: int = 60,
) -> int:
    """Aynı tür+IP için son `dedupe_minutes` içinde açık olay varsa sayacı artırır, yoksa yeni olay açar."""
    now_iso = get_istanbul_now_iso()
    row = conn.execute(
        """
        SELECT id FROM security_incidents
        WHERE incident_type = ? AND source_ip = ? AND status IN ('open', 'investigating')
          AND last_event_at >= ?
        ORDER BY id DESC LIMIT 1
        """,
        (incident_type, source_ip, _window_start_iso(dedupe_minutes)),
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE security_incidents SET event_count = event_count + 1, last_event_at = ? WHERE id = ?",
            (now_iso, row["id"]),
        )
        return row["id"]

    # Engellenen girişimlerde veri ifşası yoktur → etkilenen kişi tahmini 0.
    # Veri ifşası olduğu düşünülen olaylarda üst sınır olarak tüm aktif kayıt sayısı alınır.
    estimate = _count_customers(conn) if data_exposed else 0
    cur = conn.execute(
        """
        INSERT INTO security_incidents
            (detected_at, last_event_at, incident_type, severity, status, source_ip, path, summary,
             event_count, affected_estimate, data_exposed, is_manual)
        VALUES (?, ?, ?, ?, 'open', ?, ?, ?, 1, ?, ?, 0)
        """,
        (now_iso, now_iso, incident_type, severity, source_ip, path[:300], summary[:500], estimate, 1 if data_exposed else 0),
    )
    return cur.lastrowid


def log_event(
    event_type: str,
    severity: str = "info",
    ip: str = "",
    path: str = "",
    method: str = "",
    user_agent: str = "",
    actor: str = "",
    detail: str = "",
    incident: Optional[dict[str, Any]] = None,
) -> None:
    """
    Güvenlik olayını kaydeder. Hiçbir koşulda istisna fırlatmaz (güvenlik günlüğü iş akışını bozmamalı).
    `incident` verilirse aynı işlemde olay kaydı da açılır/artırılır.
    """
    try:
        with get_db(immediate=True) as conn:
            _insert_event(conn, event_type, severity, ip, path, method, user_agent, actor, detail)
            if incident:
                open_or_bump_incident(
                    conn,
                    incident_type=incident["type"],
                    severity=incident.get("severity", "medium"),
                    source_ip=ip,
                    path=path,
                    summary=incident.get("summary", detail),
                    data_exposed=bool(incident.get("data_exposed", False)),
                )
    except Exception as exc:  # pragma: no cover - savunmacı
        logger.warning("Güvenlik olayı yazılamadı (%s): %s", event_type, exc)


def _recent_event_count(conn: sqlite3.Connection, event_type: str, ip: str, minutes: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS c FROM audit_logs WHERE event_type = ? AND ip = ? AND at >= ?",
        (event_type, ip, _window_start_iso(minutes)),
    ).fetchone()["c"]


def record_failed_login(ip: str, role: str, user_agent: str = "", path: str = "") -> int:
    """Hatalı panel girişini kaydeder; 15 dk içinde 5. denemede olay açar. Son 15 dk'daki hata sayısını döner."""
    count = 0
    try:
        with get_db(immediate=True) as conn:
            _insert_event(conn, "failed_login", "medium", ip, path, "POST", user_agent, role, f"{role} paneli hatalı parola denemesi")
            count = _recent_event_count(conn, "failed_login", ip, 15)
            if count >= 5:
                open_or_bump_incident(
                    conn,
                    incident_type="brute_force_login",
                    severity="high",
                    source_ip=ip,
                    path=path,
                    summary=f"15 dakika içinde {count} hatalı yönetim paneli giriş denemesi ({role}).",
                )
    except Exception as exc:  # pragma: no cover
        logger.warning("Hatalı giriş kaydı yazılamadı: %s", exc)
    return count


def record_unauthorized(ip: str, path: str, method: str, user_agent: str = "") -> None:
    """Oturumsuz yönetim isteğini kaydeder. Hassas veri uçlarında hemen olay açar, diğerlerinde 3+ denemede."""
    sensitive = any(path.startswith(marker) for marker in SENSITIVE_PATH_MARKERS)
    try:
        with get_db(immediate=True) as conn:
            _insert_event(
                conn, "unauthorized_access", "high" if sensitive else "medium", ip, path, method, user_agent, "",
                "Oturum açılmadan yönetim ucuna erişim denemesi (engellendi)",
            )
            recent = _recent_event_count(conn, "unauthorized_access", ip, 10)
            if sensitive:
                open_or_bump_incident(
                    conn,
                    incident_type="unauthorized_data_access_attempt",
                    severity="high",
                    source_ip=ip,
                    path=path,
                    summary="Yetkisiz kişi veri uçlarına (müşteri listesi/dışa aktarma) erişmeyi denedi; istek engellendi.",
                )
            elif recent >= 3:
                open_or_bump_incident(
                    conn,
                    incident_type="unauthorized_access_attempts",
                    severity="medium",
                    source_ip=ip,
                    path=path,
                    summary=f"10 dakika içinde {recent} yetkisiz yönetim isteği (engellendi).",
                )
    except Exception as exc:  # pragma: no cover
        logger.warning("Yetkisiz erişim kaydı yazılamadı: %s", exc)


def record_rate_limit_exceeded(key: str) -> None:
    """Hız sınırı aşımını kaydeder; 10 dk içinde 10+ aşımda 'rate_limit_abuse' olayı açar."""
    ip = _client_ip_from_key(key)
    try:
        with get_db(immediate=True) as conn:
            _insert_event(conn, "rate_limit_exceeded", "medium", ip, "", "", "", "", f"Hız sınırı aşıldı: {key}")
            count = _recent_event_count(conn, "rate_limit_exceeded", ip, 10)
            if count >= 10:
                open_or_bump_incident(
                    conn,
                    incident_type="rate_limit_abuse",
                    severity="medium",
                    source_ip=ip,
                    path="",
                    summary=f"10 dakika içinde {count} kez hız sınırı aşıldı (olası otomatik/toplu istek).",
                )
    except Exception as exc:  # pragma: no cover
        logger.warning("Hız sınırı kaydı yazılamadı: %s", exc)


def record_bulk_operation(
    kind: str,
    actor: str,
    ip: str,
    detail: str,
    threshold: int,
    window_minutes: int = 60,
    user_agent: str = "",
) -> None:
    """
    Toplu veri çekme/silme işlemlerini (dışa aktarma, çoklu silme) kaydeder.
    Pencere içinde `threshold` kez aşılırsa olay açar.
    """
    try:
        with get_db(immediate=True) as conn:
            _insert_event(conn, kind, "medium", ip, "", "", user_agent, actor, detail)
            count = conn.execute(
                "SELECT COUNT(*) AS c FROM audit_logs WHERE event_type = ? AND at >= ?",
                (kind, _window_start_iso(window_minutes)),
            ).fetchone()["c"]
            if count >= threshold:
                open_or_bump_incident(
                    conn,
                    incident_type=f"{kind}_spike",
                    severity="high",
                    source_ip=ip,
                    path="",
                    summary=f"{window_minutes} dakika içinde {count} adet '{kind}' işlemi yapıldı (eşik: {threshold}).",
                    data_exposed=(kind == "bulk_data_export"),
                )
    except Exception as exc:  # pragma: no cover
        logger.warning("Toplu işlem kaydı yazılamadı: %s", exc)


# ----------------------------------------------------------------------------------
# Yönetici paneli: olay listesi, müdahale ekranı, rapor
# ----------------------------------------------------------------------------------

def _deadline_info(detected_at: str, closed_at: Optional[str], notified_at: Optional[str]) -> dict[str, Any]:
    start = parse_to_istanbul(detected_at) or get_istanbul_now()
    deadline = start + timedelta(hours=BREACH_NOTIFICATION_HOURS)
    now = get_istanbul_now()
    remaining = (deadline - now).total_seconds() / 3600.0
    stopped = bool(notified_at or closed_at)
    return {
        "deadline": deadline.isoformat(),
        "hours_remaining": round(remaining, 1),
        "overdue": (remaining < 0) and not stopped,
        "clock_stopped": stopped,
    }


def _row_to_incident(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["status_label"] = STATUS_LABELS.get(d["status"], d["status"])
    d.update(_deadline_info(d["detected_at"], d.get("closed_at"), d.get("authority_notified_at")))
    return d


def list_incidents(conn: sqlite3.Connection, limit: int = 100) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM security_incidents ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [_row_to_incident(r) for r in rows]


def get_incident(conn: sqlite3.Connection, incident_id: int) -> Optional[dict[str, Any]]:
    row = conn.execute("SELECT * FROM security_incidents WHERE id = ?", (incident_id,)).fetchone()
    return _row_to_incident(row) if row else None


def security_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    open_rows = conn.execute(
        "SELECT * FROM security_incidents WHERE status IN ('open', 'investigating') ORDER BY detected_at ASC"
    ).fetchall()
    incidents = [_row_to_incident(r) for r in open_rows]
    since_24h = (get_istanbul_now() - timedelta(hours=24)).isoformat()
    events_24h = conn.execute("SELECT COUNT(*) AS c FROM audit_logs WHERE at >= ?", (since_24h,)).fetchone()["c"]
    failed_24h = conn.execute(
        "SELECT COUNT(*) AS c FROM audit_logs WHERE at >= ? AND event_type IN ('failed_login', 'unauthorized_access')",
        (since_24h,),
    ).fetchone()["c"]
    return {
        "open_incidents": len(incidents),
        "overdue_incidents": sum(1 for i in incidents if i["overdue"]),
        "most_urgent_hours_remaining": min((i["hours_remaining"] for i in incidents), default=None),
        "events_24h": events_24h,
        "blocked_attempts_24h": failed_24h,
        "total_customers": _count_customers(conn),
    }


def create_manual_incident(
    conn: sqlite3.Connection,
    summary: str,
    severity: str = "high",
    detected_at: Optional[str] = None,
    data_exposed: bool = True,
    affected_confirmed: Optional[int] = None,
    actor: str = "yonetici",
) -> int:
    """Yöneticinin elle açtığı olası ihlal kaydı. `detected_at` verilmezse 'şimdi' kabul edilir."""
    summary = (summary or "").strip()
    if not summary:
        raise ValueError("Olay açıklaması zorunludur.")
    if severity not in SEVERITIES:
        severity = "high"
    when = None
    if detected_at:
        dt = parse_to_istanbul(detected_at)
        if dt is None:
            raise ValueError("Tespit zamanı geçersiz.")
        when = dt.isoformat()
    when = when or get_istanbul_now_iso()
    estimate = _count_customers(conn) if data_exposed else 0
    cur = conn.execute(
        """
        INSERT INTO security_incidents
            (detected_at, last_event_at, incident_type, severity, status, source_ip, path, summary,
             event_count, affected_estimate, affected_confirmed, data_exposed, is_manual)
        VALUES (?, ?, 'manual_report', ?, 'open', '', '', ?, 1, ?, ?, ?, 1)
        """,
        (when, get_istanbul_now_iso(), severity, summary[:500], estimate, affected_confirmed, 1 if data_exposed else 0),
    )
    _insert_event(conn, "incident_created_manually", severity, "", "", "", "", actor, summary[:300])
    return cur.lastrowid


def update_incident(
    conn: sqlite3.Connection,
    incident_id: int,
    status: Optional[str] = None,
    notes: Optional[str] = None,
    affected_confirmed: Optional[int] = None,
    data_exposed: Optional[bool] = None,
    authority_notified: Optional[bool] = None,
    subjects_notified: Optional[bool] = None,
    actor: str = "yonetici",
) -> Optional[dict[str, Any]]:
    inc = get_incident(conn, incident_id)
    if not inc:
        return None
    now_iso = get_istanbul_now_iso()
    sets: list[str] = []
    params: list[Any] = []

    if status is not None:
        if status not in VALID_STATUSES:
            raise ValueError("Geçersiz durum.")
        sets.append("status = ?")
        params.append(status)
        sets.append("closed_at = ?")
        params.append(now_iso if status == "closed" else None)
    if notes is not None:
        sets.append("notes = ?")
        params.append(notes[:4000])
    if affected_confirmed is not None:
        sets.append("affected_confirmed = ?")
        params.append(max(0, int(affected_confirmed)))
    if data_exposed is not None:
        sets.append("data_exposed = ?")
        params.append(1 if data_exposed else 0)
        if data_exposed and not inc["affected_estimate"]:
            sets.append("affected_estimate = ?")
            params.append(_count_customers(conn))
    if authority_notified is not None:
        sets.append("authority_notified_at = ?")
        params.append(now_iso if authority_notified else None)
        if authority_notified and status is None and inc["status"] in ("open", "investigating"):
            sets.append("status = ?")
            params.append("notified")
    if subjects_notified is not None:
        sets.append("subjects_notified_at = ?")
        params.append(now_iso if subjects_notified else None)

    if sets:
        params.append(incident_id)
        conn.execute(f"UPDATE security_incidents SET {', '.join(sets)} WHERE id = ?", params)
        _insert_event(conn, "incident_updated", "info", "", "", "", "", actor, f"Olay #{incident_id} güncellendi.")
    return get_incident(conn, incident_id)


def build_incident_report(conn: sqlite3.Connection, incident_id: int, business_name: str = "") -> Optional[str]:
    """Kurul bildirimi için kullanılabilecek düz metin teknik rapor taslağı (hukuki metin değildir)."""
    inc = get_incident(conn, incident_id)
    if not inc:
        return None

    def fmt(ts: Optional[str]) -> str:
        dt = parse_to_istanbul(ts) if ts else None
        return dt.strftime("%d.%m.%Y %H:%M (İstanbul)") if dt else "—"

    affected = inc["affected_confirmed"] if inc["affected_confirmed"] is not None else inc["affected_estimate"]
    basis = "yönetici onaylı" if inc["affected_confirmed"] is not None else "otomatik tahmin (üst sınır)"
    recent = conn.execute(
        "SELECT at, event_type, ip, path, detail FROM audit_logs WHERE ip = ? ORDER BY id DESC LIMIT 15",
        (inc["source_ip"],),
    ).fetchall() if inc["source_ip"] else []

    lines = [
        "VERİ İHLALİ TEKNİK OLAY RAPORU TASLAĞI",
        "=" * 44,
        f"İşletme / Veri Sorumlusu : {business_name or '—'}",
        f"Olay No                  : #{inc['id']}",
        f"Olay Türü                : {inc['incident_type']}" + ("  (elle açıldı)" if inc["is_manual"] else "  (otomatik tespit)"),
        f"Önem Derecesi            : {inc['severity']}",
        f"Tespit Zamanı            : {fmt(inc['detected_at'])}",
        f"Son Olay Zamanı          : {fmt(inc['last_event_at'])}",
        f"72 Saatlik Süre Sonu     : {fmt(inc['deadline'])}",
        f"Kalan Süre               : {inc['hours_remaining']} saat" + ("  (SÜRE DOLDU)" if inc["overdue"] else ""),
        f"Kaynak IP                : {inc['source_ip'] or '—'}",
        f"İlgili Yol               : {inc['path'] or '—'}",
        f"Olay Sayısı              : {inc['event_count']}",
        f"Veri İfşası Var Mı       : {'Evet / şüpheli' if inc['data_exposed'] else 'Hayır (engellenen girişim; doğrulanmadı)'}",
        f"Etkilenen Kişi/Kayıt     : {affected}  ({basis})",
        f"Kurul'a Bildirim         : {fmt(inc['authority_notified_at'])}",
        f"İlgili Kişilere Bildirim : {fmt(inc['subjects_notified_at'])}",
        "",
        "Açıklama:",
        f"  {inc['summary']}",
    ]
    if inc["notes"]:
        lines += ["", "Yönetici Notları:", f"  {inc['notes']}"]
    if recent:
        lines += ["", "Aynı IP'den son kayıtlar:"]
        for r in recent:
            lines.append(f"  - {fmt(r['at'])} | {r['event_type']} | {r['path'] or '-'} | {r['detail'] or '-'}")
    lines += [
        "",
        "Not: Bu metin sistem kayıtlarından otomatik üretilmiş teknik bir taslaktır; hukuki değerlendirme içermez.",
    ]
    return "\n".join(lines)


def list_audit_logs(conn: sqlite3.Connection, limit: int = 100, event_type: Optional[str] = None) -> list[dict[str, Any]]:
    if event_type:
        rows = conn.execute(
            "SELECT * FROM audit_logs WHERE event_type = ? ORDER BY id DESC LIMIT ?", (event_type, limit)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM audit_logs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def list_consent_records(conn: sqlite3.Connection, limit: int = 200) -> list[dict[str, Any]]:
    """KVKK onay kayıtları (telefon maskeli)."""
    from app.utils import mask_phone

    rows = conn.execute(
        """
        SELECT id, display_name, phone_e164, created_at, kvkk_read_at, explicit_consent_at, consent_ip,
               consent_version, consent_source, marketing_consent, marketing_consent_at
        FROM customers WHERE deleted_at IS NULL ORDER BY id DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["masked_phone"] = mask_phone(d.pop("phone_e164"))
        out.append(d)
    return out

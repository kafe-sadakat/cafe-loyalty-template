"""
Müdür paneli "Genel (Özet)" analitiği: kartlar, günlük seriler, kullanıcı dağılımı ve tıklayınca açılan müşteri listeleri.

Tarihler İstanbul saatiyle ISO biçiminde saklanır (örn. 2026-11-05T14:00:00+03:00); gün anahtarı ilk 10 karakterdir.
"""
from __future__ import annotations

import sqlite3
from datetime import timedelta
from typing import Any

from app.utils import format_phone, get_istanbul_now

_PUSH_ON = (
    "(COALESCE(c.push_opt_in, 0) = 1 OR EXISTS ("
    "SELECT 1 FROM push_subscriptions ps WHERE ps.customer_id = c.id AND ps.active = 1))"
)

BUCKETS = [
    ("0", "Hiç damga almamış", lambda n: n == 0),
    ("1", "1 damga", lambda n: n == 1),
    ("2", "2 damga", lambda n: n == 2),
    ("3_4", "3-4 damga", lambda n: 3 <= n <= 4),
    ("5_7", "5-7 damga", lambda n: 5 <= n <= 7),
    ("8p", "8+ damga", lambda n: n >= 8),
]

SEGMENT_TITLES = {
    "customers_all": "Tüm Müşteriler",
    "new_today": "Bugün Kayıt Olanlar",
    "new_week": "Son 7 Günde Kayıt Olanlar",
    "stamps_total": "Damga Almış Müşteriler",
    "stamps_today": "Bugün Damga Okutanlar",
    "gifts_earned": "Hediye Kazananlar",
    "gifts_used": "Hediyesini Kullananlar",
    "gifts_pending": "Hediyesi Bekleyenler",
    "push_on": "Bildirimi Açık Olanlar",
    "push_off": "Bildirimi Kapalı Olanlar",
    "pwa_on": "Uygulamayı İndirenler",
    "pwa_off": "Uygulamayı İndirmeyenler",
    **{f"bucket_{key}": f"Damga Dağılımı: {label}" for key, label, _ in BUCKETS},
}


def _customer_rows(conn: sqlite3.Connection, today: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"""
        SELECT c.id, c.phone_e164, c.display_name, c.created_at,
          COALESCE(c.pwa_bonus_claimed, 0) AS pwa,
          {_PUSH_ON} AS push_on,
          (SELECT COUNT(*) FROM stamps s WHERE s.customer_id = c.id AND s.voided_at IS NULL AND s.reward_id IS NULL) AS current_stamps,
          (SELECT COUNT(*) FROM stamps s WHERE s.customer_id = c.id AND s.voided_at IS NULL) AS lifetime_stamps,
          (SELECT COUNT(*) FROM stamps s WHERE s.customer_id = c.id AND s.voided_at IS NULL AND substr(s.created_at, 1, 10) = ?) AS stamps_today,
          (SELECT COUNT(*) FROM rewards r WHERE r.customer_id = c.id AND r.voided_at IS NULL) AS rewards_earned,
          (SELECT COUNT(*) FROM rewards r WHERE r.customer_id = c.id AND r.voided_at IS NULL AND r.redeemed_at IS NOT NULL) AS rewards_used,
          (SELECT COUNT(*) FROM rewards r WHERE r.customer_id = c.id AND r.voided_at IS NULL AND r.redeemed_at IS NULL) AS rewards_pending,
          (SELECT MAX(created_at) FROM stamps s WHERE s.customer_id = c.id AND s.voided_at IS NULL) AS last_stamp,
          (SELECT MAX(redeemed_at) FROM rewards r WHERE r.customer_id = c.id AND r.voided_at IS NULL) AS last_redeem
        FROM customers c WHERE c.deleted_at IS NULL
        """,
        (today,),
    ).fetchall()
    out = []
    for r in rows:
        last = max([x for x in (r["last_stamp"], r["last_redeem"], r["created_at"]) if x] or [""])
        out.append({
            "id": r["id"],
            "display_name": (r["display_name"] or "").strip() or f"user{r['id']}",
            "phone": format_phone(r["phone_e164"]),
            "created_at": r["created_at"],
            "pwa": bool(r["pwa"]),
            "push_on": bool(r["push_on"]),
            "current_stamps": r["current_stamps"],
            "lifetime_stamps": r["lifetime_stamps"],
            "stamps_today": r["stamps_today"],
            "rewards_earned": r["rewards_earned"],
            "rewards_used": r["rewards_used"],
            "rewards_pending": r["rewards_pending"],
            "last_activity": last,
        })
    return out


def _segment_filter(kind: str, today: str, week_start: str):
    simple = {
        "customers_all": lambda c: True,
        "new_today": lambda c: (c["created_at"] or "")[:10] == today,
        "new_week": lambda c: (c["created_at"] or "")[:10] >= week_start,
        "stamps_total": lambda c: c["lifetime_stamps"] > 0,
        "stamps_today": lambda c: c["stamps_today"] > 0,
        "gifts_earned": lambda c: c["rewards_earned"] > 0,
        "gifts_used": lambda c: c["rewards_used"] > 0,
        "gifts_pending": lambda c: c["rewards_pending"] > 0,
        "push_on": lambda c: c["push_on"],
        "push_off": lambda c: not c["push_on"],
        "pwa_on": lambda c: c["pwa"],
        "pwa_off": lambda c: not c["pwa"],
    }
    if kind in simple:
        return simple[kind]
    for key, _label, pred in BUCKETS:
        if kind == f"bucket_{key}":
            return lambda c, pred=pred: pred(c["lifetime_stamps"])
    return None


def _day_keys(days: int) -> list[str]:
    now = get_istanbul_now()
    return [(now - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days - 1, -1, -1)]


def get_dashboard_analytics(conn: sqlite3.Connection, days: int = 7) -> dict[str, Any]:
    days = 30 if days >= 30 else 7
    now = get_istanbul_now()
    today = now.strftime("%Y-%m-%d")
    week_start = (now - timedelta(days=6)).strftime("%Y-%m-%d")
    customers = _customer_rows(conn, today)
    total = len(customers)

    def count(kind: str) -> int:
        f = _segment_filter(kind, today, week_start)
        return sum(1 for c in customers if f(c))

    stamps_total = conn.execute("SELECT COUNT(*) AS n FROM stamps WHERE voided_at IS NULL").fetchone()["n"]
    stamps_today = conn.execute(
        "SELECT COUNT(*) AS n FROM stamps WHERE voided_at IS NULL AND substr(created_at, 1, 10) = ?", (today,)
    ).fetchone()["n"]
    gifts_earned = conn.execute("SELECT COUNT(*) AS n FROM rewards WHERE voided_at IS NULL").fetchone()["n"]
    gifts_used = conn.execute(
        "SELECT COUNT(*) AS n FROM rewards WHERE voided_at IS NULL AND redeemed_at IS NOT NULL"
    ).fetchone()["n"]

    keys = _day_keys(days)
    first = keys[0]
    visitors = {r["d"]: r["v"] for r in conn.execute(
        "SELECT substr(created_at, 1, 10) AS d, COUNT(DISTINCT customer_id) AS v FROM stamps "
        "WHERE voided_at IS NULL AND substr(created_at, 1, 10) >= ? GROUP BY d", (first,))}
    stamps = {r["d"]: r["n"] for r in conn.execute(
        "SELECT substr(created_at, 1, 10) AS d, COUNT(*) AS n FROM stamps "
        "WHERE voided_at IS NULL AND substr(created_at, 1, 10) >= ? GROUP BY d", (first,))}
    gifts = {r["d"]: r["n"] for r in conn.execute(
        "SELECT substr(redeemed_at, 1, 10) AS d, COUNT(*) AS n FROM rewards "
        "WHERE voided_at IS NULL AND redeemed_at IS NOT NULL AND substr(redeemed_at, 1, 10) >= ? GROUP BY d", (first,))}
    signups = {r["d"]: r["n"] for r in conn.execute(
        "SELECT substr(created_at, 1, 10) AS d, COUNT(*) AS n FROM customers "
        "WHERE deleted_at IS NULL AND substr(created_at, 1, 10) >= ? GROUP BY d", (first,))}
    series = [
        {
            "date": k, "label": f"{k[8:10]}.{k[5:7]}",
            "visitors": visitors.get(k, 0), "stamps": stamps.get(k, 0),
            "gifts": gifts.get(k, 0), "signups": signups.get(k, 0),
        }
        for k in keys
    ]

    def pct(n: int) -> float:
        return round(n * 100.0 / total, 1) if total else 0.0

    distribution = {
        "stamp_buckets": [
            {"key": f"bucket_{key}", "label": label, "count": count(f"bucket_{key}"), "pct": pct(count(f"bucket_{key}"))}
            for key, label, _ in BUCKETS
        ],
        "shares": [
            {"key": "gifts_earned", "label": "Hediye alan", "count": count("gifts_earned"), "pct": pct(count("gifts_earned"))},
            {"key": "push_on", "label": "Bildirimi açık", "count": count("push_on"), "pct": pct(count("push_on"))},
            {"key": "pwa_on", "label": "Uygulamayı indiren", "count": count("pwa_on"), "pct": pct(count("pwa_on"))},
        ],
    }
    return {
        "days": days,
        "cards": {
            "customers_total": total,
            "new_today": count("new_today"),
            "new_week": count("new_week"),
            "stamps_total": stamps_total,
            "stamps_today": stamps_today,
            "stamp_customers": count("stamps_total"),
            "stamp_customers_today": count("stamps_today"),
            "gifts_earned": gifts_earned,
            "gifts_used": gifts_used,
            "gifts_pending": count("gifts_pending"),
            "push_on": count("push_on"),
            "pwa_on": count("pwa_on"),
        },
        "series": series,
        "distribution": distribution,
    }


def get_segment_customers(conn: sqlite3.Connection, kind: str, limit: int = 500) -> dict[str, Any] | None:
    now = get_istanbul_now()
    today = now.strftime("%Y-%m-%d")
    week_start = (now - timedelta(days=6)).strftime("%Y-%m-%d")
    flt = _segment_filter(kind, today, week_start)
    if flt is None:
        return None
    rows = [c for c in _customer_rows(conn, today) if flt(c)]
    rows.sort(key=lambda c: c["last_activity"] or "", reverse=True)
    return {
        "kind": kind,
        "title": SEGMENT_TITLES.get(kind, kind),
        "total": len(rows),
        "customers": [
            {
                "id": c["id"], "display_name": c["display_name"], "phone": c["phone"],
                "current_stamps": c["current_stamps"], "lifetime_stamps": c["lifetime_stamps"],
                "stamps_today": c["stamps_today"], "rewards_pending": c["rewards_pending"],
                "last_activity": c["last_activity"],
            }
            for c in rows[:limit]
        ],
    }

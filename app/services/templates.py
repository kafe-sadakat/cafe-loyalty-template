"""Kampanya ve bildirim şablonları (müdür: ekle / düzenle / sil).

kind = 'campaign' (kampanya başlığı + detay metni) veya 'push' (bildirim başlığı + metni). Şablon yalnızca formu doldurur;
gönderim/yayın yine mevcut uçlardan yapılır (bildirim metni olduğu gibi gider; "kapat" satırı eklenmez, ADR-27).
"""
from __future__ import annotations

import sqlite3
from typing import Any, Optional

from app.db import log_audit
from app.utils import get_istanbul_now_iso

KINDS = ("campaign", "push")
KIND_LABELS = {"campaign": "Kampanya", "push": "Bildirim"}
MAX_PER_KIND = 30
# (başlık sınırı, metin sınırı): bildirim metni telefon bildirim alanına sığsın diye kısa tutulur
LIMITS = {"campaign": (120, 1000), "push": (80, 240)}
MAX_LABEL = 60
_COLS = "id, kind, label, title, body, sort_order"


def _clean(text: Optional[str]) -> str:
    return (text or "").strip()


def validate_template(
    kind: str, label: Optional[str], title: Optional[str], body: Optional[str]
) -> tuple[Optional[dict[str, str]], Optional[str]]:
    """Alanları temizler ve doğrular; (temiz alanlar, hata) döner. Etiket boşsa başlıktan türetilir."""
    if kind not in KINDS:
        return None, "Geçersiz şablon türü."
    clean_title, clean_body = _clean(title), _clean(body)
    max_title, max_body = LIMITS[kind]
    if not clean_title:
        return None, "Şablon başlığı boş bırakılamaz."
    if not clean_body:
        return None, "Şablon metni boş bırakılamaz."
    if len(clean_title) > max_title:
        return None, f"Başlık en fazla {max_title} karakter olabilir."
    if len(clean_body) > max_body:
        return None, f"Metin en fazla {max_body} karakter olabilir."
    clean_label = " ".join(_clean(label).split()) or clean_title
    if len(clean_label) > MAX_LABEL:
        clean_label = clean_label[: MAX_LABEL - 1].rstrip() + "…"
    return {"label": clean_label, "title": clean_title, "body": clean_body}, None


def list_templates(conn: sqlite3.Connection, kind: Optional[str] = None) -> list[dict[str, Any]]:
    sql = f"SELECT {_COLS} FROM message_templates"
    args: tuple = ()
    if kind in KINDS:
        sql += " WHERE kind = ?"
        args = (kind,)
    sql += " ORDER BY kind ASC, sort_order ASC, id ASC"
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def create_template(
    conn: sqlite3.Connection, kind: str, label: Optional[str], title: Optional[str], body: Optional[str], actor: str = "mudur"
) -> tuple[bool, Optional[dict[str, Any]], Optional[str]]:
    fields, err = validate_template(kind, label, title, body)
    if err:
        return False, None, err
    count = conn.execute("SELECT COUNT(*) AS c FROM message_templates WHERE kind = ?", (kind,)).fetchone()["c"]
    if count >= MAX_PER_KIND:
        return False, None, f"En fazla {MAX_PER_KIND} {KIND_LABELS[kind].lower()} şablonu tutulabilir. Önce birini silin."
    nxt = conn.execute(
        "SELECT COALESCE(MAX(sort_order), 0) + 10 AS n FROM message_templates WHERE kind = ?", (kind,)
    ).fetchone()["n"]
    cur = conn.execute(
        "INSERT INTO message_templates (kind, label, title, body, sort_order, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (kind, fields["label"], fields["title"], fields["body"], nxt, get_istanbul_now_iso()),
    )
    log_audit(conn, actor, "template_created", f"{KIND_LABELS[kind]} şablonu eklendi: '{fields['label']}'.")
    row = conn.execute(f"SELECT {_COLS} FROM message_templates WHERE id = ?", (cur.lastrowid,)).fetchone()
    return True, dict(row), None


def update_template(
    conn: sqlite3.Connection, template_id: int, label: Optional[str], title: Optional[str], body: Optional[str], actor: str = "mudur"
) -> tuple[bool, Optional[dict[str, Any]], Optional[str]]:
    row = conn.execute("SELECT id, kind FROM message_templates WHERE id = ?", (template_id,)).fetchone()
    if not row:
        return False, None, "Şablon bulunamadı."
    fields, err = validate_template(row["kind"], label, title, body)
    if err:
        return False, None, err
    conn.execute(
        "UPDATE message_templates SET label = ?, title = ?, body = ?, updated_at = ? WHERE id = ?",
        (fields["label"], fields["title"], fields["body"], get_istanbul_now_iso(), template_id),
    )
    log_audit(conn, actor, "template_updated", f"{KIND_LABELS[row['kind']]} şablonu güncellendi: '{fields['label']}'.")
    out = conn.execute(f"SELECT {_COLS} FROM message_templates WHERE id = ?", (template_id,)).fetchone()
    return True, dict(out), None


def delete_template(conn: sqlite3.Connection, template_id: int, actor: str = "mudur") -> tuple[bool, Optional[str]]:
    row = conn.execute("SELECT kind, label FROM message_templates WHERE id = ?", (template_id,)).fetchone()
    if not row:
        return False, "Şablon bulunamadı."
    conn.execute("DELETE FROM message_templates WHERE id = ?", (template_id,))
    log_audit(conn, actor, "template_deleted", f"{KIND_LABELS[row['kind']]} şablonu silindi: '{row['label']}'.")
    return True, None

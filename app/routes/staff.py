"""Çalışan (kasiyer) paneli JSON API'si (/api/staff/*). Çalışan veya müdür oturumu gerekir.
Kasa ekranının tek kullanımlık Damga QR kodları da buradan üretilir ve yoklanır (ADR-28)."""
from __future__ import annotations

import base64
import threading
import time
from typing import Optional

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel

from app import auth
from app.config import PUBLIC_BASE_URL, request_public_base
from app.db import get_db
from app.services import security, stamp_codes
from app.services.admin import generate_qr_png_bytes
from app.services.staff import (
    actor_username,
    staff_add_stamp,
    staff_campaign_review,
    staff_lookup,
    staff_otp_reset,
    staff_redeem_reward,
    staff_register_customer,
    staff_search,
)
from app.utils import check_rate_limit, get_istanbul_now_iso

router = APIRouter(prefix="/api/staff", dependencies=[Depends(auth.require_staff)])


class PhoneIn(BaseModel):
    """Müşteri: tam telefon numarası YA DA arama sonucundan gelen müşteri kimliği."""
    phone: Optional[str] = None
    customer_id: Optional[int] = None


class SearchIn(BaseModel):
    q: str


class RegisterIn(BaseModel):
    phone: str
    display_name: Optional[str] = None
    consent_confirmed: bool = False


def _guard(request: Request) -> Optional[dict]:
    if not check_rate_limit(f"staff_ops_{auth.client_ip(request)}", max_requests=90, window_seconds=60):
        return {"status": "error", "message": "Çok fazla istek. Lütfen biraz bekleyin."}
    return None


# G-07: müşteri kimliği (id) ardışık denenerek müşteri tabanı taranmasın. Kasa ekranı kimliği DAİMA bir arama sonucundan
# alır; bu yüzden son 30 dakikada bu çalışanın aramasında dönmemiş kimliklerle yapılan sorgular ayrıca sınırlanır
# (10 dakikada 20). Normal iş akışı (ara → müşteriyi seç → damga/hediye) etkilenmez.
_SEEN_TTL_SECONDS = 1800
_SEEN_MAX_PER_STAFF = 500
_seen_ids: dict[str, dict[int, float]] = {}
_seen_lock = threading.Lock()


def _staff_key(request: Request) -> str:
    return f"{_sid(request) or ''}:{_name(request)}"


def _remember_customer_ids(request: Request, result: dict) -> None:
    ids = [c.get("id") for c in (result.get("customers") or []) if isinstance(c, dict)]
    ids += [(result.get("customer") or {}).get("id")] if isinstance(result.get("customer"), dict) else []
    now = time.time()
    with _seen_lock:
        seen = _seen_ids.setdefault(_staff_key(request), {})
        for cid in ids:
            if isinstance(cid, int):
                seen[cid] = now
        if len(seen) > _SEEN_MAX_PER_STAFF:
            for cid, ts in sorted(seen.items(), key=lambda kv: kv[1])[: len(seen) - _SEEN_MAX_PER_STAFF]:
                seen.pop(cid, None)


def _probe_guard(request: Request, customer_id: Optional[int]) -> Optional[dict]:
    """Aramadan gelmemiş `customer_id` denemelerini sınırlar; aşılırsa olay kaydı açar."""
    if not customer_id:
        return None
    now = time.time()
    with _seen_lock:
        ts = _seen_ids.get(_staff_key(request), {}).get(int(customer_id))
    if ts is not None and now - ts < _SEEN_TTL_SECONDS:
        return None
    if check_rate_limit(f"staff_idprobe_{_staff_key(request)}", max_requests=20, window_seconds=600):
        return None
    security.log_event(
        "staff_id_enumeration", "high", ip=auth.client_ip(request), path=request.scope.get("path", ""), method="POST",
        actor=_name(request), detail="Aramadan gelmeyen müşteri kimlikleri art arda denendi (müşteri taraması şüphesi).",
    )
    return {"status": "error", "message": "Çok fazla istek. Müşteriyi arayarak seçin."}


def _name(request: Request) -> str:
    return (auth.current_staff(request) or {}).get("name") or "çalışan"


def _sid(request: Request) -> Optional[int]:
    return (auth.current_staff(request) or {}).get("staff_id")


@router.get("/session")
def session_info(request: Request):
    return {"status": "success", "name": _name(request)}


@router.post("/search")
def search(req: SearchIn, request: Request):
    """Telefon numarası (5XX…) veya kullanıcı adı ile müşteri arar; bekleyen hediye bilgisi sonuçla gelir."""
    if (denied := _guard(request)):
        return denied
    with get_db(immediate=True) as conn:
        result = staff_search(conn, req.q, _name(request), auth.client_ip(request), _sid(request))
    _remember_customer_ids(request, result)
    return result


class CustomerLookupIn(BaseModel):
    q: Optional[str] = None       # telefon (5XX…) veya kullanıcı adı
    phone: Optional[str] = None
    customer_id: Optional[int] = None


@router.post("/customer-lookup")
def customer_lookup(req: CustomerLookupIn, request: Request):
    """
    Tek sorgu ucu: `q` (telefon/kullanıcı adı) → eşleşen müşteriler; `phone`/`customer_id` → tek müşteri.
    Her müşteri kanıt tablosu alanlarıyla döner (welcome/pwa/push_bonus_*, is_re_registered, recent_stamps_log).
    """
    if (denied := _guard(request)):
        return denied
    if (denied := _probe_guard(request, req.customer_id if not req.q else None)):
        return denied
    with get_db(immediate=True) as conn:
        if req.q:
            result = staff_search(conn, req.q, _name(request), auth.client_ip(request), _sid(request))
        else:
            result = staff_lookup(conn, req.phone, _name(request), auth.client_ip(request), _sid(request), req.customer_id)
    _remember_customer_ids(request, result)
    return result


@router.post("/lookup")
def lookup(req: PhoneIn, request: Request):
    if (denied := _guard(request)) or (denied := _probe_guard(request, req.customer_id)):
        return denied
    with get_db(immediate=True) as conn:
        result = staff_lookup(conn, req.phone, _name(request), auth.client_ip(request), _sid(request), req.customer_id)
    _remember_customer_ids(request, result)
    return result


@router.post("/stamp")
def stamp(req: PhoneIn, request: Request):
    if (denied := _guard(request)) or (denied := _probe_guard(request, req.customer_id)):
        return denied
    with get_db(immediate=True) as conn:
        return staff_add_stamp(conn, req.phone, _name(request), auth.client_ip(request), _sid(request), req.customer_id)


@router.post("/register")
def register(req: RegisterIn, request: Request):
    if (denied := _guard(request)):
        return denied
    with get_db(immediate=True) as conn:
        result = staff_register_customer(
            conn, req.phone, req.display_name, req.consent_confirmed, _name(request), auth.client_ip(request), _sid(request)
        )
    _remember_customer_ids(request, result)   # yeni kaydedilen müşteriyle hemen işlem yapılabilir (kimlik "aramadan gelmiş" sayılır)
    return result


@router.post("/redeem")
def redeem(req: PhoneIn, request: Request):
    if (denied := _guard(request)) or (denied := _probe_guard(request, req.customer_id)):
        return denied
    with get_db(immediate=True) as conn:
        return staff_redeem_reward(conn, req.phone, _name(request), auth.client_ip(request), _sid(request), req.customer_id)


@router.post("/otp-reset")
def otp_reset(req: PhoneIn, request: Request):
    """"Doğrulama kilidini sıfırla": müşteri kasadayken numaranın SMS doğrulama kilidini ve günlük sayacını sıfırlar."""
    if (denied := _guard(request)) or (denied := _probe_guard(request, req.customer_id)):
        return denied
    with get_db(immediate=True) as conn:
        result = staff_otp_reset(conn, req.phone, _name(request), auth.client_ip(request), _sid(request), req.customer_id)
    _remember_customer_ids(request, result)
    return result


class CampaignReviewIn(BaseModel):
    customer_id: int
    campaign_id: int
    action: str                  # "approve" (yorumu gördüm, damga ver) | "reject"
    reason: Optional[str] = ""


@router.post("/campaign-review")
def campaign_review(req: CampaignReviewIn, request: Request):
    """Ödüllü kampanya (ör. Google yorumu) doğrulaması: onay → kampanya damgası; ret → damga yok."""
    if (denied := _guard(request)):
        return denied
    with get_db(immediate=True) as conn:
        return staff_campaign_review(
            conn, req.customer_id, req.campaign_id, req.action, req.reason or "",
            _name(request), auth.client_ip(request), _sid(request),
        )


@router.get("/logs")
def recent_logs(request: Request):
    sid = _sid(request)
    name = _name(request)
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT id, at, action, customer_phone_masked, detail
            FROM staff_audit_logs
            WHERE (staff_id = ? OR staff_name = ?) AND action NOT IN ('customer_search', 'customer_lookup')
            ORDER BY id DESC LIMIT 20
            """,
            (sid, name),
        ).fetchall()
        today = get_istanbul_now_iso()[:10]
        stamps_count = conn.execute(
            "SELECT COUNT(*) FROM staff_audit_logs WHERE (staff_id = ? OR staff_name = ?) AND action = 'stamp_added' AND at LIKE ?",
            (sid, name, f"{today}%"),
        ).fetchone()[0]
        gifts_count = conn.execute(
            "SELECT COUNT(*) FROM staff_audit_logs WHERE (staff_id = ? OR staff_name = ?) AND action = 'reward_redeemed' AND at LIKE ?",
            (sid, name, f"{today}%"),
        ).fetchone()[0]
        return {
            "status": "success",
            "logs": [
                {
                    "id": r["id"],
                    "at": r["at"],
                    "action": r["action"],
                    "phone": r["customer_phone_masked"],
                    "detail": r["detail"],
                }
                for r in rows
            ],
            "today_stamps": stamps_count,
            "today_gifts": gifts_count,
        }


class StampCodeIn(BaseModel):
    replaces: Optional[int] = None   # kasa ekranında gösterilmekte olan (yerine yenisi istenen) kod


@router.post("/stamp-code")
def stamp_code_create(req: StampCodeIn, request: Request):
    """
    Kasa ekranı için yeni tek kullanımlık Damga QR kodu (ADR-28). Müşteri okutunca damga verilir ve kod biter; ekran
    hemen yenisini ister. Önceki (okutulmamış) kod kısa süre sonra geçersizleşir. QR görseli PNG data URL olarak döner.
    """
    if not check_rate_limit(f"stamp_code_{auth.client_ip(request)}", max_requests=60, window_seconds=60):
        return {"status": "error", "message": "Çok fazla istek. Lütfen biraz bekleyin."}
    name, sid = _name(request), _sid(request)
    base_url, base_path = request_public_base(request)
    with get_db(immediate=True) as conn:
        if req.replaces:
            stamp_codes.retire_code(conn, req.replaces)
        info = stamp_codes.create_stamp_code(
            conn, created_by=name, staff_id=sid, actor=actor_username(conn, sid, name),
            base_url=base_url, base_path=base_path,
        )
    png = generate_qr_png_bytes(info["url"])
    return {"status": "success", **info, "qr": "data:image/png;base64," + base64.b64encode(png).decode("ascii")}


@router.get("/stamp-code/status")
def stamp_code_status(request: Request, ids: str = ""):
    """Kasa ekranı yoklaması (en çok 5 kod): waiting | held (müşteri kayıt oluyor) | used (kime, sonucu) | expired."""
    if not check_rate_limit(f"stamp_code_poll_{auth.client_ip(request)}", max_requests=150, window_seconds=60):
        return {"status": "error", "message": "Çok fazla istek. Lütfen biraz bekleyin."}
    id_list = [int(x) for x in ids.split(",") if x.strip().isdigit()][:5]
    with get_db() as conn:
        return {"status": "success", "codes": [stamp_codes.code_status_for_staff(conn, i) for i in id_list]}


@router.get("/qr-image")
def qr_image(request: Request, kind: str = "damga"):
    """
    Sabit (basılabilir) QR görseli (ADR-35): kind=kayit → /k/m/kayit Kayıt / Uygulama QR'ı (damga VERMEZ); kind=damga →
    /k/m/qr kasadaki Damga QR'ı; kind=install → /k/indir masa menü + uygulama. Eski adlar register / stamp → /k/m/qr.
    Canlı (tek kullanımlık) Damga QR'ı ayrıca /stamp-code ile üretilir.
    """
    base_url, base_path = request_public_base(request)
    with get_db() as conn:
        path, _fname = stamp_codes.qr_image_target(conn, kind)   # ADR-41: damga → /m/{gizli kod}
    return Response(content=generate_qr_png_bytes(base_url + base_path + path), media_type="image/png", headers={"Cache-Control": "no-store"})

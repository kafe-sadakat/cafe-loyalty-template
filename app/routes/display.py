"""
Kasa QR Ekranı (ADR-30): /ekran sayfası ve /api/ekran/* uçları.

Ekran, müdür panelinden alınan tek kullanımlık eşleştirme bağlantısıyla bir kez eşleşir; sonra yalnızca kendi HttpOnly
çereziyle (sadakat_qr_display) çalışır. Çalışan/müdür oturumu gerekmez ve ekran başka hiçbir işleme erişemez: yalnızca
kendi tek kullanımlık Damga QR'larını üretir ve sonuçlarını (müşteri adı olmadan) yoklar. 302 yok; sayfalar 200 OK (ADR-01).
"""
from __future__ import annotations

import base64
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader
from pydantic import BaseModel

from app import auth
from app.config import DEFAULT_BUSINESS_NAME, request_public_base
from app.db import get_all_settings, get_db
from app.services import qr_displays, stamp_codes
from app.services.admin import generate_qr_png_bytes
from app.utils import check_rate_limit

page_router = APIRouter()
router = APIRouter(prefix="/api/ekran")

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
jinja_env = Environment(loader=FileSystemLoader(TEMPLATES_DIR), autoescape=True)
_NO_INDEX = {"X-Robots-Tag": "noindex, nofollow"}
_UNPAIRED = {"status": "error", "message": "Bu cihaz QR ekranı olarak eşleştirilmemiş ya da ekran kaldırıldı."}


class PairIn(BaseModel):
    code: str = ""


class DisplayCodeIn(BaseModel):
    replaces: Optional[int] = None   # ekranda gösterilmekte olan (yerine yenisi istenen) kod


def _set_display_cookie(response: Response, secret: str) -> None:
    response.set_cookie(
        key=qr_displays.DISPLAY_COOKIE, value=secret, httponly=True, secure=True, samesite="strict",
        path="/", max_age=qr_displays.DISPLAY_COOKIE_MAX_AGE,
    )


def _render(mode: str, label: str = "", pair_code: str = "") -> HTMLResponse:
    with get_db() as conn:
        settings = get_all_settings(conn)
    html = jinja_env.get_template("qr_ekran.html").render(
        mode=mode,
        label=label,
        pair_code=pair_code,
        business_name=settings.get("business_name", DEFAULT_BUSINESS_NAME),
        accent_color=settings.get("accent_color", "#1E4D3F"),
    )
    return HTMLResponse(html, status_code=200, headers=_NO_INDEX)


def _current_display(request: Request):
    with get_db() as conn:
        return qr_displays.display_from_secret(conn, request.cookies.get(qr_displays.DISPLAY_COOKIE))


# ------------------------------------------------------------------ sayfalar
@page_router.get("/ekran")
@page_router.get("/ekran/")
def display_page(request: Request):
    """Eşleştirilmiş cihazda canlı QR ekranı; değilse nasıl eşleştirileceğini anlatan sayfa."""
    secret = request.cookies.get(qr_displays.DISPLAY_COOKIE)
    display = _current_display(request)
    if display is None:
        return _render("unpaired")
    resp = _render("display", label=display["label"])
    _set_display_cookie(resp, secret)   # çerezin süresi her açılışta yenilenir
    return resp


@page_router.get("/ekran/eslestir/{code}")
@page_router.get("/ekran/eslestir/{code}/")
def display_pair_page(code: str, request: Request):
    """Eşleştirme sayfası. GET kodu TÜKETMEZ (bağlantı önizleyicileri); cihaz düğmeyle POST /api/ekran/pair gönderir."""
    if not check_rate_limit(f"display_pair_page_{auth.client_ip(request)}", max_requests=30, window_seconds=600):
        return HTMLResponse("Çok fazla istek yapıldı. Lütfen biraz bekleyiniz.", status_code=429)
    with get_db() as conn:
        row = qr_displays.pairing_row(conn, code)
    if row is None:
        return _render("pair_invalid")
    return _render("pair", label=row["label"], pair_code=code)


# ------------------------------------------------------------------ JSON uçları
@router.post("/pair")
def display_pair(req: PairIn, request: Request, response: Response):
    """Eşleştirme kodunu tüketir ve cihaza ekran çerezini yazar; önceki cihaz (varsa) düşer."""
    if not check_rate_limit(f"display_pair_{auth.client_ip(request)}", max_requests=10, window_seconds=900):
        response.status_code = 429
        return {"status": "error", "message": "Çok fazla deneme. 15 dakika sonra tekrar deneyin."}
    with get_db(immediate=True) as conn:
        paired = qr_displays.pair_device(conn, req.code)
    if paired is None:
        return {"status": "error", "message": "Eşleştirme bağlantısı geçersiz ya da süresi dolmuş. Müdür panelinden yeni bağlantı alın."}
    row, secret = paired
    _set_display_cookie(response, secret)
    return {"status": "success", "label": row["label"]}


@router.post("/stamp-code")
def display_stamp_code(req: DisplayCodeIn, request: Request, response: Response):
    """Ekran için yeni tek kullanımlık Damga QR'ı; önceki (okutulmamış) kod kısa süre sonra geçersizleşir."""
    display = _current_display(request)
    if display is None:
        response.status_code = 401
        return _UNPAIRED
    if not check_rate_limit(f"display_code_{display['id']}", max_requests=60, window_seconds=60):
        return {"status": "error", "message": "Çok fazla istek. Lütfen biraz bekleyin."}
    base_url, base_path = request_public_base(request)
    with get_db(immediate=True) as conn:
        if req.replaces and conn.execute(
            "SELECT 1 FROM stamp_codes WHERE id = ? AND display_id = ?", (req.replaces, display["id"])
        ).fetchone():
            stamp_codes.retire_code(conn, req.replaces)
        info = stamp_codes.create_stamp_code(
            conn, origin="display", display_id=display["id"], base_url=base_url, base_path=base_path
        )
        qr_displays.touch(conn, display["id"])
    png = generate_qr_png_bytes(info["url"])
    return {
        "status": "success",
        "id": info["id"],
        "url": info["url"],          # QR'ın içeriği (zaten ekranda görünür)
        "expires_at": info["expires_at"],
        "ttl": info["ttl"],
        "rotate_after": info["rotate_after"],
        "qr": "data:image/png;base64," + base64.b64encode(png).decode("ascii"),
    }


@router.get("/stamp-code/status")
def display_stamp_code_status(request: Request, response: Response, ids: str = ""):
    """Ekran yoklaması (en çok 5 kod, yalnızca bu ekranın kodları): waiting | held | used (+ sonuç) | expired.
    Ekran müşteriye dönük olduğu için müşteri adı verilmez."""
    display = _current_display(request)
    if display is None:
        response.status_code = 401
        return _UNPAIRED
    if not check_rate_limit(f"display_poll_{display['id']}", max_requests=150, window_seconds=60):
        return {"status": "error", "message": "Çok fazla istek. Lütfen biraz bekleyin."}
    id_list = [int(x) for x in ids.split(",") if x.strip().isdigit()][:5]
    if not id_list:
        return {"status": "success", "codes": []}
    with get_db() as conn:
        marks = ",".join("?" for _ in id_list)
        own = {r["id"] for r in conn.execute(
            f"SELECT id FROM stamp_codes WHERE display_id = ? AND id IN ({marks})", (display["id"], *id_list)
        ).fetchall()}
        codes = [stamp_codes.code_status_for_staff(conn, i, with_customer=False) for i in id_list if i in own]
    return {"status": "success", "codes": codes}

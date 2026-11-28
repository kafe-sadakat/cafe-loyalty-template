"""
Müdür paneli JSON API'si (/api/admin/*). TÜM uçlar müdür oturumu gerektirir.
Çalışan oturumu bu uçlara erişemez. Damga güvenliği (ADR-30): /stamp-security (basılı QR anahtarı + NTAG 424 DNA
anahtarları), /nfc-tags/{id}, /qr-displays (Kasa QR Ekranı eşleştirme / kaldırma).
"""
from __future__ import annotations

import base64
import re
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, Form, Query, Request, Response, UploadFile
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel

from app import auth
from app.config import DEFAULT_BUSINESS_NAME, PUBLIC_BASE_URL, is_push_enabled, request_public_base
from app.db import get_all_settings, get_db, get_kvkk_text, get_setting, log_audit, set_setting
from app.services import activity, nfc_sun, qr_displays, reports, stamp_codes
from app.services import otp as otp_service
from app.services import security
from app.services.admin import (
    admin_add_stamp,
    admin_create_customer,
    admin_redeem_reward,
    admin_remove_stamp,
    admin_set_stamps,
    admin_update_customer_name,
    create_campaign_admin,
    delete_campaign_admin,
    generate_qr_png_bytes,
    get_all_customers_admin,
    get_dashboard_metrics,
    get_recent_stamps,
    get_seals_admin,
    list_campaigns_admin,
    toggle_campaign_admin,
    update_campaign_admin,
    update_settings_admin,
)
from app.services.campaign_rewards import campaign_stats, list_participants, review_participant
from app.services.loyalty import anonymize_customer
from app.services.products import (
    create_product,
    delete_product,
    list_products_admin,
    update_product,
)
from app.services.push import (
    AUTO_NOTIF_DEFAULTS, AUTO_NOTIF_MAX, get_notification_logs, preview_campaign, send_campaign_push, send_test_push,
)
from app.services.accounts import (
    create_staff_account,
    delete_staff_account,
    get_account,
    list_staff_accounts,
    update_own_account,
    update_staff_account,
)
from app.services.analytics import get_dashboard_analytics, get_segment_customers
from app.services.categories import (
    create_category,
    delete_category,
    list_categories,
    rename_category,
    reorder_categories,
)
from app.services.fraud import review_customer
from app.services.templates import create_template, delete_template, list_templates, update_template
from app.services.staff import list_staff_logs, log_staff_action
from app.services.uploads import (
    MAX_UPLOAD_BYTES, UploadError, save_content_image, save_logo, validate_upload_name_and_magic,
)
from app.utils import hash_token, check_rate_limit, mask_phone

router = APIRouter(prefix="/api/admin", dependencies=[Depends(auth.require_manager)])

QR_INSTALL_PATH = "/k/indir"


def _uname(conn, request: Request) -> str:
    """Oturumdaki müdürün kullanıcı adı (rapor: işlemi yapan)."""
    uid = (auth.current_manager(request) or {}).get("user_id")
    row = conn.execute("SELECT username FROM admin_users WHERE id = ?", (uid,)).fetchone() if uid is not None else None
    return row["username"] if row else "mudur"


def _actor(request: Request) -> str:
    sess = auth.current_manager(request)
    return f"mudur:{(sess or {}).get('name') or 'mudur'}"


# ---------------------------------------------------------------- modeller
class CustomerCreate(BaseModel):
    phone: str
    display_name: Optional[str] = None
    consent_confirmed: bool = False


class CustomerName(BaseModel):
    display_name: str


class CampaignIn(BaseModel):
    title: str
    description: Optional[str] = ""
    image_url: Optional[str] = ""
    badge: Optional[str] = "KAMPANYA"
    active: int = 1
    # Ödüllü görev (ADR-26): 0 = düz duyuru, 1-5 = onaylanınca verilecek damga. Güncellemede None → değişmez.
    reward_stamps: Optional[int] = None
    action_url: Optional[str] = None
    action_label: Optional[str] = None


class ParticipantReviewIn(BaseModel):
    action: str                     # "approve" (damga ver) | "reject"
    reason: Optional[str] = ""


class TemplateIn(BaseModel):
    kind: str = "campaign"
    label: Optional[str] = ""
    title: str = ""
    body: str = ""


class ProductIn(BaseModel):
    name: str
    description: Optional[str] = ""
    price: float = 0
    category: Optional[str] = "Genel"
    image_url: Optional[str] = ""
    active: int = 1
    sort_order: int = 0
    is_new: int = 0
    discount_price: Optional[float] = None
    discount_label: Optional[str] = ""


class StaffCreate(BaseModel):
    name: str
    username: str
    password: str


class StaffUpdate(BaseModel):
    password: Optional[str] = None
    active: Optional[bool] = None
    username: Optional[str] = None
    name: Optional[str] = None


class AccountIn(BaseModel):
    current_password: str
    username: Optional[str] = None
    new_password: Optional[str] = None
    name: Optional[str] = None


class CategoryIn(BaseModel):
    name: str


class ReorderIn(BaseModel):
    ids: list[int]


class StampSetIn(BaseModel):
    count: int


class FraudReviewIn(BaseModel):
    action: str  # "clear" (incelendi) | "reset_stamps" (damgaları sıfırla)


class SettingsIn(BaseModel):
    business_name: Optional[str] = None
    reward_name: Optional[str] = None
    stamp_target: Optional[int] = None
    daily_limit: Optional[int] = None
    min_interval_minutes: Optional[int] = None
    bonus_install_enabled: Optional[bool] = None
    bonus_notif_enabled: Optional[bool] = None
    gift_requires_visit: Optional[bool] = None
    accent_color: Optional[str] = None
    kvkk_text: Optional[str] = None
    kvkk_contact: Optional[str] = None
    kvkk_controller: Optional[str] = None
    kvkk_address: Optional[str] = None
    menu_note: Optional[str] = None
    # ADR-45: otomatik bildirim metinleri (boş = varsayılan)
    notif_stamp_title: Optional[str] = None
    notif_stamp_body: Optional[str] = None
    notif_reward_title: Optional[str] = None
    notif_reward_body: Optional[str] = None


class StampSecurityIn(BaseModel):
    """ADR-30: None = değiştirme. Anahtarlar 32 karakter hex (boş = kaldır)."""
    static_qr_stamps: Optional[bool] = None
    nfc_meta_key: Optional[str] = None
    nfc_file_key: Optional[str] = None


class NfcTagIn(BaseModel):
    label: Optional[str] = None
    active: Optional[bool] = None


class QrDisplayIn(BaseModel):
    label: Optional[str] = ""


class PushSend(BaseModel):
    title: str
    body: str
    url: Optional[str] = None
    audience: str = "all"


class PushTest(BaseModel):
    title: Optional[str] = "Test Bildirimi"
    body: Optional[str] = "Bu bir test bildirimidir."
    endpoint: Optional[str] = None


class IncidentCreate(BaseModel):
    summary: str
    severity: str = "high"
    detected_at: Optional[str] = None
    data_exposed: bool = True
    affected_confirmed: Optional[int] = None


class IncidentUpdate(BaseModel):
    status: Optional[str] = None
    notes: Optional[str] = None
    affected_confirmed: Optional[int] = None
    data_exposed: Optional[bool] = None
    authority_notified: Optional[bool] = None
    subjects_notified: Optional[bool] = None


# ---------------------------------------------------------------- genel
@router.get("/session")
def session_info(request: Request):
    return {"status": "success", "role": "manager", "name": (auth.current_manager(request) or {}).get("name", "")}


@router.get("/overview")
def overview(request: Request, search: Optional[str] = None):
    base_url, base_path = request_public_base(request)
    ins_path = f"{base_path}/indir"
    with get_db() as conn:
        reg_path = base_path + stamp_codes.stamp_qr_path(conn)   # ADR-41: tahmin edilemez Damga QR adresi
        settings = get_all_settings(conn)
        return {
            "status": "success",
            "settings": {
                "business_name": settings.get("business_name", ""),
                "reward_name": settings.get("reward_name", ""),
                "stamp_target": int(settings.get("stamp_target", "5")),
                "daily_limit": int(settings.get("daily_limit", "2")),
                "min_interval_minutes": int(settings.get("min_interval_minutes", "0")),
                "bonus_install_enabled": (settings.get("bonus_install_enabled", "1") or "1").strip() != "0",
                "bonus_notif_enabled": (settings.get("bonus_notif_enabled", "1") or "1").strip() != "0",
                "gift_requires_visit": (settings.get("gift_requires_visit", "1") or "1").strip() != "0",
                "accent_color": settings.get("accent_color", "#1E4D3F"),
                "kvkk_text": settings.get("kvkk_text", ""),
                "kvkk_contact": settings.get("kvkk_contact", ""),
                "kvkk_controller": settings.get("kvkk_controller", ""),
                "kvkk_address": settings.get("kvkk_address", ""),
                "logo_url": settings.get("logo_url", ""),
                "menu_note": settings.get("menu_note", ""),
                # ADR-45: otomatik bildirim metinleri (kayıtlı değer; boşsa varsayılan gösterilir)
                **{k: (settings.get(k) or "").strip() or v for k, v in AUTO_NOTIF_DEFAULTS.items()},
            },
            "auto_notif_defaults": AUTO_NOTIF_DEFAULTS,
            "metrics": get_dashboard_metrics(conn),
            "customers": get_all_customers_admin(conn, search=search),
            "campaigns": list_campaigns_admin(conn),
            "templates": list_templates(conn),
            "categories": list_categories(conn),
            "products": list_products_admin(conn),
            "seals": get_seals_admin(conn),
            "recent_stamps": get_recent_stamps(conn, limit=30),
            "push": {
                "enabled": is_push_enabled(),
                "preview": preview_campaign(conn, audience="all"),
                "logs": get_notification_logs(conn, limit=20),
            },
            "security": security.security_summary(conn),
            "qr_stamp_url": base_url + reg_path,      # eski anahtar (geriye uyumluluk)
            "qr_register_url": base_url + reg_path,
            "qr_install_url": base_url + ins_path,
            "guest_url": f"{base_url}{base_path}/misafir",   # sosyal medya profil bağlantısı (menü + kampanyalar)
            "kvkk_preview": get_kvkk_text(settings),
        }


# ---------------------------------------------------------------- müşteriler
@router.get("/customers")
def customers(search: Optional[str] = None):
    with get_db() as conn:
        rows = get_all_customers_admin(conn, search=search)
    otp_on = otp_service.is_enabled()
    for row in rows:
        if isinstance(row, dict):
            row["otp_enabled"] = otp_on   # "Doğrulama kilidini sıfırla" düğmesi yalnızca SMS doğrulaması açıkken
    return {"status": "success", "customers": rows, "otp_enabled": otp_on}


@router.post("/customers/create")
def customer_create(req: CustomerCreate, request: Request):
    consent = None
    if req.consent_confirmed:
        consent = {"kvkk_read": True, "explicit_consent": True, "ip": auth.client_ip(request)}
    with get_db(immediate=True) as conn:
        ok, customer, err = admin_create_customer(conn, req.phone, req.display_name, consent=consent, actor=_actor(request))
        if not ok:
            return {"status": "error", "message": err}
        return {"status": "success", "message": "Müşteri başarıyla kaydedildi.", "customer": customer}


@router.post("/customers/{customer_id}/update-name")
def customer_update_name(customer_id: int, req: CustomerName):
    with get_db(immediate=True) as conn:
        ok, customer, err = admin_update_customer_name(conn, customer_id, req.display_name)
        if not ok:
            return {"status": "error", "message": err}
        return {"status": "success", "message": "Müşteri adı güncellendi.", "customer": customer}


@router.post("/customers/{customer_id}/stamp-add")
def customer_stamp_add(customer_id: int, request: Request):
    with get_db(immediate=True) as conn:
        ok, customer, err = admin_add_stamp(conn, customer_id, actor_username=_uname(conn, request))
        if not ok:
            return {"status": "error", "message": err}
        return {"status": "success", "message": "+1 Damga eklendi.", "customer": customer}


@router.post("/customers/{customer_id}/stamp-remove")
def customer_stamp_remove(customer_id: int):
    with get_db(immediate=True) as conn:
        ok, customer, err = admin_remove_stamp(conn, customer_id)
        if not ok:
            return {"status": "error", "message": err}
        return {"status": "success", "message": "-1 Damga düşürüldü.", "customer": customer}


@router.post("/customers/{customer_id}/redeem-reward")
def customer_redeem(customer_id: int, request: Request):
    with get_db(immediate=True) as conn:
        ok, customer, err = admin_redeem_reward(
            conn, customer_id, actor=_actor(request), redeemed_by=_uname(conn, request)
        )
        if not ok:
            return {"status": "error", "message": err}
        # Müdür ekranından verilen hediye de personel işlem günlüğüne işlenir (kim verdi, hangi müşteri)
        mgr = auth.current_manager(request) or {}
        log_staff_action(
            conn, (mgr.get("name") or "Müdür"), "reward_redeemed", customer_id,
            mask_phone(customer["phone_e164"]) if customer else "", "Müdür panelinden hediye verildi",
            auth.client_ip(request),
        )
        return {"status": "success", "message": "Ödül kullandırıldı.", "customer": customer}


@router.post("/customers/{customer_id}/otp-reset")
def customer_otp_reset(customer_id: int, request: Request):
    """
    "Doğrulama kilidini sıfırla" (docs/OTP.md): müşteri yanındayken numaranın SMS doğrulama kilitlerini (hatalı deneme)
    ve bugünkü SMS sayacını sıfırlar. Denetim ve personel işlem günlüğüne yazılır.
    """
    if not otp_service.is_enabled():
        return {"status": "error", "message": "SMS doğrulaması (OTP) bu kurulumda kapalı."}
    with get_db(immediate=True) as conn:
        row = conn.execute(
            "SELECT id, phone_e164 FROM customers WHERE id = ? AND deleted_at IS NULL", (customer_id,)
        ).fetchone()
        if not row:
            return {"status": "error", "message": "Müşteri bulunamadı."}
        res = otp_service.reset_phone(conn, row["phone_e164"], actor=_actor(request))
        mgr = auth.current_manager(request) or {}
        log_staff_action(
            conn, (mgr.get("name") or "Müdür"), "otp_reset", customer_id, mask_phone(row["phone_e164"]),
            "Müdür panelinden SMS doğrulama kilidi sıfırlandı", auth.client_ip(request),
        )
    return {"status": "success", "reset": res,
            "message": "Doğrulama kilidi sıfırlandı. Müşteri SMS kodunu yazabilir ya da yeni kod isteyebilir."}


@router.post("/customers/{customer_id}/fraud-review")
def customer_fraud_review(customer_id: int, req: FraudReviewIn, request: Request):
    """Şüpheli istismar uyarısını incele: 'clear' → uyarıyı kapat, 'reset_stamps' → müşterinin damgalarını sıfırla."""
    with get_db(immediate=True) as conn:
        ok, err = review_customer(conn, customer_id, req.action, actor=_actor(request))
        if not ok:
            return {"status": "error", "message": err}
        msg = "Damgalar sıfırlandı ve uyarı kapatıldı." if req.action == "reset_stamps" else "Uyarı incelendi ve kapatıldı."
        return {"status": "success", "message": msg}


@router.post("/customers/{customer_id}/stamp-set")
def customer_stamp_set(customer_id: int, req: StampSetIn):
    """Karttaki damga sayısını belirli bir değere ayarlar (0 … hedef-1)."""
    with get_db(immediate=True) as conn:
        ok, customer, err = admin_set_stamps(conn, customer_id, req.count)
        if not ok:
            return {"status": "error", "message": err}
        return {"status": "success", "message": f"Damga sayısı {req.count} olarak ayarlandı.", "customer": customer}


@router.post("/customers/{customer_id}/delete")
def customer_delete(customer_id: int, request: Request):
    """Müşteriyi anonimleştirir (KVKK silme). Toplu silme eşiği aşılırsa güvenlik olayı açılır."""
    with get_db(immediate=True) as conn:
        row = conn.execute(
            "SELECT id, phone_e164 FROM customers WHERE id = ? AND deleted_at IS NULL", (customer_id,)
        ).fetchone()
        if not row:
            return {"status": "error", "message": "Müşteri bulunamadı."}
        anonymize_customer(conn, row["id"], row["phone_e164"], actor=_actor(request), reason="Yönetici silme işlemi")
    security.record_bulk_operation(
        "bulk_customer_delete", actor=_actor(request), ip=auth.client_ip(request),
        detail=f"Müşteri #{customer_id} yönetici tarafından silindi.", threshold=5,
    )
    return {"status": "success", "message": "Müşteri kaydı silindi (anonimleştirildi)."}


# ---------------------------------------------------------------- şablonlar (kampanya + bildirim)
@router.get("/templates")
def templates_list(kind: Optional[str] = None):
    with get_db() as conn:
        return {"status": "success", "templates": list_templates(conn, kind)}


@router.post("/templates/create")
def template_create(req: TemplateIn, request: Request):
    with get_db(immediate=True) as conn:
        ok, tpl, err = create_template(conn, req.kind, req.label, req.title, req.body, actor=_actor(request))
        if not ok:
            return {"status": "error", "message": err}
        return {"status": "success", "message": "Şablon eklendi.", "template": tpl, "templates": list_templates(conn)}


@router.post("/templates/{template_id}/update")
def template_update(template_id: int, req: TemplateIn, request: Request):
    with get_db(immediate=True) as conn:
        ok, tpl, err = update_template(conn, template_id, req.label, req.title, req.body, actor=_actor(request))
        if not ok:
            return {"status": "error", "message": err}
        return {"status": "success", "message": "Şablon güncellendi.", "template": tpl, "templates": list_templates(conn)}


@router.post("/templates/{template_id}/delete")
@router.delete("/templates/{template_id}")
def template_delete(template_id: int, request: Request):
    with get_db(immediate=True) as conn:
        ok, err = delete_template(conn, template_id, actor=_actor(request))
        if not ok:
            return {"status": "error", "message": err}
        return {"status": "success", "message": "Şablon silindi.", "templates": list_templates(conn)}


# ---------------------------------------------------------------- kampanyalar
@router.post("/campaigns/create")
def campaign_create(req: CampaignIn):
    with get_db(immediate=True) as conn:
        ok, campaign, err = create_campaign_admin(
            conn, req.title, req.description or "", req.image_url or "", req.badge or "KAMPANYA", req.active,
            reward_stamps=req.reward_stamps or 0, action_url=req.action_url or "", action_label=req.action_label or "",
        )
        if not ok:
            return {"status": "error", "message": err}
        return {"status": "success", "message": "Kampanya eklendi.", "campaign": campaign}


@router.post("/campaigns/{campaign_id}/update")
def campaign_update(campaign_id: int, req: CampaignIn):
    with get_db(immediate=True) as conn:
        ok, campaign, err = update_campaign_admin(
            conn, campaign_id, req.title, req.description or "", req.image_url or "", req.badge or "KAMPANYA", req.active,
            reward_stamps=req.reward_stamps, action_url=req.action_url, action_label=req.action_label,
        )
        if not ok:
            return {"status": "error", "message": err}
        return {"status": "success", "message": "Kampanya güncellendi.", "campaign": campaign}


@router.post("/campaigns/{campaign_id}/toggle")
def campaign_toggle(campaign_id: int):
    with get_db(immediate=True) as conn:
        ok, campaign, err = toggle_campaign_admin(conn, campaign_id)
        if not ok:
            return {"status": "error", "message": err}
        return {"status": "success", "message": "Kampanya durumu güncellendi.", "campaign": campaign}


@router.delete("/campaigns/{campaign_id}")
@router.post("/campaigns/{campaign_id}/delete")
def campaign_delete(campaign_id: int):
    with get_db(immediate=True) as conn:
        ok, err = delete_campaign_admin(conn, campaign_id)
        if not ok:
            return {"status": "error", "message": err}
        return {"status": "success", "message": "Kampanya silindi."}


def _participants_payload(conn, campaign_id: int) -> Optional[dict[str, Any]]:
    camp = conn.execute(
        "SELECT id, title, active, COALESCE(reward_stamps, 0) AS reward_stamps FROM campaigns WHERE id = ?", (campaign_id,)
    ).fetchone()
    if not camp or camp["reward_stamps"] <= 0:
        return None
    return {
        "campaign": dict(camp),
        "participants": list_participants(conn, campaign_id),
        "stats": campaign_stats(conn, [campaign_id])[campaign_id],
    }


@router.get("/campaigns/{campaign_id}/participants")
def campaign_participants(campaign_id: int):
    """Ödüllü kampanyaya katılanlar (kampanya adıyla): ad, telefon, yorumdaki ad, durum, tarih."""
    with get_db() as conn:
        payload = _participants_payload(conn, campaign_id)
    if payload is None:
        return {"status": "error", "message": "Ödüllü kampanya bulunamadı."}
    return {"status": "success", **payload}


@router.post("/campaigns/{campaign_id}/participants/{customer_id}/review")
def campaign_participant_review(campaign_id: int, customer_id: int, req: ParticipantReviewIn, request: Request):
    """Doğrulama: 'approve' → kampanyanın ödül damgası verilir; 'reject' → damga verilmez (müşteri tekrar gönderebilir)."""
    with get_db(immediate=True) as conn:
        res = review_participant(
            conn, campaign_id, customer_id, req.action, reviewer=_uname(conn, request),
            reason=req.reason or "", audit_actor=_actor(request),
        )
        if res.get("status") == "success":
            mgr = auth.current_manager(request) or {}
            row = conn.execute("SELECT phone_e164 FROM customers WHERE id = ?", (customer_id,)).fetchone()
            log_staff_action(
                conn, (mgr.get("name") or "Müdür"), "campaign_approved" if req.action == "approve" else "campaign_rejected",
                customer_id, mask_phone(row["phone_e164"]) if row else "", f"Kampanya #{campaign_id} (müdür paneli)",
                auth.client_ip(request),
            )
        payload = _participants_payload(conn, campaign_id) or {}
    return {**res, **payload}


# ---------------------------------------------------------------- ürünler
@router.get("/products")
def products_list():
    with get_db() as conn:
        return {"status": "success", "products": list_products_admin(conn)}


@router.post("/products/create")
def product_create(req: ProductIn):
    with get_db(immediate=True) as conn:
        ok, product, err = create_product(
            conn, req.name, req.description or "", req.price, req.category or "Genel",
            req.image_url or "", req.active, req.sort_order, req.is_new, req.discount_price, req.discount_label or "",
        )
        if not ok:
            return {"status": "error", "message": err}
        return {"status": "success", "message": "Ürün eklendi.", "product": product}


@router.post("/products/{product_id}/update")
def product_update(product_id: int, req: ProductIn):
    with get_db(immediate=True) as conn:
        ok, product, err = update_product(
            conn, product_id, req.name, req.description or "", req.price, req.category or "Genel",
            req.image_url or "", req.active, req.sort_order, req.is_new, req.discount_price, req.discount_label or "",
        )
        if not ok:
            return {"status": "error", "message": err}
        return {"status": "success", "message": "Ürün güncellendi.", "product": product}


@router.delete("/products/{product_id}")
@router.post("/products/{product_id}/delete")
def product_delete(product_id: int):
    with get_db(immediate=True) as conn:
        ok, err = delete_product(conn, product_id)
        if not ok:
            return {"status": "error", "message": err}
        return {"status": "success", "message": "Ürün silindi."}


# ---------------------------------------------------------------- yükleme / ayarlar
@router.post("/upload")
async def upload_image(request: Request, kind: str = Form("content"), file: UploadFile = File(...)):
    """Cihazdan görsel yükler (ürün/kampanya: kind=content, kafe logosu: kind=logo)."""
    if not check_rate_limit(f"upload_{auth.client_ip(request)}", max_requests=40, window_seconds=60):
        return {"status": "error", "message": "Çok fazla yükleme isteği. Lütfen biraz bekleyin."}
    data = await file.read(MAX_UPLOAD_BYTES + 1)  # sınırı bir bayt aşan dosya bellekte biriktirilmeden reddedilir
    # Görsel işleme (Pillow) ve veritabanı yazımı iş parçacığı havuzunda: olay döngüsü bloklanmaz (E-01)
    return await run_in_threadpool(_upload_sync, file.filename, data, kind)


def _upload_sync(filename, data: bytes, kind: str):
    try:
        validate_upload_name_and_magic(filename, data)
        if kind == "logo":
            ref = save_logo(data)
            with get_db(immediate=True) as conn:
                update_settings_admin(conn, {})  # günlük kaydı için
                conn.execute(
                    "INSERT INTO settings (key, value) VALUES ('logo_url', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (ref,),
                )
        else:
            ref = save_content_image(data)
    except UploadError as exc:
        return {"status": "error", "message": str(exc)}
    return {"status": "success", "url": ref}


@router.post("/settings")
def update_settings(req: SettingsIn, request: Request):
    payload: dict[str, Any] = {}
    for key, value in req.model_dump().items():
        if value is None:
            continue
        if key == "stamp_target":
            value = max(1, min(30, int(value)))
        elif key == "daily_limit":
            value = max(1, min(50, int(value)))
        elif key == "min_interval_minutes":
            value = max(0, min(1440, int(value)))
        elif key in ("bonus_install_enabled", "bonus_notif_enabled", "gift_requires_visit"):
            value = "1" if value else "0"
        elif key == "kvkk_controller":
            value = str(value).strip()[:400]
        elif key == "kvkk_address":
            value = str(value).strip()[:500]
        elif key == "kvkk_contact":
            value = str(value).strip()[:200]
        elif key in AUTO_NOTIF_DEFAULTS:
            value = " ".join(str(value).split())[:AUTO_NOTIF_MAX["title" if key.endswith("_title") else "body"]]
        payload[key] = value
    with get_db(immediate=True) as conn:
        update_settings_admin(conn, payload, respect_lock=False)
        return {"status": "success", "message": "Ayarlar kaydedildi.", "settings": get_all_settings(conn)}


@router.get("/qr-image")
def qr_image(request: Request, kind: str = "damga", download: int = 0):
    """
    Basılabilir sabit QR görselleri (ADR-35): kind=kayit → /k/m/kayit Kayıt / Uygulama QR'ı (sistemi açar, damga VERMEZ);
    kind=damga → /k/m/qr kasadaki Damga QR'ı / NFC etiketi (ADR-29 tek kullanımlık açılış); kind=install → /k/indir masa
    menü + uygulama. Eski adlar register / stamp geriye uyum için /k/m/qr (damga QR'ı) kodlar.
    """
    base_url, base_path = request_public_base(request)
    with get_db() as conn:
        path, fname = stamp_codes.qr_image_target(conn, kind)
    headers = {"Cache-Control": "no-store"}
    if download:
        headers["Content-Disposition"] = f'attachment; filename="{fname}.png"'
    return Response(content=generate_qr_png_bytes(base_url + base_path + path), media_type="image/png", headers=headers)


@router.post("/stamp-qr/rotate")
def stamp_qr_rotate(request: Request):
    """ADR-41: Damga QR'ına yeni gizli adres. Eski basılı QR / NFC etiketi hemen damga vermez olur (yeniden basılmalı)."""
    base_url, base_path = request_public_base(request)
    with get_db(immediate=True) as conn:
        stamp_codes.rotate_stamp_qr_token(conn)
        url = base_url + base_path + stamp_codes.stamp_qr_path(conn)
        log_audit(conn, _actor(request), "stamp_qr_rotated", "Damga QR adresi yenilendi; eski basılı QR damga vermez.")
    return {"status": "success", "url": url, "message": "Yeni Damga QR'ı hazır. Eski QR artık damga vermez."}


# ---------------------------------------------------------------- damga güvenliği (ADR-30)
def _stamp_security_payload(conn, request: Optional[Request] = None) -> dict[str, Any]:
    base_url, base_path = request_public_base(request)
    return {
        "status": "success",
        "static_qr_stamps": (get_setting(conn, "static_qr_stamps", "1") or "1").strip() != "0",
        "nfc": nfc_sun.admin_payload(conn, base_url=base_url, base_path=base_path),
        "displays": qr_displays.list_displays(conn),
        "display_url": qr_displays.display_url(base_url=base_url, base_path=base_path),
    }


def _pairing_out(info: dict[str, Any]) -> dict[str, Any]:
    png = generate_qr_png_bytes(info["pair_url"])
    return {
        "id": info["id"],
        "label": info["label"],
        "url": info["pair_url"],
        "expires_at": info["pair_expires_at"],
        "qr": "data:image/png;base64," + base64.b64encode(png).decode("ascii"),
    }


@router.get("/stamp-security")
def stamp_security(request: Request):
    """Basılı QR damga anahtarı, NTAG 424 DNA anahtarları / etiketleri / son dokunuş ve Kasa QR Ekranları."""
    with get_db() as conn:
        return _stamp_security_payload(conn, request)


@router.post("/stamp-security")
def stamp_security_update(req: StampSecurityIn, request: Request):
    with get_db(immediate=True) as conn:
        err = nfc_sun.save_keys(conn, req.nfc_meta_key, req.nfc_file_key)
        if err:
            return {"status": "error", "message": err}
        changes = []
        if req.nfc_meta_key is not None or req.nfc_file_key is not None:
            changes.append("NFC 424 DNA anahtarları güncellendi")
        if req.static_qr_stamps is not None:
            set_setting(conn, "static_qr_stamps", "1" if req.static_qr_stamps else "0")
            changes.append("basılı QR damgası " + ("açıldı" if req.static_qr_stamps else "kapatıldı (yalnızca kayıt)"))
        if changes:
            log_audit(conn, _actor(request), "stamp_security_updated", "; ".join(changes) + ".")
        return {**_stamp_security_payload(conn, request), "message": "Kaydedildi."}


@router.post("/nfc-tags/{tag_id}")
def nfc_tag_update(tag_id: int, req: NfcTagIn, request: Request):
    """Etiket adı / etkin-devre dışı. Etiket silinmez (sayaç kaydı korunur)."""
    with get_db(immediate=True) as conn:
        if not nfc_sun.update_tag(conn, tag_id, req.label, req.active):
            return {"status": "error", "message": "Etiket bulunamadı."}
        if req.active is not None:
            log_audit(conn, _actor(request), "nfc_tag_updated",
                      f"NFC etiketi #{tag_id} {'etkinleştirildi' if req.active else 'devre dışı bırakıldı'}.")
        return _stamp_security_payload(conn, request)


@router.post("/qr-displays")
def qr_display_create(req: QrDisplayIn, request: Request):
    """Yeni Kasa QR Ekranı + 15 dk geçerli eşleştirme bağlantısı (ve bağlantının QR'ı)."""
    base_url, base_path = request_public_base(request)
    with get_db(immediate=True) as conn:
        info = qr_displays.create_display(
            conn, req.label or "", created_by=_uname(conn, request), base_url=base_url, base_path=base_path
        )
        log_audit(conn, _actor(request), "qr_display_created", f"Kasa QR Ekranı eklendi: {info['label']}.")
        payload = _stamp_security_payload(conn, request)
    return {**payload, "pairing": _pairing_out(info)}


@router.post("/qr-displays/{display_id}/pair")
def qr_display_pair(display_id: int, request: Request):
    """Ekran için yeni eşleştirme bağlantısı (cihaz değişince); yeni cihaz eşleşince önceki düşer."""
    base_url, base_path = request_public_base(request)
    with get_db(immediate=True) as conn:
        info = qr_displays.new_pairing(conn, display_id, base_url=base_url, base_path=base_path)
        if not info:
            return {"status": "error", "message": "Ekran bulunamadı."}
        payload = _stamp_security_payload(conn, request)
    return {**payload, "pairing": _pairing_out(info)}


@router.post("/qr-displays/{display_id}/revoke")
def qr_display_revoke(display_id: int, request: Request):
    with get_db(immediate=True) as conn:
        if not qr_displays.revoke_display(conn, display_id):
            return {"status": "error", "message": "Ekran bulunamadı."}
        log_audit(conn, _actor(request), "qr_display_revoked", f"Kasa QR Ekranı #{display_id} kaldırıldı.")
        return _stamp_security_payload(conn, request)


# ---------------------------------------------------------------- aylık geçmiş raporu (Excel / PDF)
@router.get("/reports/months")
def report_months():
    with get_db() as conn:
        return {"status": "success", "periods": reports.available_periods(conn)}


_TR_ASCII_MAP = str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU")


def _report_filename_prefix(business_name: str) -> str:
    ascii_name = (business_name or "").translate(_TR_ASCII_MAP)
    slug = re.sub(r"[^A-Za-z0-9]+", "_", ascii_name).strip("_")
    return slug or "Sadakat"


@router.get("/reports/monthly-export")
def report_monthly_export(request: Request, fmt: str = Query("xlsx", alias="format"), month: str = ""):
    """
    Aylık rapor indirir: format=xlsx|pdf, month=YYYY-MM | last30 | all (boşsa bu ay).
    Örnek dosya adı: Kahve_Dukkanim_Rapor_2026_11.xlsx / .pdf
    """
    fmt = (fmt or "xlsx").lower()
    if fmt not in ("xlsx", "pdf"):
        return JSONResponse({"status": "error", "message": "Biçim xlsx veya pdf olmalıdır."}, status_code=400)
    period = reports.parse_period(month)
    if period is None:
        return JSONResponse({"status": "error", "message": "Dönem YYYY-AA, last30 veya all olmalıdır."}, status_code=400)
    if not check_rate_limit(f"report_{auth.client_ip(request)}", max_requests=20, window_seconds=60):
        return JSONResponse({"status": "error", "message": "Çok fazla rapor isteği. Lütfen biraz bekleyin."}, status_code=429)
    with get_db(immediate=True) as conn:
        business = get_all_settings(conn).get("business_name", DEFAULT_BUSINESS_NAME) or DEFAULT_BUSINESS_NAME
        data = reports.collect_report(conn, period)
        log_audit(conn, _actor(request), "report_exported", f"Aylık rapor indirildi ({fmt}, {period.key}).")
    content = reports.build_xlsx(data, business) if fmt == "xlsx" else reports.build_pdf(data, business)
    media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" if fmt == "xlsx" else "application/pdf"
    filename = f"{_report_filename_prefix(business)}_Rapor_{period.file_part}.{fmt}"
    return Response(
        content=content, media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"},
    )


# ---------------------------------------------------------------- bildirim
@router.get("/push/preview")
def push_preview(audience: str = "all"):
    with get_db() as conn:
        return preview_campaign(conn, audience=audience)


@router.post("/push/send")
def push_send(req: PushSend, request: Request):
    """Kampanya bildirimi: kısıtlamasız (sessiz saat/24 saat sınırı yok); yalnızca onaylı abonelere gider."""
    if not check_rate_limit(f"push_send_{auth.client_ip(request)}", max_requests=30, window_seconds=60):
        return {"status": "error", "message": "Çok fazla istek."}
    with get_db(immediate=True) as conn:
        kwargs: dict[str, Any] = {}
        if req.url:
            kwargs["url"] = req.url
        return send_campaign_push(
            conn, title=req.title, body=req.body, audience=req.audience or "all", enforce_limits=False, **kwargs
        )


@router.post("/push/test")
def push_test(req: PushTest, request: Request):
    """Yalnızca verilen (kendi cihazının) endpoint'ine test bildirimi yollar."""
    if not req.endpoint:
        return {"status": "error", "message": "Önce bu cihazı bildirim alıcısı yapın."}
    with get_db(immediate=True) as conn:
        return send_test_push(
            conn, title=req.title or "Test", body=req.body or "Test", endpoint=req.endpoint,
        )


# ---------------------------------------------------------------- loglar & güvenlik
@router.get("/staff")
def staff_list():
    with get_db() as conn:
        return {"status": "success", "staff": list_staff_accounts(conn)}


@router.post("/staff/create")
def staff_create(req: StaffCreate, request: Request):
    with get_db(immediate=True) as conn:
        ok, account, err = create_staff_account(conn, req.name, req.username, req.password, created_by=_actor(request))
        if not ok:
            return {"status": "error", "message": err}
        return {"status": "success", "message": "Personel eklendi.", "staff": account}


@router.post("/staff/{staff_id}/update")
def staff_update(staff_id: int, req: StaffUpdate, request: Request):
    with get_db(immediate=True) as conn:
        ok, account, err = update_staff_account(
            conn, staff_id, req.password, req.active, username=req.username, name=req.name, actor=_actor(request)
        )
        if not ok:
            return {"status": "error", "message": err}
        return {"status": "success", "message": "Personel güncellendi.", "staff": account}


# ---------------------------------------------------------------- müdürün kendi hesabı
@router.get("/account")
def account_info(request: Request):
    uid = (auth.current_manager(request) or {}).get("user_id")
    with get_db() as conn:
        return {"status": "success", "account": get_account(conn, int(uid)) if uid is not None else None}


@router.post("/account")
def account_update(req: AccountIn, request: Request, response: Response):
    """Müdür kendi kullanıcı adını / parolasını / görünen adını değiştirir (mevcut parola zorunlu)."""
    if not check_rate_limit(f"account_update_{auth.client_ip(request)}", max_requests=10, window_seconds=900):
        response.status_code = 429
        return {"status": "error", "message": "Çok fazla deneme. Lütfen daha sonra tekrar deneyin."}
    mgr = auth.current_manager(request) or {}
    with get_db(immediate=True) as conn:
        ok, account, err = update_own_account(
            conn, int(mgr["user_id"]), req.current_password, req.username, req.new_password, req.name
        )
    if not ok:
        return {"status": "error", "message": err}
    # Görünen ad değiştiyse oturum çerezi yenilenir (kullanıcı çıkış yapmak zorunda kalmaz)
    auth.set_session_cookie(response, "manager", account["name"], account["id"], remember=bool(mgr.get("remember")))
    return {"status": "success", "message": "Hesap bilgileri güncellendi.", "account": account}


# ---------------------------------------------------------------- analitik (Genel / Özet)
@router.get("/analytics")
def analytics(days: int = 7):
    with get_db() as conn:
        return {"status": "success", **get_dashboard_analytics(conn, days)}


@router.get("/segment")
def segment(kind: str):
    """Karta / grafik dilimine tıklanınca açılan müşteri listesi (Kullanıcı adı, telefon, damga, son işlem)."""
    with get_db() as conn:
        data = get_segment_customers(conn, kind)
    if data is None:
        return {"status": "error", "message": "Bilinmeyen liste türü."}
    return {"status": "success", **data}


# ---------------------------------------------------------------- işlem kayıtları (ADR-31)
@router.get("/activity")
def activity_list(
    group: str = "all", days: int = 7, customer_id: Optional[int] = None, q: Optional[str] = None,
    before_id: Optional[int] = None, limit: int = 50,
):
    """Müşteri + kasa işlem kayıtları (yeniden eskiye). group: all | ok | denied | flagged | kasa | qr; days 0 = tümü."""
    with get_db() as conn:
        return {"status": "success", **activity.list_events(
            conn, group=group, days=days, customer_id=customer_id, q=q, before_id=before_id, limit=limit,
        )}


@router.get("/activity/summary")
def activity_summary(days: int = 7):
    """Ana ekran analizi: verilen / verilmeyen damgalar, ret nedenleri, olası kötüye kullanım notları."""
    with get_db() as conn:
        return {"status": "success", **activity.summary(conn, days=days)}


@router.post("/activity/seen")
def activity_seen():
    """İşlem Kayıtları sekmesi açıldı: kayıtlar okunmuş sayılır, menüdeki uyarı rozeti söner."""
    with get_db(immediate=True) as conn:
        activity.mark_seen(conn)
        return {"status": "success", "unseen_flagged": 0}


# ---------------------------------------------------------------- kategoriler
@router.get("/categories")
def categories_list():
    with get_db() as conn:
        return {"status": "success", "categories": list_categories(conn)}


@router.post("/categories/create")
def category_create(req: CategoryIn, request: Request):
    with get_db(immediate=True) as conn:
        ok, cat, err = create_category(conn, req.name, actor=_actor(request))
        if not ok:
            return {"status": "error", "message": err}
        return {"status": "success", "message": "Kategori eklendi.", "category": cat, "categories": list_categories(conn)}


@router.post("/categories/reorder")
def category_reorder(req: ReorderIn, request: Request):
    with get_db(immediate=True) as conn:
        cats = reorder_categories(conn, req.ids, actor=_actor(request))
    return {"status": "success", "message": "Sıra kaydedildi.", "categories": cats}


@router.post("/categories/{category_id}/update")
def category_update(category_id: int, req: CategoryIn, request: Request):
    with get_db(immediate=True) as conn:
        ok, cat, err = rename_category(conn, category_id, req.name, actor=_actor(request))
        if not ok:
            return {"status": "error", "message": err}
        return {"status": "success", "message": "Kategori güncellendi.", "category": cat, "categories": list_categories(conn)}


@router.post("/categories/{category_id}/delete")
@router.delete("/categories/{category_id}")
def category_delete(category_id: int, request: Request):
    with get_db(immediate=True) as conn:
        ok, err = delete_category(conn, category_id, actor=_actor(request))
        if not ok:
            return {"status": "error", "message": err}
        return {"status": "success", "message": "Kategori silindi.", "categories": list_categories(conn)}


@router.post("/staff/{staff_id}/delete")
@router.delete("/staff/{staff_id}")
def staff_delete(staff_id: int, request: Request):
    with get_db(immediate=True) as conn:
        ok, err = delete_staff_account(conn, staff_id, actor=_actor(request))
        if not ok:
            return {"status": "error", "message": err}
        return {"status": "success", "message": "Personel silindi. Geçmiş işlem kayıtları denetim izi olarak korunur."}


@router.get("/staff-logs")
def staff_logs(limit: int = 200):
    with get_db() as conn:
        return {"status": "success", "logs": list_staff_logs(conn, min(max(limit, 1), 500))}


@router.get("/security/summary")
def security_summary():
    with get_db() as conn:
        return {"status": "success", "summary": security.security_summary(conn)}


@router.get("/security/incidents")
def incidents():
    with get_db() as conn:
        return {"status": "success", "incidents": security.list_incidents(conn)}


@router.post("/security/incidents")
def incident_create(req: IncidentCreate, request: Request):
    try:
        with get_db(immediate=True) as conn:
            iid = security.create_manual_incident(
                conn, req.summary, req.severity, req.detected_at, req.data_exposed, req.affected_confirmed,
                actor=_actor(request),
            )
            return {"status": "success", "incident": security.get_incident(conn, iid)}
    except ValueError as exc:
        return {"status": "error", "message": str(exc)}


@router.post("/security/incidents/{incident_id}/update")
def incident_update(incident_id: int, req: IncidentUpdate, request: Request):
    try:
        with get_db(immediate=True) as conn:
            inc = security.update_incident(
                conn, incident_id, status=req.status, notes=req.notes, affected_confirmed=req.affected_confirmed,
                data_exposed=req.data_exposed, authority_notified=req.authority_notified,
                subjects_notified=req.subjects_notified, actor=_actor(request),
            )
    except ValueError as exc:
        return {"status": "error", "message": str(exc)}
    if not inc:
        return {"status": "error", "message": "Olay bulunamadı."}
    return {"status": "success", "incident": inc}


@router.get("/security/incidents/{incident_id}/report")
def incident_report(incident_id: int):
    with get_db() as conn:
        name = get_all_settings(conn).get("business_name", "")
        text = security.build_incident_report(conn, incident_id, name)
    if text is None:
        return {"status": "error", "message": "Olay bulunamadı."}
    return {"status": "success", "report": text}


@router.get("/security/audit-logs")
def audit_logs(limit: int = 150, event_type: Optional[str] = None):
    with get_db() as conn:
        return {"status": "success", "logs": security.list_audit_logs(conn, min(max(limit, 1), 500), event_type)}


@router.get("/consents")
def consents(limit: int = 200):
    with get_db() as conn:
        return {"status": "success", "records": security.list_consent_records(conn, min(max(limit, 1), 500))}

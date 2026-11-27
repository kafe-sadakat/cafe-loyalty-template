import logging
from fastapi import APIRouter, Request, Response
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel
from typing import Optional, Dict, Any
from app import auth as panel_auth
from app.config import DEFAULT_BUSINESS_NAME, get_vapid_settings, is_push_enabled
from app.db import get_db
from app.services.loyalty import (
    ConsentRequired,
    CONSENT_REQUIRED_MSG,
    process_claim_submission,
    claim_pwa_install_bonus,
    claim_notif_bonus,
    answer_marketing_reconfirm,
    confirm_browser_notification_permission,
    set_marketing_consent,
    get_customer_from_cookie,
    get_customer_card_data,
    get_home_data,
    update_customer_profile,
    lookup_card_by_phone,
    check_reward_redemption_status,
    confirm_reward_redemption,
    delete_customer_data,
    export_customer_data,
    DEVICE_COOKIE_NAME,
    DEVICE_COOKIE_MAX_AGE
)
from app.services.push import (
    subscribe_push,
    subscription_status,
    unsubscribe_push,
)
from app.services.campaign_rewards import join_campaign, submit_campaign
from app.services import otp as otp_service, security, stamp_codes
from app.services.otp import OtpContext, OtpRequired
from app.utils import check_rate_limit, mask_phone_for_log, hash_token, generate_token, get_istanbul_now_iso

logger = logging.getLogger("sadakat")

router = APIRouter(prefix="/api")


def set_device_cookie(response: Response, token: str) -> None:
    """Cihaz çerezini 1 yıl süreli, SameSite=Lax, path=/ olarak ayarlar veya yeniler."""
    response.set_cookie(
        key=DEVICE_COOKIE_NAME,
        value=token,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
        max_age=DEVICE_COOKIE_MAX_AGE
    )


def resolve_device_token(request: Request, explicit_token: Optional[str] = None) -> Optional[str]:
    """
    Cihaz token'ını sırasıyla:
    1) İstek gövdesindeki explicit_token,
    2) X-Device-Token başlığı (localStorage 'loyalty_device_token'),
    3) 1 yıllık HTTP çerezi ('sadakat_device_token')
    üzerinden okur. Adres (query) parametresi KABUL EDİLMEZ: adresteki kimlik belirteci sunucu/vekil günlüklerine,
    tarayıcı geçmişine ve paylaşılan bağlantılara sızar (uygulama bu yolu hiç kullanmaz).
    """
    if explicit_token and explicit_token.strip():
        return explicit_token.strip()
    header_tok = request.headers.get("X-Device-Token")
    if header_tok and header_tok.strip():
        return header_tok.strip()
    cookie_tok = request.cookies.get(DEVICE_COOKIE_NAME)
    if cookie_tok and cookie_tok.strip():
        return cookie_tok.strip()
    return None


def resolve_customer(conn, request: Request, explicit_token: Optional[str] = None) -> tuple[Optional[dict], Optional[str]]:
    """
    Müşteriyi tüm aday cihaz token'larıyla dener (gövde → X-Device-Token → HttpOnly çerez).
    localStorage'daki eski/bozuk bir token, geçerli çerezi gölgeleyip aboneliği "anonim" kaydettirmesin diye
    ilk eşleşen müşteri döner. Dönüş: (müşteri | None, kullanılan token | ilk aday).
    """
    candidates: list[str] = []
    for tok in (
        explicit_token,
        request.headers.get("X-Device-Token"),
        request.cookies.get(DEVICE_COOKIE_NAME),
    ):
        tok = (tok or "").strip()
        if tok and tok not in candidates:
            candidates.append(tok)
    for tok in candidates:
        customer = get_customer_from_cookie(conn, tok)
        if customer:
            return customer, tok
    return None, (candidates[0] if candidates else None)


class ClaimRequest(BaseModel):
    claim_token: Optional[str] = "qr"
    full_name: Optional[str] = None   # ADR-47: yeni kayıtta ad-soyad
    nonce: Optional[str] = None  # dinamik QR zaman damgası (?n=...)
    phone: Optional[str] = None
    device_token: Optional[str] = None
    kvkk_read: Optional[bool] = None
    explicit_consent: Optional[bool] = None
    marketing_consent: Optional[bool] = None
    # ADR-31: sayfanın açılış şekli (nav: navigate | reload | back_forward, hist: sekme geçmişi uzunluğu, standalone,
    # via: page | scanner). Yalnızca işlem kaydı ve şüphe notu içindir; damga kararını değiştirmez.
    client: Optional[Dict[str, Any]] = None
    # Yeni cihaz SMS doğrulaması (docs/OTP.md): otp_required yanıtından sonra aynı istek bu alanlarla yeniden gönderilir
    otp_challenge_id: Optional[str] = None
    otp_code: Optional[str] = None


def _claim_client(raw: Optional[Dict[str, Any]], request: Request) -> dict[str, Any]:
    """ADR-31: müşteri uygulamasının bildirdiği açılış şekli + User-Agent (türü denetlenmiş, kırpılmış)."""
    raw = raw if isinstance(raw, dict) else {}
    out: dict[str, Any] = {"ua": (request.headers.get("user-agent") or "")[:300]}
    for key in ("nav", "via"):
        if isinstance(raw.get(key), str):
            out[key] = raw[key][:20]
    if isinstance(raw.get("hist"), (int, float)) and not isinstance(raw.get("hist"), bool):
        out["hist"] = int(raw["hist"])
    if isinstance(raw.get("standalone"), bool):
        out["standalone"] = raw["standalone"]
    return out


class CardLookupRequest(BaseModel):
    phone: str
    full_name: Optional[str] = None   # ADR-47: yeni kayıtta ad-soyad
    device_token: Optional[str] = None
    kvkk_read: Optional[bool] = None
    explicit_consent: Optional[bool] = None
    marketing_consent: Optional[bool] = None
    otp_challenge_id: Optional[str] = None   # docs/OTP.md
    otp_code: Optional[str] = None


class OtpResendRequest(BaseModel):
    challenge_id: Optional[str] = None


def otp_context(req: Any, request: Request) -> OtpContext:
    """İstekteki OTP alanları (yalnızca müşteri giriş uçları). OTP_MODE=off iken kapı hiçbir şey yapmaz."""
    return OtpContext.from_request(
        getattr(req, "otp_challenge_id", None), getattr(req, "otp_code", None), panel_auth.client_ip(request)
    )


class MarketingConsentRequest(BaseModel):
    consent: bool
    device_token: Optional[str] = None


def build_consent(req: Any, request: Request) -> dict[str, Any]:
    """İstekteki onay kutularını (KVKK okundu / ayrı açık rıza / ticari ileti) IP ile birlikte toplar."""
    return {
        "kvkk_read": bool(getattr(req, "kvkk_read", None)),
        "explicit_consent": bool(getattr(req, "explicit_consent", None)),
        "marketing": bool(getattr(req, "marketing_consent", None)),
        "ip": panel_auth.client_ip(request),
        "full_name": getattr(req, "full_name", None),   # ADR-47: yeni hesap açılırken zorunlu
    }


class ProfileUpdateRequest(BaseModel):
    display_name: str
    device_token: Optional[str] = None


class RedeemCheckRequest(BaseModel):
    staff_token: str
    device_token: Optional[str] = None


class RedeemConfirmRequest(BaseModel):
    staff_token: str
    reward_id: int
    device_token: Optional[str] = None


class DeleteDataRequest(BaseModel):
    phone: str
    device_token: Optional[str] = None


class MeIdentifyRequest(BaseModel):
    phone: str
    full_name: Optional[str] = None   # ADR-47: yeni kayıtta ad-soyad
    endpoint: Optional[str] = None
    device_token: Optional[str] = None
    kvkk_read: Optional[bool] = None
    explicit_consent: Optional[bool] = None
    marketing_consent: Optional[bool] = None
    otp_challenge_id: Optional[str] = None   # docs/OTP.md
    otp_code: Optional[str] = None


class PushSubscribeRequest(BaseModel):
    endpoint: str
    p256dh: Optional[str] = None
    auth: Optional[str] = None
    keys: Optional[Dict[str, str]] = None
    device_token: Optional[str] = None
    marketing_consent: Optional[bool] = None


class PushUnsubscribeRequest(BaseModel):
    endpoint: Optional[str] = None
    device_token: Optional[str] = None


class CampaignActionRequest(BaseModel):
    proof_name: Optional[str] = None   # yorumda görünen ad (yalnızca "Yaptım" bildiriminde)
    device_token: Optional[str] = None


# ==========================================
# MÜŞTERİ & ANA SAYFA / KART / PROFİL UÇ NOKTALARI
# ==========================================

@router.get("/home-data")
def api_home_data(request: Request, response: Response):
    """
    Ana sayfa ve PWA için tüm canlı verileri döner:
    - Aktif kampanyalar (campaigns tablosu)
    - Müşteri özeti (user1 / gerçek isim, damga sayısı, ödül biletleri)
    - PWA & Web Push durumu
    """
    device_token = resolve_device_token(request)
    with get_db() as conn:
        data = get_home_data(conn, device_token=device_token)
        if data.get("identified") and device_token:
            set_device_cookie(response, device_token)
            data["device_token"] = device_token
        data["push_enabled"] = is_push_enabled()
        return data


@router.post("/auth/login")
def api_auth_login(req: CardLookupRequest, request: Request, response: Response):
    """
    Sadece Telefon Numarası ile Giriş/Kayıt uç noktası.
    Cihazı hem 1 yıllık HTTP Cookie ('sadakat_device_token') hem de
    localStorage ('loyalty_device_token') için kalıcı olarak bağlar.
    """
    res = api_card_lookup(req, request, response)
    # Cookie'ye yazılan token'ı localStorage için JSON yanıtına da ekle
    cookie_header = response.headers.get("set-cookie", "")
    if DEVICE_COOKIE_NAME in cookie_header:
        try:
            tok = cookie_header.split(f"{DEVICE_COOKIE_NAME}=")[1].split(";")[0]
            res["device_token"] = tok
        except Exception:
            pass
    return res


@router.post("/auth/otp/resend")
def api_auth_otp_resend(req: OtpResendRequest, request: Request):
    """
    Yeni cihaz SMS kodunu yeniden gönderir (aynı challenge_id, yeni kod). Yanıt: otp_sent | otp_rate_limited |
    otp_expired | otp_unavailable. Bekleme (OTP_RESEND_SECONDS), numara başına günlük sınır ve IP sınırı sunucudadır.
    """
    client_ip = panel_auth.client_ip(request)
    if not check_rate_limit(f"otp_resend_{client_ip}", max_requests=10, window_seconds=60):
        return {"status": "otp_rate_limited", "retry_after": 60,
                "message": "Çok fazla istek yapıldı. Lütfen biraz bekleyip tekrar dene."}
    with get_db(immediate=True) as conn:
        result = otp_service.resend(conn, req.challenge_id, client_ip)
    return result   # SMS gönderilemediyse COMMIT sonrası yerinde otp_unavailable yapıldı


@router.post("/auth/logout")
def api_auth_logout(response: Response):
    """Cihaz oturum çerezini temizler."""
    response.delete_cookie(key=DEVICE_COOKIE_NAME, path="/")
    return {"status": "success", "message": "Çıkış yapıldı."}


@router.post("/profile/update")
def api_profile_update(req: ProfileUpdateRequest, request: Request, response: Response):
    """
    Kullanıcı Adı / Ad Soyad Güncelleme uç noktası.
    Hem SQLite customers tablosunu hem de PostgreSQL directus_users tablosunu anında günceller.
    """
    device_token = resolve_device_token(request, req.device_token)
    if not device_token:
        return {"status": "error", "message": "Oturum bulunamadı. Lütfen telefon numaranızla giriş yapınız."}

    with get_db(immediate=True) as conn:
        customer = get_customer_from_cookie(conn, device_token)
        if not customer:
            return {"status": "error", "message": "Müşteri kaydı bulunamadı. Lütfen tekrar giriş yapınız."}

        ok, new_name, err_msg = update_customer_profile(conn, customer["id"], req.display_name)
        if not ok:
            return {"status": "error", "message": err_msg}

        set_device_cookie(response, device_token)
        card = get_customer_card_data(conn, customer["id"])
        return {
            "status": "success",
            "message": "Kullanıcı adınız başarıyla güncellendi.",
            "display_name": new_name,
            "customer": card,
        }


@router.post("/claim")
def api_claim(req: ClaimRequest, request: Request, response: Response):
    """
    Damga talep işleme uç noktası (ADR-28).
    - Hem 1 yıllık HTTP çerezi hem de localStorage (X-Device-Token) destekler.
    - Damga yalnızca kasa ekranındaki tek kullanımlık Damga QR koduyla (/d/{kod}) ve günlük limit dahilinde verilir;
      aynı kod ikinci kez damga vermez. Sabit kasa QR'ı / NFC (/m/qr) yalnızca kayıt içindir (damga yok).
    - Kayıtlı olmayan tarayıcıya ayrılan kod HttpOnly `sadakat_stamp_hold` çereziyle bu tarayıcıya bağlanır.
    """
    client_ip = panel_auth.client_ip(request)
    if not check_rate_limit(f"claim_api_{client_ip}", max_requests=40, window_seconds=60):
        return {"status": "error", "message": "Çok fazla istek yapıldı. Lütfen biraz bekleyiniz."}

    device_token = resolve_device_token(request, req.device_token)
    hold_secret = request.cookies.get(stamp_codes.HOLD_COOKIE)

    try:
        with get_db(immediate=True) as conn:
            result = process_claim_submission(
                conn=conn,
                claim_token=req.claim_token or "qr",
                device_token=device_token,
                phone_raw=req.phone,
                consent=build_consent(req, request),
                nonce=req.nonce,
                client_ip=client_ip,
                hold_secret=hold_secret,
                client=_claim_client(req.client, request),
                otp=otp_context(req, request),
            )
    except Exception as exc:
        logger.error(
            "Damga/kayıt işlemi sırasında beklenmeyen hata (tel: %s): %s",
            mask_phone_for_log(req.phone),
            str(exc)
        )
        return {
            "status": "error",
            "message": "Kayıt veya damga işlemi sırasında geçici bir hata oluştu. Lütfen tekrar deneyiniz."
        }

    new_hold = result.pop("_hold_secret", None)
    if new_hold:
        # Kod, kayıt formu süresince yalnızca bu tarayıcıya ayrılır (başka telefon aynı kodu kullanamaz)
        response.set_cookie(
            key=stamp_codes.HOLD_COOKIE, value=new_hold, httponly=True, secure=True, samesite="lax",
            path="/", max_age=stamp_codes.HOLD_SECONDS,
        )
    elif (hold_secret and result.get("status") not in ("need_auth", "consent_required", "error")
          and not otp_service.is_otp_status(result.get("status"))):
        # OTP bekleniyorsa ayrılan kod bu tarayıcıda kalır: kod doğrulanınca aynı damga işlenir
        response.delete_cookie(key=stamp_codes.HOLD_COOKIE, path="/")

    active_token = result.get("new_device_token")
    if active_token and result.get("status") not in ("need_auth", "consent_required", "error"):
        set_device_cookie(response, active_token)
        result["device_token"] = active_token

    return result


@router.post("/claim-install-bonus")
async def api_claim_install_bonus(request: Request, response: Response):
    """
    Kullanıcının web uygulamasını indirmesi (PWA Install) karşılığında
    günlük limite takılmadan tek seferlik +1 ücretsiz hediye damga verir.
    """
    req_data = {}
    try:
        req_data = await request.json()
    except Exception:
        pass
    # Veritabanı işi iş parçacığı havuzunda yapılır: olay döngüsü bloklanmaz (E-01)
    return await run_in_threadpool(_claim_install_bonus_sync, request, response, req_data)


def _claim_install_bonus_sync(request: Request, response: Response, req_data: Any):
    raw_device_token = req_data.get("device_token") if isinstance(req_data, dict) else None
    device_token = resolve_device_token(request, raw_device_token)
    if not device_token:
        return {"status": "device_not_found", "message": "Cihaz veya müşteri kaydı bulunamadı."}

    with get_db(immediate=True) as conn:
        customer = get_customer_from_cookie(conn, device_token)
        if not customer:
            return {"status": "device_not_found", "message": "Müşteri kaydı bulunamadı. Lütfen önce telefon numaranızla giriş yapınız."}

        result = claim_pwa_install_bonus(conn, customer["id"])

    if result.get("status") == "success" and device_token:
        set_device_cookie(response, device_token)
        result["device_token"] = device_token

    return result


@router.post("/me/client-state")
async def api_client_state(request: Request):
    """Müşteri uygulamasının canlı kipini (ana ekrana yüklü/standalone mi) kaydeder; kasa ve müdür ekranında 'Cihaz' rozetini besler."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    return await run_in_threadpool(_client_state_sync, request, body)


def _client_state_sync(request: Request, body: Any):
    if not isinstance(body, dict):
        body = {}
    standalone = bool(body.get("standalone"))
    device_token = resolve_device_token(request, body.get("device_token"))
    if not device_token:
        return {"status": "device_not_found"}
    with get_db(immediate=True) as conn:
        customer = get_customer_from_cookie(conn, device_token)
        if not customer:
            return {"status": "device_not_found"}
        conn.execute(
            "UPDATE customers SET pwa_standalone = ?, pwa_seen_at = ? WHERE id = ?",
            (1 if standalone else 0, get_istanbul_now_iso(), customer["id"]),
        )
    return {"status": "success", "standalone": standalone}


@router.post("/claim-notif-bonus")
def api_claim_notif_bonus(request: Request, response: Response):
    """Bildirimleri açan (aktif push aboneliği sunucuda kayıtlı) müşteriye tek seferlik +1 hediye damga."""
    device_token = resolve_device_token(request)
    if not device_token:
        return {"status": "device_not_found", "message": "Cihaz veya müşteri kaydı bulunamadı."}

    with get_db(immediate=True) as conn:
        customer = get_customer_from_cookie(conn, device_token)
        if not customer:
            return {"status": "device_not_found", "message": "Müşteri kaydı bulunamadı. Lütfen önce giriş yapınız."}
        result = claim_notif_bonus(conn, customer["id"])

    if result.get("status") == "success":
        set_device_cookie(response, device_token)
    return result


@router.post("/customer/notification-permission")
async def api_notification_permission(request: Request, response: Response):
    """
    Tarayıcının resmî bildirim izni sonucunu (Notification.permission: granted | denied | default) işler (ADR-23).
    'granted' + hak daha önce kullanılmamışsa ANINDA +1 hediye damga (Web Push aboneliği şart değil, ayrıca kurulur);
    'denied' / 'default' durumunda kesinlikle damga verilmez. ADR-36 (Y-02): bu uç kampanya (ticari ileti) onayını ASLA
    kaydetmez; eski istemcilerin gönderebileceği `consent` alanı yok sayılır. Kampanya onayı ayrı uçlardan gelir.
    """
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    return await run_in_threadpool(_notification_permission_sync, request, response, body)


def _notification_permission_sync(request: Request, response: Response, body: Any):
    if not isinstance(body, dict):
        body = {}
    client_ip = panel_auth.client_ip(request)
    if not check_rate_limit(f"notif_perm_{client_ip}", max_requests=30, window_seconds=60):
        return {"status": "error", "message": "Çok fazla istek yapıldı. Lütfen biraz bekleyin."}

    with get_db(immediate=True) as conn:
        customer, device_token = resolve_customer(conn, request, body.get("device_token"))
        if not customer:
            return {"status": "device_not_found", "message": "Önce telefon numaranla giriş yap."}
        result = confirm_browser_notification_permission(conn, customer["id"], str(body.get("permission") or "default"))

    set_device_cookie(response, device_token)
    return result


@router.post("/me/marketing-consent")
def api_marketing_consent(req: MarketingConsentRequest, request: Request):
    """
    Açık rıza + ticari ileti (kampanya bildirimi) onayını verir/geri alır. ADR-23'ten beri müşteri arayüzü bu ucu
    çağırmaz (onay kayıt ekranındaki zorunlu kutuyla alınır); geriye dönük uyumluluk için duruyor. Hediye damga ayrıdır
    (/api/customer/notification-permission, yalnızca 'granted').
    """
    client_ip = panel_auth.client_ip(request)
    if not check_rate_limit(f"mkt_consent_{client_ip}", max_requests=30, window_seconds=60):
        return {"status": "error", "message": "Çok fazla istek yapıldı. Lütfen biraz bekleyin."}
    with get_db(immediate=True) as conn:
        customer, _ = resolve_customer(conn, request, req.device_token)
        if not customer:
            return {"status": "device_not_found", "message": "Müşteri kaydı bulunamadı."}
        # Aynı durum tekrar gönderilirse onay zamanı/denetim kaydı yeniden yazılmaz (ispat kaydı ilk onayı korur)
        if bool(customer.get("marketing_consent")) != bool(req.consent):
            set_marketing_consent(conn, customer["id"], req.consent, ip=client_ip, channel="site içi bildirim anahtarı")
    return {"status": "success", "marketing_consent": req.consent}


class MarketingReconfirmRequest(BaseModel):
    answer: str   # yes | no | dismiss
    device_token: Optional[str] = None


@router.post("/me/marketing-reconfirm")
def api_marketing_reconfirm(req: MarketingReconfirmRequest, request: Request):
    """
    ADR-36: eski "Bildirimleri Aç" onayı geçersiz sayılan müşterinin ana ekrandaki sakin kartına cevabı (Evet / Hayır / kapat).
    Evet/Hayır kalıcı kanıt sütunlarına kanal 'reconfirm_card' ile yazılır. Yalnızca yeniden onay bekleyen müşteri için işler.
    """
    client_ip = panel_auth.client_ip(request)
    if not check_rate_limit(f"mkt_reconfirm_{client_ip}", max_requests=30, window_seconds=60):
        return {"status": "error", "message": "Çok fazla istek yapıldı. Lütfen biraz bekleyin."}
    with get_db(immediate=True) as conn:
        customer, _ = resolve_customer(conn, request, req.device_token)
        if not customer:
            return {"status": "device_not_found", "message": "Müşteri kaydı bulunamadı."}
        return answer_marketing_reconfirm(conn, customer["id"], req.answer, ip=client_ip)


def _campaign_action(campaign_id: int, req: CampaignActionRequest, request: Request, submit: bool) -> dict:
    client_ip = panel_auth.client_ip(request)
    if not check_rate_limit(f"campaign_{'submit' if submit else 'join'}_{client_ip}", max_requests=20, window_seconds=60):
        return {"status": "error", "message": "Çok fazla istek yapıldı. Lütfen biraz bekleyin."}
    with get_db(immediate=True) as conn:
        customer, _ = resolve_customer(conn, request, req.device_token)
        if not customer:
            return {"status": "device_not_found", "message": "Kampanyaya katılmak için telefon numaranla giriş yap."}
        if submit:
            return submit_campaign(conn, campaign_id, customer["id"], req.proof_name)
        return join_campaign(conn, campaign_id, customer["id"])


@router.post("/campaigns/{campaign_id}/join")
def api_campaign_join(campaign_id: int, req: CampaignActionRequest, request: Request):
    """Ödüllü kampanyaya katıl (kayıt). Damga VERMEZ; damga yalnızca doğrulama (müdür/kasiyer onayı) sonrası."""
    return _campaign_action(campaign_id, req, request, submit=False)


@router.post("/campaigns/{campaign_id}/submit")
def api_campaign_submit(campaign_id: int, req: CampaignActionRequest, request: Request):
    """Görevi yaptım (ör. Google yorumu) + yorumda görünen ad → 'Doğrulama bekliyor'. Damga onayla gelir."""
    return _campaign_action(campaign_id, req, request, submit=True)


@router.get("/my-card")
def api_my_card(request: Request, response: Response):
    """
    Müşterinin cihaz çerezine veya localStorage token'ına göre SQLite'tan gerçek kart ve bilet durumunu döner.
    """
    device_token = resolve_device_token(request)
    if not device_token:
        return {"status": "device_not_found", "message": "Numaranı yaz, seni tanıyalım."}

    with get_db() as conn:
        customer = get_customer_from_cookie(conn, device_token)
        if not customer:
            return {"status": "device_not_found", "message": "Numaranı yaz, seni tanıyalım."}

        set_device_cookie(response, device_token)
        data = get_customer_card_data(conn, customer["id"])
        data["status"] = "success"
        data["device_token"] = device_token
        return data


@router.post("/card-lookup")
def api_card_lookup(req: CardLookupRequest, request: Request, response: Response):
    """
    Telefon numarasıyla giriş/kayıt ve kalıcı cihaz bağlama.
    Hem 1 yıllık HTTP Cookie atar hem de localStorage için device_token döner.
    """
    client_ip = panel_auth.client_ip(request)
    if not check_rate_limit(f"lookup_api_{client_ip}", max_requests=25, window_seconds=60):
        return {"status": "error", "message": "Lütfen biraz bekleyip tekrar deneyiniz."}

    otp_response: Optional[dict] = None
    try:
        device_token = resolve_device_token(request, req.device_token)
        with get_db(immediate=True) as conn:
            try:
                success, card_data, err_msg = lookup_card_by_phone(
                    conn,
                    req.phone,
                    existing_device_token=device_token,
                    consent=build_consent(req, request),
                    otp=otp_context(req, request),
                )
            except OtpRequired as exc:
                # İşlem COMMIT edilir (deneme sayacı / challenge kalıcı); cihaz bağlanmadı, token verilmez
                otp_response = exc.response
            else:
                if not success or not card_data:
                    return {"status": "error", "message": err_msg}
        if otp_response is not None:
            return otp_response   # SMS gönderilemediyse COMMIT sonrası yerinde otp_unavailable yapıldı
    except ConsentRequired:
        return {"status": "consent_required", "message": CONSENT_REQUIRED_MSG}
    except Exception as exc:
        logger.error(
            "Kart sorgulama/cihaz bağlama sırasında beklenmeyen hata (tel: %s): %s",
            mask_phone_for_log(req.phone),
            str(exc)
        )
        return {
            "status": "error",
            "message": "İşlem sırasında geçici bir hata oluştu. Lütfen tekrar deneyiniz."
        }

    new_token = card_data.pop("_new_device_token", None)
    if new_token:
        set_device_cookie(response, new_token)

    return {
        "status": "success",
        **card_data
    }


def _redeem_rate_limited(request: Request) -> Optional[dict]:
    """Personel kartı belirteci deneme-yanılmayla aranmasın: IP başına dakikada 20 istek."""
    client_ip = panel_auth.client_ip(request)
    if not check_rate_limit(f"redeem_{client_ip}", max_requests=20, window_seconds=60):
        return {"status": "error", "message": "Çok fazla istek yapıldı. Lütfen biraz bekleyiniz."}
    return None


@router.post("/redeem-check")
def api_redeem_check(req: RedeemCheckRequest, request: Request):
    """Personel kartı tarandığında müşterinin kullanılabilir biletini kontrol eder."""
    if (limited := _redeem_rate_limited(request)):
        return limited
    device_token = resolve_device_token(request, req.device_token)
    with get_db() as conn:
        return check_reward_redemption_status(conn, staff_token=req.staff_token, device_token=device_token)


@router.post("/redeem-confirm")
def api_redeem_confirm(req: RedeemConfirmRequest, request: Request):
    """Personel yetkisiyle ödül biletini kullanıldı olarak işaretler."""
    if (limited := _redeem_rate_limited(request)):
        return limited
    device_token = resolve_device_token(request, req.device_token)
    with get_db(immediate=True) as conn:
        return confirm_reward_redemption(
            conn,
            staff_token=req.staff_token,
            device_token=device_token,
            reward_id=req.reward_id
        )


@router.post("/delete-my-data")
def api_delete_my_data(req: DeleteDataRequest, request: Request, response: Response):
    """KVKK Unutulma Hakkı: Müşterinin tüm kayıtlarını anonimleştirir/siler ve çerezi temizler."""
    client_ip = panel_auth.client_ip(request)
    if not check_rate_limit(f"delete_api_{client_ip}", max_requests=10, window_seconds=60):
        return {"status": "error", "message": "Çok fazla istek yapıldı. Lütfen biraz bekleyiniz."}

    device_token = resolve_device_token(request, req.device_token)
    with get_db(immediate=True) as conn:
        success, err_msg = delete_customer_data(conn, raw_phone=req.phone, device_token=device_token)
        if not success:
            failed = True
        else:
            failed = False
    if failed:
        security.log_event(
            "customer_delete_denied", "medium", ip=client_ip, path="/api/delete-my-data", method="POST",
            detail="Veri silme isteği doğrulanamadı (cihaz/telefon eşleşmedi).",
        )
        return {"status": "error", "message": err_msg}

    # Toplu silme tespiti: 60 dakikada 5+ silme isteği → olay kaydı
    security.record_bulk_operation(
        "customer_self_delete", actor="customer", ip=client_ip, detail="Müşteri kendi verisini sildi.", threshold=5,
    )
    response.delete_cookie(key=DEVICE_COOKIE_NAME, path="/")
    return {"status": "success", "message": "Hesabınız ve kişisel verileriniz silindi."}


@router.post("/me/export")
def api_me_export(req: DeleteDataRequest, request: Request):
    """KVKK m.11 Verilerimi İndir: müşterinin kendi kayıtları JSON olarak (silme ucuyla aynı doğrulama + hız sınırı)."""
    client_ip = panel_auth.client_ip(request)
    if not check_rate_limit(f"export_api_{client_ip}", max_requests=10, window_seconds=60):
        return {"status": "error", "message": "Çok fazla istek yapıldı. Lütfen biraz bekleyiniz."}
    device_token = resolve_device_token(request, req.device_token)
    with get_db(immediate=True) as conn:
        success, data, err_msg = export_customer_data(conn, raw_phone=req.phone, device_token=device_token)
    if not success:
        security.log_event(
            "customer_export_denied", "medium", ip=client_ip, path="/api/me/export", method="POST",
            detail="Veri kopyası isteği doğrulanamadı (cihaz/telefon eşleşmedi).",
        )
        return {"status": "error", "message": err_msg}
    return {"status": "success", "data": data}


@router.get("/me/card")
def api_me_card(request: Request, response: Response):
    """Müşteri kart özetini döner."""
    device_token = resolve_device_token(request)
    if not device_token:
        return {
            "status": "device_not_found",
            "identified": False,
            "message": "Numaranı yaz, seni tanıyalım.",
        }

    with get_db() as conn:
        customer = get_customer_from_cookie(conn, device_token)
        if not customer:
            return {
                "status": "device_not_found",
                "identified": False,
                "message": "Numaranı yaz, seni tanıyalım.",
            }

        set_device_cookie(response, device_token)
        card = get_customer_card_data(conn, customer["id"])
        usable_count = len(card.get("usable_rewards", []))

        return {
            "status": "success",
            "identified": True,
            "display_name": card.get("display_name", f"user{customer['id']}"),
            "business_name": card.get("business_name", DEFAULT_BUSINESS_NAME),
            "accent_color": card.get("accent_color", "#1E4D3F"),
            "current_stamps": card.get("current_stamps", 0),
            "stamp_target": card.get("stamp_target", 5),
            "usable_rewards_count": usable_count,
            "pending_rewards": usable_count,
            "reward_name": card.get("reward_name", "Bir filtre kahve"),
        }


@router.post("/me/identify")
def api_me_identify(req: MeIdentifyRequest, request: Request, response: Response):
    """Telefon numarası verildiğinde müşteriyi bulur veya oluşturur."""
    client_ip = panel_auth.client_ip(request)
    if not check_rate_limit(f"identify_api_{client_ip}", max_requests=20, window_seconds=60):
        return {"status": "error", "message": "Çok fazla istek yapıldı. Lütfen biraz bekleyiniz."}

    device_token = resolve_device_token(request, req.device_token)

    otp_response: Optional[dict] = None
    try:
        with get_db(immediate=True) as conn:
            try:
                success, card_data, err_msg = lookup_card_by_phone(
                    conn,
                    req.phone,
                    existing_device_token=device_token,
                    push_endpoint=req.endpoint,
                    consent=build_consent(req, request),
                    otp=otp_context(req, request),
                )
            except OtpRequired as exc:
                otp_response = exc.response
            else:
                if not success or not card_data:
                    return {"status": "error", "message": err_msg}
        if otp_response is not None:
            return otp_response
    except ConsentRequired:
        return {"status": "consent_required", "message": CONSENT_REQUIRED_MSG}
    except Exception as exc:
        logger.error(
            "Kimlik doğrulama (/api/me/identify) sırasında hata (tel: %s): %s",
            mask_phone_for_log(req.phone),
            str(exc),
        )
        return {"status": "error", "message": "İşlem sırasında geçici bir hata oluştu."}

    new_token = card_data.pop("_new_device_token", None)
    if new_token:
        set_device_cookie(response, new_token)

    return {
        "status": "success",
        "identified": True,
        **card_data,
    }


# ==========================================
# WEB PUSH BİLDİRİM UÇ NOKTALARI (/api/push/*)
# ==========================================

@router.get("/push/public-key")
def api_push_public_key():
    """Tarayıcı Web Push aboneliği için VAPID açık anahtarını (public key) döner."""
    vapid = get_vapid_settings()
    return {
        "status": "success",
        "enabled": is_push_enabled(),
        "public_key": vapid["public_key"],
    }


@router.post("/push/subscribe")
def api_push_subscribe(req: PushSubscribeRequest, request: Request, response: Response):
    """Tarayıcıdan gelen Web Push aboneliğini kaydeder."""
    client_ip = panel_auth.client_ip(request)
    if not check_rate_limit(f"push_sub_{client_ip}", max_requests=25, window_seconds=60):
        return {"status": "error", "message": "Çok fazla abonelik isteği gönderildi. Lütfen bekleyiniz."}

    p256dh = req.p256dh or (req.keys.get("p256dh") if req.keys else None) or ""
    auth = req.auth or (req.keys.get("auth") if req.keys else None) or ""

    with get_db(immediate=True) as conn:
        customer, device_token = resolve_customer(conn, request, req.device_token)
        new_anon_cookie = None
        if not device_token:
            device_token = generate_token(32)
            new_anon_cookie = device_token
        customer_id = customer["id"] if customer else None
        result = subscribe_push(
            conn=conn,
            endpoint=req.endpoint,
            p256dh=p256dh,
            auth=auth,
            customer_id=customer_id,
            token_hash=hash_token(device_token),
        )   # ADR-36: abonelik kampanya onayı yazmaz (req.marketing_consent eski istemciler için yok sayılır)

    if result.get("status") == "success":
        if new_anon_cookie:
            set_device_cookie(response, new_anon_cookie)
        elif device_token:
            set_device_cookie(response, device_token)

    return result


@router.post("/push/unsubscribe")
def api_push_unsubscribe(req: PushUnsubscribeRequest, request: Request):
    """Web Push aboneliğini pasife alır (active=0)."""
    client_ip = panel_auth.client_ip(request)
    if not check_rate_limit(f"push_unsub_{client_ip}", max_requests=25, window_seconds=60):
        return {"status": "error", "message": "Çok fazla istek yapıldı."}

    with get_db(immediate=True) as conn:
        customer, device_token = resolve_customer(conn, request, req.device_token)
        customer_id = customer["id"] if customer else None
        return unsubscribe_push(
            conn=conn,
            endpoint=req.endpoint,
            customer_id=customer_id,
            token_hash=hash_token(device_token) if device_token else None,
            ip=client_ip,
        )


@router.post("/push/status")
def api_push_status(req: PushUnsubscribeRequest, request: Request):
    """
    Bu cihazın bildirim durumunu SUNUCUDAN döner (uygulamadaki aç/kapa anahtarı buna göre çizilir):
    tarayıcıda abonelik görünse bile sunucu onu pasife almış (ör. VAPID anahtarı değişmiş) olabilir.
    """
    client_ip = panel_auth.client_ip(request)
    if not check_rate_limit(f"push_status_{client_ip}", max_requests=60, window_seconds=60):
        return {"status": "error", "message": "Çok fazla istek yapıldı."}
    with get_db() as conn:
        customer, _ = resolve_customer(conn, request, req.device_token)
        return subscription_status(conn, req.endpoint, customer["id"] if customer else None)

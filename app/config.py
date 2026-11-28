import hmac
import ipaddress
import logging
import os
import re
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

# SQLite veritabanı klasörü ve dosya yolu
DATA_DIR = Path(os.getenv("DATA_DIR", "./data")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "loyalty.db"

# Güvenlik ve oturum gizli anahtarı (varsayılanı yoktur, zorunludur)
def get_secret_key() -> str:
    key = os.getenv("SECRET_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "SECRET_KEY ortam değişkeni tanımlı değil! Uygulama güvenlik nedeniyle başlatılamıyor."
        )
    return key


SECRET_KEY = get_secret_key()
if len(SECRET_KEY) < 32:
    # Oturum çerezi imzası, telefon özeti ve parola izi bu anahtara dayanır; kısa anahtar kaba kuvvetle bulunabilir.
    logging.getLogger("sadakat").warning(
        "SECRET_KEY %d karakter: en az 32 karakterlik rastgele bir anahtar önerilir (openssl rand -hex 32).", len(SECRET_KEY)
    )

# Yönetim paneli parolaları: YALNIZCA sunucudaki .env dosyasından okunur (kodda varsayılan yoktur).
# Tanımlı değilse ilgili panele giriş kapalı kalır.
def get_manager_password() -> str:
    return os.getenv("MANAGER_PASSWORD", "").strip()


def get_staff_password() -> str:
    return os.getenv("STAFF_PASSWORD", "").strip()


# Yüklenen görsellerin (ürün/kampanya/logo) saklandığı klasör. Docker'da bir volume olarak bağlanır.
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", str(BASE_DIR / "app" / "static" / "uploads"))).resolve()

# Varsayılan marka ve sadakat programı ayarları (.env üzerinden özelleştirilebilir; yönetim panelinden değiştirilir)
DEFAULT_BUSINESS_NAME = os.getenv("BUSINESS_NAME", "Kahve Dükkanım").strip() or "Kahve Dükkanım"
DEFAULT_ACCENT_COLOR = os.getenv("ACCENT_COLOR", "#1E4D3F").strip() or "#1E4D3F"
DEFAULT_REWARD_NAME = os.getenv("REWARD_NAME", "1 İkram El Yapımı Kahve").strip() or "1 İkram El Yapımı Kahve"
DEFAULT_STAMP_TARGET = os.getenv("STAMP_TARGET", "5").strip() or "5"

# Ortak adres bilgileri (QR kodlar ve bildirimlerdeki bağlantılar için).
# Her ikisi de ortamdan okunur; alan adı veya alt yol değiştiğinde yalnızca .env güncellenir.
_DEFAULT_PUBLIC_BASE_URL = "http://localhost:8000"
_DEFAULT_PUBLIC_BASE_PATH = ""


def _normalize_base_path(raw: str) -> str:
    """'k', '/k/', '' → '/k' ya da ''. Başında tek bir '/' olur, sonunda olmaz (boş = kök yol)."""
    raw = (raw or "").strip().strip("/")
    return f"/{raw}" if raw else ""


# Tanımsız → varsayılan; tanımlı ama boş (PUBLIC_BASE_PATH=) → kök yol (alt yolsuz yayın).
PUBLIC_BASE_URL = (os.getenv("PUBLIC_BASE_URL", "").strip() or _DEFAULT_PUBLIC_BASE_URL).rstrip("/")
PUBLIC_BASE_PATH = _normalize_base_path(os.getenv("PUBLIC_BASE_PATH", _DEFAULT_PUBLIC_BASE_PATH))

# Web Push VAPID yapılandırması (isteğe bağlı; yoksa uygulama çökmez, push devre dışı kalır)
_DEFAULT_VAPID_SUBJECT = "mailto:admin@example.com"
VAPID_PUBLIC_KEY = os.getenv("VAPID_PUBLIC_KEY", "").strip()
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY", "").strip()
VAPID_SUBJECT = os.getenv("VAPID_SUBJECT", "").strip() or _DEFAULT_VAPID_SUBJECT


def get_vapid_settings() -> dict[str, str]:
    """Güncel VAPID ayarlarını döner."""
    pub = (os.getenv("VAPID_PUBLIC_KEY") if "VAPID_PUBLIC_KEY" in os.environ else VAPID_PUBLIC_KEY) or ""
    priv = (os.getenv("VAPID_PRIVATE_KEY") if "VAPID_PRIVATE_KEY" in os.environ else VAPID_PRIVATE_KEY) or ""
    subj = (os.getenv("VAPID_SUBJECT") if "VAPID_SUBJECT" in os.environ else VAPID_SUBJECT) or _DEFAULT_VAPID_SUBJECT
    return {
        "public_key": pub.strip(),
        "private_key": priv.strip(),
        "subject": subj.strip() or _DEFAULT_VAPID_SUBJECT,
    }


def get_edge_shared_secret() -> str:
    """Cloudflare Pages vekili ile paylaşılan sır (X-Edge-Auth). Boşsa başlıklara yalnızca bilinen-host uyumluluk kipinde güvenilir."""
    return os.getenv("EDGE_SHARED_SECRET", "").strip()


# --- Kenar (Cloudflare Pages / Worker) başlıklarına güven ----------------------------------------------------------
# X-Edge-Host / X-Edge-Client-IP başlıklarını kenar vekili ekler; ama kaynağa doğrudan ulaşan herkes de yazabilir.
# Güven kuralı (G-01/G-10):
#  1) EDGE_SHARED_SECRET tanımlıysa ve istek doğru X-Edge-Auth taşıyorsa başlıklara güvenilir (en güçlü kanıt).
#  2) Uyumluluk kipi (sır henüz kenara girilmediği geçiş dönemi): sır YOKSA ya da istek hiç X-Edge-Auth taşımıyorsa,
#     yalnızca biçimce geçerli ve izin listesindeki alan adları (EDGE_DEFAULT_HOST / EDGE_ALLOWED_HOSTS) kabul edilir.
#  3) Tek yönlü geçiş: sunucu BİR KEZ doğru X-Edge-Auth gördüğünde `data/.edge_auth_enforced` işaretini yazar; bundan sonra
#     sırsız / yanlış sırlı istekler kenar başlıklarına güvenemez (sahte başlıkla IP/host sahteciliği kapanır).
#  4) QR/manifest adresi için host yalnızca izin listesinden (ya da doğru sırla gelen önizleme alt alanından) alınır.
EDGE_BUILTIN_HOSTS: tuple[str, ...] = ()
_EDGE_HOST_RE = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+")
_EDGE_STRICT_MARKER_NAME = ".edge_auth_enforced"
_edge_strict_cache: Optional[bool] = None


def get_edge_default_host() -> str:
    """Varsayılan Cloudflare Pages alan adı (EDGE_DEFAULT_HOST ortam değişkeni, ör. kafe.pages.dev)."""
    raw = os.getenv("EDGE_DEFAULT_HOST", "").strip().lower()
    host = raw.rsplit(":", 1)[0] if re.fullmatch(r".+:\d{1,5}", raw) else raw
    if host and len(host) <= 253 and _EDGE_HOST_RE.fullmatch(host):
        return host
    return ""


def edge_allowed_hosts() -> frozenset:
    """Kenar vekilinden gelebilecek izinli alan adları: EDGE_DEFAULT_HOST + EDGE_ALLOWED_HOSTS (virgülle ayrılmış)."""
    hosts = set(EDGE_BUILTIN_HOSTS)
    default_host = get_edge_default_host()
    if default_host:
        hosts.add(default_host)
    for item in os.getenv("EDGE_ALLOWED_HOSTS", "").split(","):
        item = item.strip().lower()
        if item and _EDGE_HOST_RE.fullmatch(item):
            hosts.add(item)
    return frozenset(hosts)


def edge_host(request) -> str:
    """X-Edge-Host başlığındaki (biçimce geçerli) alan adı; yoksa/bozuksa ''. (X-Forwarded-Host'a güvenilmez: Caddy onu ezer.)"""
    raw = (request.headers.get("x-edge-host") or "").split(",")[0].strip().lower()
    host = raw.rsplit(":", 1)[0] if re.fullmatch(r".+:\d{1,5}", raw) else raw
    if len(host) > 253 or not _EDGE_HOST_RE.fullmatch(host):
        return ""
    return host


def _edge_strict() -> bool:
    global _edge_strict_cache
    if _edge_strict_cache is None:
        try:
            _edge_strict_cache = (DATA_DIR / _EDGE_STRICT_MARKER_NAME).exists()
        except OSError:
            _edge_strict_cache = False
    return _edge_strict_cache


def _mark_edge_strict() -> None:
    global _edge_strict_cache
    _edge_strict_cache = True
    try:
        (DATA_DIR / _EDGE_STRICT_MARKER_NAME).write_text("1", encoding="utf-8")
    except OSError:  # yazılamazsa bu süreç boyunca bellekte kalır
        logging.getLogger("sadakat").warning("Kenar sırrı işareti yazılamadı (%s).", _EDGE_STRICT_MARKER_NAME)


def edge_auth_valid(request) -> bool:
    """Sır tanımlı VE istek doğru X-Edge-Auth taşıyor mu? İlk doğru sırrı görünce sunucu sıkı kipe geçer (kalıcı işaret)."""
    secret = get_edge_shared_secret()
    auth = (request.headers.get("x-edge-auth") or "").strip()
    if not (secret and auth and hmac.compare_digest(auth.encode("utf-8", "ignore"), secret.encode("utf-8", "ignore"))):
        return False
    if not _edge_strict():
        _mark_edge_strict()
    return True


def edge_trusted(request) -> bool:
    """İstekteki X-Edge-Client-IP / X-Edge-Host başlıklarına güvenilir mi? (kurallar için yukarıdaki yoruma bakın)"""
    if edge_auth_valid(request):
        return True
    if get_edge_shared_secret() and ((request.headers.get("x-edge-auth") or "").strip() or _edge_strict()):
        return False  # yanlış sır ya da sıkı kipte sırsız istek
    return edge_host(request) in edge_allowed_hosts()


def request_public_base(request=None) -> tuple[str, str]:
    """
    İsteğin geldiği alan adına göre (PUBLIC_BASE_URL, PUBLIC_BASE_PATH) çiftini döner.
    Cloudflare Pages (EDGE_DEFAULT_HOST / EDGE_ALLOWED_HOSTS) üzerinden gelen isteklerde uygulama kök yolda ('') çalışır;
    doğrudan sunucu veya test isteklerinde varsayılan (PUBLIC_BASE_URL, PUBLIC_BASE_PATH) döner.
    Host yalnızca açık izin listesinden (ya da doğru sırla gelen önizleme alt alanından) kabul edilir (G-10).
    """
    default_host = get_edge_default_host()
    if request is not None and edge_trusted(request):
        host = edge_host(request)
        if host in edge_allowed_hosts() or (default_host and host.endswith("." + default_host) and edge_auth_valid(request)):
            return f"https://{host}", ""
    return PUBLIC_BASE_URL, PUBLIC_BASE_PATH


def is_push_enabled() -> bool:
    """VAPID anahtarları tanımlıysa True, aksi halde False döner."""
    cfg = get_vapid_settings()
    return bool(cfg["public_key"] and cfg["private_key"])


# --- Tek kullanımlık SMS kodu (OTP) — yeni cihaz doğrulaması (docs/OTP.md) ---------------------------------------------
# OTP_MODE=off (VARSAYILAN): davranış bu özellikten önceki gibidir (geriye dönük uyumluluk: güncellemeyi çeken kurulumda
# müşteri dışarıda kalmaz). OTP_MODE=new_device: KAYITLI bir numara, o müşteriye bağlı OLMAYAN bir cihazdan girilirse
# numaraya SMS kodu gider; kod doğrulanmadan cihaz bağlanmaz. Yeni kayıt ve zaten bağlı cihaz kod istemez.
# Değerler her çağrıda ortamdan okunur (testler ve canlı ayar değişikliği için); açılışta validate_otp_config() denetler.
# off kipinde diğer OTP değişkenleri HİÇ okunmaz (bozuk bir değer de açılışı engellemez).
OTP_MODES = ("off", "new_device")
OTP_PROVIDERS = ("console", "sms_gate")

# (ortam değişkeni, varsayılan, en az, en çok) — sınır dışı değerde uygulama AÇILMAZ (yanlış ayar sessizce güvenliği
# zayıflatmasın: ör. sınırsız deneme hakkı ya da saatlerce geçerli kod).
_OTP_INT_SETTINGS = {
    "ttl_seconds": ("OTP_TTL_SECONDS", 300, 30, 900),
    "max_attempts": ("OTP_MAX_ATTEMPTS", 3, 1, 10),
    "resend_seconds": ("OTP_RESEND_SECONDS", 60, 0, 900),
    "daily_limit_per_phone": ("OTP_DAILY_LIMIT_PER_PHONE", 5, 1, 10),
    "global_hourly_limit": ("OTP_GLOBAL_HOURLY_LIMIT", 30, 1, 1000),
    "global_daily_limit": ("OTP_GLOBAL_DAILY_LIMIT", 200, 1, 10000),
}


def _env_int(name: str, default: int, minimum: int = 1, maximum: Optional[int] = None) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise RuntimeError(f"{name} bir tam sayı olmalı (şu an: {raw!r}).")
    if value < minimum:
        raise RuntimeError(f"{name} en az {minimum} olmalı (şu an: {value}).")
    if maximum is not None and value > maximum:
        raise RuntimeError(f"{name} en çok {maximum} olabilir (şu an: {value}).")
    return value


def get_otp_mode() -> str:
    """'off' | 'new_device' (geçersiz değer olduğu gibi döner; validate_otp_config açılışta reddeder)."""
    return (os.getenv("OTP_MODE", "").strip().lower() or "off")


def get_otp_settings() -> dict:
    """OTP ayarları (süreler saniye). Sağlayıcı kimlik bilgileri burada değil, sağlayıcının kendisinde okunur."""
    cfg: dict = {"mode": get_otp_mode(), "provider": os.getenv("OTP_PROVIDER", "").strip().lower()}
    for key, (name, default, low, high) in _OTP_INT_SETTINGS.items():
        cfg[key] = _env_int(name, default, minimum=low, maximum=high)
    return cfg


def get_sms_gate_settings() -> dict:
    """SMS Gateway for Android (SMSGate) bağlantısı. Varsayılan adres YOKTUR (kafenin kendi telefonu / sunucusu)."""
    return {
        "url": os.getenv("SMS_GATE_URL", "").strip(),
        "username": os.getenv("SMS_GATE_USERNAME", "").strip(),
        "password": os.getenv("SMS_GATE_PASSWORD", "").strip(),
    }


# Şifresiz (http://) SMSGate adresine yalnızca bu ağlarda izin verilir: kod içeren SMS metni ve SMSGate parolası
# internete açık metin olarak çıkmasın. RFC1918, loopback, Tailscale/CGNAT (100.64/10) ve IPv6 yerel ağları.
_PRIVATE_SMS_NETWORKS = tuple(ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "100.64.0.0/10", "::1/128", "fc00::/7",
))


def is_private_sms_host(host: Optional[str]) -> bool:
    """SMSGate adresinin host'u yerel / özel ağda mı? (localhost, *.local, RFC1918, 127/8, ::1, 100.64/10, fc00::/7)"""
    h = (host or "").strip().lower().rstrip(".")
    if not h:
        return False
    if h == "localhost" or h.endswith(".localhost") or h.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(h.strip("[]"))
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return any(ip in net for net in _PRIVATE_SMS_NETWORKS if net.version == ip.version)


def console_provider_allowed() -> bool:
    return os.getenv("OTP_ALLOW_CONSOLE", "").strip() == "1"


def validate_otp_config() -> None:
    """
    Açılışta OTP yapılandırmasını denetler; hatalıysa uygulama AÇILMAZ (açık Türkçe hata). OTP_MODE=off iken başka hiçbir
    OTP değişkeni okunmaz (davranış tamamen eskisi gibidir). new_device kipinde sağlayıcı eksik/yanlışsa ya da sayısal
    ayarlar sınır dışındaysa müşteriler sessizce kod alamaz / güvenlik zayıflamış hâle gelmesin diye hemen durulur.
    """
    mode = get_otp_mode()
    if mode not in OTP_MODES:
        raise RuntimeError(f"OTP_MODE geçersiz: {mode!r}. Geçerli değerler: off (varsayılan) | new_device.")
    if mode == "off":
        return
    cfg = get_otp_settings()   # sayısal ayarlar bozuk / sınır dışıysa burada RuntimeError
    if cfg["global_hourly_limit"] > cfg["global_daily_limit"]:
        raise RuntimeError(
            f"OTP_GLOBAL_HOURLY_LIMIT ({cfg['global_hourly_limit']}) OTP_GLOBAL_DAILY_LIMIT "
            f"({cfg['global_daily_limit']}) değerinden büyük olamaz."
        )
    provider = cfg["provider"]
    if not provider:
        raise RuntimeError(
            "OTP_MODE=new_device için OTP_PROVIDER tanımlı değil! SMS kodu gönderilemeyeceği için uygulama başlatılmıyor. "
            "OTP_PROVIDER=sms_gate (gerçek SMS) ya da yalnızca geliştirmede OTP_PROVIDER=console yazın."
        )
    if provider not in OTP_PROVIDERS:
        raise RuntimeError(f"OTP_PROVIDER geçersiz: {provider!r}. Geçerli değerler: {', '.join(OTP_PROVIDERS)}.")
    if provider == "console" and not console_provider_allowed():
        raise RuntimeError(
            "OTP_PROVIDER=console SMS GÖNDERMEZ, kodları uygulama günlüğüne yazar (yalnızca geliştirme). Bilerek "
            "kullanıyorsanız OTP_ALLOW_CONSOLE=1 ekleyin; canlıda OTP_PROVIDER=sms_gate kullanın."
        )
    if provider == "sms_gate":
        sg = get_sms_gate_settings()
        missing = [name for name, key in (("SMS_GATE_URL", "url"), ("SMS_GATE_USERNAME", "username"),
                                          ("SMS_GATE_PASSWORD", "password")) if not sg[key]]
        if missing:
            raise RuntimeError(
                "OTP_PROVIDER=sms_gate için eksik ayar: " + ", ".join(missing)
                + ". SMSGate uygulamasındaki adres ve kullanıcı adı/parolayı .env dosyasına yazın (docs/OTP.md)."
            )
        if not re.fullmatch(r"https?://[^\s/]+(/\S*)?", sg["url"]):
            raise RuntimeError(
                f"SMS_GATE_URL geçersiz: {sg['url']!r}. http:// veya https:// ile başlayan tam adres yazın "
                "(ör. http://192.168.1.50:8080 ya da https://api.sms-gate.app)."
            )
        parts = urlsplit(sg["url"])
        try:
            host = parts.hostname
        except ValueError:
            host = None
        if not host:
            raise RuntimeError(f"SMS_GATE_URL geçersiz: {sg['url']!r} (sunucu adı okunamadı).")
        if parts.scheme.lower() != "https" and not is_private_sms_host(host):
            raise RuntimeError(
                f"SMS_GATE_URL şifresiz (http://) ve yerel ağda değil: {host!r}. SMS kodu ve SMSGate parolası internette "
                "açık metin olarak gider. https:// kullanın; http:// yalnızca yerel ağ adreslerinde (192.168.x.x, 10.x.x.x, "
                "172.16-31.x.x, 127.x.x.x, ::1, *.local, Tailscale 100.64.0.0/10) kabul edilir."
            )

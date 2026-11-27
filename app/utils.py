import hmac
import hashlib
import re
import secrets
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Optional, Tuple
from app.config import SECRET_KEY

ISTANBUL_TZ = ZoneInfo("Europe/Istanbul")

# Türkiye Cep Telefonu Regex (+90 5xx xxx xx xx formatında E.164)
PHONE_E164_REGEX = re.compile(r"^\+905\d{9}$")


def normalize_phone(raw_phone: str) -> str:
    """
    Kullanıcı tarafından girilen telefon numarasını standart E.164 (+905xxxxxxxxx) formatına dönüştürür.
    Kabul edilen formatlar:
    - 05xx xxx xx xx
    - 5xx xxx xx xx
    - +90 5xx xxx xx xx
    - 905xxxxxxxxx
    - Boşluk, parantez veya tire içeren tüm varyasyonlar
    """
    if not raw_phone:
        raise ValueError("Telefon numarası boş bırakılamaz.")

    # Sadece rakamları ve baştaki '+' işaretini al
    cleaned = re.sub(r"[^\d+]", "", raw_phone.strip())

    # Başlangıç varyasyonlarını temizle
    if cleaned.startswith("+90"):
        digits = cleaned[3:]
    elif cleaned.startswith("90") and len(cleaned) == 12:
        digits = cleaned[2:]
    elif cleaned.startswith("0"):
        digits = cleaned[1:]
    else:
        digits = cleaned

    normalized = f"+90{digits}"

    if not PHONE_E164_REGEX.match(normalized):
        raise ValueError("Lütfen geçerli bir Türkiye cep telefonu numarası giriniz (örn: 5XX XXX XX XX).")

    return normalized


def mask_phone(phone_e164: str) -> str:
    """
    Telefon numarasını güvenli görüntüleme için maskeler.
    Örnek: +905321234567 -> +90 532 *** ** 67
    """
    if not phone_e164 or len(phone_e164) < 13:
        return "***"
    return f"{phone_e164[:6]} *** ** {phone_e164[-2:]}"


def mask_phone_for_log(raw_phone: Optional[str]) -> str:
    """
    Log kayıtları için telefon numarasını (geçersiz/kısa girişler dahil) güvenle maskeler.
    Ham numara asla loga yazılmaz.
    """
    if not raw_phone:
        return "***"
    digits = re.sub(r"\D", "", str(raw_phone))
    if len(digits) <= 4:
        return "***"
    return f"{digits[:2]}***{digits[-2:]}"


def format_phone(phone_e164: str) -> str:
    """
    E.164 numarasını arayüzde gösterilen 5XX XXX XX XX formatına çevirir (başında 0 yok):
    Örnek: +905321234567 -> 532 123 45 67
    """
    if not phone_e164 or len(phone_e164) != 13:
        return phone_e164
    d = phone_e164[3:]  # 10 haneli (5xxxxxxxxx)
    return f"{d[:3]} {d[3:6]} {d[6:8]} {d[8:]}"


def hash_token(token: str) -> str:
    """Token değerinin SHA-256 özetini üretir."""
    if not token:
        return ""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def phone_hash(phone_e164: str) -> str:
    """
    Telefon numarasının anahtarlı (HMAC-SHA256) özeti. Silinen hesap kayıt defterinde numaranın kendisi değil,
    yalnızca bu geri döndürülemez özet saklanır (SECRET_KEY olmadan tahmin edilemez).
    """
    if not phone_e164:
        return ""
    return hmac.new(SECRET_KEY.encode("utf-8"), f"phone:{phone_e164}".encode("utf-8"), hashlib.sha256).hexdigest()


def generate_token(nbytes: int = 24) -> str:
    """Kriptografik olarak güvenli, URL-safe rastgele token üretir (en az 32 karakter)."""
    return secrets.token_urlsafe(nbytes)


# Basit bellek içi hız sınırlayıcı (Rate Limiter)
_rate_limits: dict[str, list[float]] = {}
_LOGGED_PREFIX = "!logged:"        # aşımın günlüğe yazıldığı an (anahtar başına, pencere başına en çok 1 kayıt)
_PRUNE_EVERY_SECONDS = 60
_PRUNE_HORIZON_SECONDS = 12 * 3600  # koddaki en uzun pencere 12 saat (tanınan giriş IP'si); bu kadar süredir dokunulmayan anahtar silinir
_last_prune = 0.0
_rate_lock = threading.Lock()


def _prune_rate_limits(now: float) -> None:
    """Uzun süredir kullanılmayan anahtarları bellekten atar (çok sayıda farklı IP/anahtarla bellek şişmesin). Kilit altında çağrılır."""
    global _last_prune
    if now - _last_prune < _PRUNE_EVERY_SECONDS:
        return
    _last_prune = now
    horizon = now - _PRUNE_HORIZON_SECONDS
    for key in [k for k, ts in list(_rate_limits.items()) if not ts or ts[-1] < horizon]:
        _rate_limits.pop(key, None)


def check_rate_limit(key: str, max_requests: int, window_seconds: int) -> bool:
    """
    Belirli bir anahtar (örn: IP + rota) için hız sınırını denetler.
    Sınır aşılmışsa False döner, aksi halde True döner ve isteği kaydeder.
    Aşım güvenlik günlüğüne anahtar başına pencere başına EN ÇOK BİR KEZ yazılır: aksi halde sel saldırısında
    engellenen her istek veritabanı yazma kilidi alıp tabloyu büyütür (koruma, yükü artırır).
    Rotalar iş parçacığı havuzunda eşzamanlı çalışır: sayaç güncellemesi kilitle korunur; günlük yazımı kilidin DIŞINDA yapılır.
    """
    log_exceeded = False
    with _rate_lock:
        now = time.time()
        _prune_rate_limits(now)
        cutoff = now - window_seconds
        timestamps = [t for t in _rate_limits.get(key, []) if t > cutoff]  # süresi geçmiş kayıtları temizle

        if len(timestamps) >= max_requests:
            _rate_limits[key] = timestamps
            logged = _rate_limits.get(_LOGGED_PREFIX + key)
            if not logged or logged[-1] <= cutoff:
                _rate_limits[_LOGGED_PREFIX + key] = [now]
                log_exceeded = True
            allowed = False
        else:
            timestamps.append(now)
            _rate_limits[key] = timestamps
            allowed = True
    if log_exceeded:
        # Hız sınırı aşımını güvenlik günlüğüne işle (ihlal tespiti için); günlük hatası akışı bozmaz.
        try:
            from app.services.security import record_rate_limit_exceeded
            record_rate_limit_exceeded(key)
        except Exception:
            pass
    return allowed


def rate_limit_blocked(key: str, max_requests: int, window_seconds: int) -> bool:
    """Sayaç `max_requests` değerine ulaştı mı? (yalnızca bakar, sayacı ARTIRMAZ; `rate_limit_record` ile birlikte kullanılır)."""
    with _rate_lock:
        cutoff = time.time() - window_seconds
        return sum(1 for t in _rate_limits.get(key, []) if t > cutoff) >= max_requests


def rate_limit_retry_after(key: str, max_requests: int, window_seconds: int) -> int:
    """Sınır doluysa en eski sayılan olayın pencereden çıkmasına kalan saniye (en az 1); dolu değilse 0."""
    with _rate_lock:
        now = time.time()
        stamps = sorted(t for t in _rate_limits.get(key, []) if t > now - window_seconds)
    if len(stamps) < max_requests:
        return 0
    return max(1, int(stamps[len(stamps) - max_requests] + window_seconds - now + 0.999))


def rate_limit_record(key: str) -> None:
    """Anahtara bir olay ekler (örn. yalnızca BAŞARISIZ giriş denemesi)."""
    with _rate_lock:
        now = time.time()
        _prune_rate_limits(now)
        _rate_limits.setdefault(key, []).append(now)


def rate_limit_clear(key: str) -> None:
    with _rate_lock:
        _rate_limits.pop(key, None)
        _rate_limits.pop(_LOGGED_PREFIX + key, None)


def get_istanbul_now() -> datetime:
    """Türkiye yerel saatini (Europe/Istanbul) döner."""
    return datetime.now(ISTANBUL_TZ)


def get_istanbul_now_iso() -> str:
    """Türkiye yerel saatini ISO 8601 string formatında döner."""
    return get_istanbul_now().isoformat()


def get_today_bounds_istanbul(now: Optional[datetime] = None) -> Tuple[str, str]:
    """
    Europe/Istanbul saat dilimine göre günün başlangıç ve bitiş ISO zamanlarını döner.
    Örnek: ('2026-11-02T00:00:00+03:00', '2026-11-02T23:59:59.999999+03:00')
    """
    if now is None:
        now = get_istanbul_now()
    else:
        now = now.astimezone(ISTANBUL_TZ)
    start_of_day = datetime(now.year, now.month, now.day, 0, 0, 0, tzinfo=ISTANBUL_TZ)
    end_of_day = datetime(now.year, now.month, now.day, 23, 59, 59, 999999, tzinfo=ISTANBUL_TZ)
    return start_of_day.isoformat(), end_of_day.isoformat()


def parse_to_istanbul(ts_str: Optional[str]) -> Optional[datetime]:
    """
    ISO 8601 (veya SQLite UTC) zaman damgasını Europe/Istanbul saat dilimine çevirir.
    """
    if not ts_str:
        return None
    s = str(ts_str).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("UTC"))
        return dt.astimezone(ISTANBUL_TZ)
    except Exception:
        return None

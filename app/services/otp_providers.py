"""
OTP (tek kullanımlık kod) SMS sağlayıcıları.

Arayüz: her sağlayıcı `send(phone_e164, text) -> None` sunar; gönderemezse `OtpProviderError` fırlatır (çağıran yakalar,
müşteriye "SMS gönderilemedi" der). Sağlayıcılar ada göre kayıt defterinde (`PROVIDERS`) tutulur; yenisini (ör. Netgsm,
Telegram Gateway) eklemek için sınıfı yazıp `register_provider("ad", Sınıf)` çağırmak ve config.OTP_PROVIDERS listesine
adı eklemek yeterlidir (docs/OTP.md → "Yeni sağlayıcı eklemek").

Güvenlik: kod içeren SMS metni YALNIZCA `console` sağlayıcısında (geliştirme) günlüğe yazılır. Diğer sağlayıcılar hata
günlüğüne bile metni / kodu yazmaz.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from typing import Callable, Optional
from urllib.parse import urlsplit

import httpx

from app.config import get_sms_gate_settings

logger = logging.getLogger("sadakat")


class OtpProviderError(Exception):
    """SMS gönderilemedi (ağ hatası, zaman aşımı, kimlik doğrulama, sağlayıcı reddi). Mesaj kodu İÇERMEZ."""


class OtpProvider:
    name = "base"

    def send(self, phone_e164: str, text: str) -> None:  # pragma: no cover - arayüz
        raise NotImplementedError


class ConsoleProvider(OtpProvider):
    """Geliştirme sağlayıcısı: SMS göndermez, kodu uygulama günlüğüne yazar. CANLIDA KULLANILMAZ."""
    name = "console"

    def send(self, phone_e164: str, text: str) -> None:
        masked = f"{phone_e164[:6]}***{phone_e164[-2:]}" if phone_e164 else "***"
        logger.warning(
            "!!! YALNIZCA GELİŞTİRME — OTP_PROVIDER=console: SMS GÖNDERİLMEDİ, kod günlüğe yazılıyor. "
            "Canlı ortamda OTP_PROVIDER=sms_gate kullanın !!! [%s] %s", masked, text,
        )


# Bağlantı en çok 3 sn; httpx'in aşama başına zaman aşımına ek olarak tüm istek en çok SMS_TOTAL_TIMEOUT saniye sürer
# (SMS, isteği yapan müşterinin yanıtı beklerken COMMIT sonrası gönderilir: yavaş SMSGate müşteriyi bekletmesin).
SMS_GATE_TIMEOUT = httpx.Timeout(5.0, connect=3.0)
SMS_TOTAL_TIMEOUT = 5.0
_send_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="otp-sms")
_SMS_GATE_CLOUD_HOSTS = ("api.sms-gate.app",)


def sms_gate_endpoint(base_url: str) -> str:
    """
    SMSGate gönderim adresini çözer. Resmî belgeler (https://docs.sms-gate.app/, 2026-11 itibarıyla):
    - Yerel sunucu kipi (telefonun kendisi, aynı ağ):  POST http://<telefon-ip>:8080/message
      (https://docs.sms-gate.app/getting-started/local-server/)
    - Bulut kipi:                                      POST https://api.sms-gate.app/3rdparty/v1/messages
      (https://docs.sms-gate.app/getting-started/public-cloud-server/)
    - Kendi sunucunuz (private):                       POST https://alan-adiniz/api/3rdparty/v1/messages
      (https://docs.sms-gate.app/getting-started/private-server/)
    Belgeler yerel kipte yalnızca tekil `/message`, bulut/özel sunucuda çoğul `/messages` gösteriyor; bu yüzden:
    1) Adres zaten `/message` ya da `/messages` ile bitiyorsa olduğu gibi kullanılır (belirsizlikte elle seçim).
    2) Yol `/3rdparty/v1` ile bitiyorsa (bulut ya da özel sunucu tabanı) sonuna `/messages` eklenir.
    3) Yol boş ve host bulut sunucusuysa (`api.sms-gate.app`) `/3rdparty/v1/messages` eklenir.
    4) Diğer her durum yerel kip sayılır: sonuna `/message` eklenir.
    """
    url = (base_url or "").strip().rstrip("/")
    parts = urlsplit(url)
    path = parts.path.rstrip("/")
    if path.endswith("/message") or path.endswith("/messages"):
        return url
    if path.endswith("/3rdparty/v1"):
        return url + "/messages"
    if not path and (parts.hostname or "").lower() in _SMS_GATE_CLOUD_HOSTS:
        return url + "/3rdparty/v1/messages"
    return url + "/message"


class SmsGateProvider(OtpProvider):
    """
    "SMS Gateway for Android" (SMSGate, açık kaynak: capcom6/android-sms-gateway). Kafenin kendi Android telefonu SMS'i
    kendi hattından gönderir (ücretsiz). HTTP Basic kimlik doğrulaması; gövde:
    {"textMessage": {"text": "..."}, "phoneNumbers": ["+90..."]}. Başarı: herhangi bir 2xx (belgeler yerel kipte
    yanıt kodunu açıkça yazmıyor; sunucu mesajı kuyruğa alınca 202 döner) — 2xx dışı her yanıt hata sayılır.
    """
    name = "sms_gate"

    def __init__(self, url: Optional[str] = None, username: Optional[str] = None, password: Optional[str] = None):
        cfg = get_sms_gate_settings()
        self.url = url if url is not None else cfg["url"]
        self.username = username if username is not None else cfg["username"]
        self.password = password if password is not None else cfg["password"]

    def send(self, phone_e164: str, text: str) -> None:
        if not (self.url and self.username and self.password):
            raise OtpProviderError("SMSGate ayarları eksik (SMS_GATE_URL / SMS_GATE_USERNAME / SMS_GATE_PASSWORD).")
        endpoint = sms_gate_endpoint(self.url)
        body = {"textMessage": {"text": text}, "phoneNumbers": [phone_e164]}
        future = _send_pool.submit(
            httpx.post, endpoint, json=body, auth=(self.username, self.password), timeout=SMS_GATE_TIMEOUT,
        )
        try:
            resp = future.result(timeout=SMS_TOTAL_TIMEOUT)
        except FutureTimeout:
            future.cancel()
            raise OtpProviderError(f"SMSGate {SMS_TOTAL_TIMEOUT:g} sn içinde yanıt vermedi.") from None
        except httpx.HTTPError as exc:
            # Metin/kod günlüğe YAZILMAZ; yalnızca hata türü
            raise OtpProviderError(f"SMSGate'e ulaşılamadı ({type(exc).__name__}).") from None
        if not 200 <= resp.status_code < 300:
            raise OtpProviderError(f"SMSGate isteği reddetti (HTTP {resp.status_code}).")


PROVIDERS: dict[str, Callable[[], OtpProvider]] = {}


def register_provider(name: str, factory: Callable[[], OtpProvider]) -> None:
    PROVIDERS[name] = factory


def get_provider(name: str) -> OtpProvider:
    factory = PROVIDERS.get((name or "").strip().lower())
    if factory is None:
        raise OtpProviderError(f"Bilinmeyen OTP sağlayıcısı: {name!r}.")
    return factory()


register_provider(ConsoleProvider.name, ConsoleProvider)
register_provider(SmsGateProvider.name, SmsGateProvider)

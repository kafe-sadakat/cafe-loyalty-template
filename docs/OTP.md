# Yeni Cihaz SMS Doğrulaması (OTP)

Sadakat kartında kimlik telefon numarasıdır. OTP olmadan, bir müşterinin numarasını bilen herkes o numarayla kendi
telefonundan giriş yapıp karta (damgalara, ödüllere) erişebilir. OTP bu açığı kapatır: **kayıtlı bir numara, o müşteriye
henüz bağlı olmayan bir cihazdan girildiğinde** numaraya 6 haneli bir SMS kodu gider; kod yazılmadan cihaz karta
bağlanmaz.

- **Yeni kayıt** → kod istenmez (numara zaten kimsenin değil).
- **Zaten bağlı cihaz** → kod istenmez (müşterinin her gün kullandığı telefon).
- **Kayıtlı numara + yeni/başka cihaz** → SMS kodu istenir.

Varsayılan kip `OTP_MODE=off`'tur: bu güncellemeyi çeken kurulumlarda davranış değişmez, hiçbir müşteri dışarıda kalmaz.
SMS gönderimi hazır olduğunda `OTP_MODE=new_device` yapılır.

---

## 1. SMS Gateway for Android (SMSGate) kurulumu

[SMS Gateway for Android](https://docs.sms-gate.app/) (SMSGate, açık kaynak:
[capcom6/android-sms-gateway](https://github.com/capcom6/android-sms-gateway)) kafenin kendi Android telefonunu bir SMS
sunucusuna çevirir. SMS'ler telefondaki SIM kartın hattından gider; ek bir SMS firmasına para ödenmez (yalnızca hattın
kendi SMS tarifesi geçerlidir — sınırsız SMS paketli bir hat önerilir).

Hazırlık (her iki kip için):

1. Kafede sürekli prizde duracak bir Android telefona SIM kart takın.
2. SMSGate uygulamasını kurun (GitHub sürümleri ya da uygulama mağazası; bağlantılar belgelerde).
3. Uygulamaya SMS gönderme izni verin; pil optimizasyonundan muaf tutun (aksi halde telefon uyuyunca SMS gitmez).

### 1a. Yerel kip (Local Server) — telefon ve sunucu aynı ağda

1. Uygulamada **Local Server**'ı açın ve **Online** düğmesine basın.
2. Ekranda görünen yerel adresi (ör. `http://192.168.1.50:8080`), **kullanıcı adı** ve **parolayı** not alın.
3. Telefonun yerel IP adresinin değişmemesi için modemden bu telefona sabit IP (DHCP rezervasyonu) verin.
4. `.env`:

   ```env
   OTP_MODE=new_device
   OTP_PROVIDER=sms_gate
   SMS_GATE_URL=http://192.168.1.50:8080
   SMS_GATE_USERNAME=uygulamadaki_kullanici
   SMS_GATE_PASSWORD=uygulamadaki_parola
   ```

   Uygulama `POST http://192.168.1.50:8080/message` adresine istek atar
   ([belge](https://docs.sms-gate.app/getting-started/local-server/)).

Yerel kip yalnızca sunucu telefonla **aynı yerel ağdaysa** çalışır (ör. kafedeki bir mini bilgisayar). Uygulama bulutta /
uzak bir sunucudaysa bulut kipini kullanın. Yerel kipte trafik şifresiz HTTP'dir; yalnızca güvendiğiniz kafe ağında
kullanın (misafir Wi-Fi'ından ayrı bir ağ önerilir).

### 1b. Bulut kipi (Cloud Server) — sunucu internette

1. Uygulamada **Cloud Server**'ı açın ve **Online** düğmesine basın; uygulama kullanıcı adı / parola üretir.
2. `.env`:

   ```env
   OTP_MODE=new_device
   OTP_PROVIDER=sms_gate
   SMS_GATE_URL=https://api.sms-gate.app
   SMS_GATE_USERNAME=uygulamadaki_kullanici
   SMS_GATE_PASSWORD=uygulamadaki_parola
   ```

   Uygulama `POST https://api.sms-gate.app/3rdparty/v1/messages` adresine istek atar
   ([belge](https://docs.sms-gate.app/getting-started/public-cloud-server/)). Bulut kipinde SMS metni (kod dahil)
   SMSGate'in ortak sunucusundan geçer; bunu istemiyorsanız kendi SMSGate sunucunuzu kurun.

### 1c. Kendi SMSGate sunucunuz (Private Server)

`SMS_GATE_URL=https://alan-adiniz/api/3rdparty/v1` yazın; uygulama sonuna `/messages` ekler
([belge](https://docs.sms-gate.app/getting-started/private-server/)).

### Adres çözümleme kuralı

| `SMS_GATE_URL` | İstek atılan adres |
|---|---|
| `/message` ya da `/messages` ile biten tam adres | olduğu gibi |
| yolu `/3rdparty/v1` ile biten adres | `…/3rdparty/v1/messages` |
| `https://api.sms-gate.app` (yolsuz) | `https://api.sms-gate.app/3rdparty/v1/messages` |
| diğer her adres (yerel kip) | `…/message` |

İstek gövdesi: `{"textMessage": {"text": "..."}, "phoneNumbers": ["+905XXXXXXXXX"]}`, HTTP Basic kimlik doğrulaması,
8 sn zaman aşımı. 2xx dışındaki her yanıt "SMS gönderilemedi" sayılır.

### Geliştirme: `console` sağlayıcısı

`OTP_PROVIDER=console` SMS göndermez; kodu uygulama günlüğüne **"YALNIZCA GELİŞTİRME"** uyarısıyla yazar. Canlıda
kullanmayın: günlüğü gören herkes kodu görür.

---

## 2. Ortam değişkenleri

| Değişken | Açıklama | Varsayılan |
|---|---|---|
| `OTP_MODE` | `off` \| `new_device` | `off` |
| `OTP_PROVIDER` | `sms_gate` \| `console` | *(boş)* |
| `SMS_GATE_URL` | SMSGate adresi (yukarıya bakın; varsayılan adres yoktur) | *(boş)* |
| `SMS_GATE_USERNAME`, `SMS_GATE_PASSWORD` | SMSGate kimlik bilgileri | *(boş)* |
| `OTP_TTL_SECONDS` | Kodun geçerlilik süresi | `300` |
| `OTP_MAX_ATTEMPTS` | Kilitlenmeden önceki hatalı deneme hakkı | `5` |
| `OTP_RESEND_SECONDS` | Yeni kod istemeden önce bekleme | `60` |
| `OTP_DAILY_LIMIT_PER_PHONE` | Bir numaraya günde (İstanbul saati) gönderilebilecek en fazla SMS | `5` |

**Açılış denetimi:** `OTP_MODE=new_device` iken `OTP_PROVIDER` boş / tanımsız ya da `sms_gate` ayarları eksik / adres
geçersizse uygulama **açılmaz** ve Türkçe bir hata yazar (müşteriler sessizce kod alamaz hâle gelmesin diye).
`OTP_MODE` geçersizse de açılmaz.

SMS metni: `{İşletme adı} doğrulama kodunuz: 123456. Bu kodu kimseyle paylaşmayın.` (işletme adı yönetim panelindeki
güncel ayardan alınır).

---

## 3. Akış (API sözleşmesi)

Kod isteyebilen dört uç: `POST /api/auth/login`, `POST /api/card-lookup`, `POST /api/me/identify` ve kayıt alanlarıyla
(telefon numarası) gelen `POST /api/claim`. Hepsi isteğe bağlı iki alan alır: `otp_challenge_id` (metin) ve `otp_code`
(metin, rakamlar).

1. İstemci isteği her zamanki gibi gönderir. Kod gerekiyorsa cihaz **bağlanmaz**, damga **işlenmez**, SMS otomatik gider ve
   HTTP 200 ile döner:

   ```json
   {"status": "otp_required", "challenge_id": "…", "masked_phone": "+90 5** *** ** 12",
    "expires_in": 300, "resend_in": 60, "message": "…"}
   ```

   Aynı numara için geçerli bir kod zaten varsa ve yeniden gönderme beklemesi dolmadıysa **yeni SMS gönderilmez**; aynı
   `challenge_id` kalan sürelerle döner.
2. İstemci kodu sorar ve **aynı isteği** `otp_challenge_id` + `otp_code` ile yeniden gönderir.
   - Doğru kod → istek bugünkü gibi işlenir (cihaz bağlanır, belirteç döner, damga işlenir); kod tüketilir.
   - `{"status": "otp_invalid", "attempts_left": N, "message": …}` — hatalı kod.
   - `{"status": "otp_expired", "message": …}` — süresi dolmuş / kullanılmış / bilinmeyen challenge.
   - `{"status": "otp_locked", "retry_after": sn, "message": …}` — çok fazla hatalı deneme.
   - `{"status": "otp_rate_limited", "retry_after": sn, "message": …}` — günlük / IP sınırı.
   - `{"status": "otp_unavailable", "message": "SMS gönderilemedi, lütfen kasiyerden yardım isteyin."}` — SMS gitmedi.
3. `POST /api/auth/otp/resend` `{"challenge_id": "…"}` → `{"status": "otp_sent", "expires_in": …, "resend_in": …}` ya da
   `otp_rate_limited` (bekleme / günlük sınır / kilit) / `otp_expired` / `otp_unavailable`. Yeniden gönderimde **yeni kod**
   üretilir (eski kod geçersiz olur), deneme sayacı sıfırlanmaz.

Kasa QR'ıyla gelen damga isteğinde (`/api/claim`) kod, kayıt formu süresince bu tarayıcıya ayrılmış kalır
(`sadakat_stamp_hold` çerezi); SMS kodu doğrulanınca aynı damga işlenir.

`challenge_id` başka bir numaraya aitse (müşteri numarayı değiştirdiyse) kod alanları yok sayılır ve yeni numaraya kod
istenir. PWA (`app/templates/app_pwa.html`) bu adımları "Telefonunu doğrula" penceresiyle uygular.

Kasiyer / müdür panelinden yapılan kayıtlar ve OTP_MODE=off kurulumlar bu akıştan etkilenmez.

---

## 4. Güvenlik özellikleri

- Kod `secrets` ile üretilir (6 hane). Veritabanında (`otp_challenges`) kodun kendisi değil,
  `HMAC-SHA256(SECRET_KEY, challenge_id + kod)` özeti durur; IP de anahtarlı özetle saklanır.
- `challenge_id` 192 bit rastgele, URL-güvenli bir değerdir.
- Karşılaştırma sabit zamanlıdır (`hmac.compare_digest`); kod tek kullanımlıktır (koşullu UPDATE: eşzamanlı iki istekten
  yalnızca biri geçer).
- `OTP_MAX_ATTEMPTS` hatalı denemeden sonra challenge kilitlenir; süresi dolana kadar o numaraya yeni kod da gitmez.
- Numara başına günlük SMS sınırı ve yeniden gönderme beklemesi sunucuda (veritabanında) uygulanır; ayrıca IP başına
  saatte en fazla 10 SMS (bellek içi hız sınırlayıcı) ve uçların kendi IP sınırları geçerlidir. Bu sınırlarla bir numara
  için günde en fazla `OTP_DAILY_LIMIT_PER_PHONE × OTP_MAX_ATTEMPTS` (varsayılan 25) tahmin yapılabilir (1 000 000 olasılık).
- Kod hiçbir API yanıtında dönmez ve `console` dışındaki sağlayıcılarda günlüğe yazılmaz.
- SMS, veritabanı işlemi kesinleştikten sonra (yazma kilidi tutulmadan) gönderilir; gönderim başarısızsa o gönderim geri
  alınır (müşteri beklemeden tekrar deneyebilir) ve `otp_unavailable` döner.
- Süresi dolan kayıtlar günlük saklama/imha görevinde (bir gün sonra) silinir.

**Bilinçli kabul edilen ödünleşimler:**

- *Hesap numaralandırma:* `otp_required` yanıtı numaranın kayıtlı olduğunu ele verir (yeni numara doğrudan kayıt olur).
  Seçilen politikanın doğal sonucudur; IP sınırları ve maskeli numara etkisini sınırlar.
- *Engelleme (DoS):* Bir saldırgan bir numaraya kod isteyip günlük sınırı doldurabilir ya da hatalı kodla kilitleyebilir;
  bu durumda müşteri o gün yeni cihazdan giremez ama **bağlı cihazı etkilenmez**. Kasiyer yardımcı olabilir.
- SMS, SIM değiştirme (SIM swap) saldırılarına karşı koruma sağlamaz; sadakat kartı için yeterli kabul edilmiştir.

---

## 5. Yeni sağlayıcı eklemek (ör. Netgsm, Telegram Gateway)

1. `app/services/otp_providers.py` içinde `OtpProvider`'dan türeyen bir sınıf yazın:

   ```python
   class NetgsmProvider(OtpProvider):
       name = "netgsm"

       def send(self, phone_e164: str, text: str) -> None:
           try:
               resp = httpx.post(..., timeout=SMS_GATE_TIMEOUT)
           except httpx.HTTPError as exc:
               raise OtpProviderError(f"Netgsm'e ulaşılamadı ({type(exc).__name__}).") from None
           if resp.status_code != 200:
               raise OtpProviderError(f"Netgsm isteği reddetti (HTTP {resp.status_code}).")

   register_provider(NetgsmProvider.name, NetgsmProvider)
   ```

   Hata mesajına ve günlüğe **SMS metnini / kodu yazmayın**.
2. `app/config.py` → `OTP_PROVIDERS` listesine adı ekleyin; gerekli ortam değişkenlerini `validate_otp_config()` içinde
   denetleyin (eksikse açılışta Türkçe hata).
3. `.env.example`, `README.md` ve bu belgeye değişkenleri ekleyin; `tests/test_otp.py`'ye HTTP çağrısı taklit edilmiş
   (ağsız) bir test yazın.

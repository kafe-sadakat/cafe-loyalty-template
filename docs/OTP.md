# Yeni Cihaz SMS Doğrulaması (OTP)

Sadakat kartında kimlik telefon numarasıdır. OTP olmadan, bir müşterinin numarasını bilen herkes o numarayla kendi
telefonundan giriş yapıp karta (damgalara, ödüllere) erişebilir. OTP bu açığı kapatır: **kayıtlı bir numara, o müşteriye
henüz bağlı olmayan bir cihazdan girildiğinde** numaraya 6 haneli bir SMS kodu gider; kod yazılmadan cihaz karta
bağlanmaz.

- **Yeni kayıt** → kod istenmez (numara zaten kimsenin değil).
- **Zaten bağlı cihaz** → kod istenmez (müşterinin her gün kullandığı telefon).
- **Kayıtlı numara + yeni/başka cihaz** → SMS kodu istenir.

Varsayılan kip `OTP_MODE=off`'tur: bu güncellemeyi çeken kurulumlarda davranış değişmez, hiçbir müşteri dışarıda kalmaz.
`off` kipinde diğer OTP değişkenleri **hiç okunmaz** (bozuk / eksik bir değer açılışı da engellemez). SMS gönderimi hazır
olduğunda `OTP_MODE=new_device` yapılır.

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

**Şifresiz adres kuralı:** `SMS_GATE_URL` `https://` olmalıdır. `http://` yalnızca host yerel / özel ağdaysa kabul edilir:
`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16` (RFC1918), `127.0.0.0/8` ve `localhost`, `::1`, IPv6 yerel ağ
`fc00::/7`, `*.local` adları ve Tailscale / CGNAT `100.64.0.0/10` (tailnet). Başka bir `http://` adresinde (ör.
`http://api.sms-gate.app`, genel IP) uygulama **açılmaz**: SMS kodu ve SMSGate parolası internette açık metin giderdi.

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
   ([belge](https://docs.sms-gate.app/getting-started/public-cloud-server/)).

**Üçüncü taraf açıklığı (bulut kipi):** bulut kipinde her SMS isteği SMSGate'in **ortak sunucusundan** (üçüncü taraf)
geçer. O sunucu şunları görür ve saklayabilir: müşterinin **telefon numarası**, SMS **metni** (işletme adı ve **geçerli
doğrulama kodu** dahil), gönderim zamanı ve kafenin SMSGate hesabı. Yani bulut sunucusunu işleten (ya da ele geçiren)
biri, kod geçerliyken (`OTP_TTL_SECONDS`) o numarayla yeni bir cihazı karta bağlayabilir. Ayrıca numara + zaman bilgisi
KVKK açısından bir veri aktarımıdır (aydınlatma metninde SMS hizmet sağlayıcısı olarak belirtin). Bunu istemiyorsanız
yerel kipi (1a) ya da kendi SMSGate sunucunuzu (1c) kullanın.

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

İstek gövdesi: `{"textMessage": {"text": "..."}, "phoneNumbers": ["+905XXXXXXXXX"]}`, HTTP Basic kimlik doğrulaması.
Zaman aşımı: bağlantı **3 sn**, isteğin tamamı en çok **5 sn** (SMS, müşterinin yanıtı beklerken gönderilir; yavaş
SMSGate müşteriyi bekletmesin). Süre dolarsa ya da 2xx dışında bir yanıt gelirse "SMS gönderilemedi" sayılır.

### Geliştirme: `console` sağlayıcısı

`OTP_PROVIDER=console` SMS göndermez; kodu uygulama günlüğüne **"YALNIZCA GELİŞTİRME"** uyarısıyla yazar. Yanlışlıkla
canlıya çıkmasın diye ayrıca `OTP_ALLOW_CONSOLE=1` yazılması **zorunludur**; yoksa uygulama açılmaz. Canlıda
kullanmayın: günlüğü gören herkes kodu görür.

---

## 2. Ortam değişkenleri

| Değişken | Açıklama | Varsayılan | İzin verilen |
|---|---|---|---|
| `OTP_MODE` | `off` \| `new_device` | `off` | — |
| `OTP_PROVIDER` | `sms_gate` \| `console` | *(boş)* | — |
| `OTP_ALLOW_CONSOLE` | `console` sağlayıcısına açık izin (yalnızca geliştirme) | *(boş)* | `1` |
| `SMS_GATE_URL` | SMSGate adresi (yukarıya bakın; varsayılan adres yoktur) | *(boş)* | `https://…`; `http://` yalnızca yerel ağ |
| `SMS_GATE_USERNAME`, `SMS_GATE_PASSWORD` | SMSGate kimlik bilgileri | *(boş)* | — |
| `OTP_TTL_SECONDS` | Kodun geçerlilik süresi (saniye) | `300` | 30 – 900 |
| `OTP_MAX_ATTEMPTS` | Bir bağlantının (IP) bir koda yapabileceği hatalı deneme | `3` | 1 – 10 |
| `OTP_RESEND_SECONDS` | Kodu yeniden göndermeden önce bekleme (saniye) | `60` | 0 – 900 |
| `OTP_DAILY_LIMIT_PER_PHONE` | Bir numaraya günde (İstanbul saati) gönderilebilecek en fazla SMS | `5` | 1 – 10 |
| `OTP_GLOBAL_HOURLY_LIMIT` | İşletme genelinde son bir saatte gönderilebilecek en fazla SMS | `30` | 1 – 1000 |
| `OTP_GLOBAL_DAILY_LIMIT` | İşletme genelinde günde (İstanbul saati) gönderilebilecek en fazla SMS | `200` | 1 – 10000 |

**Açılış denetimi (`OTP_MODE=new_device`):** aşağıdakilerden biri varsa uygulama **açılmaz** ve Türkçe bir hata yazar
(müşteriler sessizce kod alamaz / güvenlik sessizce zayıflamış hâle gelmesin diye):

- `OTP_PROVIDER` boş / tanımsız / bilinmeyen; `console` iken `OTP_ALLOW_CONSOLE=1` yok;
- `sms_gate` ayarları eksik, adres geçersiz ya da yerel ağ dışında `http://`;
- sayısal ayarlardan biri tam sayı değil ya da yukarıdaki aralığın dışında;
- `OTP_GLOBAL_HOURLY_LIMIT` > `OTP_GLOBAL_DAILY_LIMIT`.

`OTP_MODE` geçersizse de açılmaz. `OTP_MODE=off` iken yalnızca `OTP_MODE` okunur.

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

   Aynı numara için süresi dolmamış bir challenge varsa **aynı `challenge_id`** döner: bekleme dolmadıysa SMS gitmez,
   dolduysa **aynı kod** yeniden gönderilir (kalan süreyle; süre uzamaz).
2. İstemci kodu sorar ve **aynı isteği** `otp_challenge_id` + `otp_code` ile yeniden gönderir.
   - Doğru kod → istek bugünkü gibi işlenir (cihaz bağlanır, belirteç döner, damga işlenir); kod tüketilir.
   - `{"status": "otp_invalid", "attempts_left": N, "message": …}` — hatalı kod (`N`: bu bağlantının kalan hakkı, en çok
     challenge'ın kalan toplam hakkı).
   - `{"status": "otp_expired", "message": …}` — süresi dolmuş / kullanılmış / bilinmeyen challenge.
   - `{"status": "otp_locked", "retry_after": sn, "message": …}` — çok fazla hatalı deneme (bu bağlantı için ya da
     toplam tavan dolduysa herkes için); `retry_after` challenge'ın kalan süresidir.
   - `{"status": "otp_rate_limited", "retry_after": sn, "message": …}` — numara başına günlük, IP başına saatlik ya da
     işletme geneli saatlik / günlük SMS sınırı.
   - `{"status": "otp_unavailable", "message": "SMS gönderilemedi, lütfen kasiyerden yardım isteyin."}` — SMS gitmedi.
3. `POST /api/auth/otp/resend` `{"challenge_id": "…"}` → `{"status": "otp_sent", "expires_in": …, "resend_in": …}` ya da
   `otp_rate_limited` (bekleme / sınırlar / kilit) / `otp_expired` / `otp_unavailable`. Yeniden gönderimde **aynı kod**
   gider (kod döndürülmez, `expires_in` uzamaz, deneme sayaçları sıfırlanmaz). Böylece numarayı bilen bir başkasının
   "Tekrar gönder"i müşterinin elindeki kodu geçersiz kılamaz. Süre dolunca istemci isteği baştan (OTP alanları olmadan)
   gönderir; yeni challenge ve yeni kod açılır.

`challenge_id` başka bir numaraya aitse (müşteri numarayı değiştirdiyse) kod alanları yok sayılır ve yeni numaraya kod
istenir. PWA (`app/templates/app_pwa.html`) bu adımları "Telefonunu doğrula" penceresiyle uygular.

Kasiyer / müdür panelinden yapılan kayıtlar ve OTP_MODE=off kurulumlar bu akıştan etkilenmez.

### 3a. Damga isteği (`/api/claim`) ve ayrılmış kod — tarayıcı ve çerezsiz (yerel) uygulama

Kayıtlı olmayan cihaz kasa ekranı kodunu (`/d/<kod>`) ya da NFC dokunuşunu (`/n?p=…&c=…`) açınca kod bu isteğe
**ayrılır** (başka telefon kullanamaz). SMS kodu istenirse (`otp_required`):

- Ayrılma `sadakat_stamp_hold` (HttpOnly) çerezine yazılır — **PWA / tarayıcı** bu çerezle devam eder.
- Ayrılma aynı zamanda **challenge'a bağlanır** ve süresi en az challenge'ın süresi kadar uzatılır. Çerez tutmayan
  **yerel uygulama**, aynı gövdeyi `otp_challenge_id` + `otp_code` ile yeniden gönderdiğinde kodun sahibi sayılır;
  kod doğrulanınca damga ve cihaz belirteci çerezli yoldaki gibi döner:

  ```json
  {"status": "success", "message": "Damganız başarıyla eklendi! ☕", "customer_id": 12, "display_name": "user12",
   "welcome_gift": false, "current_stamps": 3, "stamp_target": 5, "reward_unlocked": false, "reward_name": "…",
   "usable_rewards_count": 0, "new_device_token": "<belirteç>", "device_token": "<belirteç>"}
  ```

  Sahiplik koşulları: OTP açık, istekte `otp_challenge_id` var, ayrılmış kod tam **bu** challenge'a bağlı, challenge
  kullanılmamış ve süresi dolmamış, istekteki `phone` challenge'ın numarası, cihaz tanınan (bağlı) bir müşteriye ait
  değil. Sahiplik damgayı **vermez**: damga yine ancak doğru SMS koduyla verilir (yanlış kod → `otp_invalid`, kilit →
  `otp_locked`) ve her zaman challenge'ın numarasına (kod sahibine) yazılır. Numarayı bilen biri aynı `challenge_id`'yi
  edinebilse de SMS kodu olmadan bir şey alamaz; başka numaranın challenge'ı ya da başka bir koda bağlı challenge ile
  ayrılmış kod alınamaz (`code_held`).
- OTP alanları olmadan yeniden gönderen çerezsiz istemci eskisi gibi `{"status": "code_held", …}` alır.
- Sabit kasa QR'ı (`/m/<gizli kod>`) her istekte yeni açılış ürettiği için ayrılma gerektirmez; iki istemci türünde de
  doğru kodla damga verilir.

**Kod doğrulanmadan önce damga kodu geçersizleşirse** (`code_used` / `code_expired`, ör. ayrılma süresi doldu): damga
kodu durumu **SMS kodundan önce** denetlenir ve OTP alanları hiç işlenmez. Yanıt yalnızca
`{"status": "code_expired", "message": "…"}` (ya da `code_used`) olur; **cihaz belirteci dönmez**, cihaz bağlanmaz ve
challenge **tüketilmez**. İstemci aynı `otp_challenge_id` + `otp_code` ile `POST /api/auth/login` (`{"phone": …}`)
göndererek cihazı bağlayabilir (kod süresi içinde), sonra kasadaki güncel QR'ı yeniden okutur. Gerekçe: reddedilen bir
damga isteği hesap / cihaz durumunu değiştirmez. Ayrık durum: kod doğrulanıp cihaz bağlandıktan sonra aynı damga kodu
eşzamanlı başka bir istekte kullanılmışsa (yarış) `code_used` yanıtı `new_device_token` / `device_token` da taşır.
Günlük limit (`limit_reached`) ve aynı ziyaret beklemesi (`interval_limit`) yanıtları, doğrulamadan sonra
oluştukları için zaten `new_device_token` taşır.

---

## 4. Güvenlik özellikleri

- Kod 6 hanedir; challenge'a özel `secrets` ile üretilen rastgele bir tohumdan ve `SECRET_KEY`'den türetilir (aynı kodun
  yeniden gönderilebilmesi için). Veritabanında (`otp_challenges`) kodun kendisi değil,
  `HMAC-SHA256(SECRET_KEY, "otp-code:" + challenge_id + ":" + kod)` özeti ve tohum durur. Veritabanı tek başına kodu
  vermez; `SECRET_KEY` + veritabanı ele geçerse kod zaten özetten (10⁶ olasılık) bulunabilirdi — güvenlik düzeyi aynıdır.
  `"otp-code:"` öneki alan ayrımıdır (aynı anahtarla üretilen telefon / IP özetleriyle karışmaz).
- `challenge_id` 192 bit rastgele, URL-güvenli bir değerdir.
- Karşılaştırma sabit zamanlıdır (`hmac.compare_digest`); kod tek kullanımlıktır (koşullu UPDATE: eşzamanlı iki istekten
  yalnızca biri geçer).
- **Hedefli kilitlemeye karşı:** hatalı denemeler (challenge, IP) başına sayılır (`otp_attempts`). `OTP_MAX_ATTEMPTS`'i
  aşan IP o challenge için kilitlenir; diğer IP'ler (ör. müşterinin kendisi) kilitlenmez. Challenge başına tüm IP'lerden
  toplam **10** hatalı denemede challenge herkes için kilitlenir (süresi dolana kadar o numaraya yeni kod da gitmez).
  IPv6'da IP anahtarı adresin `/64` ağıdır.
- Yeniden gönderim **aynı kodu** gönderir: başka bir IP'nin yeniden gönderme isteği müşterinin kodunu geçersiz kılamaz.
- **SMS sınırları (veritabanında, `otp_sends`; yeniden başlatmada sıfırlanmaz):** numara başına günlük
  (`OTP_DAILY_LIMIT_PER_PHONE`), IP (IPv6: /64) başına saatte 10, işletme geneli saatlik (`OTP_GLOBAL_HOURLY_LIMIT`) ve
  günlük (`OTP_GLOBAL_DAILY_LIMIT`) — SMS bombalaması ve hat maliyetine karşı. Numara ve IP yalnızca anahtarlı özetle
  saklanır. Gitmeyen SMS sayılmaz. Uçların kendi (bellek içi) istek sınırları da geçerlidir.
- Tahmin bütçesi: bir challenge'a en çok 10 hatalı deneme, bir numaraya günde en çok `OTP_DAILY_LIMIT_PER_PHONE`
  challenge → varsayılanla günde en çok 50 tahmin (1 000 000 olasılık).
- Denetim günlüğü: günlük numara sınırı dolunca numara başına günde **tek** kayıt (`otp_daily_limit`), genel sınır dolunca
  saat / gün başına tek kayıt (`otp_global_limit`); kilit (`otp_locked`), doğrulama (`otp_verified`), sıfırlama
  (`otp_reset`).
- Kod hiçbir API yanıtında dönmez ve `console` dışındaki sağlayıcılarda günlüğe yazılmaz.
- SMS, veritabanı işlemi kesinleştikten sonra (yazma kilidi tutulmadan) gönderilir; gönderim başarısızsa o gönderim geri
  alınır (müşteri beklemeden tekrar deneyebilir) ve `otp_unavailable` döner.
- Süresi dolan challenge'lar ve deneme sayaçları bir gün, gönderim kayıtları iki gün sonra günlük saklama/imha görevinde
  silinir.

### 4a. Kasiyer / müdür yardımı: "Doğrulama kilidini sıfırla"

Müşteri yeni telefonunu bağlayamıyorsa (çok hatalı deneme kilidi ya da günlük SMS sınırı — ör. bir başkası numarasını
denediyse) ve **kasadaysa**:

- **Kasa paneli** (`/personel`): müşteriyi arayın → kartta **🔓 Doğrulama kilidini sıfırla** → onaylayın
  (`POST /api/staff/otp-reset` `{"customer_id": …}`; kasiyer veya müdür oturumu).
- **Müdür paneli** (`/mudur`): hızlı aramada müşteri kartındaki **🔓 Doğrulama kilidini sıfırla**
  (`POST /api/admin/customers/{id}/otp-reset`; müdür oturumu).

İşlem numaranın açık challenge'larındaki tüm hatalı deneme sayaçlarını (tüm IP'ler) ve bugünkü numara başı SMS sayacını
sıfırlar. Kod **değişmez**: müşteri telefonuna gelmiş SMS kodunu yazabilir ya da yeni kod isteyebilir. İşletme geneli SMS
sınırları sıfırlanmaz. Düğme yalnızca `OTP_MODE=new_device` iken görünür. Her sıfırlama denetim günlüğüne
(`otp_reset`, işlemi yapan) ve personel işlem günlüğüne yazılır. Yalnızca müşteri yanınızdayken ve numaranın ona ait
olduğundan emin olduğunuzda yapın.

**Bilinçli kabul edilen ödünleşimler:**

- *Hesap numaralandırma (rıza öncesi):* `otp_required` yanıtı numaranın kayıtlı olduğunu ele verir (yeni numara doğrudan
  kayıt olur ya da `consent_required` alır). Bu yanıt, KVKK aydınlatma teyidi (`kvkk_read`) istenmeden **önce** döner:
  kayıtlı numara için rıza zaten istenmez, bu yüzden "kayıtlı mı?" sorusu rızasız da yanıtlanmış olur. OTP öncesinde de
  aynıydı (kayıtlı numara rızasız giriş yapar, yeni numara `consent_required` alır). Seçilen politikanın doğal
  sonucudur; IP sınırları, SMS sınırları ve maskeli numara (`+90 5** *** ** 12`) etkisini sınırlar.
- *Silinmiş (anonimleştirilmiş) numara:* hesabını silen müşterinin numarası **yeni kayıt** sayılır ve kod **istenmez**.
  Numarayı bilen biri o numarayla yeni (boş) bir hesap açabilir; eski damgalar / ödüller / kişisel veriler silindiği
  için erişilemez, kayıt hediyesi de tekrar verilmez. Numaranın gerçek sahibi daha sonra yeni cihazdan girerse bu yeni
  hesap için SMS kodu ister.
- *Engelleme (DoS):* Bir saldırgan bir numaraya kod isteyip günlük sınırı doldurabilir ya da çok sayıda IP'den challenge'ı
  toplam tavana kadar deneyip kilitleyebilir; bu durumda müşteri o gün yeni cihazdan giremez ama **bağlı cihazı
  etkilenmez**. Kasiyer / müdür "Doğrulama kilidini sıfırla" ile yardımcı olur. Genel SMS sınırı dolarsa tüm yeni cihaz
  girişleri bir süre bekler (maliyet tavanı).
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

   Hata mesajına ve günlüğe **SMS metnini / kodu yazmayın**. Toplam süre sınırı için `SmsGateProvider.send` içindeki
   `_send_pool` / `SMS_TOTAL_TIMEOUT` kalıbını kullanın.
2. `app/config.py` → `OTP_PROVIDERS` listesine adı ekleyin; gerekli ortam değişkenlerini `validate_otp_config()` içinde
   denetleyin (eksikse açılışta Türkçe hata; adres varsa `https://` zorunluluğu).
3. `.env.example`, `README.md` ve bu belgeye değişkenleri ekleyin; `tests/test_otp.py`'ye HTTP çağrısı taklit edilmiş
   (ağsız) bir test yazın.

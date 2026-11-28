# Dijital Sadakat Kartı & Kafe Yönetim Sistemi (Hazır Şablon)

Kafeler ve yiyecek-içecek işletmeleri için **sunucu, alan adı ve markadan tamamen bağımsız** PWA tabanlı dijital sadakat kartı, tek kullanımlık QR / NTAG 424 DNA NFC damga toplama sistemi, müdür & kasiyer panelleri ve KVKK uyumlu raporlama şablonu.

---

## ✨ Öne Çıkan Özellikler

- **Tek Dosyalı Güvenilir Depolama:** SQLite (`WAL` modu + `BEGIN IMMEDIATE`) ile harici veritabanı bağımlılığı olmadan yarış koşulu (race condition) korumalı damga/ödül akışı.
- **Anti-Fraud & Tek Kullanımlık Damga:**
  - `/m/{token}` sabit QR taramalarında 302 yönlendirme olmadan (`200 OK`) tek seferlik açılış token'ı üretilir.
  - Kasa ekranı (`/ekran`), canlı QR ve **NTAG 424 DNA (SUN CMAC)** kriptografik NFC etiket doğrulaması desteklenir.
  - Günlük damga üst sınırı, aynı ziyarette bekleme süresi (`min_interval_minutes`), hesap sil-tekrar kayıt (HMAC kara liste) koruması.
- **Kişiselleştirilebilir Marka & Menü (.env + Panel):**
  - İşletme adı (`BUSINESS_NAME`), tema rengi (`ACCENT_COLOR`), ödül adı (`REWARD_NAME`), damga hedefi (`STAMP_TARGET`) kurulumda `.env` ile, sonrasında Müdür Panelinden canlı değiştirilir.
- **KVKK & Ticari İleti Ayrımı:**
  - Aydınlatma Metni ile Açık Rıza / Kampanya Bildirimi onayı ayrı onay kutularıyla alınır, metin sürümü + zaman damgası saklanır.
- **Web Push & PWA:**
  - Tarayıcıdan ana ekrana eklenebilir (`manifest.webmanifest`, `sw.js`), özelleştirilebilir ikon (`tools/make_icons.py`), VAPID imzalı bildirimler.
- **Raporlama:** Aylık/dönemlik `.xlsx` ve `.pdf` dışa aktarma, kasiyer işlem denetim günlükleri.

---

## 🚀 Hızlı Kurulum (Yerel / Tek Sunucu)

### Seçenek A — Docker Compose (Önerilen)

1. Örnek ortam dosyasını kopyalayın ve güçlü bir `SECRET_KEY` belirleyin:
   ```bash
   cp .env.example .env
   ```
2. İsteğe bağlı olarak Web Push (VAPID) anahtarı üretin:
   ```bash
   python -m app.vapid_gen
   ```
3. Başlatın (`http://127.0.0.1:8000` üzerinden dinler):
   ```bash
   docker compose up -d --build
   ```

### Seçenek B — Doğrudan Python ile Yerel Geliştirme

```bash
pip install -r requirements.txt
# Windows PowerShell için: $env:SECRET_KEY="yerel-gelistirme-icin-en-az-32-karakter-anahtar"
python -m app.seed
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

- **Müşteri Uygulaması (PWA):** `http://localhost:8000/kart/`
- **Misafir Menü Sayfası:** `http://localhost:8000/misafir`
- **Ortak Panel Girişi (Müdür & Kasa):** `http://localhost:8000/giris`

> Not: `MANAGER_PASSWORD` boş bırakılırsa ilk açılışta rastgele bir müdür parolası üretilip uygulama günlüğüne bir kez yazdırılır.

---

## ⚙️ Ortam Değişkenleri (`.env`)

Tüm marka, güvenlik ve uygulama adresi ayarları `.env.example` içindeki değişkenlerle yönetilir:

| Değişken | Açıklama | Örnek / Varsayılan |
|---|---|---|
| `SECRET_KEY` | Oturum ve token imzalama anahtarı (zorunlu) | `python -c "import secrets; print(secrets.token_hex(32))"` |
| `DATA_DIR` | SQLite veritabanı dizini | `/data` (yerelde `./data`) |
| `BUSINESS_NAME` | İşletme adı (sonradan panelden değişebilir) | `Kahve Dükkanım` |
| `ACCENT_COLOR` | Tema ana rengi | `#1E4D3F` |
| `REWARD_NAME` | Tam kart ödülü adı | `1 İkram El Yapımı Kahve` |
| `STAMP_TARGET` | Ücretsiz ödül için damga hedefi | `5` |
| `MANAGER_PASSWORD` | İlk `mudur` hesabı parolası (boşsa ilk açılışta üretilir) | *(boş)* |
| `STAFF_PASSWORD` | İlk `kasa1` kasiyer parolası (isteğe bağlı) | *(boş)* |
| `STAMP_SEAL_TOKEN` | Önceden basılı QR mührü token'ı (isteğe bağlı) | *(boş)* |
| `PUBLIC_BASE_URL` | QR ve bildirim bağlantıları için uygulama adresi | `http://localhost:8000` |
| `PUBLIC_BASE_PATH` | Alt yol öneki (kök yolda boş bırakın) | *(boş)* |
| `APP_PORT` | Docker host port eşlemesi | `8000` |
| `VAPID_PUBLIC_KEY` / `VAPID_PRIVATE_KEY` | Web Push bildirim anahtarları | `python -m app.vapid_gen` |
| `VAPID_SUBJECT` | Web Push iletişim adresi | `mailto:admin@example.com` |
| `OTP_MODE` | Yeni cihaz SMS doğrulaması: `off` (kod istenmez) ya da `new_device` (kayıtlı numara yeni cihazdan girilince SMS kodu). Ayrıntı: [`docs/OTP.md`](docs/OTP.md) | `off` |
| `OTP_PROVIDER` | SMS sağlayıcısı: `sms_gate` (kafenin Android telefonu) ya da yalnızca geliştirmede `console`. `new_device` kipinde boşsa uygulama açılmaz | *(boş)* |
| `OTP_ALLOW_CONSOLE` | `console` sağlayıcısına açık izin (`1`); yoksa `console` ile uygulama açılmaz | *(boş)* |
| `SMS_GATE_URL` | SMSGate adresi: yerel kip `http://<telefon-ip>:8080`, bulut `https://api.sms-gate.app` (varsayılan yok). `https://` zorunlu; `http://` yalnızca yerel ağ / Tailscale adreslerinde | *(boş)* |
| `SMS_GATE_USERNAME` / `SMS_GATE_PASSWORD` | SMSGate uygulamasında görünen kullanıcı adı / parola (HTTP Basic) | *(boş)* |
| `OTP_TTL_SECONDS` | Kodun geçerlilik süresi (saniye, 30 – 900) | `300` |
| `OTP_MAX_ATTEMPTS` | Bir bağlantının (IP) bir koda hatalı deneme hakkı (1 – 10; challenge başına toplam tavan 10) | `3` |
| `OTP_RESEND_SECONDS` | Kodu yeniden göndermeden önce bekleme (saniye, 0 – 900; aynı kod yeniden gider) | `60` |
| `OTP_DAILY_LIMIT_PER_PHONE` | Bir numaraya günde gönderilebilecek en fazla SMS (1 – 10) | `5` |
| `OTP_GLOBAL_HOURLY_LIMIT` | İşletme genelinde saatte en fazla SMS (1 – 1000) | `30` |
| `OTP_GLOBAL_DAILY_LIMIT` | İşletme genelinde günde en fazla SMS (1 – 10000) | `200` |

---

## 🎨 İkon ve CSS Özelleştirme

- Varsayılan PWA ikonları ve panel logosu: `python tools/make_icons.py`
- Tailwind CSS derlemesi: `python tools/build_css.py`

---

## 🧪 Testleri Çalıştırma

```bash
pip install -r requirements-dev.txt
python -m pytest -v
```

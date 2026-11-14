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

## 🌐 Üretim Ortamı Dağıtımı

### 1. VPS + Caddy (Docker Harici Proxy Ağı & Alt Yol Desteği)

Uygulamayı alan adınızın kökünde (`PUBLIC_BASE_PATH=`) veya `/k/*` alt yolunda (`PUBLIC_BASE_PATH=/k`) yayımlayabilirsiniz:

1. `deploy/Caddyfile.example` dosyasını inceleyip Caddy yapılandırmanıza alan adınızı yazın.
2. `.env` içinde `PUBLIC_BASE_URL=https://alanadiniz.com` ve gerekiyorsa `PUBLIC_BASE_PATH=/k` tanımlayın.
3. Harici `proxy` ağıyla başlatın:
   ```bash
   docker compose -f docker-compose.yml -f deploy/docker-compose.caddy.yml up -d --build
   ```
4. İsteğe bağlı SSH + canlı uç doğrulaması için `.env.deploy` dosyasını doldurup `python auto_deploy.py` çalıştırın.

### 2. Cloudflare Pages / Worker Kenar Vekili (Opsiyonel)

Müşteri trafiğini Cloudflare üzerinden kök alan adınızdan (`https://kafeniz.com`) karşılayıp kaynak sunucunuza iletmek için `cloudflare_pages/` klasörünü kullanın:

1. `cloudflare_pages/wrangler.toml.example` (Cloudflare Pages) veya `wrangler.worker.toml.example` (Worker Custom Domain) dosyasını kopyalayın.
2. Cloudflare panosunda aşağıdaki **Environment Variables / Secrets** değerlerini girin:
   - `UPSTREAM_ORIGIN`: Kaynak sunucu kökeni (örn. `https://sunucunuz.duckdns.org`)
   - `UPSTREAM_PREFIX`: Sunucuda Caddy alt yolu varsa `/k`, yoksa boş
   - `PAGES_PROJECT_HOST`: Projenizin `.pages.dev` adresi (örn. `kafem.pages.dev`)
   - `ALLOWED_HOSTS`: Virgülle ayrılmış özel alan adlarınız (örn. `kafem.com,www.kafem.com`)
   - `EDGE_SHARED_SECRET`: Sunucu `.env` dosyasındaki `EDGE_SHARED_SECRET` ile **birebir aynı** değer (Secret)
   - `GOOGLE_REVIEW_URL`: *(İsteğe bağlı)* `/yorum` yönlendirmesi için Google İşletme yorum linkiniz
3. Sunucu `.env` dosyasında eşleşen değerleri ayarlayın:
   ```ini
   EDGE_DEFAULT_HOST=kafem.pages.dev
   EDGE_ALLOWED_HOSTS=kafem.com,www.kafem.com
   EDGE_SHARED_SECRET=<ayni-gizli-anahtar>
   ```

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

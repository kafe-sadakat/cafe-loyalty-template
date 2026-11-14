# Python 3.12-slim (arm64 ve amd64 mimarileriyle tam uyumlu)
FROM python:3.12-slim

# Python tamponlamasını kapat ve bytecode dosyalarını diskte biriktirme
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data

WORKDIR /app

# Bağımlılıkları kopyala ve yükle
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Uygulama kodlarını kopyala
COPY app/ ./app/

# Veri klasörünü oluştur
RUN mkdir -p /data

# 8000 portunu konteyner içi dinleme için belirt
EXPOSE 8000

# NOT (SB-06, bilinçli ertelendi): kap hâlâ kök kullanıcıyla çalışır. Kök olmayan kullanıcıya geçmek için sunucuda
# ./data klasörünün sahibi bir kez o kullanıcıya verilmelidir (chown); bu sunucu tarafı adım yapılmadan USER eklenirse
# veritabanı yazılamaz. Taban imaj özeti (digest) sabitlemesi de ağ erişimiyle bir kez alınmalıdır.
# Uygulamayı ters vekil (Caddy) başlıklarıyla başlat. Caddy eş adresini (X-Forwarded-For) sağlar; --proxy-headers
# bayrakları kaldırılmamalıdır. Gerçek müşteri IP'si Cloudflare vekilinden X-Edge-* başlıklarıyla (EDGE_SHARED_SECRET) gelir.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]

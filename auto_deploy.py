"""
Otonom Sunucu Yükleme, Yedekleme ve Canlı Doğrulama Betiği (auto_deploy.py)
SSH ve sunucu bağlantı ayarları `.env.deploy` veya ortam değişkenleri
(DEPLOY_SSH_KEY, DEPLOY_REMOTE_HOST, DEPLOY_REMOTE_DIR) üzerinden güvenle okunur.
"""
import re
import secrets
import subprocess
import sys
import tarfile
import time
from pathlib import Path
import httpx

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

def _load_deploy_env():
    deploy_env = BASE_DIR / ".env.deploy"
    if deploy_env.exists():
        with open(deploy_env, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())

_load_deploy_env()

SSH_KEY = os.getenv("DEPLOY_SSH_KEY", "")
REMOTE_HOST = os.getenv("DEPLOY_REMOTE_HOST", "")
REMOTE_DIR = os.getenv("DEPLOY_REMOTE_DIR", "/home/ubuntu/cafe-loyalty")
ARCHIVE_NAME = "deploy_bundle.tar.gz"

EXCLUDE_NAMES = {
    ".env",
    ".env.deploy",
    "data",
    ".venv",
    "venv",
    "__pycache__",
    ".git",
    ".pytest_cache",
    ARCHIVE_NAME,
}

# Sunucuya yalnızca çalışma için gerekenler gider (testler ve yerel araçlar gitmez)
INCLUDE_ITEMS = [
    "app",
    "requirements.txt",
    "Dockerfile",
    "docker-compose.yml",
]

# Sunucu anahtarı ilk bağlantıda kaydedilir; sonradan DEĞİŞİRSE bağlantı reddedilir (araya giren sahte sunucuya
# kod paketi ve .env gizli değerleri gönderilmez). Sunucu yeniden kurulduysa: ssh-keygen -R <sunucu>
SSH_HOSTKEY_OPT = "StrictHostKeyChecking=accept-new"


def run_ssh(remote_cmd: str, stdin_text: str | None = None) -> subprocess.CompletedProcess:
    cmd = [
        "ssh",
        "-o", SSH_HOSTKEY_OPT,
        "-i", SSH_KEY,
        REMOTE_HOST,
        remote_cmd,
    ]
    return subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", input=stdin_text
    )


def run_scp(local_path: Path, remote_target: str) -> subprocess.CompletedProcess:
    cmd = [
        "scp",
        "-o", SSH_HOSTKEY_OPT,
        "-i", SSH_KEY,
        str(local_path),
        f"{REMOTE_HOST}:{remote_target}",
    ]
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")


def build_tar_bundle() -> Path:
    bundle_path = BASE_DIR / ARCHIVE_NAME
    if bundle_path.exists():
        bundle_path.unlink()

    def _filter(tarinfo: tarfile.TarInfo):
        parts = Path(tarinfo.name).parts
        for part in parts:
            if part in EXCLUDE_NAMES or part.endswith(".pyc"):
                return None
        return tarinfo

    with tarfile.open(bundle_path, "w:gz") as tar:
        for item_name in INCLUDE_ITEMS:
            item_path = BASE_DIR / item_name
            if item_path.exists():
                tar.add(item_path, arcname=item_name, filter=_filter)

    print(f"[1/5] Paket oluşturuldu: {bundle_path.name} ({bundle_path.stat().st_size} byte)")
    return bundle_path


LOCAL_ENV_PATH = BASE_DIR / ".env"
# Sunucudaki .env dosyasında yoksa eklenecek anahtarlar (değerler asla ekrana yazdırılmaz)
# Not: müdür/personel girişi artık veritabanındaki kullanıcı adı+şifre hesaplarıdır (ADR-13); .env parolası üretilmez.
# EDGE_SHARED_SECRET (G-01): Cloudflare Pages vekili ile sunucu arasindaki paylasilan sir. Yerel .env'de uretilir, sunucu
# .env'ine eklenir. Ayni deger Cloudflare Pages'e (Settings > Variables and Secrets, Production + Preview, tur: Secret)
# SAHIP tarafindan girilmelidir. Sunucu, Cloudflare'den dogru sirri ilk kez gordugunde sirsiz kenar basliklarini reddetmeye
# baslar (gecis donemi boyunca pages.dev kesintisiz calisir).
MANAGED_ENV_KEYS = ["ADMIN_PASSWORD", "EDGE_SHARED_SECRET"]
GENERATED_ENV_KEYS: list[str] = ["EDGE_SHARED_SECRET"]
_GENERATED_BYTES = {"EDGE_SHARED_SECRET": 32}


def _read_local_env() -> dict[str, str]:
    values: dict[str, str] = {}
    if LOCAL_ENV_PATH.exists():
        for line in LOCAL_ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip()
    return values


def ensure_local_secrets() -> dict[str, str]:
    """Yerel .env'de (git'e girmez) panel parolaları yoksa güçlü rastgele değerler üretip ekler."""
    values = _read_local_env()
    added = []
    for key in GENERATED_ENV_KEYS:
        if not values.get(key):
            values[key] = secrets.token_urlsafe(_GENERATED_BYTES.get(key, 18))
            added.append(key)
    if added:
        with open(LOCAL_ENV_PATH, "a", encoding="utf-8") as f:
            f.write("\n# Panel parolaları (auto_deploy.py tarafından üretildi; sunucudaki .env ile aynıdır)\n")
            for key in added:
                f.write(f"{key}={values[key]}\n")
        print(f"      Yerel .env dosyasına eklendi: {', '.join(added)} (değerler gösterilmez)")
    return values


def configure_server_env() -> None:
    """
    Sunucudaki {REMOTE_DIR}/.env dosyasını güvenle yapılandırır:
    - Dosya yoksa oluşturulur; izinleri 600 yapılır.
    - MANAGED_ENV_KEYS (yoksa) yerel .env'den eklenir.
    - Var olan hiçbir değerin üzerine yazılmaz; hiçbir değer ekrana yazdırılmaz.
    """
    local = ensure_local_secrets()
    # Yalnizca DEGERI DOLU anahtarlarin adlari listelenir (degerler okunmaz/yazdirilmaz): bos `ANAHTAR=` satiri "yok" sayilir.
    res = run_ssh(
        f"mkdir -p {REMOTE_DIR}/data/uploads && touch {REMOTE_DIR}/.env && chmod 600 {REMOTE_DIR}/.env && "
        f"awk -F= 'NF>1 && length($0)>length($1)+1 {{print $1}}' {REMOTE_DIR}/.env"
    )
    if res.returncode != 0:
        raise RuntimeError(f"Sunucu .env okunamadı: {res.stderr}")
    present = {line.strip() for line in res.stdout.splitlines() if line.strip() and not line.startswith("#")}
    to_add = [k for k in MANAGED_ENV_KEYS if k not in present and local.get(k)]
    if to_add:
        payload = "".join(f"{k}={local[k]}\n" for k in to_add)
        add = run_ssh(f"cat >> {REMOTE_DIR}/.env", stdin_text="\n" + payload)
        if add.returncode != 0:
            raise RuntimeError(f"Sunucu .env güncellenemedi: {add.stderr}")
    print(f"      Sunucu .env: eklenen anahtarlar = {to_add or 'yok'}; mevcutlar korundu.")
    if "EDGE_SHARED_SECRET" in to_add:
        cf_proj = os.getenv("CF_PROJECT_NAME", "").strip() or "<CLOUDFLARE_PAGES_PROJENIZ>"
        print(f"      >>> SAHIP ISLEMI: Cloudflare Pages > {cf_proj} > Settings > Variables and Secrets (Production ve Preview)")
        print("          EDGE_SHARED_SECRET adiyla, tur 'Secret', deger = yerel .env dosyasindaki EDGE_SHARED_SECRET degeri olarak ekleyin,")
        print("          sonra Pages'i yeniden dagitin. Bu yapilana kadar mevcut izinli host uyumlulugu calisir (sir zorunlu degildir).")
    missing = [k for k in ("SECRET_KEY",) if k not in present]
    if missing:
        raise RuntimeError(f"Sunucu .env içinde zorunlu anahtar eksik: {missing}")
    for k in ("PG_PASSWORD", "VAPID_PRIVATE_KEY"):
        if k not in present:
            print(f"      UYARI: sunucu .env içinde {k} tanımlı değil (ilgili özellik devre dışı kalır).")


def deploy_to_server(bundle_path: Path) -> None:
    print("[1.5/5] Sunucu .env yapılandırılıyor (gizli değerler yazdırılmaz)...")
    configure_server_env()
    print("[2/5] Paket SCP ile sunucuya gönderiliyor...")
    scp_res = run_scp(bundle_path, f"/tmp/{ARCHIVE_NAME}")
    if scp_res.returncode != 0:
        raise RuntimeError(f"SCP Hatası: {scp_res.stderr}")
    print("      SCP tamamlandı.")

    print("[3/5] Sunucuda veri yedeği alınıyor (en yeni 3 tutulur) ve yeni sürüm açılıyor...")
    remote_script = f"""
set -e
TS=$(date +%F_%H%M)
if docker ps --format '{{{{.Names}}}}' | grep -qx sadakat; then
  rm -f {REMOTE_DIR}/data/loyalty-yedek.db
  docker exec sadakat python -c "import sqlite3; s=sqlite3.connect('/data/loyalty.db'); d=sqlite3.connect('/data/loyalty-yedek.db'); s.backup(d); d.close(); s.close()" || {{ echo BACKUP_FAILED; exit 1; }}
  test -s {REMOTE_DIR}/data/loyalty-yedek.db || {{ echo BACKUP_FAILED_EMPTY; exit 1; }}
fi
( umask 077; cp -r {REMOTE_DIR}/data {REMOTE_DIR}_data_yedek_$TS ) || {{ echo BACKUP_FAILED_COPY; exit 1; }}
rm -f {REMOTE_DIR}/data/loyalty-yedek.db
ls -1dt {REMOTE_DIR}_data_yedek_* 2>/dev/null | tail -n +4 | xargs -r rm -rf
rm -rf {REMOTE_DIR}/app {REMOTE_DIR}/tests {REMOTE_DIR}/_archive_backup {REMOTE_DIR}/cleanup_backups.py
tar -xzf /tmp/{ARCHIVE_NAME} -C {REMOTE_DIR}
rm -f /tmp/{ARCHIVE_NAME}
echo "ARCHIVE_EXTRACTED_OK"
"""
    ssh_res = run_ssh(remote_script)
    if ssh_res.returncode != 0:
        if "BACKUP_FAILED" in (ssh_res.stdout or ""):
            raise RuntimeError(
                "Veri yedegi alinamadi: dagitim DURDURULDU, sunucudaki mevcut surum degistirilmedi.\n"
                f"{ssh_res.stdout}\n{ssh_res.stderr}"
            )
        raise RuntimeError(f"Sunucu dosya güncelleme hatası: {ssh_res.stderr}")
    print("      Yedekleme ve dosya güncelleme tamamlandı.")

    print("[4/5] Docker konteyneri yeniden derlenip başlatılıyor (docker compose up -d --build)...")
    compose_cmd = f"cd {REMOTE_DIR} && docker compose up -d --build && sleep 4 && docker compose ps"
    comp_res = run_ssh(compose_cmd)
    if comp_res.returncode != 0:
        raise RuntimeError(f"Docker Compose hatası: {comp_res.stderr}\n{comp_res.stdout}")
    print(comp_res.stdout.strip())

    # Caddy konteyneri varsa sadakat:8000/m/qr doğrudan test edilir
    caddy_wget_res = run_ssh(
        "if docker ps --format '{{.Names}}' | grep -qx caddy-caddy-1; then "
        "docker exec caddy-caddy-1 wget -S -O- http://sadakat:8000/m/qr 2>&1 | head -n 25; "
        "else echo 'caddy-caddy-1 konteyneri yok, atlandi.'; fi"
    )
    print("------ CADDY -> SADAKAT WGET ÇIKTISI ------")
    print(caddy_wget_res.stdout.strip())
    print("------------------------------------------")

    # Log kontrolü
    log_res = run_ssh("docker logs sadakat --tail 50")
    logs_combined = (log_res.stdout or "") + "\n" + (log_res.stderr or "")
    if "Traceback" in logs_combined:
        raise RuntimeError(f"Konteyner loglarında Traceback tespit edildi:\n{logs_combined}")
    print("      Konteyner logları temiz (Traceback yok).")


FOREIGN_HOST_RE = re.compile(
    r"fonts\.googleapis|fonts\.gstatic|cdn\.jsdelivr|unpkg\.com|cdnjs|cdn\.tailwindcss|google-analytics|unsplash",
    re.I,
)


def verify_live_endpoints() -> None:
    print("[5/5] Canlı uç noktalar doğrulanıyor...")
    local_env = _read_local_env()
    base = (
        os.getenv("DEPLOY_VERIFY_BASE_URL")
        or local_env.get("DEPLOY_VERIFY_BASE_URL")
        or os.getenv("PUBLIC_BASE_URL")
        or local_env.get("PUBLIC_BASE_URL")
        or "http://127.0.0.1:8000"
    ).strip().rstrip("/")
    raw_prefix = (
        os.getenv("DEPLOY_VERIFY_BASE_PATH")
        if "DEPLOY_VERIFY_BASE_PATH" in os.environ
        else local_env.get("DEPLOY_VERIFY_BASE_PATH", local_env.get("PUBLIC_BASE_PATH", ""))
    )
    clean_prefix = (raw_prefix or "").strip().strip("/")
    prefix = f"/{clean_prefix}" if clean_prefix else ""

    checks = [
        ("Müşteri Paneli (PWA)", f"{prefix}/kart/", (200,)),
        ("QR 1 — Kayıt & Damga", f"{prefix}/m/qr", (200,)),
        ("QR 2 — Uygulama Ekle", f"{prefix}/indir", (200,)),
        ("Ortak Giriş (/giris)", f"{prefix}/giris", (200,)),
        ("Müdür Paneli (giriş)", f"{prefix}/mudur", (200,)),
        ("Müdür Paneli (/yonetim)", f"{prefix}/yonetim", (200,)),
        ("Çalışan Paneli (giriş)", f"{prefix}/personel", (200,)),
        ("Ana Sayfa API", f"{prefix}/api/home-data", (200,)),
        ("Web Push Public Key", f"{prefix}/api/push/public-key", (200,)),
        ("Manifest", f"{prefix}/manifest.webmanifest", (200,)),
        ("Service Worker", f"{prefix}/sw.js", (200,)),
        ("Tailwind CSS (yerel)", f"{prefix}/static/css/app.css", (200,)),
        ("Fontlar CSS (yerel)", f"{prefix}/static/css/fonts.css", (200,)),
        ("QR kütüphanesi (yerel)", f"{prefix}/static/js/vendor/html5-qrcode.min.js", (200,)),
        ("Admin API oturumsuz → 401", f"{prefix}/api/admin/overview", (401,)),
        ("Admin müşteri listesi oturumsuz → 401", f"{prefix}/api/admin/customers", (401,)),
        ("Personel API oturumsuz → 401", f"{prefix}/api/staff/session", (401,)),
        ("Personel hesapları oturumsuz → 401", f"{prefix}/api/admin/staff", (401,)),
        ("CSV dışa aktarma kaldırıldı → 404", f"{prefix}/api/admin/export", (404,)),
        ("Maskable PWA ikonu", f"{prefix}/static/img/icon-maskable-512.png", (200,)),
        ("Marka logosu", f"{prefix}/static/img/logo.png", (200,)),
    ]
    if os.getenv("ENABLE_DIRECTUS_CHECK", "0") == "1":
        checks.extend([
            ("Directus Admin (/admin)", "/admin", (200, 301, 302, 303)),
            ("Directus Panel (/panel/)", "/panel/", (200, 301, 302, 303)),
        ])
    failures = []
    with httpx.Client(timeout=20.0, follow_redirects=False) as client:
        for label, path, expected in checks:
            r = client.get(base + path)
            ok = r.status_code in expected
            print(f"      - {'OK ' if ok else 'HATA'} {label}: HTTP {r.status_code} ({base}{path})")
            if not ok:
                failures.append(f"{label}: beklenen {expected}, gelen {r.status_code}")
            if ok and (not prefix or path.startswith(prefix + "/")) and r.status_code == 200 and "text/html" in r.headers.get("content-type", ""):
                if FOREIGN_HOST_RE.search(r.text):
                    failures.append(f"{label}: sayfada yurt dışı sunucu referansı bulundu!")
            if path.endswith(("/kart/", "/indir")):
                csp = r.headers.get("content-security-policy", "")
                if "https:" in csp or "http:" in csp:
                    failures.append(f"{label}: CSP hâlâ geniş (https:/http:)")

        r_push = client.post(base + f"{prefix}/api/admin/push/send", json={"title": "t", "body": "b"})
        print(f"      - Oturumsuz bildirim gönderimi: HTTP {r_push.status_code} (401 beklenir)")
        if r_push.status_code != 401:
            failures.append(f"Oturumsuz bildirim gönderimi 401 değil: {r_push.status_code}")

        r_cons = client.post(base + f"{prefix}/api/card-lookup", json={"phone": "0500 000 00 00"})
        status = r_cons.json().get("status")
        print(f"      - Onaysız kayıt denemesi: {status} (consent_required beklenir)")
        if status != "consent_required":
            failures.append(f"Onaysız kayıt reddedilmedi: {status}")

        r_bonus = client.post(base + f"{prefix}/api/claim-install-bonus", json={})
        print(f"      - PWA Install Bonus API: HTTP {r_bonus.status_code} ({r_bonus.json().get('status')})")
        if r_bonus.status_code >= 500:
            failures.append("Install bonus API 5xx döndü")

    if failures:
        raise RuntimeError("Canlı doğrulama başarısız:\n - " + "\n - ".join(failures))


if __name__ == "__main__":
    bundle = build_tar_bundle()
    try:
        deploy_to_server(bundle)
        time.sleep(2)
        verify_live_endpoints()
        print("\n✅ OTONOM DAĞITIM VE DOĞRULAMA BAŞARIYLA TAMAMLANDI!")
    finally:
        if bundle.exists():
            bundle.unlink()


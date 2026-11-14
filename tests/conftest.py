import os
import pytest

import tempfile

os.environ["SECRET_KEY"] = "test_icin_uzun_ve_guvenli_rastgele_anahtar_32_karakter"
os.environ["MENU_SEED"] = "0"  # menü tohumlaması testlerde kapalı (ayrı testte açıkça denenir)
os.environ["RETENTION_JOB"] = "0"  # günlük saklama/imha görevi testlerde arka planda çalışmaz (ayrı testte doğrudan çağrılır)
os.environ["UPLOAD_DIR"] = tempfile.mkdtemp(prefix="sadakat_uploads_")
# Başlangıç hesaplarının parolası koddan değil ortamdan gelir (kaynak kodda parola yok): testler bu değerlerle girer.
os.environ["MANAGER_PASSWORD"] = "Test-Mudur-2026"
os.environ["STAFF_PASSWORD"] = "Test-Kasa-2026"

from starlette.testclient import TestClient
from app.utils import hash_token

from app import config as app_config
from app.db import init_db, get_db, set_setting
from app.main import app


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path, monkeypatch):
    """Her test için izole, geçici bir SQLite veritabanı kurar ve demo mühürleri tohumlar."""
    test_db = tmp_path / "test_loyalty.db"
    monkeypatch.setattr(app_config, "DB_PATH", test_db)

    # Tabloları oluştur ve hız sınırlayıcı (rate limit) belleğini sıfırla
    from app.utils import _rate_limits
    _rate_limits.clear()
    init_db()

    # Demo mühür ve personel kartı tohumla
    with get_db(immediate=True) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO seals (token, label, active) VALUES (?, ?, ?)",
            ("kasa_1", "Ana Kasa Mührü", 1)
        )
        cursor.execute(
            "INSERT INTO seals (token, label, active) VALUES (?, ?, ?)",
            ("kasa_pasif", "Eski Pasif Mühür", 0)
        )
        cursor.execute(
            "INSERT INTO staff_cards (token_hash, label, active) VALUES (?, ?, ?)",
            (hash_token("barista_gizli_anahtar"), "Kıdemli Barista", 1)
        )
        # Varsayılan ayarları teyit et
        set_setting(conn, "daily_limit", "1")
        set_setting(conn, "stamp_target", "5")
        set_setting(conn, "min_interval_minutes", "0")
        # ADR-39 (hediyeler ilk ziyarette) eski testlerde kapalı: onlar hemen-verme davranışını sınar.
        # Koruma modu tests/test_adr39_fake_number_guard.py içinde açıkça açılarak denenir.
        set_setting(conn, "gift_requires_visit", "0")
        # ADR-41: canlıda Damga QR'ı /m/{rastgele gizli kod}'dur. Eski testler kasa akışını /k/m/qr ile sınar; burada
        # gizli kod "qr" yapılır. Rastgele kod ve eski adresin damga vermemesi tests/test_adr41_secret_stamp_qr.py'de.
        set_setting(conn, "stamp_qr_token", "qr")
        # ADR-47: canlıda yeni kayıtta ad-soyad zorunlu; eski testler yalnızca telefonla kayıt yapar.
        # Zorunluluk tests/test_adr47_registration_full_name.py içinde açılarak denenir.
        set_setting(conn, "registration_requires_name", "0")

    yield test_db


@pytest.fixture
def client():
    with TestClient(app, base_url="https://testserver") as test_client:
        yield test_client

"""
Demo Veritabanı Tohumlama (Seed) Scripti
Kullanım: python -m app.seed
"""

import sys
import os
from app.config import DEFAULT_BUSINESS_NAME, PUBLIC_BASE_PATH, PUBLIC_BASE_URL
from app.db import init_db, get_db, get_setting
from app.utils import generate_token, hash_token


def seed_demo_data():
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    init_db()

    with get_db(immediate=True) as conn:
        cursor = conn.cursor()

        # 1. Demo Mühür Kontrolü / Ekleme
        cursor.execute("SELECT id, token FROM seals WHERE label = 'Kasa Mührü'")
        seal = cursor.fetchone()
        if not seal:
            demo_seal_token = generate_token(24)
            cursor.execute(
                "INSERT INTO seals (token, label, active) VALUES (?, 'Kasa Mührü', 1)",
                (demo_seal_token,)
            )
        else:
            demo_seal_token = seal["token"]

        # 2. Demo Personel Kartı Kontrolü / Ekleme (belirteç rastgele; kodda sabit anahtar YOK, yalnızca özeti saklanır)
        cursor.execute("SELECT id FROM staff_cards WHERE label = 'Barista Kartı'")
        staff = cursor.fetchone()
        demo_staff_token = None
        if not staff:
            demo_staff_token = generate_token(24)
            cursor.execute(
                "INSERT INTO staff_cards (token_hash, label, active) VALUES (?, 'Barista Kartı', 1)",
                (hash_token(demo_staff_token),)
            )

        business_name = get_setting(conn, "business_name", DEFAULT_BUSINESS_NAME)

    base = f"{PUBLIC_BASE_URL}{PUBLIC_BASE_PATH}".rstrip("/")
    print("=" * 65)
    print(f"☕ {business_name.upper()} - DİJİTAL SADAKAT KARTI (SQLite)")
    print("=" * 65)
    print("\n✅ Veritabanı ve demo kayıtlar hazır!\n")
    print("🔗 ERİŞİM ADRESLERİ:")
    print("-" * 65)
    print("📌 Müşteri Damga Alma (QR / NFC) Adresi:")
    print(f"   {base}/m/{demo_seal_token}")
    print()
    print("💳 Müşteri Sadakat Kartı:")
    print(f"   {base}/kart/")
    print()
    print("🎁 Personel Ödül Teslim Kartı (Barista QR):")
    if demo_staff_token:
        print(f"   {base}/r/{demo_staff_token}")
    else:
        print("   (kart daha önce oluşturuldu; belirteç yalnızca oluşturulurken gösterilir)")
    print("-" * 65)
    print("💡 Not: Her /m/{token} taramasında 2 dakikalık, tek kullanımlık")
    print("   bir claim üretilir ve müşteri kartına güvenle damga işlenir.")
    print("=" * 65)


if __name__ == "__main__":
    seed_demo_data()

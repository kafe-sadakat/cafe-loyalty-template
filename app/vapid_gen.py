"""
VAPID Anahtar Üretici (Web Push)
Kullanım: python -m app.vapid_gen

Üretilen VAPID_PUBLIC_KEY, VAPID_PRIVATE_KEY ve VAPID_SUBJECT değerlerini .env dosyasına ekler.
GÜVENLİK: Özel anahtarı (VAPID_PRIVATE_KEY) ASLA ekrana veya loga yazdırmaz.
"""

import base64
import os
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def generate_vapid_keypair() -> tuple[str, str]:
    """ECDSA P-256 (SECP256R1) VAPID (public_key, private_key) çiftini URL-safe base64 olarak üretir."""
    private_key = ec.generate_private_key(ec.SECP256R1())
    priv_num = private_key.private_numbers().private_value
    priv_bytes = priv_num.to_bytes(32, byteorder="big")

    public_key = private_key.public_key()
    pub_bytes = public_key.public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint
    )
    return _b64url_encode(pub_bytes), _b64url_encode(priv_bytes)


def ensure_vapid_in_env(env_path: Path | None = None, subject: str = "mailto:admin@example.com") -> str:
    """
    .env dosyasında VAPID anahtarları yoksa üretip ekler.
    Dönüş: VAPID_PUBLIC_KEY (Özel anahtar asla döndürülmez/yazdırılmaz).
    """
    if env_path is None:
        env_path = Path(os.getenv("ENV_FILE", Path(__file__).resolve().parent.parent / ".env"))

    existing_lines: list[str] = []
    env_map: dict[str, str] = {}
    if env_path.exists():
        existing_lines = env_path.read_text(encoding="utf-8").splitlines()
        for line in existing_lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                k, v = stripped.split("=", 1)
                env_map[k.strip()] = v.strip()

    pub_key = env_map.get("VAPID_PUBLIC_KEY", "")
    priv_key = env_map.get("VAPID_PRIVATE_KEY", "")
    subj_val = env_map.get("VAPID_SUBJECT", "") or subject

    if not pub_key or not priv_key:
        pub_key, priv_key = generate_vapid_keypair()
        filtered = [
            ln for ln in existing_lines
            if not ln.strip().startswith(("VAPID_PUBLIC_KEY=", "VAPID_PRIVATE_KEY=", "VAPID_SUBJECT="))
        ]
        filtered.append(f"VAPID_PUBLIC_KEY={pub_key}")
        filtered.append(f"VAPID_PRIVATE_KEY={priv_key}")
        filtered.append(f"VAPID_SUBJECT={subj_val}")
        env_path.write_text("\n".join(filtered) + "\n", encoding="utf-8")

    return pub_key


def main() -> None:
    pub_key = ensure_vapid_in_env()
    print(f"VAPID anahtarları hazır (.env güncellendi, özel anahtar gizli tutuldu). Public key uzunluğu: {len(pub_key)}")


if __name__ == "__main__":
    main()


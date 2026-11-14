"""
NTAG 424 DNA "SUN" (Secure Unique NFC) doğrulaması — her dokunuşta imzası değişen NFC etiketi (ADR-30).

Etiket her dokunuşta adresin sonuna yeni bir imza ekler, ör.:
    https://kafe.example.com/n?p=EF963FF7828658A599F3041510671E88&c=94EED9EE65337086
- p (şifreli PICCData, 16 bayt = 32 hex): AES-128-CBC (IV = 0), anahtar K_SDMMetaRead. Çözülünce: etiket baytı 0xC7
  (UID + sayaç yansıtılıyor, UID 7 bayt) + UID (7 bayt) + okuma sayacı (3 bayt, düşük bayt önce) + rastgele dolgu.
- c (SDMMAC, 8 bayt = 16 hex): KSes = CMAC(K_SDMFileRead, 3C C3 00 01 00 80 ‖ UID ‖ sayaç); c = CMAC(KSes, MAC girdisi)'nin
  1., 3., …, 15. baytları. MAC girdisi çoğu yapılandırmada boştur; bazı araçlar adresteki metni (ör. "…&c=") katar —
  ikisi de denenir.
- Düz mod da desteklenir: ?uid=<14 hex>&ctr=<6 hex, yüksek bayt önce>&c=<16 hex> (UID/sayaç şifresiz, imza aynı).
- Sayaç her dokunuşta artar. Sunucu her etiketin görülen en büyük sayacını tutar; küçük ya da eşit sayaç reddedilir.
  Böylece kopyalanan / paylaşılan / evden yeniden açılan adres ve sonradan geçersizleşmiş eski dokunuşlar damga vermez.
- Bir dokunuş tek bir tek kullanımlık açılışa bağlanır (stamp_codes, origin='nfc', kod = "nfc:UID:sayaç").
Doğrulama NXP AN12196 test vektörleriyle sınanır (tests/test_nfc_sun.py). Anahtarlar Müdür → QR Kodlar → Damga Güvenliği.
"""
from __future__ import annotations

import hmac
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.cmac import CMAC

from app.config import PUBLIC_BASE_PATH, PUBLIC_BASE_URL
from app.db import get_setting, set_setting
from app.utils import get_istanbul_now

META_KEY_SETTING = "nfc_sdm_meta_key"    # K_SDMMetaRead: p (PICCData) şifresini çözer
FILE_KEY_SETTING = "nfc_sdm_file_key"    # K_SDMFileRead: c (CMAC) imzası; boşsa Meta anahtarıyla aynı sayılır
LAST_EVENT_SETTING = "nfc_last_event"    # panel teşhisi: son dokunuşun sonucu

PICC_PARAMS = ("p", "picc_data", "picc", "e")
MAC_PARAMS = ("c", "cmac", "m", "mac")
CTR_PARAMS = ("ctr", "counter")
MAX_QUERY_LEN = 400

_HEX = frozenset("0123456789abcdefABCDEF")
_KEY_RE = re.compile(r"^[0-9A-F]{32}$")
_QUERY_SAFE_RE = re.compile(r"^[A-Za-z0-9_.~%=&-]{1,400}$")
_SV2_PREFIX = b"\x3C\xC3\x00\x01\x00\x80"
_PICC_TAG_UID_CTR = 0xC7     # bit7: UID yansıtma, bit6: sayaç yansıtma, düşük 4 bit: UID uzunluğu (7)
_MAX_MAC_INPUTS = 5
_URI_PREFIXES = ("https://www.", "http://www.", "https://", "http://")   # NDEF URI ön ek kodları 0x02, 0x01, 0x04, 0x03


@dataclass(frozen=True)
class SunMessage:
    """Adresten okunan SUN alanları: şifreli mod (picc) ya da düz mod (uid + counter)."""
    mac: bytes
    picc: Optional[bytes] = None
    uid: Optional[bytes] = None
    counter: Optional[int] = None
    mac_inputs: tuple[bytes, ...] = (b"",)


@dataclass(frozen=True)
class SunResult:
    ok: bool
    uid: str = ""         # büyük harf hex (14 karakter)
    counter: int = -1
    reason: str = ""      # bad_meta_key (p çözülemedi) | bad_mac (imza tutmadı)


# ------------------------------------------------------------------ ayrıştırma
def _hex_value(value: str, length: int) -> Optional[bytes]:
    if len(value) != length or any(ch not in _HEX for ch in value):
        return None
    return bytes.fromhex(value)


def parse_query(raw_query: Optional[str]) -> Optional[SunMessage]:
    """Ham sorgu metninden SUN mesajı (p=…&c=… ya da uid=…&ctr=…&c=…); SUN değilse None."""
    raw = (raw_query or "").lstrip("?")
    if not raw or len(raw) > MAX_QUERY_LEN:
        return None
    fields: dict[str, tuple[str, int]] = {}       # ad → (değer, değerin ham metindeki başlangıcı)
    pos = 0
    for part in raw.split("&"):
        name, sep, value = part.partition("=")
        if sep and name.lower() not in fields:
            fields[name.lower()] = (value, pos + len(name) + 1)
        pos += len(part) + 1

    mac_field = next((fields[k] for k in MAC_PARAMS if k in fields and _hex_value(fields[k][0], 16)), None)
    if mac_field is None:
        return None
    mac, mac_start = bytes.fromhex(mac_field[0]), mac_field[1]
    # MAC girdisi adayları: boş (en yaygın) + MAC'ten önceki her alanın değerinden MAC değerine kadar olan metin
    inputs = [b""]
    for _value, start in sorted(fields.values(), key=lambda f: f[1]):
        if start < mac_start and len(inputs) < _MAX_MAC_INPUTS:
            inputs.append(raw[start:mac_start].encode("ascii", "ignore"))

    for key in PICC_PARAMS:
        picc = _hex_value(fields[key][0], 32) if key in fields else None
        if picc:
            return SunMessage(mac=mac, picc=picc, mac_inputs=tuple(inputs))
    uid = _hex_value(fields["uid"][0], 14) if "uid" in fields else None
    ctr = next((_hex_value(fields[k][0], 6) for k in CTR_PARAMS if k in fields), None)
    if uid and ctr:
        return SunMessage(mac=mac, uid=uid, counter=int.from_bytes(ctr, "big"), mac_inputs=tuple(inputs))
    return None


def parse_claim_token(claim_token: Optional[str]) -> Optional[SunMessage]:
    """Damga isteğindeki "/n?…" (ya da tam adres) içinden SUN mesajı; NFC dokunuşu değilse None."""
    val = (claim_token or "").strip()
    if "?" not in val:
        return None
    return parse_query(val.split("?", 1)[1].split("#", 1)[0])


def claim_token_for_page(raw_query: Optional[str]) -> str:
    """GET /n sayfasına gömülecek talep metni ("/n?p=…&c=…"); SUN değilse ya da güvenli karakter dışıysa ''."""
    raw = (raw_query or "").strip()
    if not raw or not _QUERY_SAFE_RE.match(raw) or parse_query(raw) is None:
        return ""
    return "/n?" + raw


# ------------------------------------------------------------------ kriptografi (NXP NTAG 424 DNA, AN12196)
def _aes_cmac(key: bytes, data: bytes) -> bytes:
    mac = CMAC(algorithms.AES(key))
    mac.update(data)
    return mac.finalize()


def _aes_cbc_zero_iv(key: bytes, block: bytes, encrypt: bool) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.CBC(bytes(16)))
    op = cipher.encryptor() if encrypt else cipher.decryptor()
    return op.update(block) + op.finalize()


def compute_sdmmac(file_key: bytes, uid: bytes, counter: int, mac_input: bytes = b"") -> bytes:
    """SDMMAC: oturum anahtarı CMAC(K_SDMFileRead, SV2), imza CMAC(oturum, girdi)'nin tek sıralı 8 baytı."""
    sv2 = _SV2_PREFIX + uid + counter.to_bytes(3, "little")   # 6 + 7 + 3 = 16 bayt: tam bir AES bloğu
    return _aes_cmac(_aes_cmac(file_key, sv2), mac_input)[1::2]


def decrypt_picc(meta_key: bytes, picc: bytes) -> Optional[tuple[bytes, int]]:
    """Şifreli PICCData → (UID, sayaç); etiket baytı UID + sayaç yansıtmasını göstermiyorsa None (yanlış anahtar)."""
    plain = _aes_cbc_zero_iv(meta_key, picc, encrypt=False)
    if plain[0] != _PICC_TAG_UID_CTR:
        return None
    return plain[1:8], int.from_bytes(plain[8:11], "little")


def encrypt_picc(meta_key: bytes, uid: bytes, counter: int, padding: Optional[bytes] = None) -> bytes:
    """Etiketin yaptığı şifreleme (testler ve kurulum denetimi için)."""
    block = bytes([_PICC_TAG_UID_CTR]) + uid + counter.to_bytes(3, "little") + (padding or os.urandom(5))
    return _aes_cbc_zero_iv(meta_key, block, encrypt=True)


def build_query(meta_key: bytes, file_key: bytes, uid: bytes, counter: int, picc_param: str = "p", mac_param: str = "c") -> str:
    """Etiketin bir dokunuşta üreteceği sorgu metni: p=<PICCData>&c=<SDMMAC> (testler için)."""
    picc = encrypt_picc(meta_key, uid, counter).hex().upper()
    return f"{picc_param}={picc}&{mac_param}={compute_sdmmac(file_key, uid, counter).hex().upper()}"


def verify(msg: SunMessage, meta_key: bytes, file_key: bytes) -> SunResult:
    """İmzayı doğrular; sayacın ileri gidip gitmediğine register_tap bakar."""
    if msg.picc is not None:
        opened = decrypt_picc(meta_key, msg.picc)
        if opened is None:
            return SunResult(False, reason="bad_meta_key")
        uid, counter = opened
    else:
        uid, counter = msg.uid or b"", int(msg.counter or 0)
    for mac_input in msg.mac_inputs:
        if hmac.compare_digest(compute_sdmmac(file_key, uid, counter, mac_input), msg.mac):
            return SunResult(True, uid.hex().upper(), counter)
    return SunResult(False, uid.hex().upper(), counter, "bad_mac")


# ------------------------------------------------------------------ anahtarlar
def normalize_key(raw: Optional[str]) -> Optional[str]:
    """'00 11 …', '00:11:…', küçük harf → 32 karakter büyük harf hex; boş → ''; geçersiz → None."""
    val = re.sub(r"[\s:\-]", "", raw or "").upper()
    if not val:
        return ""
    return val if _KEY_RE.match(val) else None


def is_zero_key(hex_key: Optional[str]) -> bool:
    """Fabrika anahtarı (00…00): herkes bilir, imza taklit edilebilir."""
    return bool(hex_key) and set(hex_key) == {"0"}


def load_keys(conn: sqlite3.Connection) -> Optional[tuple[bytes, bytes]]:
    """(K_SDMMetaRead, K_SDMFileRead) ya da yapılandırılmamışsa None. File anahtarı boşsa Meta anahtarı kullanılır."""
    meta = normalize_key(get_setting(conn, META_KEY_SETTING, ""))
    file_key = normalize_key(get_setting(conn, FILE_KEY_SETTING, "")) or meta
    if not meta or not file_key:
        return None
    return bytes.fromhex(meta), bytes.fromhex(file_key)


def save_keys(conn: sqlite3.Connection, meta_raw: Optional[str], file_raw: Optional[str]) -> Optional[str]:
    """Panelden gelen anahtarları kaydeder (None = değiştirme). Hata metni ya da None döner."""
    updates = {}
    for setting, raw, title in ((META_KEY_SETTING, meta_raw, "Meta Read"), (FILE_KEY_SETTING, file_raw, "File Read")):
        if raw is None:
            continue
        key = normalize_key(raw)
        if key is None:
            return f"{title} anahtarı 32 karakterlik onaltılık (0-9, A-F) bir değer olmalı."
        updates[setting] = key
    for setting, key in updates.items():
        set_setting(conn, setting, key)
    return None


# ------------------------------------------------------------------ etiket kayıtları (sayaç)
def tap_code(uid_hex: str, counter: int) -> str:
    """Bir dokunuşun tek kullanımlık açılış anahtarı (stamp_codes.code_hash bunun özetidir). ':' içerdiği için hiçbir
    /d/{kod} biçimine (A-Z a-z 0-9 _ -) denk gelemez."""
    return f"nfc:{uid_hex.upper()}:{int(counter)}"


def register_tap(conn: sqlite3.Connection, uid_hex: str, counter: int, now: Optional[datetime] = None) -> tuple[str, Optional[sqlite3.Row]]:
    """
    Dokunuşu etiketin sayaç kaydına işler. Dönüş: ('ok' | 'stale' | 'disabled', etiket satırı).
    - Yeni etiket kendiliğinden eklenir: geçerli imzayı yalnızca bizim anahtarlarımızla programlanmış etiket üretebilir.
    - Sayaç son görülenden büyük olmalıdır; küçük ya da eşitse 'stale' (kopya ya da sonradan geçersizleşmiş eski dokunuş).
    """
    now_iso = (now or get_istanbul_now()).isoformat()
    uid = uid_hex.upper()
    row = conn.execute("SELECT * FROM nfc_tags WHERE uid = ?", (uid,)).fetchone()
    if row is None:
        number = conn.execute("SELECT COUNT(*) FROM nfc_tags").fetchone()[0] + 1
        conn.execute(
            "INSERT INTO nfc_tags (uid, label, last_counter, active, taps, created_at, last_seen_at) VALUES (?, ?, ?, 1, 1, ?, ?)",
            (uid, f"NFC Etiketi {number}", int(counter), now_iso, now_iso),
        )
        return "ok", conn.execute("SELECT * FROM nfc_tags WHERE uid = ?", (uid,)).fetchone()
    if not row["active"]:
        return "disabled", row
    cur = conn.execute(
        "UPDATE nfc_tags SET last_counter = ?, last_seen_at = ?, taps = taps + 1 WHERE id = ? AND last_counter < ?",
        (int(counter), now_iso, row["id"], int(counter)),
    )
    return ("ok" if cur.rowcount == 1 else "stale"), row


def record_event(conn: sqlite3.Connection, result: str, uid: str = "", counter: int = -1, tag_id: Optional[int] = None) -> None:
    """Panel teşhisi için son dokunuşun sonucu: ok | stale | disabled | bad_meta_key | bad_mac | not_configured."""
    payload = {"at": get_istanbul_now().isoformat(), "result": result, "uid": uid, "counter": counter, "tag_id": tag_id}
    set_setting(conn, LAST_EVENT_SETTING, json.dumps(payload, ensure_ascii=False))


def list_tags(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM nfc_tags ORDER BY id ASC").fetchall()
    return [
        {"id": r["id"], "uid": r["uid"], "label": r["label"], "active": bool(r["active"]), "taps": r["taps"],
         "last_counter": r["last_counter"], "created_at": r["created_at"], "last_seen_at": r["last_seen_at"]}
        for r in rows
    ]


def update_tag(conn: sqlite3.Connection, tag_id: int, label: Optional[str] = None, active: Optional[bool] = None) -> bool:
    """Etiketin adını değiştirir / devre dışı bırakır. Etiket silinmez: silinse sayaç kaydı kaybolur ve eski dokunuşlar
    yeniden geçerli olurdu."""
    if not conn.execute("SELECT 1 FROM nfc_tags WHERE id = ?", (tag_id,)).fetchone():
        return False
    if label is not None:
        conn.execute("UPDATE nfc_tags SET label = ? WHERE id = ?", (label.strip()[:60] or f"NFC Etiketi {tag_id}", tag_id))
    if active is not None:
        conn.execute("UPDATE nfc_tags SET active = ? WHERE id = ?", (1 if active else 0, tag_id))
    return True


# ------------------------------------------------------------------ panel: etikete yazılacak adres ve ofsetler
def tag_url_template(base_url: Optional[str] = None, base_path: Optional[str] = None) -> str:
    """Etikete yazılacak adres: p (32 karakter) ve c (16 karakter) yerine etiket her dokunuşta yeni değer yazar."""
    u = PUBLIC_BASE_URL if base_url is None else base_url
    p = PUBLIC_BASE_PATH if base_path is None else base_path
    return f"{u}{p}/n?p={'0' * 32}&c={'0' * 16}"


def sdm_offsets(url: str) -> dict[str, int]:
    """
    NDEF dosyasındaki SDM ofsetleri (elle ofset isteyen araçlar için). Dosya: NLEN (2) + kayıt başlığı D1 (1) + tür
    uzunluğu (1) + yük uzunluğu (1) + 'U' (1) + URI ön ek kodu (1) = 7 bayt, ardından ön eki atılmış adres.
    """
    rest = next((url[len(p):] for p in _URI_PREFIXES if url.startswith(p)), url)
    picc_at = 7 + rest.index("?p=") + 3
    mac_at = 7 + rest.index("&c=") + 3
    return {"picc_offset": picc_at, "mac_offset": mac_at, "mac_input_offset": mac_at}


def admin_payload(
    conn: sqlite3.Connection, base_url: Optional[str] = None, base_path: Optional[str] = None
) -> dict[str, Any]:
    """Müdür paneli: anahtar durumu, etikete yazılacak adres + ofsetler, etiketler ve son dokunuşun sonucu."""
    meta = normalize_key(get_setting(conn, META_KEY_SETTING, "")) or ""
    file_key = normalize_key(get_setting(conn, FILE_KEY_SETTING, "")) or ""
    try:
        last = json.loads(get_setting(conn, LAST_EVENT_SETTING, "") or "null")
    except ValueError:
        last = None
    url = tag_url_template(base_url=base_url, base_path=base_path)
    return {
        "configured": load_keys(conn) is not None,
        "meta_key": meta,
        "file_key": file_key,
        "zero_key": is_zero_key(meta) or is_zero_key(file_key),
        "template_url": url,
        **sdm_offsets(url),
        "tags": list_tags(conn),
        "last_event": last,
    }

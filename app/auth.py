"""
Yönetim paneli kimlik doğrulaması (Müdür ve Çalışan rolleri).

- Parolalar yalnızca ortam değişkenlerinden (MANAGER_PASSWORD / STAFF_PASSWORD) okunur.
- Oturum çerezi HMAC-SHA256 ile imzalanır; HttpOnly + Secure + SameSite=Strict.
- Müdür oturumu çalışan uçlarına da erişebilir; çalışan oturumu müdür uçlarına ASLA erişemez.
- Başarısız giriş ve yetkisiz erişimler güvenlik günlüğüne (audit_logs / security_incidents) yazılır.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import secrets
import time
from typing import Optional

from fastapi import HTTPException, Request, Response

from app.config import SECRET_KEY, edge_trusted
from app.services import security
from app.utils import check_rate_limit

MANAGER_COOKIE = "mgr_session"
STAFF_COOKIE = "stf_session"
SESSION_HOURS = 12
# ADR-42: "Bu cihazda oturumum açık kalsın" — oturum REMEMBER_DAYS gün sürer ve panel açıldıkça (günde en çok bir kez)
# yeniden REMEMBER_DAYS güne uzar; eski çerezin oturumu sunucuda iptal edilir. Parola değişince / çıkışta yine düşer.
REMEMBER_DAYS = 30
RENEW_AFTER_SECONDS = 24 * 3600
ROLE_COOKIES = {"manager": MANAGER_COOKIE, "staff": STAFF_COOKIE}


def _lifetime(remember: bool) -> int:
    return REMEMBER_DAYS * 86400 if remember else SESSION_HOURS * 3600


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(payload_b64: str) -> str:
    return hmac.new(SECRET_KEY.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256).hexdigest()


def password_fingerprint(password_hash: str) -> str:
    """
    Oturum çerezine konan parola parmak izi: parola değişince / müdür sıfırlayınca o hesabın açık oturumları anında düşer
    (çalınmış oturum, parola değiştirildikten sonra kullanılamaz). Anahtarlı (HMAC) olduğundan özet hakkında bilgi vermez.
    """
    return hmac.new(SECRET_KEY.encode("utf-8"), f"pv:{password_hash or ''}".encode("utf-8"), hashlib.sha256).hexdigest()[:24]


def create_session(role: str, name: str = "", staff_id: Optional[int] = None, pv: str = "", remember: bool = False) -> str:
    payload = {
        "r": role,
        "n": (name or "")[:40],
        "e": int(time.time()) + _lifetime(remember),
        "x": secrets.token_hex(8),
    }
    if staff_id is not None:
        payload["i"] = int(staff_id)
    if pv:
        payload["p"] = pv
    if remember:
        payload["m"] = 1
    payload_b64 = _b64(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    return f"{payload_b64}.{_sign(payload_b64)}"


def verify_session(token: Optional[str], expected_role: str) -> Optional[dict]:
    """İmza ve süre geçerliyse {'role','name','staff_id','user_id','pv'} döner; aksi halde None."""
    if not token or "." not in token or len(token) > 4096:
        return None
    payload_b64, _, sig = token.partition(".")
    # ASCII dışı (sahte) çerez değeri imza hesabında / karşılaştırmada istisna fırlatıp 500 döndürmesin
    if not (payload_b64.isascii() and sig.isascii()) or not hmac.compare_digest(sig, _sign(payload_b64)):
        return None
    try:
        payload = json.loads(_unb64(payload_b64).decode("utf-8"))
    except Exception:
        return None
    if payload.get("r") != expected_role or int(payload.get("e", 0)) < time.time():
        return None
    return {
        "role": payload["r"], "name": payload.get("n") or "",
        "staff_id": payload.get("i") if payload["r"] == "staff" else None,
        "user_id": payload.get("i"),
        "pv": str(payload.get("p") or ""),
        "sid": str(payload.get("x") or ""),
        "exp": int(payload.get("e", 0)),
        "remember": bool(payload.get("m")),
    }


def _account_fingerprint(user_id: Optional[int]) -> str:
    if user_id is None:
        return ""
    from app.db import get_db
    with get_db() as conn:
        row = conn.execute("SELECT password_hash FROM admin_users WHERE id = ?", (int(user_id),)).fetchone()
    return password_fingerprint(row["password_hash"]) if row else ""


def _session_account_ok(user_id: int, role: str, pv: str, sid: str = "") -> bool:
    """
    Oturumun hesabı hâlâ etkin mi ve oturum, hesabın GÜNCEL parolasıyla mı açılmış? Silinen / devre dışı bırakılan hesap
    ya da değiştirilen parola → eski oturum anında geçersiz. Parola izi taşımayan (bu sürümden önceki) oturumlar da
    geçersizdir; kullanıcı bir kez yeniden giriş yapar.
    """
    if not pv:
        return False
    from app.db import get_db
    with get_db() as conn:
        row = conn.execute(
            "SELECT active, password_hash FROM admin_users WHERE id = ? AND role = ?", (user_id, role)
        ).fetchone()
        # Çıkış yapılmış (sunucuda iptal edilmiş) oturum, çerez çalınmış olsa bile süresi dolana kadar geçersizdir (G-06)
        revoked = bool(sid) and conn.execute("SELECT 1 FROM revoked_sessions WHERE sid = ?", (sid,)).fetchone() is not None
    if revoked:
        return False
    return bool(row and row["active"]) and hmac.compare_digest(pv, password_fingerprint(row["password_hash"]))


def set_session_cookie(
    response: Response, role: str, name: str = "", staff_id: Optional[int] = None, remember: bool = False
) -> None:
    """`staff_id`: oturumu açılan hesabın kimliği (müdür ya da personel); parola izi bu hesaptan okunur.
    `remember`: bu cihazda REMEMBER_DAYS gün açık kalır (ADR-42); değilse SESSION_HOURS saat."""
    response.set_cookie(
        key=ROLE_COOKIES[role],
        value=create_session(role, name, staff_id, pv=_account_fingerprint(staff_id), remember=remember),
        httponly=True,
        secure=True,
        samesite="strict",
        path="/",
        max_age=_lifetime(remember),
    )


def renew_remembered_session(response: Response, session: Optional[dict]) -> None:
    """
    ADR-42: hatırlanan oturum panel açıldıkça yeniden REMEMBER_DAYS güne uzar (günde en çok bir kez). Yeni çerez yazılır,
    eskisinin oturumu sunucuda iptal edilir (eski çerezin kopyası kullanılamaz). Hatırlanmayan oturum uzamaz.
    """
    if not session or not session.get("remember") or session.get("role") not in ROLE_COOKIES:
        return
    if session.get("user_id") is None or session.get("exp", 0) - time.time() > _lifetime(True) - RENEW_AFTER_SECONDS:
        return
    from app.db import get_db
    try:
        with get_db(immediate=True) as conn:
            if session.get("sid"):
                conn.execute(
                    "INSERT OR REPLACE INTO revoked_sessions (sid, expires_at) VALUES (?, ?)", (session["sid"], session["exp"])
                )
    except Exception:
        return   # iptal yazılamazsa çerez de yenilenmez (eski oturum süresi dolana kadar geçerli kalır)
    set_session_cookie(response, session["role"], session.get("name") or "", int(session["user_id"]), remember=True)


def revoke_request_sessions(request: Request) -> None:
    """Çıkışta: isteğin taşıdığı geçerli müdür/personel oturumlarını sunucuda iptal eder (yalnızca bu oturumlar; diğer cihazlar sürer)."""
    from app.db import get_db
    now = int(time.time())
    try:
        with get_db(immediate=True) as conn:
            conn.execute("DELETE FROM revoked_sessions WHERE expires_at < ?", (now,))
            for role, cookie in ROLE_COOKIES.items():
                sess = verify_session(request.cookies.get(cookie), role)
                if sess and sess.get("sid"):
                    conn.execute(
                        "INSERT OR REPLACE INTO revoked_sessions (sid, expires_at) VALUES (?, ?)", (sess["sid"], sess["exp"])
                    )
    except Exception:  # iptal kaydı yazılamasa da çerez yine silinir
        pass


def clear_session_cookies(response: Response) -> None:
    for cookie in ROLE_COOKIES.values():
        response.delete_cookie(key=cookie, path="/")


# Cloudflare'in yayımladığı genel adres aralıkları (https://www.cloudflare.com/ips/). Alan adı Cloudflare vekili (turuncu bulut)
# arkasındaysa Caddy yalnızca bu adresleri görür; gerçek istemci CF-Connecting-IP başlığındadır.
_CLOUDFLARE_NETS = tuple(ipaddress.ip_network(n) for n in (
    "173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22", "141.101.64.0/18", "108.162.192.0/18",
    "190.93.240.0/20", "188.114.96.0/20", "197.234.240.0/22", "198.41.128.0/17", "162.158.0.0/15", "104.16.0.0/13",
    "104.24.0.0/14", "172.64.0.0/13", "131.0.72.0/22",
    "2400:cb00::/32", "2606:4700::/32", "2803:f800::/32", "2405:b500::/32", "2405:8100::/32", "2a06:98c0::/29", "2c0f:f248::/32",
))


def _is_cloudflare_peer(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(ip.version == net.version and ip in net for net in _CLOUDFLARE_NETS)


def client_ip(request: Request) -> str:
    """İstemci IP'si. Kenar (Cloudflare Pages) başlığına yalnızca `config.edge_trusted` kurallarıyla güvenilir (G-01).
    Doğrudan Cloudflare vekilinden gelen istekte (eş adres Cloudflare aralığındaysa) CF-Connecting-IP kullanılır; başka eşlerden
    gelen sahte başlık yok sayılır."""
    edge_ip = (request.headers.get("x-edge-client-ip") or "").strip()
    if edge_ip and edge_trusted(request):
        try:
            return str(ipaddress.ip_address(edge_ip))
        except ValueError:
            pass
    peer = request.client.host if request.client else "0.0.0.0"
    cf_ip = (request.headers.get("cf-connecting-ip") or "").strip()
    if cf_ip and _is_cloudflare_peer(peer):
        try:
            return str(ipaddress.ip_address(cf_ip))
        except ValueError:
            pass
    return peer


def current_manager(request: Request) -> Optional[dict]:
    """Müdür oturumu: hesap silindi / devre dışı bırakıldı / parolası değişti ise anında geçersiz."""
    mgr = verify_session(request.cookies.get(MANAGER_COOKIE), "manager")
    if not mgr or mgr.get("user_id") is None:
        return None
    if not _session_account_ok(int(mgr["user_id"]), "manager", mgr["pv"], mgr["sid"]):
        return None
    return mgr


def current_staff(request: Request) -> Optional[dict]:
    """Çalışan oturumu; yoksa müdür oturumu da çalışan işlemleri için kabul edilir."""
    staff = verify_session(request.cookies.get(STAFF_COOKIE), "staff")
    if staff:
        # Personel hesabıyla açılmış oturum: hesap silindi / devre dışı bırakıldı / parolası müdürce sıfırlandıysa geçersiz.
        if staff.get("staff_id") is None:
            return None
        if not _session_account_ok(int(staff["staff_id"]), "staff", staff["pv"], staff["sid"]):
            return None
        return staff
    mgr = current_manager(request)
    if mgr:
        return {"role": "manager", "name": mgr["name"] or "Müdür"}
    return None


def _deny(request: Request) -> HTTPException:
    ip = client_ip(request)
    # Oturumsuz yönetim isteği IP başına 10 dakikada en çok 30 kez güvenlik günlüğüne yazılır: sel saldırısında her
    # istek veritabanı yazma kilidi alıp tabloyu büyütmesin. İstek her durumda reddedilir (401).
    if check_rate_limit(f"unauth_log_{ip}", max_requests=30, window_seconds=600):
        security.record_unauthorized(
            ip=ip,
            path=request.scope.get("path", request.url.path),
            method=request.method,
            user_agent=request.headers.get("user-agent", ""),
        )
    return HTTPException(status_code=401, detail="Oturum gerekli. Lütfen giriş yapınız.")


def require_manager(request: Request) -> dict:
    session = current_manager(request)
    if not session:
        raise _deny(request)
    return session


def require_staff(request: Request) -> dict:
    session = current_staff(request)
    if not session:
        raise _deny(request)
    return session

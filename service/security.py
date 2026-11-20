"""Password hashing (stdlib scrypt), API keys, signed session cookies and magic-link tokens."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1
API_KEY_PREFIX = "rcpt_"
SESSION_COOKIE = "receipts_session"
SESSION_TTL_S = 14 * 24 * 3600


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(dk)}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        algo, n, r, p, salt, dk = stored.split("$")
        if algo != "scrypt":
            return False
        got = hashlib.scrypt(password.encode(), salt=_unb64(salt), n=int(n), r=int(r), p=int(p), dklen=len(_unb64(dk)))
        return hmac.compare_digest(got, _unb64(dk))
    except Exception:
        return False


def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def new_api_key() -> tuple[str, str, str]:
    """Returns (plaintext key shown once, display prefix, sha256 stored in the database)."""
    key = API_KEY_PREFIX + secrets.token_urlsafe(32)
    return key, key[: len(API_KEY_PREFIX) + 6], sha256_hex(key)


def new_magic_token() -> tuple[str, str]:
    tok = secrets.token_urlsafe(32)
    return tok, sha256_hex(tok)


def sign_session(user_id: str, secret: str, now: float | None = None) -> str:
    issued = str(int(now if now is not None else time.time()))
    body = f"{user_id}.{issued}"
    mac = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{mac}"


def read_session(cookie: str | None, secret: str, now: float | None = None) -> str | None:
    if not cookie or cookie.count(".") != 2:
        return None
    user_id, issued, mac = cookie.split(".")
    want = hmac.new(secret.encode(), f"{user_id}.{issued}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mac, want):
        return None
    try:
        if (now if now is not None else time.time()) - int(issued) > SESSION_TTL_S:
            return None
    except ValueError:
        return None
    return user_id

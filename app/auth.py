import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import string
from typing import Optional

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

_argon2_hasher = PasswordHasher()

SESSION_COOKIE = "telepsych_session"
SESSION_MAX_AGE = 60 * 30  # 30 minutes (sliding with activity refresh)
SESSION_IDLE_TIMEOUT = 60 * 30  # 30 minutes inactivity

_LOG = logging.getLogger(__name__)


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    pad = 4 - (len(s) % 4)
    if pad != 4:
        s += "=" * pad
    return base64.urlsafe_b64decode(s)


def _get_session_secret() -> bytes:
    """
    Secret used to sign session tokens.
    In production, SESSION_SECRET must be set. In development, we allow a fallback.
    """
    secret = (os.environ.get("SESSION_SECRET") or "").strip()
    if secret:
        return secret.encode("utf-8")
    env = (os.environ.get("ENV") or "").lower()
    is_prod = env in ("production", "prod") or (os.environ.get("FRONTEND_ORIGIN") or "").strip().lower().startswith("https://")
    if is_prod:
        raise RuntimeError("SESSION_SECRET must be set in production")
    _LOG.warning("SESSION_SECRET not set; using insecure development fallback secret")
    return b"dev-insecure-session-secret"


def _sign_hs256(message: bytes, secret: bytes) -> str:
    sig = hmac.new(secret, message, hashlib.sha256).digest()
    return _b64url_encode(sig)


def _encode_session_token(payload: dict) -> str:
    """
    Create a compact signed token (JWT-like, HS256).
    Token format: base64url(header).base64url(payload).base64url(signature)
    """
    header = {"alg": "HS256", "typ": "JWT"}
    header_b64 = _b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    secret = _get_session_secret()
    sig_b64 = _sign_hs256(signing_input, secret)
    return f"{header_b64}.{payload_b64}.{sig_b64}"


def _decode_session_token(token: Optional[str]) -> Optional[dict]:
    if not token or not token.strip():
        return None
    token = token.strip()
    try:
        # Expect JWT-like format
        parts = token.split(".")
        if len(parts) != 3:
            return None
        header_b64, payload_b64, sig_b64 = parts
        signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
        secret = _get_session_secret()
        expected_sig = _sign_hs256(signing_input, secret)
        if not hmac.compare_digest(expected_sig, sig_b64):
            return None

        payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
        user_id = payload.get("userId")
        role = payload.get("role")
        exp = payload.get("exp")
        now = __import__("time").time()
        if exp and (now > exp):
            return None
        last_activity_at = payload.get("lat")
        if last_activity_at is None:
            last_activity_at = payload.get("iat")
        if last_activity_at and ((now - float(last_activity_at)) > SESSION_IDLE_TIMEOUT):
            return None
        if role not in ("doctor", "patient"):
            return None
        if not user_id:
            return None
        return {
            "id": user_id,
            "role": role,
            "exp": exp,
            "iat": payload.get("iat"),
            "lat": payload.get("lat"),
        }
    except Exception:
        return None


def get_session_from_cookie(cookie_value_or_header: Optional[str]) -> Optional[dict]:
    """
    Accept either:
    - the raw cookie value (what FastAPI's Cookie(...) dependency provides), or
    - the full Cookie header string (e.g. "a=1; telepsych_session=...; b=2").
    """
    if not cookie_value_or_header:
        return None

    s = cookie_value_or_header.strip()

    # FastAPI Cookie(alias=...) passes the cookie VALUE only (no "name=" prefix).
    # Our session token is base64url without '=' padding, so treat a bare value as the token.
    if "=" not in s and ";" not in s:
        return _decode_session_token(s)

    # Otherwise parse a full Cookie header.
    for part in s.split(";"):
        part = part.strip()
        if part.startswith(f"{SESSION_COOKIE}="):
            token = part.split("=", 1)[1].strip()
            return _decode_session_token(token)
    return None


def get_session_from_bearer(authorization: Optional[str]) -> Optional[dict]:
    if not authorization or not authorization.strip().lower().startswith("bearer "):
        return None
    token = authorization.strip()[7:].strip()  # after "Bearer "
    return _decode_session_token(token)


def create_session_payload(
    user_id: str,
    role: str,
    issued_at: Optional[int] = None,
    expires_at: Optional[int] = None,
    last_activity_at: Optional[int] = None,
) -> str:
    import time
    now = int(time.time())
    iat = int(issued_at) if issued_at else now
    exp = int(expires_at) if expires_at else (iat + SESSION_MAX_AGE)
    lat = int(last_activity_at) if last_activity_at else now
    payload = {"userId": user_id, "role": role, "iat": iat, "exp": exp, "lat": lat}
    return _encode_session_token(payload)


def hash_password(password: str) -> str:
    return _argon2_hasher.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    if not hashed:
        return False
    try:
        _argon2_hasher.verify(hashed, plain)
        return True
    except VerifyMismatchError:
        return False


def generate_reset_token() -> str:
    return secrets.token_hex(32)

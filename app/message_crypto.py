import base64
import hashlib
import logging
import os
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken


ENCRYPTION_PREFIX = "enc:v1:"
UNAVAILABLE_MESSAGE = "[Encrypted message unavailable]"
_LOG = logging.getLogger(__name__)


def _is_production() -> bool:
    env = (os.getenv("ENV") or "").strip().lower()
    if env in {"production", "prod"}:
        return True
    frontend_origin = (os.getenv("FRONTEND_ORIGIN") or "").strip().lower()
    return frontend_origin.startswith("https://")


def validate_message_encryption_config() -> None:
    """
    Enforce encryption key presence in production.

    In development, missing key is allowed for local convenience.
    """
    raw_key = (os.getenv("MESSAGE_ENCRYPTION_KEY") or "").strip()
    if raw_key:
        return
    if _is_production():
        raise RuntimeError("MESSAGE_ENCRYPTION_KEY must be set in production")
    _LOG.warning(
        "MESSAGE_ENCRYPTION_KEY not set; message bodies will be stored as plaintext in development"
    )


@lru_cache(maxsize=1)
def _get_fernet() -> Fernet | None:
    """
    Build a Fernet instance from MESSAGE_ENCRYPTION_KEY.

    Supports:
    1) Native Fernet key (recommended): urlsafe base64-encoded 32-byte key.
    2) Arbitrary passphrase: deterministically derived via SHA-256.
    """
    validate_message_encryption_config()
    raw_key = (os.getenv("MESSAGE_ENCRYPTION_KEY") or "").strip()
    if not raw_key:
        return None

    try:
        decoded = base64.urlsafe_b64decode(raw_key.encode("utf-8"))
        if len(decoded) == 32:
            fernet_key = raw_key.encode("utf-8")
        else:
            raise ValueError("Invalid Fernet key length")
    except Exception:
        digest = hashlib.sha256(raw_key.encode("utf-8")).digest()
        fernet_key = base64.urlsafe_b64encode(digest)

    return Fernet(fernet_key)


def encrypt_message_body(plaintext: str) -> str:
    fernet = _get_fernet()
    if not fernet or not plaintext:
        return plaintext
    token = fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")
    return f"{ENCRYPTION_PREFIX}{token}"


def decrypt_message_body(stored_text: str | None) -> str | None:
    if stored_text is None:
        return None
    if not stored_text.startswith(ENCRYPTION_PREFIX):
        return stored_text

    fernet = _get_fernet()
    if not fernet:
        return UNAVAILABLE_MESSAGE

    token = stored_text[len(ENCRYPTION_PREFIX) :]
    try:
        return fernet.decrypt(token.encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError):
        return UNAVAILABLE_MESSAGE

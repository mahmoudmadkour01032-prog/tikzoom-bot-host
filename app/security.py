"""Encryption + Telegram WebApp init_data verification helpers."""
from __future__ import annotations

import hashlib
import hmac
import json
from urllib.parse import parse_qsl

from cryptography.fernet import Fernet, InvalidToken

from .config import get_settings


def _fernet() -> Fernet:
    key = get_settings().fernet_key
    if not key:
        # Generate a temporary key for development. In production you must set FERNET_KEY.
        # We persist the key via env var to ensure stable encryption across restarts.
        raise RuntimeError(
            "FERNET_KEY is not set. Generate one with:\n"
            "  python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    return Fernet(key.encode())


def encrypt_token(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_token(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise ValueError("Invalid encrypted token") from exc


def token_hash(token: str) -> str:
    """Stable hash for routing webhook URLs without revealing the token."""
    return hashlib.sha256(token.encode()).hexdigest()[:32]


def verify_webapp_init_data(init_data: str, bot_token: str) -> dict | None:
    """Validate Telegram WebApp init_data per official spec.

    Returns parsed dict on success, None on failure.
    """
    try:
        parsed = dict(parse_qsl(init_data, keep_blank_values=True))
        provided_hash = parsed.pop("hash", None)
        if not provided_hash:
            return None
        check_string = "\n".join(f"{k}={parsed[k]}" for k in sorted(parsed))
        secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        calc_hash = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc_hash, provided_hash):
            return None
        if "user" in parsed:
            try:
                parsed["user"] = json.loads(parsed["user"])
            except json.JSONDecodeError:
                pass
        return parsed
    except Exception:
        return None

"""Хеширование паролей и токены сессий. Только стандартная библиотека."""
from __future__ import annotations

import hashlib
import hmac
import secrets

PBKDF2_ITERATIONS = 390_000


def hash_password(password: str, salt: str | None = None) -> str:
    """Возвращает строку вида ``pbkdf2$итерации$соль$хеш``."""
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ITERATIONS
    ).hex()
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _algo, iterations, salt, expected = stored.split("$")
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt.encode("utf-8"), int(iterations)
        ).hex()
        return hmac.compare_digest(digest, expected)
    except (ValueError, AttributeError):
        return False


def new_session_token() -> str:
    return secrets.token_urlsafe(32)

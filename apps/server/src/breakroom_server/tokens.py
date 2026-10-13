"""Opaque tokens and one-time codes. The server stores only hashes of either.

Tokens are 32 random bytes, URL-safe, with a kind prefix. Codes are 8 Crockford
base32 characters; normalize_code accepts lower case, one hyphen, and the
usual misreadings (I/L → 1, O → 0).
"""

import hashlib
import hmac
import secrets
from typing import Literal

CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
CODE_LENGTH = 8
_MISREAD = str.maketrans({"I": "1", "L": "1", "O": "0"})


def new_token(prefix: Literal["bra_", "brr_"]) -> str:
    return prefix + secrets.token_urlsafe(32)


def hash_token(token: str) -> bytes:
    # surrogatepass: a lone surrogate in client input hashes to a miss, not a 500.
    return hashlib.sha256(token.encode("utf-8", "surrogatepass")).digest()


def new_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def normalize_code(raw: str) -> str | None:
    """Canonical code, or None if it cannot be one (never raises)."""
    text = raw.strip().upper()
    if text.count("-") > 1:
        return None
    text = text.replace("-", "").translate(_MISREAD)
    if len(text) != CODE_LENGTH or any(c not in CODE_ALPHABET for c in text):
        return None
    return text


def hmac_code(secret: bytes, code: str) -> bytes:
    return hmac.new(secret, code.encode(), hashlib.sha256).digest()

"""Opaque tokens and one-time codes. The server stores only hashes of either.

Tokens are 32 random bytes, URL-safe, with a kind prefix. Codes are 8 Crockford
base32 characters; normalize_code accepts lower case, one hyphen, and the
usual misreadings (I/L → 1, O → 0).
"""

import base64
import hashlib
import hmac
import secrets
from typing import Literal

CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
CODE_LENGTH = 8
_MISREAD = str.maketrans({"I": "1", "L": "1", "O": "0"})


def new_token(prefix: Literal["bra_", "brr_"]) -> str:
    return prefix + secrets.token_urlsafe(32)


def derive_successor(secret: bytes, parent_token: str) -> tuple[str, str]:
    """The (access, refresh) pair that rotating the raw token `parent_token` yields.

    Deterministic, so a retried refresh inside the grace window can return the
    same pair without the server storing a raw token. The input is the raw
    token, which the database never holds (only its hash), and the key is a
    subkey of the secret used for nothing else.
    """
    key = hmac.new(secret, b"refresh-successor", hashlib.sha256).digest()
    parent = parent_token.encode("utf-8", "surrogatepass")

    def part(label: bytes, prefix: str) -> str:
        digest = hmac.new(key, label + b":" + parent, hashlib.sha256)
        return prefix + base64.urlsafe_b64encode(digest.digest()).decode().rstrip("=")

    return part(b"access", "bra_"), part(b"refresh", "brr_")


def tombstone_hmac(secret: bytes, email_canonical: str) -> bytes:
    """Keyed hash of a canonical email for the deleted-account tombstone.

    Under a subkey of the secret used for nothing else, so it matches nothing
    else the server stores. Rotating the secret makes old tombstones unmatchable.
    """
    key = hmac.new(secret, b"account-tombstone", hashlib.sha256).digest()
    return hmac.new(key, email_canonical.encode(), hashlib.sha256).digest()


def github_tombstone_hmac(secret: bytes, github_id: int) -> bytes:
    """Tombstone key of a GitHub account: `HMAC(subkey, "github:" + id)`.

    An email always contains "@", so the two kinds of key cannot collide.
    """
    return tombstone_hmac(secret, f"github:{github_id}")


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

"""Ephemeral local session keys: the raw Claude session ID never leaves the hook."""

import hashlib
import hmac
import os
from pathlib import Path

SALT_BYTES = 32
KEY_CHARS = 16


def derive_key(session_id: str, salt: bytes) -> str:
    digest = hmac.new(salt, session_id.encode("utf-8"), hashlib.sha256).hexdigest()
    return digest[:KEY_CHARS]


def load_or_create_salt(path: Path) -> bytes:
    """Return the salt at `path`, creating it atomically (0600) if absent."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return _read_full(path)
    salt = os.urandom(SALT_BYTES)
    with os.fdopen(fd, "wb") as f:
        f.write(salt)
    return salt


def _read_full(path: Path) -> bytes:
    # The creator may not have written yet; wait briefly for the full salt.
    for _ in range(200):
        data = path.read_bytes()
        if len(data) == SALT_BYTES:
            return data
        os.sched_yield()
    raise OSError("salt file incomplete")

"""Ephemeral local session keys: the raw Claude session ID never leaves the hook."""

import hashlib
import hmac
import os
import tempfile
from pathlib import Path

SALT_BYTES = 32
KEY_CHARS = 16


def derive_key(session_id: str, salt: bytes) -> str:
    digest = hmac.new(salt, session_id.encode("utf-8"), hashlib.sha256).hexdigest()
    return digest[:KEY_CHARS]


def load_or_create_salt(path: Path) -> bytes:
    """Return the salt at `path`, creating it atomically (0600) if absent.

    The salt is written to a temp file and hard-linked into place, so a reader
    never sees a partial file. A leftover file shorter than SALT_BYTES (from an
    older, non-atomic creator) is replaced once.
    """
    for _ in range(2):
        existing = _read_if_present(path)
        if existing is not None and len(existing) == SALT_BYTES:
            return existing
        if existing is not None:
            path.unlink(missing_ok=True)
        salt = _create(path)
        if salt is not None:
            return salt
    existing = _read_if_present(path)
    if existing is not None and len(existing) == SALT_BYTES:
        return existing
    raise OSError("salt file unusable")


def _read_if_present(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _create(path: Path) -> bytes | None:
    """Link a fresh salt into place; None if another process got there first."""
    salt = os.urandom(SALT_BYTES)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".salt-")  # mode 0600
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(salt)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(tmp, path)
        except FileExistsError:
            return None
        return salt
    finally:
        os.unlink(tmp)

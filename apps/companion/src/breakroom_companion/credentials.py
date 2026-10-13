"""Refresh-token storage: OS keyring, or a 0600 file fallback. Owned by M3-04.

The access token is never stored. Nothing here logs or raises with token text.
"""

import contextlib
import hashlib
import json
import os
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import keyring
import keyring.errors
from keyring.backends import fail, null

from breakroom_shared.paths import credentials_path, state_dir

_VERSION = 1
_SERVICE = "breakroom"


@dataclass(frozen=True)
class StoredCredentials:
    refresh_token: str
    refresh_expires_at: datetime  # aware UTC


class CredentialStore(Protocol):
    def load(self) -> StoredCredentials | None: ...
    def save(self, creds: StoredCredentials) -> bool: ...
    def clear(self) -> bool: ...
    @property
    def encrypted(self) -> bool: ...


def _payload(creds: StoredCredentials, origin: str | None) -> dict[str, object]:
    data: dict[str, object] = {
        "version": _VERSION,
        "refresh_token": creds.refresh_token,
        "refresh_expires_at": creds.refresh_expires_at.isoformat(),
    }
    if origin is not None:
        data["origin"] = origin
    return data


def _parse(data: object, origin: str | None) -> StoredCredentials | None:
    """None for anything that is not a usable, unexpired version-1 record."""
    if not isinstance(data, dict):
        return None
    if type(data.get("version")) is not int or data["version"] != _VERSION:
        return None
    if origin is not None and data.get("origin") != origin:
        return None
    token = data.get("refresh_token")
    raw = data.get("refresh_expires_at")
    if not isinstance(token, str) or not token or not isinstance(raw, str):
        return None
    try:
        when = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if when.tzinfo is None or when.utcoffset() is None:
        return None
    when = when.astimezone(UTC)
    if when <= datetime.now(UTC):
        return None
    return StoredCredentials(refresh_token=token, refresh_expires_at=when)


class KeyringCredentialStore:
    """Secret Service (or another real keyring backend)."""

    def __init__(self, origin: str) -> None:
        # Keyed by origin and state dir so two companions do not share a login.
        digest = hashlib.sha256(str(state_dir()).encode()).hexdigest()[:16]
        self._username = f"{origin}|{digest}"

    @property
    def encrypted(self) -> bool:
        return True

    def load(self) -> StoredCredentials | None:
        try:
            raw = keyring.get_password(_SERVICE, self._username)
            if raw is None:
                return None
            return _parse(json.loads(raw), None)
        except Exception:
            return None

    def save(self, creds: StoredCredentials) -> bool:
        try:
            keyring.set_password(
                _SERVICE, self._username, json.dumps(_payload(creds, None))
            )
        except Exception:
            return False
        return True

    def clear(self) -> bool:
        try:
            keyring.delete_password(_SERVICE, self._username)
        except keyring.errors.PasswordDeleteError:
            return True  # nothing stored is the desired end state
        except Exception:
            return False
        return True


class FileCredentialStore:
    """Plain 0600 file in the private state dir. Not encrypted."""

    def __init__(self, path: Path, origin: str) -> None:
        self._path = path
        self._origin = origin

    @property
    def encrypted(self) -> bool:
        return False

    def load(self) -> StoredCredentials | None:
        try:
            # O_NONBLOCK: a FIFO planted at the path must not hang start-up.
            fd = os.open(self._path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError:
            return None
        try:
            with os.fdopen(fd, encoding="utf-8") as f:
                if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                    return None
                data = json.load(f)
            return _parse(data, self._origin)
        except (OSError, ValueError, OverflowError, RecursionError):
            return None

    def save(self, creds: StoredCredentials) -> bool:
        tmp = self._path.with_name(f"{self._path.name}.{os.getpid()}.tmp")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(_payload(creds, self._origin), f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._path)
        except OSError:
            with contextlib.suppress(OSError):
                tmp.unlink()
            return False
        return True

    def clear(self) -> bool:
        try:
            self._path.unlink()
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return True


def _keyring_usable() -> bool:
    try:
        backend = keyring.get_keyring()
        if isinstance(backend, fail.Keyring | null.Keyring):
            return False
        # keyrings.alt plaintext/obfuscated backends are never "encrypted".
        if type(backend).__module__.startswith("keyrings.alt"):
            return False
        keyring.get_password(_SERVICE, "probe")
    except Exception:
        return False
    return True


def default_credential_store(origin: str) -> CredentialStore:
    if _keyring_usable():
        return KeyringCredentialStore(origin)
    return FileCredentialStore(credentials_path(), origin)

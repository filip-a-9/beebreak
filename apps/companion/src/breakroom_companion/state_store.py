"""Persisted consent state: suggestions on/off and cooldown (spec §6.2).

Contract frozen by the M2 README;
"""

import contextlib
import json
import os
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from breakroom_shared.presence import StackTag


@dataclass(frozen=True)
class PersistedState:
    enabled: bool = True
    # Aware UTC.
    cooldown_until: datetime | None = None
    # Chosen in Settings, sent only with consent (M4-06). At most 5, vocabulary.
    stack_tags: tuple[StackTag, ...] = ()


class StateStore(Protocol):
    def load(self) -> PersistedState: ...

    def save(self, state: PersistedState) -> bool:
        """Persist; False on failure (the caller keeps running in memory)."""
        ...


_VERSION = 1


class FileStateStore:
    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> PersistedState:
        """Missing file: defaults. Anything unusable: suggestions off."""
        try:
            # O_NONBLOCK: a FIFO planted at the path must not hang start-up.
            fd = os.open(self._path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return PersistedState()
        except OSError:
            return PersistedState(enabled=False)
        try:
            with os.fdopen(fd, encoding="utf-8") as f:
                if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                    return PersistedState(enabled=False)
                data = json.load(f)
            return _parse(data)
        except (OSError, ValueError, OverflowError, RecursionError):
            return PersistedState(enabled=False)

    def save(self, state: PersistedState) -> bool:
        payload = {
            "version": _VERSION,
            "enabled": state.enabled,
            "cooldown_until": (
                state.cooldown_until.isoformat() if state.cooldown_until else None
            ),
        }
        tmp = self._path.with_name(f"{self._path.name}.{os.getpid()}.tmp")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._path)
        except OSError:
            with contextlib.suppress(OSError):
                tmp.unlink()
            return False
        return True


def _parse(data: object) -> PersistedState:
    """Raise ValueError for anything that is not a well-formed version-1 file."""
    # type() check: True and 1.0 compare equal to 1.
    if not isinstance(data, dict) or type(data.get("version")) is not int:
        raise ValueError("unsupported state file")
    if data["version"] != _VERSION:
        raise ValueError("unsupported state file")
    enabled = data.get("enabled")
    if not isinstance(enabled, bool):
        raise ValueError("bad enabled flag")
    raw = data.get("cooldown_until")
    if raw is None:
        return PersistedState(enabled=enabled)
    if not isinstance(raw, str):
        raise ValueError("bad cooldown")
    when = datetime.fromisoformat(raw)
    if when.tzinfo is None or when.utcoffset() is None:
        raise ValueError("naive cooldown")
    return PersistedState(enabled=enabled, cooldown_until=when.astimezone(UTC))

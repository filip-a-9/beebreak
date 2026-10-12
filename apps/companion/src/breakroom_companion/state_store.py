"""Persisted consent state: suggestions on/off and cooldown (spec §6.2).

Contract frozen by the M2 README; FileStateStore implemented by M2-04.
"""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class PersistedState:
    enabled: bool = True
    # Aware UTC.
    cooldown_until: datetime | None = None


class StateStore(Protocol):
    def load(self) -> PersistedState: ...

    def save(self, state: PersistedState) -> bool:
        """Persist; False on failure (the caller keeps running in memory)."""
        ...


class FileStateStore:
    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> PersistedState:
        raise NotImplementedError

    def save(self, state: PersistedState) -> bool:
        raise NotImplementedError

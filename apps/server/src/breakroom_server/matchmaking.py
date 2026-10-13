"""Matchmaker: expiry, match ends, pairing and retention purge. Owned by M4-03.

`run_once()` does one pass and notifies users through the `Notifier`; `run()`
repeats it on every `kick()` and every `match_sweep_interval_s`. Single server
instance (spec §9.3); uniqueness is enforced by the database, not by the lock.
"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from breakroom_server.config import ServerConfig
from breakroom_server.db import SessionFactory
from breakroom_server.notify import Notifier


@dataclass(frozen=True)
class MatchCreated:
    match_id: UUID
    users: tuple[UUID, UUID]


class Matchmaker:
    def __init__(
        self,
        factory: SessionFactory,
        config: ServerConfig,
        clock: Callable[[], datetime],
        notifier: Notifier,
    ) -> None:
        self._factory = factory
        self._config = config
        self._clock = clock
        self._notifier = notifier

    def kick(self) -> None:
        """Wake the loop for a pass soon. Thread-safe. M4-03."""

    def run_once(self) -> list[MatchCreated]:
        """Expire, end, pair, purge; notify. Returns the matches created. M4-03."""
        raise NotImplementedError

    async def run(self, stop: asyncio.Event) -> None:
        """Loop until `stop` is set: wait for a kick or the sweep interval. M4-03."""
        await stop.wait()

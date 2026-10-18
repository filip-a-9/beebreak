"""Recording stand-ins for the M4 Notifier and Matchmaker.

Install them on a test app after `create_app`:
`app.state.notifier = RecordingNotifier()`, `app.state.matchmaker =
RecordingMatchmaker()`. Handlers read both from `app.state` per request.
"""

import asyncio
import threading
from uuid import UUID

from pydantic import BaseModel

from beebreak_server.matchmaking import MatchCreated


class RecordingNotifier:
    """Notifier that records frames; `connected` decides is_user_connected."""

    def __init__(self, connected: set[UUID] | None = None) -> None:
        self._lock = threading.Lock()
        self.connected: set[UUID] = set(connected or ())
        self.sent: list[tuple[UUID, BaseModel]] = []

    def send_to_user(self, user_id: UUID, frame: BaseModel) -> None:
        with self._lock:
            self.sent.append((user_id, frame))

    def is_user_connected(self, user_id: UUID) -> bool:
        with self._lock:
            return user_id in self.connected

    def frames_for(self, user_id: UUID) -> list[BaseModel]:
        with self._lock:
            return [frame for uid, frame in self.sent if uid == user_id]

    def clear(self) -> None:
        with self._lock:
            self.sent.clear()


class RecordingMatchmaker:
    """Matchmaker that counts kicks and passes and does nothing else."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.kicks = 0
        self.passes = 0

    def kick(self) -> None:
        with self._lock:
            self.kicks += 1

    def run_once(self) -> list[MatchCreated]:
        with self._lock:
            self.passes += 1
        return []

    async def run(self, stop: asyncio.Event) -> None:
        await stop.wait()

"""Realtime fan-out seen from the matchmaker and the HTTP handlers (M4).

`realtime.Hub` implements `Notifier` (M4-04); tests use a recording fake.
Callers pass a complete frame built with `make_frame`; the notifier fills in
nothing. `make_frame` lives here (re-exported by `realtime`) so that `queue`
and `matchmaking` need not import `realtime`, which imports `queue`.
"""

from datetime import datetime
from typing import Any, Protocol, get_args
from uuid import UUID, uuid4

from pydantic import BaseModel


def make_frame[F: BaseModel](cls: type[F], now: datetime, **fields: Any) -> F:
    """A server frame with its `type`, `version`, a fresh `event_id` and
    `server_time=now` (the injected clock's time)."""
    (frame_type,) = get_args(cls.model_fields["type"].annotation)
    return cls(type=frame_type, version=1, event_id=uuid4(), server_time=now, **fields)


class Notifier(Protocol):
    def send_to_user(self, user_id: UUID, frame: BaseModel) -> None:
        """Queue `frame` for every live socket of the user. Any thread; best
        effort: never blocks, never raises."""

    def is_user_connected(self, user_id: UUID) -> bool:
        """True while at least one device of the user has an authenticated
        socket."""

"""Simple M1 activity heuristic (spec §7.1). Pure; no clocks, no I/O.

A "run" is a stretch of agent activity in one session. turn_stopped is never
treated as task completion; it only ends the run.
"""

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from breakroom_companion.trigger_config import TriggerConfig
from breakroom_shared.events import NormalizedEvent

_TOOL_EVENTS = frozenset({"tool_started", "tool_completed"})
_END_EVENTS = frozenset({"turn_stopped", "session_closed"})
_SEEN_LIMIT = 256


@dataclass
class _Run:
    start: datetime
    last: datetime
    tool_events: int = 0


class ActivityTrigger:
    def __init__(self, config: TriggerConfig) -> None:
        self._config = config
        self._runs: dict[str, _Run] = {}
        # Sessions whose current turn already produced a suggestion (spec §11:
        # no repeated invitations after decline). Only a new turn re-arms them.
        self._spent: set[str] = set()
        self._seen: set[UUID] = set()
        self._seen_order: deque[UUID] = deque()

    def _remember(self, event_id: UUID) -> bool:
        """Record an id; return False if it was already seen."""
        if event_id in self._seen:
            return False
        self._seen.add(event_id)
        self._seen_order.append(event_id)
        if len(self._seen_order) > _SEEN_LIMIT:
            self._seen.discard(self._seen_order.popleft())
        return True

    def spend(self, session_key: str) -> None:
        """Stop this session from qualifying again until its next turn."""
        self._spent.add(session_key)

    def feed(self, event: NormalizedEvent) -> bool:
        """Consume an event; True when its session's run is now qualifying."""
        if not self._remember(event.event_id):
            return False
        key, now = event.local_session_key, event.observed_at
        run = self._runs.get(key)
        if run is not None and now < run.last:
            return False  # out of order: ignored for timing

        if event.type in _END_EVENTS:
            self._runs.pop(key, None)
            if event.type == "session_closed":
                self._spent.discard(key)
            return False
        if event.type == "turn_submitted":
            self._runs[key] = _Run(start=now, last=now)
            self._spent.discard(key)
            return False
        if event.type not in _TOOL_EVENTS:
            return False

        gap = timedelta(seconds=self._config.quiet_gap_s)
        if run is None or now - run.last >= gap:
            run = self._runs[key] = _Run(start=now, last=now)
        run.last = now
        run.tool_events += 1
        return (
            key not in self._spent
            and now - run.start >= timedelta(seconds=self._config.activity_threshold_s)
            and run.tool_events >= self._config.min_tool_events
        )

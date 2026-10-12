"""Per-session activity state machine (spec §6.1). Pure; no clocks, no I/O.

Contract frozen by the M2 README; behaviour implemented by M2-02.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from breakroom_companion.config import TriggerConfig
from breakroom_shared.events import NormalizedEvent
from breakroom_shared.ipc import ActivityState


@dataclass(frozen=True)
class SessionActivity:
    state: ActivityState
    # Increments whenever a new run (stretch of "active") starts.
    run_id: int
    # Set only while state == "active".
    run_started_at: datetime | None
    # tool_started + tool_completed events in the current run.
    run_tool_events: int
    # Started minus completed, floored at 0.
    open_tools: int
    # Created minus completed, floored at 0. Per session, not per run.
    open_tasks: int
    # Seconds between task progress points, last 8, oldest first.
    task_intervals_s: tuple[float, ...]
    last_event_at: datetime


class ActivityTracker:
    def __init__(self, config: TriggerConfig) -> None:
        self._config = config

    def feed(self, event: NormalizedEvent) -> bool:
        """Apply an event; False if it was a duplicate or out of order."""
        raise NotImplementedError

    def sessions(self, now: datetime) -> Mapping[str, SessionActivity]:
        """Snapshot of every known session with staleness applied at ``now``."""
        raise NotImplementedError

    def aggregate(self, now: datetime) -> ActivityState:
        """One state for all sessions; waiting_input takes precedence."""
        raise NotImplementedError

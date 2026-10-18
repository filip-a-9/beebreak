"""Per-session activity state machine (spec §6.1). Pure; no clocks, no I/O.

Contract frozen by the M2 README; behaviour implemented by M2-02.
"""

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from uuid import UUID

from beebreak_companion.config import TriggerConfig
from beebreak_shared.events import NormalizedEvent
from beebreak_shared.ipc import ActivityState

_SEEN_LIMIT = 256
_MAX_INTERVALS = 8


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


@dataclass
class _Session:
    state: ActivityState
    last_event_at: datetime
    run_id: int = 0
    run_started_at: datetime | None = None
    run_tool_events: int = 0
    open_tools: int = 0
    open_tasks: int = 0
    last_progress_at: datetime | None = None
    intervals: deque[float] = field(
        default_factory=lambda: deque(maxlen=_MAX_INTERVALS)
    )

    def start_run(self, at: datetime) -> None:
        self.state = "active"
        self.run_id += 1
        self.run_started_at = at
        self.run_tool_events = 0

    def progress(self, at: datetime) -> None:
        if self.last_progress_at is not None:
            self.intervals.append((at - self.last_progress_at).total_seconds())
        self.last_progress_at = at


class ActivityTracker:
    def __init__(self, config: TriggerConfig) -> None:
        self._config = config
        self._sessions: dict[str, _Session] = {}
        # Closed session -> time of its close, so a late async event stamped
        # before the close cannot recreate it. Bounded by forget_after_s.
        self._closed: dict[str, datetime] = {}
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

    def _apply_staleness(self, sess: _Session, now: datetime) -> None:
        """Move a silent session to unknown. Idempotent; derived from the clock
        argument only, so reads and feeds agree."""
        silence = now - sess.last_event_at
        cfg = self._config
        if sess.state == "waiting_input":
            limit = cfg.waiting_max_s
        elif sess.state == "active" and sess.open_tools == 0:
            limit = cfg.stale_after_s
        elif sess.state == "active":
            limit = cfg.open_tool_max_s
        else:
            return
        if silence >= timedelta(seconds=limit):
            sess.state = "unknown"
            sess.run_started_at = None
            # A lost completion must not leak into the next run.
            sess.open_tools = 0

    def _forget_silent(self, now: datetime) -> None:
        limit = timedelta(seconds=self._config.forget_after_s)
        for key in [
            k for k, s in self._sessions.items() if now - s.last_event_at >= limit
        ]:
            del self._sessions[key]
        for key in [k for k, t in self._closed.items() if now - t >= limit]:
            del self._closed[key]

    def feed(self, event: NormalizedEvent) -> bool:
        """Apply an event; False if it was a duplicate or out of order."""
        if not self._remember(event.event_id):
            return False
        key, at, kind = event.local_session_key, event.observed_at, event.type
        self._forget_silent(at)
        sess = self._sessions.get(key)
        last = sess.last_event_at if sess is not None else self._closed.get(key)
        if last is not None and at < last:
            return False
        if kind == "heartbeat":
            return True  # liveness only; must not keep a session fresh
        if kind == "session_closed":
            if self._sessions.pop(key, None) is not None:
                self._closed[key] = at
            return True
        self._closed.pop(key, None)

        if sess is None:
            sess = self._sessions[key] = _Session(state="unknown", last_event_at=at)
        self._apply_staleness(sess, at)
        sess.last_event_at = at

        if kind == "session_opened":
            sess.state = "idle"
            sess.run_started_at = None
            sess.open_tools = 0
        elif kind == "turn_submitted":
            sess.start_run(at)
            sess.open_tools = 0
        elif kind in ("tool_started", "tool_completed"):
            if sess.state != "active":
                sess.start_run(at)
            if kind == "tool_started":
                sess.open_tools += 1
            else:
                sess.open_tools = max(0, sess.open_tools - 1)
            sess.run_tool_events += 1
        elif kind in ("task_created", "task_completed"):
            if sess.state not in ("active", "waiting_input"):
                sess.start_run(at)
            if kind == "task_created":
                if sess.open_tasks == 0:
                    sess.progress(at)
                sess.open_tasks += 1
            else:
                sess.open_tasks = max(0, sess.open_tasks - 1)
                sess.progress(at)
        elif kind == "waiting_input":
            sess.state = "waiting_input"
            sess.run_started_at = None
        elif kind == "turn_stopped":
            # The turn ended; that says nothing about the task being complete.
            sess.state = "idle"
            sess.run_started_at = None
            sess.open_tools = 0
        return True

    def sessions(self, now: datetime) -> Mapping[str, SessionActivity]:
        """Snapshot of every known session with staleness applied at ``now``."""
        self._forget_silent(now)
        out: dict[str, SessionActivity] = {}
        for key, s in self._sessions.items():
            view = replace(s)  # staleness must not mutate stored state
            self._apply_staleness(view, now)
            out[key] = SessionActivity(
                state=view.state,
                run_id=view.run_id,
                run_started_at=view.run_started_at,
                run_tool_events=view.run_tool_events,
                open_tools=view.open_tools,
                open_tasks=view.open_tasks,
                task_intervals_s=tuple(view.intervals),
                last_event_at=view.last_event_at,
            )
        return out

    def aggregate(self, now: datetime) -> ActivityState:
        """One state for all sessions; waiting_input takes precedence."""
        states = {s.state for s in self.sessions(now).values()}
        for state in ("waiting_input", "active", "idle"):
            if state in states:
                return state
        return "unknown"

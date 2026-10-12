"""Invitation controller: turns events and clicks into window states.

Pure and Qt-free. The app calls every method on the Qt main thread; each returns
the new InvitationState, or None when the state is unchanged. Eligibility
(consent, cooldown, one outstanding suggestion, spent runs) lives here; the
decision engine only judges the sessions it is shown.
"""

import sys
from datetime import datetime, timedelta
from typing import Literal

from breakroom_companion.activity import ActivityTracker
from breakroom_companion.config import TriggerConfig
from breakroom_companion.decision import DecisionEngine, HeuristicEngine
from breakroom_companion.state_store import FileStateStore, PersistedState, StateStore
from breakroom_shared.events import NormalizedEvent
from breakroom_shared.ipc import ActivityState, InvitationState
from breakroom_shared.paths import state_path
from breakroom_shared.presence import RemainingTimeBucket

Outcome = Literal["accepted", "declined"]


class _UnsafeStore:
    """Stand-in when the state directory is unsafe: suggestions stay off."""

    def load(self) -> PersistedState:
        return PersistedState(enabled=False)

    def save(self, state: PersistedState) -> bool:
        return False


def _default_store() -> StateStore:
    try:
        return FileStateStore(state_path())
    except (RuntimeError, OSError):  # UnsafeRuntimeDir, or Path.home() failing
        # Fixed text only. Without it, consent silently stops persisting.
        print(
            "breakroom-companion: unsafe state directory; settings not saved",
            file=sys.stderr,
        )
        return _UnsafeStore()


class Controller:
    def __init__(
        self,
        config: TriggerConfig | None = None,
        store: StateStore | None = None,
        engine: DecisionEngine | None = None,
    ) -> None:
        self._config = config or TriggerConfig.from_env()
        self._store = store if store is not None else _default_store()
        self._engine = engine or HeuristicEngine(self._config)
        self._tracker = ActivityTracker(self._config)
        persisted = self._store.load()
        self._enabled = persisted.enabled
        self._cooldown_until: datetime | None = persisted.cooldown_until
        self.state: InvitationState = "idle" if persisted.enabled else "disabled"
        self.last_event_at: datetime | None = None
        self.estimate: RemainingTimeBucket | None = None
        self._suggested_at: datetime | None = None
        # Latest wall-clock tick. Events can be stamped earlier (late IPC
        # delivery); a suggestion's timeout must not start before this.
        self._last_tick: datetime | None = None
        # (session, run_id) pairs that already produced a suggestion: one
        # invitation per run, however it ended (spec §11).
        self._spent: set[tuple[str, int]] = set()

    def activity(self, now: datetime) -> ActivityState:
        return self._tracker.aggregate(now)

    def _persist(self) -> None:
        # A failed save is not fatal: the in-memory state keeps working.
        self._store.save(
            PersistedState(enabled=self._enabled, cooldown_until=self._cooldown_until)
        )

    def _end_suggestion(self) -> None:
        self.state = "idle"
        self.estimate = None
        self._suggested_at = None

    def _start_cooldown(self, now: datetime) -> None:
        self._end_suggestion()
        self._cooldown_until = now + timedelta(seconds=self._config.cooldown_s)
        self._persist()

    def _step(self, now: datetime, *, timeout: bool) -> None:
        """Timeout, withdraw, then evaluate. Mutates self.state."""
        if (
            timeout
            and self.state == "suggested"
            and self._suggested_at is not None
            and now - self._suggested_at
            >= timedelta(seconds=self._config.suggestion_timeout_s)
        ):
            self._start_cooldown(now)
        sessions = self._tracker.sessions(now)
        if self.state == "suggested":
            if any(s.state == "waiting_input" for s in sessions.values()):
                # The user is needed elsewhere: withdraw, no cooldown. The run
                # stays spent, so it is not re-invited when the wait ends.
                self._end_suggestion()
            return
        if self.state != "idle":
            return
        self._spent = {p for p in self._spent if p[0] in sessions}
        if self._cooldown_until is not None and now < self._cooldown_until:
            return
        decision = self._engine.decide(sessions, now, exclude=self._spent)
        if (
            decision.result.invite_suitable
            and decision.session_key is not None
            and decision.run_id is not None
        ):
            self._spent.add((decision.session_key, decision.run_id))
            self.state = "suggested"
            self.estimate = decision.result.remaining_time_bucket
            self._suggested_at = (
                max(now, self._last_tick) if self._last_tick is not None else now
            )

    def on_event(self, event: NormalizedEvent) -> InvitationState | None:
        self.last_event_at = event.observed_at
        before = self.state
        self._tracker.feed(event)
        self._step(event.observed_at, timeout=False)
        return self.state if self.state != before else None

    def on_outcome(self, outcome: Outcome, now: datetime) -> InvitationState | None:
        if self.state != "suggested":
            return None
        self._start_cooldown(now)
        return "idle"

    def set_enabled(self, enabled: bool) -> InvitationState | None:
        self._enabled = enabled
        self._persist()
        new: InvitationState = "idle" if enabled else "disabled"
        if new == self.state:
            return None
        self.state = new
        self.estimate = None
        self._suggested_at = None
        return new

    def tick(self, now: datetime) -> InvitationState | None:
        """Called about once a second: timeout, withdrawal, silent-tool invites."""
        before = self.state
        self._last_tick = now
        self._step(now, timeout=True)
        return self.state if self.state != before else None

"""Invitation controller: turns events and clicks into window states.

Pure and Qt-free. The app calls every method on the Qt main thread; each returns
the new InvitationState, or None when the state is unchanged. Eligibility
(consent, cooldown, one outstanding suggestion, spent runs) lives here; the
decision engine only judges the sessions it is shown.
"""

import sys
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID

from beebreak_companion.activity import ActivityTracker
from beebreak_companion.config import TriggerConfig
from beebreak_companion.decision import DecisionEngine, HeuristicEngine
from beebreak_companion.state_store import (
    FileStateStore,
    PersistedState,
    StateStore,
    normalize_tags,
)
from beebreak_shared.events import NormalizedEvent
from beebreak_shared.ipc import ActivityState, InvitationState
from beebreak_shared.paths import state_path
from beebreak_shared.presence import RemainingTimeBucket, StackTag

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
            "beebreak-companion: unsafe state directory; settings not saved",
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
        self._stack_tags: tuple[StackTag, ...] = normalize_tags(persisted.stack_tags)
        self._read: tuple[UUID | None, int] = (persisted.read_match, persisted.read_seq)
        # Suggestions need a connected, set-up account and no break in progress
        # (M4). The app pushes both; tests of the bare engine keep the default.
        self._ready = True
        self._break_active = False
        self.state: InvitationState = "idle" if persisted.enabled else "disabled"
        # True when the last offer ended by timing out; the window says so.
        self.offer_expired = False
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
            PersistedState(
                enabled=self._enabled,
                cooldown_until=self._cooldown_until,
                stack_tags=self._stack_tags,
                read_match=self._read[0],
                read_seq=self._read[1],
            )
        )

    def _end_suggestion(self, *, expired: bool = False) -> None:
        # `expired`: nobody answered before the timeout (not a withdrawal).
        self.offer_expired = expired
        self.state = "idle"
        self.estimate = None
        self._suggested_at = None

    def _start_cooldown(self, now: datetime, *, expired: bool = False) -> None:
        self._end_suggestion(expired=expired)
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
            self._start_cooldown(now, expired=True)
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
        if not self._ready or self._break_active:
            return
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
        self.offer_expired = False
        self.estimate = None
        self._suggested_at = None
        return new

    @property
    def stack_tags(self) -> tuple[StackTag, ...]:
        return self._stack_tags

    def set_stack_tags(self, tags: tuple[StackTag, ...]) -> None:
        """Remember the user's stack tags locally (vocabulary only, at most 5)."""
        self._stack_tags = normalize_tags(tags)
        self._persist()

    def read_marker(self, match_id: UUID) -> int:
        """Highest peer message seq the user has read in this chat (0: none)."""
        return self._read[1] if self._read[0] == match_id else 0

    def set_read_marker(self, match_id: UUID, seq: int) -> None:
        """Remember how far the user has read, so a restart keeps the count."""
        if self._read != (match_id, seq):
            self._read = (match_id, seq)
            self._persist()

    def _withdraw(self) -> InvitationState | None:
        """Take back an outstanding suggestion without a cooldown."""
        if self.state != "suggested":
            return None
        self._end_suggestion()
        return "idle"

    def set_ready(self, ready: bool) -> InvitationState | None:
        """Connected with nickname and consent saved. Not ready: no invitations."""
        self._ready = ready
        return None if ready else self._withdraw()

    def set_break_active(
        self, active: bool, now: datetime, *, cooldown: bool = True
    ) -> InvitationState | None:
        """A search or match is in progress. Its end starts the cooldown, unless
        `cooldown` is False (no break happened, e.g. the join failed)."""
        was, self._break_active = self._break_active, active
        if active:
            return self._withdraw()
        if was and cooldown:
            self._cooldown_until = now + timedelta(seconds=self._config.cooldown_s)
            self._persist()
        return None

    def tick(self, now: datetime) -> InvitationState | None:
        """Called about once a second: timeout, withdrawal, silent-tool invites."""
        before = self.state
        self._last_tick = now
        self._step(now, timeout=True)
        return self.state if self.state != before else None

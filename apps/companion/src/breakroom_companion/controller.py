"""Invitation controller: turns events and clicks into window states. Owned by M1-05.

Pure and Qt-free. The app calls every method on the Qt main thread; each returns
the new InvitationState, or None when the state is unchanged.
"""

from datetime import datetime, timedelta
from typing import Literal

from breakroom_companion.config import TriggerConfig
from breakroom_companion.trigger import ActivityTrigger
from breakroom_shared.events import NormalizedEvent
from breakroom_shared.ipc import InvitationState

Outcome = Literal["accepted", "declined"]


class Controller:
    def __init__(self, config: TriggerConfig | None = None) -> None:
        self._config = config or TriggerConfig.from_env()
        self._trigger = ActivityTrigger(self._config)
        self.state: InvitationState = "idle"
        self.last_event_at: datetime | None = None
        self._suggested_at: datetime | None = None
        self._cooldown_until: datetime | None = None

    def _start_cooldown(self, now: datetime) -> None:
        self._cooldown_until = now + timedelta(seconds=self._config.cooldown_s)
        self._suggested_at = None
        self.state = "idle"

    def on_event(self, event: NormalizedEvent) -> InvitationState | None:
        self.last_event_at = event.observed_at
        qualifying = self._trigger.feed(event)  # always feed, to keep runs current
        if not qualifying or self.state != "idle":
            return None
        if (
            self._cooldown_until is not None
            and event.observed_at < self._cooldown_until
        ):
            return None
        self._trigger.spend(event.local_session_key)
        self.state = "suggested"
        self._suggested_at = event.observed_at
        return "suggested"

    def on_outcome(self, outcome: Outcome, now: datetime) -> InvitationState | None:
        if self.state != "suggested":
            return None
        self._start_cooldown(now)
        return "idle"

    def set_enabled(self, enabled: bool) -> InvitationState | None:
        new: InvitationState = "idle" if enabled else "disabled"
        if new == self.state:
            return None
        self.state = new
        self._suggested_at = None
        return new

    def tick(self, now: datetime) -> InvitationState | None:
        """Called about once a second; handles suggestion timeout."""
        if self.state != "suggested" or self._suggested_at is None:
            return None
        if now - self._suggested_at < timedelta(
            seconds=self._config.suggestion_timeout_s
        ):
            return None
        self._start_cooldown(now)
        return "idle"

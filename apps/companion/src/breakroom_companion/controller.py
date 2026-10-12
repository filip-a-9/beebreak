"""Invitation controller: turns events and clicks into window states. Owned by M1-05.

Pure and Qt-free. The app calls every method on the Qt main thread; each returns
the new InvitationState, or None when the state is unchanged.
"""

from datetime import datetime
from typing import Literal

from breakroom_shared.events import NormalizedEvent
from breakroom_shared.ipc import InvitationState

Outcome = Literal["accepted", "declined"]


class Controller:
    def __init__(self) -> None:
        self.state: InvitationState = "idle"
        self.last_event_at: datetime | None = None

    def on_event(self, event: NormalizedEvent) -> InvitationState | None:
        self.last_event_at = event.observed_at
        return None  # M1-05 stub: never suggests.

    def on_outcome(self, outcome: Outcome, now: datetime) -> InvitationState | None:
        return None  # M1-05 stub.

    def set_enabled(self, enabled: bool) -> InvitationState | None:
        new: InvitationState = "idle" if enabled else "disabled"
        if new == self.state:
            return None
        self.state = new
        return new

    def tick(self, now: datetime) -> InvitationState | None:
        """Called about once a second; handles suggestion timeout."""
        return None  # M1-05 stub.

"""Break (search and match) state of the companion. Owned by M4-05.

Pure and Qt-free: `RemoteWorker` reduces server replies and frames into a
`BreakState` and emits it as `break_changed`.
"""

from dataclasses import dataclass
from typing import Literal

from breakroom_shared.api import MatchView, QueueEntryView

BreakPhase = Literal[
    "none", "joining", "searching", "matched", "nobody_available", "ended"
]


@dataclass(frozen=True)
class BreakState:
    phase: BreakPhase = "none"
    entry: QueueEntryView | None = None
    match: MatchView | None = None

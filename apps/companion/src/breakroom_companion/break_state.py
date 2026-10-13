"""Break (search and match) state of the companion. Owned by M4-05.

Pure and Qt-free: `RemoteWorker` reduces server replies and frames into a
`BreakState` and emits it as `break_changed`.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from breakroom_shared.api import (
    MatchView,
    QueueEntryView,
    WsMatchCreated,
    WsMatchEnded,
    WsQueueEnded,
    WsQueueJoined,
)

BreakPhase = Literal[
    "none", "joining", "searching", "matched", "nobody_available", "ended"
]


@dataclass(frozen=True)
class BreakState:
    phase: BreakPhase = "none"
    entry: QueueEntryView | None = None
    match: MatchView | None = None


# --- events ---


@dataclass(frozen=True)
class Joining:
    """A join request is about to be sent."""


@dataclass(frozen=True)
class Joined:
    entry: QueueEntryView


@dataclass(frozen=True)
class JoinFailed:
    code: str


@dataclass(frozen=True)
class Sync:
    queue: QueueEntryView | None
    match: MatchView | None
    # True while this worker's own join request is outstanding: an empty sync
    # may predate it, so `joining` is kept until the reply arrives.
    join_in_flight: bool = False


@dataclass(frozen=True)
class Frame:
    frame: WsQueueJoined | WsQueueEnded | WsMatchCreated | WsMatchEnded


@dataclass(frozen=True)
class SignedOut:
    pass


@dataclass(frozen=True)
class Dismiss:
    pass


BreakEvent = Joining | Joined | JoinFailed | Sync | Frame | SignedOut | Dismiss

# The phases that may start a new search.
_IDLE = ("none", "nobody_available", "ended")
_NONE = BreakState()


def reduce(state: BreakState, event: BreakEvent, now: datetime) -> BreakState:
    """The next state. An event that makes no sense in `state` returns it as is.

    `now` is part of the contract for callers that need a clock; no rule uses it.
    """
    del now
    match event:
        case SignedOut():
            return _NONE
        case Dismiss():
            if state.phase in ("nobody_available", "ended"):
                return _NONE
            return state
        case Joining():
            return BreakState(phase="joining") if state.phase in _IDLE else state
        case Joined(entry=entry):
            return _joined(state, entry)
        case JoinFailed():
            # For "already_queued"/"already_matched" the existing entry or match
            # may have been announced (and dropped) before this join began, so
            # waiting in `joining` could last forever. Go idle: the next frame
            # or sync, both accepted from `none`, shows it.
            return _NONE if state.phase == "joining" else state
        case Sync(queue=queue, match=match_view, join_in_flight=in_flight):
            return _sync(state, queue, match_view, in_flight)
        case Frame(frame=frame):
            return _frame(state, frame)
    return state  # pragma: no cover


def _joined(state: BreakState, entry: QueueEntryView) -> BreakState:
    if state.phase != "joining":
        return state  # a frame got here first, or the user signed out
    match entry.status:
        case "queued":
            return BreakState(phase="searching", entry=entry)
        case "expired":
            return BreakState(phase="nobody_available")
        case "cancelled":
            return _NONE
        case _:  # "matched": match.created follows
            return state


def _sync(
    state: BreakState,
    queue: QueueEntryView | None,
    match: MatchView | None,
    in_flight: bool,
) -> BreakState:
    if state.phase in ("nobody_available", "ended"):
        return state  # stays until dismissed
    if match is not None:
        return BreakState(phase="matched", match=match)
    if queue is not None:
        return BreakState(phase="searching", entry=queue)
    if state.phase == "joining" and in_flight:
        return state  # our own request is still outstanding
    return _NONE


def _frame(
    state: BreakState,
    frame: WsQueueJoined | WsQueueEnded | WsMatchCreated | WsMatchEnded,
) -> BreakState:
    match frame:
        case WsQueueJoined(entry=entry):
            if state.phase in ("none", "joining"):
                return BreakState(phase="searching", entry=entry)
            if state.phase == "searching" and _entry_id(state) == entry.entry_id:
                return BreakState(phase="searching", entry=entry)
            return state
        case WsQueueEnded(entry_id=entry_id, reason=reason):
            if state.phase != "searching" or _entry_id(state) != entry_id:
                return state
            return (
                BreakState(phase="nobody_available") if reason == "expired" else _NONE
            )
        case WsMatchCreated(match=match_view):
            if state.phase in ("none", "searching", "joining"):
                return BreakState(phase="matched", match=match_view)
            return state
        case WsMatchEnded(match_id=match_id):
            if (
                state.phase == "matched"
                and state.match is not None
                and state.match.match_id == match_id
            ):
                return BreakState(phase="ended", match=state.match)
            return state
    return state  # pragma: no cover


def _entry_id(state: BreakState) -> UUID | None:
    return state.entry.entry_id if state.entry is not None else None

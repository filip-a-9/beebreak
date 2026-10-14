"""The current chat as the companion sees it. Owned by M5-05.

Pure and Qt-free: `RemoteWorker` reduces server replies, frames and its own
send attempts into a `ChatState` and emits it on `chat_changed`. Messages are
ordered by server sequence number only, never by time.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from breakroom_shared.api import (
    ChatStatus,
    MatchEndReason,
    MatchView,
    MessagePage,
    MessageView,
    WsChatMessage,
)

PendingStatus = Literal["sending", "failed"]


@dataclass(frozen=True)
class PendingMessage:
    client_message_id: UUID
    body: str
    status: PendingStatus


ChatEnd = Literal["ended", "expired", "disconnected"]


@dataclass(frozen=True)
class ChatState:
    match_id: UUID | None = None
    peer_nickname: str = ""
    status: ChatStatus | None = None
    messages: tuple[MessageView, ...] = ()  # by seq, no duplicates
    pending: tuple[PendingMessage, ...] = ()  # mine, not yet confirmed
    ended: ChatEnd | None = None  # None while live
    server_offset_s: float = 0.0  # server_time - local time
    reported: bool = False


# --- events ---


@dataclass(frozen=True)
class Matched:
    """`match.created`, or a `sync` / snapshot that carries a match."""

    match: MatchView


@dataclass(frozen=True)
class Messages:
    """A `GET …/messages` page for `match_id`."""

    match_id: UUID
    page: MessagePage


@dataclass(frozen=True)
class ChatFrame:
    frame: WsChatMessage


@dataclass(frozen=True)
class StatusEvent:
    """From an extension reply or a `chat.status` frame."""

    status: ChatStatus


@dataclass(frozen=True)
class Sending:
    client_message_id: UUID
    body: str  # already normalized


@dataclass(frozen=True)
class SendFailed:
    client_message_id: UUID


@dataclass(frozen=True)
class Ended:
    """`match.ended` for `match_id` (reason set), or a `sync` without the
    current match (`match_id` None, reason None → "ended")."""

    match_id: UUID | None
    reason: MatchEndReason | None


@dataclass(frozen=True)
class Reported:
    match_id: UUID


@dataclass(frozen=True)
class ServerTime:
    server_time: datetime
    local_now: datetime


@dataclass(frozen=True)
class Closed:
    """The user closed an ended chat window; ignored while live."""


@dataclass(frozen=True)
class SignedOut:
    pass


ChatEvent = (
    Matched
    | Messages
    | ChatFrame
    | StatusEvent
    | Sending
    | SendFailed
    | Ended
    | Reported
    | ServerTime
    | Closed
    | SignedOut
)


def reduce(state: ChatState, event: ChatEvent, now: datetime) -> ChatState:
    """The next state. Events for another match id leave `state` unchanged."""
    raise NotImplementedError

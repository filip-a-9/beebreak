"""The current chat as the companion sees it. Owned by M5-05.

Pure and Qt-free: `RemoteWorker` reduces server replies, frames and its own
send attempts into a `ChatState` and emits it on `chat_changed`. Messages are
ordered by server sequence number only, never by time.
"""

from dataclasses import dataclass, replace
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
    # A `sync` learns of the end after an offline spell: a pending message may
    # already be stored (its reply lost with the connection, even if it was
    # shown as failed), so every pending one shows `sending` until `Settled`
    # (after the gap fill has had the chance to echo it).
    hold_pending: bool = False


@dataclass(frozen=True)
class Settled:
    """The ended chat's gap fill is over: what is still pending was not stored."""

    match_id: UUID


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
    | Settled
    | Reported
    | ServerTime
    | Closed
    | SignedOut
)


_NONE = ChatState()


def reduce(state: ChatState, event: ChatEvent, now: datetime) -> ChatState:
    """The next state. Events for another match id leave `state` unchanged.

    `now` is part of the contract for callers that need a clock; no rule uses
    it (`ServerTime` carries its own local time).
    """
    del now
    match event:
        case SignedOut():
            return _NONE
        case ServerTime(server_time=server_time, local_now=local_now):
            offset = (server_time - local_now).total_seconds()
            return replace(state, server_offset_s=offset)
        case Closed():
            # Only an ended chat is forgotten; a live one stays.
            return _NONE if state.ended is not None else state
        case Matched(match=match):
            return _matched(state, match)
        case Messages(match_id=match_id, page=page):
            if match_id != state.match_id:
                return state
            return _merge(state, page.messages)
        case ChatFrame(frame=frame):
            if frame.match_id != state.match_id:
                return state
            return _merge(state, (frame.message,))
        case StatusEvent(status=status):
            if status.match_id != state.match_id or state.ended is not None:
                return state
            return replace(state, status=status)
        case Sending(client_message_id=cid, body=body):
            return _sending(state, cid, body)
        case SendFailed(client_message_id=cid):
            return _send_failed(state, cid)
        case Settled(match_id=match_id):
            if match_id != state.match_id or state.ended is None:
                return state
            return _fail_all(state)
        case Ended(match_id=match_id, reason=reason, hold_pending=hold):
            if state.match_id is None or state.ended is not None:
                return state
            if match_id is not None and match_id != state.match_id:
                return state
            ended = replace(state, ended=reason or "ended")
            return _recheck_all(ended) if hold else _fail_all(ended)
        case Reported(match_id=match_id):
            if match_id != state.match_id or state.reported:
                return state
            return replace(state, reported=True)
    return state  # pragma: no cover


def _fail_all(state: ChatState) -> ChatState:
    failed = tuple(replace(p, status="failed") for p in state.pending)
    return state if failed == state.pending else replace(state, pending=failed)


def _recheck_all(state: ChatState) -> ChatState:
    pending = tuple(replace(p, status="sending") for p in state.pending)
    return state if pending == state.pending else replace(state, pending=pending)


def _matched(state: ChatState, match: MatchView) -> ChatState:
    if match.match_id != state.match_id:
        return ChatState(
            match_id=match.match_id,
            peer_nickname=match.peer_nickname,
            status=match.chat,
            server_offset_s=state.server_offset_s,
        )
    if state.ended is not None:
        return state  # an ended chat never comes back to life
    return replace(state, peer_nickname=match.peer_nickname, status=match.chat)


def _merge(state: ChatState, incoming: tuple[MessageView, ...]) -> ChatState:
    """Add messages by seq (the first copy of a seq wins) and drop the pending
    entries their echoes confirm. Sorted by seq only, never by time."""
    by_seq = {m.seq: m for m in state.messages}
    for message in incoming:
        by_seq.setdefault(message.seq, message)
    confirmed = {
        m.client_message_id
        for m in incoming
        if m.mine and m.client_message_id is not None
    }
    pending = tuple(p for p in state.pending if p.client_message_id not in confirmed)
    messages = tuple(by_seq[seq] for seq in sorted(by_seq))
    if messages == state.messages and pending == state.pending:
        return state
    return replace(state, messages=messages, pending=pending)


def _sending(state: ChatState, cid: UUID, body: str) -> ChatState:
    if state.match_id is None or state.ended is not None:
        return state
    mine = PendingMessage(cid, body, "sending")
    for index, existing in enumerate(state.pending):
        if existing.client_message_id == cid:  # a retry keeps its place
            pending = (*state.pending[:index], mine, *state.pending[index + 1 :])
            return replace(state, pending=pending)
    return replace(state, pending=(*state.pending, mine))


def _send_failed(state: ChatState, cid: UUID) -> ChatState:
    pending = tuple(
        replace(p, status="failed") if p.client_message_id == cid else p
        for p in state.pending
    )
    return state if pending == state.pending else replace(state, pending=pending)

"""Chat values for tests that build a MatchView, MessageView or ChatState by
hand (M5)."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from beebreak_companion.chat_state import ChatEnd, ChatState, PendingMessage
from beebreak_shared.api import ChatStatus, ExtensionState, MessageView

T0 = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)


def chat_status(
    match_id: UUID,
    ends_at: datetime,
    *,
    grace_s: int = 120,
    extensions_left: int = 2,
    extension_s: int = 600,
    extension: ExtensionState = "none",
    peer_online: bool = True,
) -> ChatStatus:
    """Defaults: a fresh chat, no extension used or pending, peer online."""
    return ChatStatus(
        match_id=match_id,
        ends_at=ends_at,
        grace_at=ends_at - timedelta(seconds=grace_s),
        extensions_left=extensions_left,
        extension_s=extension_s,
        extension=extension,
        peer_online=peer_online,
    )


def chat_json(match_id: UUID | str, ends_at: datetime | str) -> dict[str, Any]:
    """`chat_status` as wire JSON."""
    when = datetime.fromisoformat(ends_at) if isinstance(ends_at, str) else ends_at
    status = chat_status(UUID(str(match_id)), when)
    result: dict[str, Any] = status.model_dump(mode="json")
    return result


def message_view(
    seq: int,
    *,
    mine: bool = False,
    body: str | None = None,
    sent_at: datetime | None = None,
    client_message_id: UUID | None = None,
) -> MessageView:
    """A message; `mine` ones get a client message id unless one is given."""
    if mine and client_message_id is None:
        client_message_id = uuid4()
    return MessageView(
        seq=seq,
        mine=mine,
        body=body if body is not None else f"message {seq}",
        sent_at=sent_at or T0 + timedelta(seconds=seq),
        client_message_id=client_message_id if mine else None,
    )


def chat_state(
    *,
    match_id: UUID | None = None,
    peer_nickname: str = "bob",
    ends_at: datetime | None = None,
    status: ChatStatus | None = None,
    messages: tuple[MessageView, ...] = (),
    pending: tuple[PendingMessage, ...] = (),
    ended: ChatEnd | None = None,
    server_offset_s: float = 0.0,
    reported: bool = False,
    extension_declined: bool = False,
) -> ChatState:
    """A live chat with `peer_nickname`, 15 minutes after T0 unless given."""
    match_id = match_id or uuid4()
    if status is None:
        status = chat_status(match_id, ends_at or T0 + timedelta(minutes=15))
    return ChatState(
        match_id=match_id,
        peer_nickname=peer_nickname,
        status=status,
        messages=messages,
        pending=pending,
        ended=ended,
        server_offset_s=server_offset_s,
        reported=reported,
        extension_declined=extension_declined,
    )

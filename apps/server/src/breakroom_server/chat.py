"""Chat HTTP API: messages and extensions, and the operations behind them.
Owned by M5-02.

Messages go over HTTP; frames go out over the WebSocket through
`app.state.notifier`. The HTTP reply is the sender's ack. Every operation checks
participation itself (rule 18): a non-participant gets the same 404 as an
unknown match. Nothing here logs a message body (rule 16).
"""

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Request, Response
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from breakroom_server.auth import ApiError, CurrentPrincipal, check_limit
from breakroom_server.config import ServerConfig
from breakroom_server.models import Match, MatchParticipant, Message
from breakroom_server.notify import make_frame
from breakroom_server.queue import chat_status
from breakroom_shared.api import (
    ChatChange,
    ChatStatus,
    ExtensionAction,
    ExtensionRequest,
    MessagePage,
    MessageView,
    SendMessageRequest,
    WsChatMessage,
    WsChatStatus,
)
from breakroom_shared.chat import normalize_text

router = APIRouter()

MAX_PAGE = 100
_EXTENSION_ATTEMPTS = 4


# --- operations (M5-02) ------------------------------------------------------


def _peer_of(session: Session, match_id: UUID, user_id: UUID) -> UUID:
    """The peer's user id; `not_found` when the caller does not take part (the
    same answer as for an unknown match: no oracle)."""
    user_ids = list(
        session.scalars(
            select(MatchParticipant.user_id).where(
                MatchParticipant.match_id == match_id
            )
        )
    )
    peers = [u for u in user_ids if u != user_id]
    if user_id not in user_ids or not peers:
        raise ApiError("not_found")
    return peers[0]


def _load_match(session: Session, match_id: UUID) -> Match:
    match = session.get(Match, match_id, populate_existing=True)
    if match is None:
        raise ApiError("not_found")
    return match


def _is_live(match: Match, now: datetime) -> bool:
    return match.status == "active" and now < match.ends_at


def _view(message: Message, user_id: UUID) -> MessageView:
    mine = message.author_id == user_id
    return MessageView(
        seq=message.seq,
        mine=mine,
        body=message.body,
        sent_at=message.created_at,
        client_message_id=message.client_message_id if mine else None,
    )


def _replay(
    session: Session, user_id: UUID, match_id: UUID, client_message_id: UUID, peer: UUID
) -> tuple[MessageView, bool, UUID] | None:
    stored = session.scalars(
        select(Message).where(
            Message.author_id == user_id,
            Message.client_message_id == client_message_id,
        )
    ).first()
    if stored is None:
        return None
    if stored.match_id != match_id:
        raise ApiError("not_found")  # keys are not looked up across matches
    return _view(stored, user_id), False, peer


def send_message(
    session: Session,
    config: ServerConfig,
    now: datetime,
    user_id: UUID,
    match_id: UUID,
    body: SendMessageRequest,
) -> tuple[MessageView, bool, UUID]:
    """Store a message; (the caller's view, created, the peer's user id).

    A replay of `(user_id, client_message_id)` returns the stored message with
    `created=False`, whatever the match state now.
    """
    peer_id = _peer_of(session, match_id, user_id)
    replayed = _replay(session, user_id, match_id, body.client_message_id, peer_id)
    if replayed is not None:
        return replayed
    if not _is_live(_load_match(session, match_id), now):
        raise ApiError("chat_ended")
    text = normalize_text(body.body)
    if text is None:
        raise ApiError("invalid_request")
    try:
        seq = session.execute(
            update(Match)
            .where(Match.id == match_id, Match.status == "active", Match.ends_at > now)
            .values(last_seq=Match.last_seq + 1)
            .returning(Match.last_seq)
            .execution_options(synchronize_session=False)
        ).scalar_one_or_none()
        if seq is None:  # the chat ended between the check and the update
            session.rollback()
            raise ApiError("chat_ended")
        message = Message(
            match_id=match_id,
            seq=seq,
            author_id=user_id,
            client_message_id=body.client_message_id,
            body=text,
            created_at=now,
        )
        session.add(message)
        session.execute(
            update(MatchParticipant)
            .where(
                MatchParticipant.match_id == match_id,
                MatchParticipant.user_id == user_id,
            )
            .values(messages_sent=MatchParticipant.messages_sent + 1)
            .execution_options(synchronize_session=False)
        )
        session.commit()
    except IntegrityError:
        # A concurrent send of the same client message ID won the unique key.
        session.rollback()
        replayed = _replay(session, user_id, match_id, body.client_message_id, peer_id)
        if replayed is None:
            raise
        return replayed
    return _view(message, user_id), True, peer_id


def list_messages(
    session: Session,
    config: ServerConfig,
    now: datetime,
    user_id: UUID,
    match_id: UUID,
    after_seq: int,
) -> MessagePage:
    """Messages with `seq > after_seq`, oldest first, at most 100."""
    _peer_of(session, match_id, user_id)
    if after_seq < 0:
        raise ApiError("invalid_request")
    match = _load_match(session, match_id)
    if (
        match.status == "ended"
        and match.ended_at is not None
        and now - match.ended_at > timedelta(seconds=config.message_retention_s)
    ):
        raise ApiError("not_found")  # the bodies are purged
    rows = session.scalars(
        select(Message)
        .where(Message.match_id == match_id, Message.seq > after_seq)
        .order_by(Message.seq)
        .limit(MAX_PAGE)
    )
    return MessagePage(
        messages=tuple(_view(row, user_id) for row in rows), last_seq=match.last_seq
    )


def _transition(
    session: Session,
    match: Match,
    now: datetime,
    *,
    pending: UUID | None,
    values: dict[str, Any],
) -> bool:
    """One conditional UPDATE: applies only if the chat is still live and the
    request state, `ends_at` and `extensions_used` are what the caller saw."""
    pending_is = (
        Match.extension_requested_by.is_(None)
        if pending is None
        else Match.extension_requested_by == pending
    )
    result = session.execute(
        update(Match)
        .where(
            Match.id == match.id,
            Match.status == "active",
            Match.ends_at > now,
            Match.ends_at == match.ends_at,
            Match.extensions_used == match.extensions_used,
            pending_is,
        )
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount > 0  # type: ignore[attr-defined, no-any-return]


def extension(
    session: Session,
    config: ServerConfig,
    now: datetime,
    user_id: UUID,
    match_id: UUID,
    action: ExtensionAction,
) -> tuple[ChatStatus, ChatChange | None, UUID]:
    """Apply an extension action; (the caller's status, the change or None for a
    no-op, the peer's user id)."""
    peer_id = _peer_of(session, match_id, user_id)
    change: ChatChange | None = None
    for _ in range(_EXTENSION_ATTEMPTS):
        match = _load_match(session, match_id)
        if not _is_live(match, now):
            session.rollback()
            raise ApiError("chat_ended")
        pending = match.extension_requested_by
        effective = action
        if action == "request":
            if match.extensions_used >= config.chat_max_extensions:
                session.rollback()
                raise ApiError("no_extensions_left")
            if pending == peer_id:
                effective = "accept"  # asking for what the peer asked for
            elif pending == user_id:
                change = None  # already asked: no-op
                break
        if effective == "accept":
            if pending != peer_id:
                session.rollback()
                raise ApiError("no_extension_request")
            applied = _transition(
                session,
                match,
                now,
                pending=peer_id,
                values={
                    "ends_at": match.ends_at
                    + timedelta(seconds=config.chat_extension_s),
                    "extensions_used": match.extensions_used + 1,
                    "extension_requested_by": None,
                },
            )
            change = "extension_accepted"
        elif effective == "decline":
            if pending is None:
                change = None  # nothing pending: no-op
                break
            applied = _transition(
                session,
                match,
                now,
                pending=pending,
                values={"extension_requested_by": None},
            )
            change = "extension_declined"
        else:  # request, nothing pending
            applied = _transition(
                session,
                match,
                now,
                pending=None,
                values={"extension_requested_by": user_id},
            )
            change = "extension_requested"
        if applied:
            break
        change = None  # lost a race: look again at the new state
    else:
        session.rollback()
        raise ApiError("no_extension_request")
    session.commit()
    match = _load_match(session, match_id)
    return chat_status(session, config, match, user_id), change, peer_id


# --- routes ------------------------------------------------------------------


@router.post("/v1/matches/{match_id}/messages", status_code=201)
def post_message(
    request: Request,
    match_id: UUID,
    body: SendMessageRequest,
    principal: CurrentPrincipal,
    response: Response,
) -> MessageView:
    state = request.app.state
    # Before touching the database.
    check_limit(state.limiters.message_user, str(principal.user_id))
    now: datetime = state.clock()
    with state.session_factory() as session:
        view, created, peer_id = send_message(
            session, state.config, now, principal.user_id, match_id, body
        )
    if not created:
        response.status_code = 200
        return view
    state.notifier.send_to_user(
        principal.user_id,
        make_frame(WsChatMessage, now, match_id=match_id, message=view),
    )
    state.notifier.send_to_user(
        peer_id,
        make_frame(
            WsChatMessage,
            now,
            match_id=match_id,
            message=view.model_copy(update={"mine": False, "client_message_id": None}),
        ),
    )
    return view


@router.get("/v1/matches/{match_id}/messages")
def get_messages(
    request: Request,
    match_id: UUID,
    principal: CurrentPrincipal,
    after_seq: int = 0,
) -> MessagePage:
    state = request.app.state
    now: datetime = state.clock()
    with state.session_factory() as session:
        return list_messages(
            session, state.config, now, principal.user_id, match_id, after_seq
        )


@router.post("/v1/matches/{match_id}/extension")
def post_extension(
    request: Request,
    match_id: UUID,
    body: ExtensionRequest,
    principal: CurrentPrincipal,
) -> ChatStatus:
    state = request.app.state
    now: datetime = state.clock()
    with state.session_factory() as session:
        status, change, peer_id = extension(
            session, state.config, now, principal.user_id, match_id, body.action
        )
        peer_status = None
        if change is not None:
            match = session.get(Match, match_id, populate_existing=True)
            if match is not None:
                peer_status = chat_status(session, state.config, match, peer_id)
    if change is not None:
        state.notifier.send_to_user(
            principal.user_id,
            make_frame(WsChatStatus, now, status=status, change=change),
        )
        if peer_status is not None:
            state.notifier.send_to_user(
                peer_id,
                make_frame(WsChatStatus, now, status=peer_status, change=change),
            )
    return status

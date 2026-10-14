"""Chat HTTP API: messages and extensions, and the operations behind them.
Owned by M5-02.

Messages go over HTTP; frames go out over the WebSocket through
`app.state.notifier`. The HTTP reply is the sender's ack. Every operation checks
participation itself (rule 18): a non-participant gets the same 404 as an
unknown match. Nothing here logs a message body (rule 16).
"""

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Request, Response
from sqlalchemy.orm import Session

from breakroom_server.auth import CurrentPrincipal, check_limit
from breakroom_server.config import ServerConfig
from breakroom_server.models import Match
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

router = APIRouter()


# --- operations (M5-02) ------------------------------------------------------


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
    raise NotImplementedError


def list_messages(
    session: Session,
    config: ServerConfig,
    now: datetime,
    user_id: UUID,
    match_id: UUID,
    after_seq: int,
) -> MessagePage:
    """Messages with `seq > after_seq`, oldest first, at most 100."""
    raise NotImplementedError


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
    raise NotImplementedError


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

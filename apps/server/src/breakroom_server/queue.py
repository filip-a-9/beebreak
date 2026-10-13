"""Queue and match HTTP API and the queue operations behind it. Owned by M4-02.

`current_state`, `entry_view`, `match_view` and `clear_presence` are shared with
the matchmaker (M4-03) and the realtime `sync` frame (M4-04). Nothing here logs
presence fields, nicknames or user IDs (rule 13).
"""

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from breakroom_server.auth import CurrentPrincipal, check_limit
from breakroom_server.config import ServerConfig
from breakroom_server.models import (
    Match,
    MatchParticipant,
    QueueEntry,
    User,
    split_tags,
)
from breakroom_server.notify import make_frame
from breakroom_shared.api import (
    MatchView,
    QueueEntryView,
    QueueJoinRequest,
    QueueStatus,
    WsMatchEnded,
    WsQueueEnded,
    WsQueueJoined,
)
from breakroom_shared.presence import TaskCategory

router = APIRouter()

# Shown if a peer somehow has no nickname (setup requires one; never expected).
_FALLBACK_NICKNAME = "unknown"
_CATEGORIES: frozenset[str] = frozenset(
    ("backend", "frontend", "devops", "data", "testing")
)


# --- shared helpers (step 0) -------------------------------------------------


def entry_view(entry: QueueEntry) -> QueueEntryView:
    status: QueueStatus = entry.status  # type: ignore[assignment]
    return QueueEntryView(entry_id=entry.id, status=status, expires_at=entry.expires_at)


def clear_presence(entry: QueueEntry) -> None:
    """Null every presence field (retention: done at each terminal transition)."""
    entry.presence_id = None
    entry.task_category = None
    entry.stack_tags = None
    entry.remaining_time_bucket = None


def match_view(session: Session, match: Match, viewer_id: UUID) -> MatchView:
    """The match as `viewer_id` sees it: the other participant's nickname."""
    peer_nickname = session.scalar(
        select(User.nickname)
        .join(MatchParticipant, MatchParticipant.user_id == User.id)
        .where(MatchParticipant.match_id == match.id, User.id != viewer_id)
    )
    category: TaskCategory | None = (
        match.shared_category  # type: ignore[assignment]
        if match.shared_category in _CATEGORIES
        else None
    )
    return MatchView(
        match_id=match.id,
        peer_nickname=peer_nickname or _FALLBACK_NICKNAME,
        shared_tags=split_tags(match.shared_tags),
        shared_category=category,
        ends_at=match.ends_at,
    )


def current_state(
    session: Session, now: datetime, user_id: UUID
) -> tuple[QueueEntryView | None, MatchView | None]:
    """The user's live queue entry (not past `expires_at`) and active match."""
    entry = session.scalars(
        select(QueueEntry).where(
            QueueEntry.user_id == user_id,
            QueueEntry.status == "queued",
            QueueEntry.expires_at > now,
        )
    ).first()
    match = session.scalars(
        select(Match)
        .join(MatchParticipant, MatchParticipant.match_id == Match.id)
        .where(
            MatchParticipant.user_id == user_id,
            MatchParticipant.active.is_(True),
            Match.status == "active",
        )
    ).first()
    return (
        entry_view(entry) if entry is not None else None,
        match_view(session, match, user_id) if match is not None else None,
    )


# --- operations (M4-02) ------------------------------------------------------


def join_queue(
    session: Session,
    config: ServerConfig,
    now: datetime,
    user_id: UUID,
    device_id: UUID,
    body: QueueJoinRequest,
) -> tuple[QueueEntryView, bool]:
    """Admit the user to the queue; (view, created). M4-02."""
    raise NotImplementedError


def leave_queue(session: Session, now: datetime, user_id: UUID) -> UUID | None:
    """Cancel the user's queued entry; its id, or None if none. M4-02."""
    raise NotImplementedError


def end_match(
    session: Session, now: datetime, user_id: UUID, match_id: UUID, *, block: bool
) -> UUID | None:
    """End (and optionally block) a match; the peer's user id if it ended now.

    Raises ApiError("not_found") when the caller is not a participant. M4-02.
    """
    raise NotImplementedError


# --- routes ------------------------------------------------------------------


@router.post("/v1/queue", status_code=201)
def post_queue(
    request: Request,
    body: QueueJoinRequest,
    principal: CurrentPrincipal,
    response: Response,
) -> QueueEntryView:
    state = request.app.state
    # Before touching the database (M4-02).
    check_limit(state.limiters.queue_user, str(principal.user_id))
    now: datetime = state.clock()
    with state.session_factory() as session:
        view, created = join_queue(
            session, state.config, now, principal.user_id, principal.device_id, body
        )
    if created:
        state.notifier.send_to_user(
            principal.user_id, make_frame(WsQueueJoined, now, entry=view)
        )
        state.matchmaker.kick()
    else:
        response.status_code = 200
    return view


@router.delete("/v1/queue/current", status_code=204)
def delete_queue(request: Request, principal: CurrentPrincipal) -> Response:
    state = request.app.state
    now: datetime = state.clock()
    with state.session_factory() as session:
        entry_id = leave_queue(session, now, principal.user_id)
    if entry_id is not None:
        state.notifier.send_to_user(
            principal.user_id,
            make_frame(WsQueueEnded, now, entry_id=entry_id, reason="cancelled"),
        )
    return Response(status_code=204)


def _end(
    request: Request, principal: CurrentPrincipal, match_id: UUID, *, block: bool
) -> Response:
    state = request.app.state
    now: datetime = state.clock()
    with state.session_factory() as session:
        peer_id = end_match(session, now, principal.user_id, match_id, block=block)
    if peer_id is not None:
        # Never "blocked": the blocked peer sees a plain end.
        for user_id in (principal.user_id, peer_id):
            state.notifier.send_to_user(
                user_id,
                make_frame(WsMatchEnded, now, match_id=match_id, reason="ended"),
            )
    return Response(status_code=204)


@router.post("/v1/matches/{match_id}/end", status_code=204)
def post_match_end(
    request: Request, match_id: UUID, principal: CurrentPrincipal
) -> Response:
    return _end(request, principal, match_id, block=False)


@router.post("/v1/matches/{match_id}/block", status_code=204)
def post_match_block(
    request: Request, match_id: UUID, principal: CurrentPrincipal
) -> Response:
    return _end(request, principal, match_id, block=True)

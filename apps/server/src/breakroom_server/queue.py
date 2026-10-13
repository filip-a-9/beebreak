"""Queue and match HTTP API and the queue operations behind it. Owned by M4-02.

`current_state`, `entry_view`, `match_view` and `clear_presence` are shared with
the matchmaker (M4-03) and the realtime `sync` frame (M4-04). Nothing here logs
presence fields, nicknames or user IDs (rule 13).
"""

from datetime import datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Request, Response
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from breakroom_server.auth import (
    ApiError,
    CurrentPrincipal,
    check_limit,
    unauthorized,
)
from breakroom_server.config import ServerConfig
from breakroom_server.models import (
    Block,
    Match,
    MatchParticipant,
    QueueEntry,
    User,
    UserPreference,
    join_tags,
    split_tags,
)
from breakroom_server.notify import make_frame
from breakroom_shared.api import (
    MatchView,
    QueueEntryView,
    QueueJoinRequest,
    QueueSnapshot,
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


def _replay(session: Session, user_id: UUID, key: UUID) -> QueueEntry | None:
    return session.scalars(
        select(QueueEntry).where(
            QueueEntry.user_id == user_id, QueueEntry.idempotency_key == key
        )
    ).first()


def _in_active_match(session: Session, user_id: UUID) -> bool:
    return (
        session.scalars(
            select(MatchParticipant.match_id)
            .join(Match, Match.id == MatchParticipant.match_id)
            .where(
                MatchParticipant.user_id == user_id,
                MatchParticipant.active.is_(True),
                Match.status == "active",
            )
        ).first()
        is not None
    )


def _check_not_busy(session: Session, now: datetime, user_id: UUID) -> None:
    """409 when the user has an active match or a live queued entry."""
    if _in_active_match(session, user_id):
        raise ApiError("already_matched")
    queued = session.scalars(
        select(QueueEntry).where(
            QueueEntry.user_id == user_id, QueueEntry.status == "queued"
        )
    ).first()
    if queued is None:
        return
    if queued.expires_at > now:
        raise ApiError("already_queued")
    # Past its TTL but not yet swept: close it so the unique index is free. The
    # update is conditional: a matcher pass may have claimed the entry since we
    # read it, and its outcome wins.
    closed = session.execute(
        update(QueueEntry)
        .where(QueueEntry.id == queued.id, QueueEntry.status == "queued")
        .values(
            status="expired",
            ended_at=now,
            presence_id=None,
            task_category=None,
            stack_tags=None,
            remaining_time_bucket=None,
        )
        .execution_options(synchronize_session=False)
    )
    if closed.rowcount == 0:  # type: ignore[attr-defined]
        session.rollback()
        if _in_active_match(session, user_id):
            raise ApiError("already_matched")
        raise ApiError("already_queued")
    # Still inside the insert transaction: no queued entry is left for the
    # matcher to claim, but check the match once more before admitting.
    if _in_active_match(session, user_id):
        session.rollback()
        raise ApiError("already_matched")


def join_queue(
    session: Session,
    config: ServerConfig,
    now: datetime,
    user_id: UUID,
    device_id: UUID,
    body: QueueJoinRequest,
) -> tuple[QueueEntryView, bool]:
    """Admit the user to the queue; (view, created)."""
    key = body.idempotency_key
    replay = _replay(session, user_id, key)
    if replay is not None:
        return entry_view(replay), False

    user = session.get(User, user_id)
    prefs = session.get(UserPreference, user_id)
    if user is None or user.status != "active":
        raise unauthorized()
    if not user.nickname or prefs is None:
        raise ApiError("setup_incomplete")

    presence = body.presence
    if (
        (presence.task_category is not None and not prefs.share_task_category)
        or (presence.stack_tags and not prefs.share_stack_tags)
        or (presence.remaining_time_bucket is not None and not prefs.share_time_bucket)
    ):
        raise ApiError("invalid_request")

    _check_not_busy(session, now, user_id)
    entry = QueueEntry(
        user_id=user_id,
        device_id=device_id,
        idempotency_key=key,
        status="queued",
        presence_id=presence.presence_id,
        task_category=presence.task_category,
        stack_tags=join_tags(presence.stack_tags),
        remaining_time_bucket=presence.remaining_time_bucket,
        admitted_at=now,
        expires_at=now + timedelta(seconds=config.queue_ttl_s),
    )
    session.add(entry)
    try:
        session.commit()
    except IntegrityError:
        # Lost a race on the unique indexes: whoever won decides our answer.
        session.rollback()
        replay = _replay(session, user_id, key)
        if replay is not None:
            return entry_view(replay), False
        _check_not_busy(session, now, user_id)
        raise ApiError("already_queued") from None
    return entry_view(entry), True


def leave_queue(session: Session, now: datetime, user_id: UUID) -> UUID | None:
    """Cancel the user's queued entry; its id, or None if none.

    The cancel is one conditional UPDATE, so an entry a matcher pass claimed in
    the meantime stays `matched` and nothing is reported cancelled.
    """
    entry_id = session.execute(
        update(QueueEntry)
        .where(QueueEntry.user_id == user_id, QueueEntry.status == "queued")
        .values(
            status="cancelled",
            ended_at=now,
            presence_id=None,
            task_category=None,
            stack_tags=None,
            remaining_time_bucket=None,
        )
        .returning(QueueEntry.id)
        .execution_options(synchronize_session=False)
    ).scalar_one_or_none()
    session.commit()
    return entry_id


def end_match(
    session: Session, now: datetime, user_id: UUID, match_id: UUID, *, block: bool
) -> UUID | None:
    """End (and optionally block) a match; the peer's user id if it ended now.

    Raises ApiError("not_found") when the caller is not a participant, the same
    answer as for a match that does not exist.
    """
    user_ids = list(
        session.scalars(
            select(MatchParticipant.user_id).where(
                MatchParticipant.match_id == match_id
            )
        )
    )
    if user_id not in user_ids:
        raise ApiError("not_found")
    peers = [u for u in user_ids if u != user_id]
    peer_id = peers[0] if peers else None

    # Conditional: a concurrent end (matcher expiry, other participant) wins and
    # keeps its end_reason; then nothing is reported as ended by us.
    ended_now = (
        session.execute(
            update(Match)
            .where(Match.id == match_id, Match.status == "active")
            .values(
                status="ended",
                ended_at=now,
                end_reason="blocked" if block else "ended",
                shared_tags=None,
                shared_category=None,
            )
            .execution_options(synchronize_session=False)
        ).rowcount  # type: ignore[attr-defined]
        > 0
    )
    if ended_now:
        session.execute(
            update(MatchParticipant)
            .where(MatchParticipant.match_id == match_id)
            .values(active=False)
            .execution_options(synchronize_session=False)
        )
    if block and peer_id is not None:
        already = session.get(Block, (user_id, peer_id))
        if already is None:
            try:
                with session.begin_nested():
                    session.add(
                        Block(blocker_id=user_id, blocked_id=peer_id, created_at=now)
                    )
            except IntegrityError:
                pass  # a concurrent duplicate: the block exists
    session.commit()
    return peer_id if ended_now else None


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


@router.get("/v1/queue/current")
def get_queue(request: Request, principal: CurrentPrincipal) -> QueueSnapshot:
    state = request.app.state
    now: datetime = state.clock()
    with state.session_factory() as session:
        entry, match = current_state(session, now, principal.user_id)
    return QueueSnapshot(queue=entry, match=match)


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

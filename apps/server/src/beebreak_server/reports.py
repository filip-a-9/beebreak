"""Reports HTTP API and the operation behind it. Owned by M5-03.

A report copies only the messages the reporter ticked, blocks the peer and ends
the chat. The reported peer is told only `ended`. Nothing here logs the note, a
message body or a nickname (rule 16); every request checks participation
(rule 18).
"""

import logging
from datetime import datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Request, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from beebreak_server.auth import ApiError, CurrentPrincipal, check_limit
from beebreak_server.config import ServerConfig
from beebreak_server.models import (
    Match,
    MatchParticipant,
    Message,
    Report,
    ReportMessage,
)
from beebreak_server.notify import make_frame
from beebreak_server.queue import end_match
from beebreak_shared.api import ReportRequest, WsMatchEnded
from beebreak_shared.chat import MAX_NOTE_CHARS, normalize_text

router = APIRouter()
logger = logging.getLogger(__name__)


def file_report(
    session: Session,
    config: ServerConfig,
    now: datetime,
    user_id: UUID,
    body: ReportRequest,
) -> UUID | None:
    """Store the report, block the peer and end the chat; the peer's user id if
    the chat ended now (None when it had already ended)."""
    match_id = body.match_id
    # Participation first; an unknown match answers the same (rule 18).
    user_ids = list(
        session.scalars(
            select(MatchParticipant.user_id).where(
                MatchParticipant.match_id == match_id
            )
        )
    )
    peers = [u for u in user_ids if u != user_id]
    match = session.get(Match, match_id)
    if user_id not in user_ids or match is None or not peers:
        raise ApiError("not_found")
    peer_id = peers[0]

    if (
        session.scalars(
            select(Report.id).where(
                Report.reporter_id == user_id, Report.match_id == match_id
            )
        ).first()
        is not None
    ):
        raise ApiError("already_reported")
    if (
        match.status == "ended"
        and match.ended_at is not None
        and now - match.ended_at > timedelta(seconds=config.report_window_s)
    ):
        raise ApiError("report_window_closed")

    note: str | None = None
    if body.note is not None and body.note.strip():
        note = normalize_text(body.note, max_chars=MAX_NOTE_CHARS)
        if note is None:
            raise ApiError("invalid_request")

    seqs = sorted(set(body.message_seqs))
    evidence: list[Message] = []
    if seqs:
        evidence = list(
            session.scalars(
                select(Message)
                .where(Message.match_id == match_id, Message.seq.in_(seqs))
                .order_by(Message.seq)
            )
        )
        if len(evidence) != len(seqs):
            raise ApiError("invalid_request")

    # Report, evidence, block and end go in one transaction: `end_match` commits
    # them together, so a failure cannot leave a report without the block.
    report = Report(
        reporter_id=user_id,
        reported_id=peer_id,
        match_id=match_id,
        reason=body.reason,
        note=note,
        status="open",
        created_at=now,
    )
    try:
        with session.begin_nested():
            session.add(report)
            session.flush()
    except IntegrityError:
        # A concurrent duplicate won on (reporter_id, match_id).
        session.rollback()
        raise ApiError("already_reported") from None
    session.add_all(
        ReportMessage(
            report_id=report.id,
            seq=m.seq,
            author="reporter" if m.author_id == user_id else "reported",
            body=m.body,
            sent_at=m.created_at,
        )
        for m in evidence
    )
    report_id = report.id
    try:
        ended_peer = end_match(
            session, now, user_id, match_id, block=True, reason="reported"
        )
    except IntegrityError:
        session.rollback()
        raise ApiError("already_reported") from None
    logger.info("report filed id=%s reason=%s", report_id, body.reason)
    return ended_peer


@router.post("/v1/reports", status_code=204)
def post_report(
    request: Request, body: ReportRequest, principal: CurrentPrincipal
) -> Response:
    state = request.app.state
    # Before touching the database.
    check_limit(state.limiters.report_user, str(principal.user_id))
    now: datetime = state.clock()
    with state.session_factory() as session:
        peer_id = file_report(session, state.config, now, principal.user_id, body)
    if peer_id is not None:
        # Never "reported" or "blocked": the reported peer sees a plain end.
        for user_id in (principal.user_id, peer_id):
            state.notifier.send_to_user(
                user_id,
                make_frame(WsMatchEnded, now, match_id=body.match_id, reason="ended"),
            )
    return Response(status_code=204)

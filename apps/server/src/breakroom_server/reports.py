"""Reports HTTP API and the operation behind it. Owned by M5-03.

A report copies only the messages the reporter ticked, blocks the peer and ends
the chat. The reported peer is told only `ended`. Nothing here logs the note, a
message body or a nickname (rule 16); every request checks participation
(rule 18).
"""

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Request, Response
from sqlalchemy.orm import Session

from breakroom_server.auth import CurrentPrincipal, check_limit
from breakroom_server.config import ServerConfig
from breakroom_server.notify import make_frame
from breakroom_shared.api import ReportRequest, WsMatchEnded

router = APIRouter()


def file_report(
    session: Session,
    config: ServerConfig,
    now: datetime,
    user_id: UUID,
    body: ReportRequest,
) -> UUID | None:
    """Store the report, block the peer and end the chat; the peer's user id if
    the chat ended now (None when it had already ended)."""
    raise NotImplementedError


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

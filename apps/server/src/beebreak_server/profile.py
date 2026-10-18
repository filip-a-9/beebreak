"""Profile and consent endpoints. Owned by M3-02."""

import uuid
from datetime import datetime

from fastapi import APIRouter, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from beebreak_server.auth import ApiError, CurrentPrincipal, check_limit
from beebreak_server.models import User, UserPreference
from beebreak_server.nickname import NicknameError, skeleton, validate_nickname
from beebreak_shared.api import CONSENT_VERSION, Consent, Me, NicknameUpdate

router = APIRouter(prefix="/v1/me")


def build_me(session: Session, user_id: uuid.UUID) -> Me:
    user = session.get(User, user_id)
    if user is None:
        raise ApiError("unauthorized")
    pref = session.get(UserPreference, user_id)
    if pref is None or pref.consent_version != CONSENT_VERSION:
        return Me(nickname=user.nickname, consent=None, consented_at=None)
    return Me(
        nickname=user.nickname,
        consent=Consent(
            version=1,
            share_task_category=pref.share_task_category,
            share_stack_tags=pref.share_stack_tags,
            share_time_bucket=pref.share_time_bucket,
        ),
        consented_at=pref.consented_at,
    )


@router.get("")
def get_me(request: Request, principal: CurrentPrincipal) -> Me:
    check_limit(request.app.state.limiters.me_read_user, str(principal.user_id))
    with request.app.state.session_factory() as session:
        return build_me(session, principal.user_id)


@router.patch("")
def set_nickname(
    request: Request, body: NicknameUpdate, principal: CurrentPrincipal
) -> Me:
    check_limit(request.app.state.limiters.me_write_user, str(principal.user_id))
    try:
        nickname = validate_nickname(body.nickname)
    except NicknameError as err:
        raise ApiError(err.code) from None
    with request.app.state.session_factory() as session:
        user = session.get(User, principal.user_id)
        if user is None:
            raise ApiError("unauthorized")
        user.nickname = nickname
        user.nickname_skeleton = skeleton(nickname)
        try:
            # The unique index decides; a pre-check alone would race.
            session.commit()
        except IntegrityError:
            session.rollback()
            raise ApiError("nickname_taken") from None
        return build_me(session, principal.user_id)


@router.put("/consent")
def set_consent(request: Request, body: Consent, principal: CurrentPrincipal) -> Me:
    check_limit(request.app.state.limiters.me_write_user, str(principal.user_id))
    now: datetime = request.app.state.clock()
    with request.app.state.session_factory() as session:
        pref = session.get(UserPreference, principal.user_id)
        if pref is None:
            pref = UserPreference(user_id=principal.user_id, consented_at=now)
            session.add(pref)
        pref.consent_version = body.version
        pref.share_task_category = body.share_task_category
        pref.share_stack_tags = body.share_stack_tags
        pref.share_time_bucket = body.share_time_bucket
        pref.consented_at = now
        session.commit()
        return build_me(session, principal.user_id)

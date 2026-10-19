"""Profile and consent endpoints. Owned by M3-02."""

import uuid
from datetime import datetime

from fastapi import APIRouter, Request, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from beebreak_server.auth import ApiError, CurrentPrincipal, check_limit
from beebreak_server.models import Device, User, UserPreference
from beebreak_server.nickname import NicknameError, skeleton, validate_nickname
from beebreak_shared.api import (
    CONSENT_VERSION,
    Consent,
    DeviceNameUpdate,
    Me,
    NicknameUpdate,
)

router = APIRouter(prefix="/v1/me")


def build_me(
    session: Session, user_id: uuid.UUID, device_id: uuid.UUID | None = None
) -> Me:
    """`device_id` is the caller's device; sign-in responses leave it out."""
    user = session.get(User, user_id)
    if user is None:
        raise ApiError("unauthorized")
    device = session.get(Device, device_id) if device_id is not None else None
    device_name = device.name if device is not None else None
    pref = session.get(UserPreference, user_id)
    if pref is None or pref.consent_version != CONSENT_VERSION:
        return Me(
            nickname=user.nickname,
            consent=None,
            consented_at=None,
            device_name=device_name,
        )
    return Me(
        nickname=user.nickname,
        consent=Consent(
            version=1,
            share_task_category=pref.share_task_category,
            share_stack_tags=pref.share_stack_tags,
            share_time_bucket=pref.share_time_bucket,
        ),
        consented_at=pref.consented_at,
        device_name=device_name,
    )


@router.get("")
def get_me(request: Request, principal: CurrentPrincipal) -> Me:
    check_limit(request.app.state.limiters.me_read_user, str(principal.user_id))
    with request.app.state.session_factory() as session:
        return build_me(session, principal.user_id, principal.device_id)


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
        return build_me(session, principal.user_id, principal.device_id)


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
        return build_me(session, principal.user_id, principal.device_id)


@router.patch("/device", status_code=204)
def set_device_name(
    request: Request, body: DeviceNameUpdate, principal: CurrentPrincipal
) -> Response:
    """Rename the caller's own device, no other."""
    check_limit(request.app.state.limiters.me_write_user, str(principal.user_id))
    with request.app.state.session_factory() as session:
        device = session.get(Device, principal.device_id)
        if device is None or device.user_id != principal.user_id:
            raise ApiError("unauthorized")
        device.name = body.device_name
        session.commit()
    return Response(status_code=204)

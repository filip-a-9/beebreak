"""Companion ↔ server wire contract (spec §9): HTTP bodies and WebSocket frames.

Both sides parse with these models only. Nothing here carries task metadata;
OutboundPresence (presence.py) is the one model for that, and M3 never sends it.
"""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    StringConstraints,
)

API_VERSION = 1
MAX_WS_FRAME_BYTES = 4 * 1024
CONSENT_VERSION = 1

Nickname = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{3,24}$")]
# 8 Crockford base32 characters, optionally written with one hyphen.
OneTimeCode = Annotated[str, StringConstraints(min_length=8, max_length=9)]
DeviceName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9 ._-]{1,40}$")]
EmailField = Annotated[str, StringConstraints(max_length=254)]

ErrorCode = Literal[
    "invalid_request",
    "invalid_code",
    "expired",
    "unauthorized",
    "nickname_taken",
    "nickname_reserved",
    "rate_limited",
]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AuthStartRequest(_Model):
    email: EmailField
    invite_code: OneTimeCode | None = None


class AuthStartResponse(_Model):
    challenge_id: UUID


class AuthVerifyRequest(_Model):
    challenge_id: UUID
    code: OneTimeCode
    device_name: DeviceName


class TokenPair(_Model):
    access_token: str
    access_expires_at: AwareDatetime
    refresh_token: str
    refresh_expires_at: AwareDatetime


class RefreshRequest(_Model):
    refresh_token: str


class Consent(_Model):
    version: Literal[1]
    share_task_category: bool = False
    share_stack_tags: bool = False
    share_time_bucket: bool = False


class Me(_Model):
    nickname: Nickname | None
    consent: Consent | None
    consented_at: AwareDatetime | None


class SessionResponse(_Model):
    tokens: TokenPair
    me: Me


class NicknameUpdate(_Model):
    nickname: Nickname


class ErrorResponse(_Model):
    error: ErrorCode


class HealthResponse(_Model):
    status: Literal["ok"]


# WebSocket, client → server.


class WsAuth(_Model):
    type: Literal["auth"]
    version: Literal[1]
    access_token: str


class WsPing(_Model):
    type: Literal["ping"]
    version: Literal[1]


ClientWsFrame = RootModel[Annotated[WsAuth | WsPing, Field(discriminator="type")]]


# WebSocket, server → client. Every frame carries version, event_id, server_time.


class WsReady(_Model):
    type: Literal["session.ready"]
    version: Literal[1]
    event_id: UUID
    server_time: AwareDatetime
    nickname: Nickname | None


class WsPong(_Model):
    type: Literal["pong"]
    version: Literal[1]
    event_id: UUID
    server_time: AwareDatetime


ServerWsFrame = RootModel[Annotated[WsReady | WsPong, Field(discriminator="type")]]

WsCloseReason = Literal[
    "bad_frame", "unauthorized", "auth_timeout", "replaced", "rate_limited"
]
WS_CLOSE: dict[WsCloseReason, int] = {
    "bad_frame": 4400,
    "unauthorized": 4401,
    "auth_timeout": 4408,
    "replaced": 4409,
    "rate_limited": 4429,
}

"""Companion ↔ server wire contract (spec §9): HTTP bodies and WebSocket frames.

Both sides parse with these models only. Task metadata travels only inside
OutboundPresence (presence.py), carried by QueueJoinRequest (spec §4 rule 13).
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

# Re-exported: Consent moved to its own module to avoid an import cycle.
from breakroom_shared.consent import CONSENT_VERSION as CONSENT_VERSION
from breakroom_shared.consent import Consent as Consent
from breakroom_shared.presence import OutboundPresence, StackTag, TaskCategory

API_VERSION = 1
MAX_WS_FRAME_BYTES = 4 * 1024

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
    "already_queued",
    "already_matched",
    "setup_incomplete",
    "not_found",
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


# Queue and matches (M4).


class QueueJoinRequest(_Model):
    idempotency_key: UUID
    presence: OutboundPresence


QueueStatus = Literal["queued", "matched", "cancelled", "expired"]


class QueueEntryView(_Model):
    entry_id: UUID
    status: QueueStatus
    expires_at: AwareDatetime


class MatchView(_Model):
    match_id: UUID
    peer_nickname: Nickname
    shared_tags: tuple[StackTag, ...] = Field(default=(), max_length=5)
    # Never "other": a category is shared only when both sent the same real one.
    shared_category: TaskCategory | None = None
    ends_at: AwareDatetime


# "blocked" is never sent: a blocked peer sees a plain end.
MatchEndReason = Literal["ended", "expired"]


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


class WsSync(_Model):
    type: Literal["sync"]
    version: Literal[1]
    event_id: UUID
    server_time: AwareDatetime
    queue: QueueEntryView | None
    match: MatchView | None


class WsQueueJoined(_Model):
    type: Literal["queue.joined"]
    version: Literal[1]
    event_id: UUID
    server_time: AwareDatetime
    entry: QueueEntryView


class WsQueueEnded(_Model):
    type: Literal["queue.ended"]
    version: Literal[1]
    event_id: UUID
    server_time: AwareDatetime
    entry_id: UUID
    reason: Literal["cancelled", "expired"]


class WsMatchCreated(_Model):
    type: Literal["match.created"]
    version: Literal[1]
    event_id: UUID
    server_time: AwareDatetime
    entry_id: UUID
    match: MatchView


class WsMatchEnded(_Model):
    type: Literal["match.ended"]
    version: Literal[1]
    event_id: UUID
    server_time: AwareDatetime
    match_id: UUID
    reason: MatchEndReason


ServerWsFrame = RootModel[
    Annotated[
        WsReady
        | WsPong
        | WsSync
        | WsQueueJoined
        | WsQueueEnded
        | WsMatchCreated
        | WsMatchEnded,
        Field(discriminator="type"),
    ]
]

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

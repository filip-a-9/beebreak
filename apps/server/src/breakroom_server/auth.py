"""Sign-in, token issue/refresh/logout and access-token checks. Owned by M3-02.

Every function takes the DB session and the time explicitly. Nothing here logs
an email, code, token or address (rule 10); failures raise ApiError with a
fixed error code.
"""

import hmac
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from breakroom_server.config import ServerConfig
from breakroom_server.db import SessionFactory
from breakroom_server.mailer import Mailer
from breakroom_server.models import (
    AuthChallenge,
    BetaInvite,
    Device,
    RefreshToken,
    User,
    email_canonical,
)
from breakroom_server.ratelimit import RateLimiter
from breakroom_server.tokens import (
    derive_successor,
    hash_token,
    hmac_code,
    new_code,
    new_token,
    normalize_code,
)
from breakroom_shared.api import ErrorCode, TokenPair

Clock = Callable[[], datetime]

LAST_SEEN_INTERVAL = timedelta(minutes=1)

_STATUS: dict[ErrorCode, int] = {
    "invalid_request": 400,
    "invalid_code": 400,
    "expired": 400,
    "unauthorized": 401,
    "nickname_taken": 409,
    "nickname_reserved": 409,
    "rate_limited": 429,
    "already_queued": 409,
    "already_matched": 409,
    "setup_incomplete": 409,
    "not_found": 404,
    "chat_ended": 409,
    "no_extensions_left": 409,
    "no_extension_request": 409,
    "already_reported": 409,
    "report_window_closed": 409,
}


class ApiError(Exception):
    """A request failure that is rendered as `{"error": code}`."""

    def __init__(self, code: ErrorCode, headers: dict[str, str] | None = None) -> None:
        super().__init__(code)
        self.code: ErrorCode = code
        self.status = _STATUS[code]
        self.headers = headers or {}


def unauthorized() -> ApiError:
    return ApiError("unauthorized", {"WWW-Authenticate": "Bearer"})


@dataclass(frozen=True)
class Principal:
    user_id: uuid.UUID
    device_id: uuid.UUID
    access_expires_at: datetime


@dataclass
class Limiters:
    start_email: RateLimiter
    start_ip: RateLimiter
    verify_ip: RateLimiter
    verify_email: RateLimiter  # counts failed verifies only
    refresh_device: RateLimiter
    queue_user: RateLimiter
    queue_action_user: RateLimiter
    queue_read_user: RateLimiter
    message_user: RateLimiter  # own window: rate_message_window_s
    report_user: RateLimiter
    extension_user: RateLimiter


def make_limiters(config: ServerConfig, clock: Clock) -> Limiters:
    window = float(config.rate_window_s)
    return Limiters(
        start_email=RateLimiter(config.rate_start_email, window, clock),
        start_ip=RateLimiter(config.rate_start_ip, window, clock),
        verify_ip=RateLimiter(config.rate_verify_ip, window, clock),
        verify_email=RateLimiter(config.rate_verify_email, window, clock),
        refresh_device=RateLimiter(config.rate_refresh_device, window, clock),
        queue_user=RateLimiter(config.rate_queue_user, window, clock),
        queue_action_user=RateLimiter(config.rate_queue_action_user, window, clock),
        queue_read_user=RateLimiter(config.rate_queue_read_user, window, clock),
        message_user=RateLimiter(
            config.rate_message_user, float(config.rate_message_window_s), clock
        ),
        report_user=RateLimiter(config.rate_report_user, window, clock),
        extension_user=RateLimiter(config.rate_extension_user, window, clock),
    )


def check_limit(limiter: RateLimiter, key: str) -> None:
    retry_after = limiter.hit(key)
    if retry_after is not None:
        raise ApiError("rate_limited", {"Retry-After": str(max(1, int(retry_after)))})


# --- access tokens -----------------------------------------------------------


def authenticate_access(
    session: Session, token: str, now: datetime
) -> Principal | None:
    """The caller of a valid, unexpired access token on an active device/user."""
    if not token.startswith("bra_"):
        return None
    device = session.scalars(
        select(Device).where(Device.access_hash == hash_token(token))
    ).first()
    if device is None or device.revoked_at is not None:
        return None
    if device.access_expires_at is None or device.access_expires_at <= now:
        return None
    user = session.get(User, device.user_id)
    if user is None or user.status != "active":
        return None
    if device.last_seen_at is None or now - device.last_seen_at >= LAST_SEEN_INTERVAL:
        device.last_seen_at = now
        session.commit()
    return Principal(user.id, device.id, device.access_expires_at)


def require_principal(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> Principal:
    """FastAPI dependency: the authenticated caller, else 401 `unauthorized`."""
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise unauthorized()
    app = request.app
    factory: SessionFactory = app.state.session_factory
    now = app.state.clock()
    with factory() as session:
        principal = authenticate_access(session, token.strip(), now)
    if principal is None:
        raise unauthorized()
    request.state.user_id = principal.user_id
    request.state.device_id = principal.device_id
    return principal


CurrentPrincipal = Annotated[Principal, Depends(require_principal)]


# --- sign-in -----------------------------------------------------------------


def start_challenge(
    session: Session,
    config: ServerConfig,
    mailer: Mailer,
    now: datetime,
    *,
    raw_email: str,
    invite_code: str | None,
) -> uuid.UUID:
    """Create a challenge row; email a code only when the caller may sign in.

    Always returns an id, so the response never says whether an account exists.
    """
    email = email_canonical(raw_email)
    if email is None:
        raise ApiError("invalid_request")

    user = session.scalars(select(User).where(User.email_canonical == email)).first()
    invite: BetaInvite | None = None
    deliver = False
    if user is not None:
        deliver = user.status == "active"
    elif invite_code is not None:
        invite = _open_invite(session, config, now, email, invite_code)
        deliver = invite is not None

    # The code is generated either way so both paths store the same shape.
    # Earlier open challenges stay valid: a stranger's start must not cancel the
    # owner's code. The per-email verify budget bounds guessing across them.
    code = new_code()
    challenge = AuthChallenge(
        email_canonical=email,
        code_hmac=hmac_code(config.secret, code),
        invite_id=invite.id if invite is not None else None,
        expires_at=now + timedelta(seconds=config.code_ttl_s),
        created_at=now,
    )
    session.add(challenge)
    session.commit()
    if deliver:
        mailer.send_code(email, code)
    return challenge.id


def _open_invite(
    session: Session,
    config: ServerConfig,
    now: datetime,
    email: str,
    raw_code: str,
) -> BetaInvite | None:
    code = normalize_code(raw_code)
    if code is None:
        return None
    return session.scalars(
        select(BetaInvite).where(
            BetaInvite.code_hmac == hmac_code(config.secret, code),
            BetaInvite.email_canonical == email,
            BetaInvite.redeemed_at.is_(None),
            BetaInvite.revoked_at.is_(None),
            BetaInvite.expires_at > now,
        )
    ).first()


@dataclass(frozen=True)
class SignedIn:
    user_id: uuid.UUID
    tokens: TokenPair


def verify_challenge(
    session: Session,
    config: ServerConfig,
    now: datetime,
    *,
    challenge_id: uuid.UUID,
    raw_code: str,
    device_name: str,
    verify_email: RateLimiter,
) -> SignedIn:
    challenge = session.get(AuthChallenge, challenge_id)
    if challenge is None or challenge.consumed_at is not None:
        raise ApiError("invalid_code")
    if challenge.expires_at <= now:
        raise ApiError("expired")
    if challenge.attempts >= config.code_max_attempts:
        raise ApiError("invalid_code")
    # Failed guesses across all of an email's open challenges share one budget.
    wait = verify_email.blocked(challenge.email_canonical)
    if wait is not None:
        raise ApiError("rate_limited", {"Retry-After": str(max(1, int(wait)))})

    code = normalize_code(raw_code)
    given = hmac_code(config.secret, code if code is not None else "")
    if code is None or not hmac.compare_digest(challenge.code_hmac, given):
        verify_email.hit(challenge.email_canonical)
        # The cap is part of the UPDATE, so parallel guesses cannot pass it.
        session.execute(
            update(AuthChallenge)
            .where(
                AuthChallenge.id == challenge.id,
                AuthChallenge.attempts < config.code_max_attempts,
            )
            .values(attempts=AuthChallenge.attempts + 1)
        )
        session.commit()
        raise ApiError("invalid_code")

    # Single use: whoever flips consumed_at first wins.
    consumed = session.execute(
        update(AuthChallenge)
        .where(
            AuthChallenge.id == challenge.id,
            AuthChallenge.consumed_at.is_(None),
            AuthChallenge.attempts < config.code_max_attempts,
        )
        .values(consumed_at=now)
    )
    if consumed.rowcount != 1:  # type: ignore[attr-defined]
        session.rollback()
        raise ApiError("invalid_code")

    try:
        user = _account_for(session, challenge, now)
        device = Device(user_id=user.id, name=device_name, created_at=now)
        session.add(device)
        session.flush()
        tokens = issue_tokens(session, config, device, now)
        session.commit()
    except IntegrityError:
        session.rollback()
        raise ApiError("invalid_code") from None
    except ApiError:
        session.rollback()
        raise
    return SignedIn(user.id, tokens)


def _account_for(session: Session, challenge: AuthChallenge, now: datetime) -> User:
    """The signing-in user; creates the account when the challenge has an invite."""
    if challenge.invite_id is not None:
        redeemed = session.execute(
            update(BetaInvite)
            .where(
                BetaInvite.id == challenge.invite_id,
                BetaInvite.redeemed_at.is_(None),
                BetaInvite.revoked_at.is_(None),
                BetaInvite.expires_at > now,
            )
            .values(redeemed_at=now)
        )
        if redeemed.rowcount != 1:  # type: ignore[attr-defined]
            raise ApiError("invalid_code")
        user = User(email_canonical=challenge.email_canonical, created_at=now)
        session.add(user)
        session.flush()
        session.execute(
            update(BetaInvite)
            .where(BetaInvite.id == challenge.invite_id)
            .values(redeemed_by=user.id)
        )
        return user
    existing = session.scalars(
        select(User).where(User.email_canonical == challenge.email_canonical)
    ).first()
    if existing is None or existing.status != "active":
        raise ApiError("invalid_code")
    return existing


def issue_tokens(
    session: Session,
    config: ServerConfig,
    device: Device,
    now: datetime,
    *,
    parent_token: str | None = None,
) -> TokenPair:
    """New access + refresh pair for the device; the previous access dies.

    A rotation passes the used raw token, which makes the pair reproducible
    for the grace window (see `refresh_tokens`); a sign-in gets random tokens.
    """
    if parent_token is None:
        access = new_token("bra_")
        refresh = new_token("brr_")
    else:
        access, refresh = derive_successor(config.secret, parent_token)
    access_expires = now + timedelta(seconds=config.access_ttl_s)
    refresh_expires = now + timedelta(seconds=config.refresh_ttl_s)
    device.access_hash = hash_token(access)
    device.access_expires_at = access_expires
    device.last_seen_at = now
    session.add(
        RefreshToken(
            token_hash=hash_token(refresh),
            device_id=device.id,
            expires_at=refresh_expires,
        )
    )
    return TokenPair(
        access_token=access,
        access_expires_at=access_expires,
        refresh_token=refresh,
        refresh_expires_at=refresh_expires,
    )


# --- refresh / logout --------------------------------------------------------


@dataclass(frozen=True)
class Refreshed:
    tokens: TokenPair | None
    revoked_device: uuid.UUID | None  # set when reuse detection fired


def revoke_device(session: Session, device: Device, now: datetime) -> None:
    device.revoked_at = now
    device.access_hash = None
    device.connected_at = None
    session.execute(delete(RefreshToken).where(RefreshToken.device_id == device.id))


def refresh_tokens(
    session: Session,
    config: ServerConfig,
    limiters: Limiters,
    now: datetime,
    raw_token: str,
) -> Refreshed:
    """Rotate a refresh token. Returns tokens, or `revoked_device` on reuse."""
    if not raw_token.startswith("brr_"):
        raise unauthorized()
    token_hash = hash_token(raw_token)
    row = session.get(RefreshToken, token_hash)
    if row is None:
        raise unauthorized()
    device = session.get(Device, row.device_id)
    if device is None:
        raise unauthorized()
    check_limit(limiters.refresh_device, str(device.id))
    user = session.get(User, device.user_id)
    if device.revoked_at is not None or user is None or user.status != "active":
        raise unauthorized()
    if row.expires_at <= now:
        raise unauthorized()

    # Mark used first; losing this race is indistinguishable from replay.
    marked = session.execute(
        update(RefreshToken)
        .where(RefreshToken.token_hash == token_hash, RefreshToken.used_at.is_(None))
        .values(used_at=now)
    )
    if marked.rowcount != 1:  # type: ignore[attr-defined]
        replayed = _replay_in_grace(session, config, device, raw_token, now)
        if replayed is not None:
            return Refreshed(replayed, None)
        revoke_device(session, device, now)
        session.commit()
        return Refreshed(None, device.id)
    tokens = issue_tokens(session, config, device, now, parent_token=raw_token)
    session.commit()
    return Refreshed(tokens, None)


def _replay_in_grace(
    session: Session,
    config: ServerConfig,
    device: Device,
    raw_token: str,
    now: datetime,
) -> TokenPair | None:
    """The same successor pair, if the token was rotated at most the grace ago
    and nothing newer has been issued since; else None (a real reuse)."""
    used_at = session.scalar(
        select(RefreshToken.used_at).where(
            RefreshToken.token_hash == hash_token(raw_token)
        )
    )
    if used_at is None or now - used_at > timedelta(seconds=config.refresh_grace_s):
        return None
    access, refresh = derive_successor(config.secret, raw_token)
    successor = session.get(RefreshToken, hash_token(refresh))
    session.refresh(device)
    if (
        successor is None
        or successor.device_id != device.id
        or successor.used_at is not None
        or device.revoked_at is not None
        or device.access_hash != hash_token(access)
        or device.access_expires_at is None
    ):
        return None
    return TokenPair(
        access_token=access,
        access_expires_at=device.access_expires_at,
        refresh_token=refresh,
        refresh_expires_at=successor.expires_at,
    )


def logout_device(session: Session, device_id: uuid.UUID, now: datetime) -> None:
    device = session.get(Device, device_id)
    if device is not None and device.revoked_at is None:
        revoke_device(session, device, now)
        session.commit()

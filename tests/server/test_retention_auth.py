"""M6-04: the purge removes stale auth rows, driven through run_once()."""

import dataclasses
import logging
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from beebreak_server.config import ServerConfig
from beebreak_server.db import SessionFactory
from beebreak_server.matchmaking import Matchmaker
from beebreak_server.models import AuthChallenge, BetaInvite, RefreshToken
from tests.server.conftest import START_TIME, FixedClock, SeededUser, add_ready_user
from tests.server.fakes import RecordingNotifier

CHALLENGE_S = 1_000
TOKEN_S = 2_000
INVITE_S = 3_000
EMAIL = "someone@example.com"
FUTURE = START_TIME + timedelta(days=365)


@dataclasses.dataclass
class Rig:
    factory: SessionFactory
    clock: FixedClock
    mm: Matchmaker
    user: SeededUser

    def sweep_at(self, now: datetime) -> None:
        self.clock.now = now
        self.mm.run_once()

    def exists(self, model: type, key: object) -> bool:
        with self.factory() as s:
            return s.get(model, key) is not None


@pytest.fixture
def rig(
    session_factory: SessionFactory, server_config: ServerConfig, clock: FixedClock
) -> Rig:
    config = dataclasses.replace(
        server_config,
        challenge_retention_s=CHALLENGE_S,
        refresh_token_retention_s=TOKEN_S,
        invite_retention_s=INVITE_S,
    )
    mm = Matchmaker(session_factory, config, clock, RecordingNotifier())
    return Rig(session_factory, clock, mm, add_ready_user(session_factory, "Ada"))


def _ago(seconds: int) -> datetime:
    """A time `seconds` before START_TIME (the sweep time in most tests)."""
    return START_TIME - timedelta(seconds=seconds)


def _challenge(
    rig: Rig,
    *,
    expires_at: datetime,
    consumed_at: datetime | None = None,
    invite_id: uuid.UUID | None = None,
) -> uuid.UUID:
    with rig.factory() as s:
        row = AuthChallenge(
            email_canonical=EMAIL,
            code_hmac=b"c" * 32,
            invite_id=invite_id,
            expires_at=expires_at,
            consumed_at=consumed_at,
            created_at=_ago(10 * INVITE_S),
        )
        s.add(row)
        s.commit()
        return row.id


def _invite(
    rig: Rig,
    *,
    expires_at: datetime,
    redeemed_at: datetime | None = None,
    revoked_at: datetime | None = None,
) -> uuid.UUID:
    with rig.factory() as s:
        row = BetaInvite(
            email_canonical=EMAIL,
            code_hmac=uuid.uuid4().bytes * 2,
            expires_at=expires_at,
            redeemed_at=redeemed_at,
            revoked_at=revoked_at,
            created_at=_ago(10 * INVITE_S),
        )
        s.add(row)
        s.commit()
        return row.id


def _token(rig: Rig, *, expires_at: datetime, used_at: datetime | None = None) -> bytes:
    token_hash = uuid.uuid4().bytes * 2
    with rig.factory() as s:
        s.add(
            RefreshToken(
                token_hash=token_hash,
                device_id=rig.user.device_id,
                expires_at=expires_at,
                used_at=used_at,
            )
        )
        s.commit()
    return token_hash


# --- auth_challenges -------------------------------------------------------


@pytest.mark.parametrize(("offset", "kept"), [(-1, True), (1, False)])
def test_consumed_challenge_purged_after_retention(
    rig: Rig, offset: int, kept: bool
) -> None:
    cid = _challenge(
        rig,
        expires_at=FUTURE,  # consumed_at decides, not the far expiry
        consumed_at=_ago(CHALLENGE_S + offset),
    )
    rig.sweep_at(START_TIME)
    assert rig.exists(AuthChallenge, cid) is kept


@pytest.mark.parametrize(("offset", "kept"), [(-1, True), (1, False)])
def test_unconsumed_challenge_purged_by_expiry(
    rig: Rig, offset: int, kept: bool
) -> None:
    cid = _challenge(rig, expires_at=_ago(CHALLENGE_S + offset))
    rig.sweep_at(START_TIME)
    assert rig.exists(AuthChallenge, cid) is kept


def test_open_challenge_survives(rig: Rig) -> None:
    cid = _challenge(rig, expires_at=START_TIME + timedelta(seconds=600))
    rig.sweep_at(START_TIME)
    assert rig.exists(AuthChallenge, cid)


# --- refresh_tokens --------------------------------------------------------


def test_used_but_unexpired_token_survives_any_age(rig: Rig) -> None:
    # Replay detection needs the used row for the token's whole lifetime.
    h = _token(rig, expires_at=FUTURE, used_at=_ago(10 * TOKEN_S))
    rig.sweep_at(START_TIME)
    assert rig.exists(RefreshToken, h)


@pytest.mark.parametrize("used", [False, True])
@pytest.mark.parametrize(("offset", "kept"), [(-1, True), (1, False)])
def test_token_purged_after_expiry_plus_retention(
    rig: Rig, used: bool, offset: int, kept: bool
) -> None:
    h = _token(
        rig,
        expires_at=_ago(TOKEN_S + offset),
        used_at=_ago(10 * TOKEN_S) if used else None,
    )
    rig.sweep_at(START_TIME)
    assert rig.exists(RefreshToken, h) is kept


def test_unused_unexpired_token_survives_any_age(rig: Rig) -> None:
    h = _token(rig, expires_at=START_TIME + timedelta(seconds=1))
    rig.sweep_at(START_TIME + timedelta(seconds=0))
    assert rig.exists(RefreshToken, h)


# --- beta_invites ----------------------------------------------------------


@pytest.mark.parametrize(("offset", "kept"), [(-1, True), (1, False)])
@pytest.mark.parametrize("terminal", ["revoked", "redeemed", "expired"])
def test_invite_purged_after_terminal_retention(
    rig: Rig, terminal: str, offset: int, kept: bool
) -> None:
    at = _ago(INVITE_S + offset)
    if terminal == "revoked":
        iid = _invite(rig, expires_at=FUTURE, revoked_at=at)
    elif terminal == "redeemed":
        iid = _invite(rig, expires_at=FUTURE, redeemed_at=at)
    else:
        iid = _invite(rig, expires_at=at)
    rig.sweep_at(START_TIME)
    assert rig.exists(BetaInvite, iid) is kept


def test_invite_uses_earliest_terminal_time(rig: Rig) -> None:
    iid = _invite(
        rig, expires_at=FUTURE, redeemed_at=_ago(INVITE_S + 1), revoked_at=_ago(5)
    )
    rig.sweep_at(START_TIME)
    assert not rig.exists(BetaInvite, iid)


def test_redeemed_invite_counts_from_redemption_not_its_old_expiry(rig: Rig) -> None:
    iid = _invite(rig, expires_at=_ago(INVITE_S + 5), redeemed_at=_ago(5))
    rig.sweep_at(START_TIME)
    assert rig.exists(BetaInvite, iid)


def test_open_invite_survives(rig: Rig) -> None:
    iid = _invite(rig, expires_at=FUTURE)
    rig.sweep_at(START_TIME)
    assert rig.exists(BetaInvite, iid)


def test_invite_referenced_by_retained_challenge_waits_for_its_removal(
    rig: Rig,
) -> None:
    iid = _invite(rig, expires_at=_ago(INVITE_S + 1))
    cid = _challenge(
        rig,
        expires_at=_ago(CHALLENGE_S - 100),  # retained for another 100 s
        invite_id=iid,
    )
    rig.sweep_at(START_TIME)
    assert rig.exists(BetaInvite, iid)
    assert rig.exists(AuthChallenge, cid)
    # Challenges are deleted first, so the invite goes in the same sweep.
    rig.sweep_at(START_TIME + timedelta(seconds=101))
    assert not rig.exists(AuthChallenge, cid)
    assert not rig.exists(BetaInvite, iid)


def test_purge_log_carries_counts_only(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    _challenge(rig, expires_at=_ago(CHALLENGE_S + 1))
    caplog.set_level(logging.INFO)
    rig.sweep_at(START_TIME)
    assert "challenges=1" in caplog.text
    assert EMAIL not in caplog.text
    with rig.factory() as s:
        assert s.scalars(select(AuthChallenge)).first() is None

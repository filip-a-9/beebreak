from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

import pytest
from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import IntegrityError, StatementError

from breakroom_server.db import SessionFactory, make_engine
from breakroom_server.models import (
    AuthChallenge,
    BetaInvite,
    Device,
    RefreshToken,
    User,
    UserPreference,
    email_canonical,
)
from tests.server.conftest import START_TIME, FixedClock


def _user(email: str = "a@example.com", **kw: object) -> User:
    return User(email_canonical=email, created_at=START_TIME, **kw)


def test_every_model_round_trips(
    session_factory: SessionFactory, clock: FixedClock
) -> None:
    now = clock()
    with session_factory() as s:
        user = _user(nickname="Ada", nickname_skeleton="ada")
        s.add(user)
        s.flush()
        invite = BetaInvite(
            email_canonical="a@example.com",
            code_hmac=b"\x01" * 32,
            expires_at=now + timedelta(days=7),
            redeemed_at=now,
            redeemed_by=user.id,
            created_at=now,
        )
        s.add(invite)
        s.flush()
        challenge = AuthChallenge(
            email_canonical="a@example.com",
            code_hmac=b"\x02" * 32,
            invite_id=invite.id,
            expires_at=now + timedelta(minutes=10),
            created_at=now,
        )
        device = Device(
            user_id=user.id,
            name="laptop",
            access_hash=b"\x03" * 32,
            access_expires_at=now + timedelta(minutes=15),
            created_at=now,
            last_seen_at=now,
        )
        s.add_all([challenge, device])
        s.flush()
        s.add(
            RefreshToken(
                token_hash=b"\x04" * 32,
                device_id=device.id,
                expires_at=now + timedelta(days=30),
            )
        )
        s.add(
            UserPreference(
                user_id=user.id,
                consent_version=1,
                share_stack_tags=True,
                consented_at=now,
            )
        )
        s.commit()
        ids = (user.id, invite.id, challenge.id, device.id)

    with session_factory() as s:
        u2 = s.get(User, ids[0])
        assert u2 is not None
        assert isinstance(u2.id, UUID)
        assert (u2.nickname, u2.status, u2.created_at) == ("Ada", "active", now)
        inv2 = s.get(BetaInvite, ids[1])
        assert inv2 is not None
        assert inv2.redeemed_by == u2.id and inv2.revoked_at is None
        assert inv2.redeemed_at == now and inv2.code_hmac == b"\x01" * 32
        ch2 = s.get(AuthChallenge, ids[2])
        assert ch2 is not None
        assert ch2.attempts == 0 and ch2.consumed_at is None
        dev2 = s.get(Device, ids[3])
        assert dev2 is not None
        assert dev2.access_expires_at == now + timedelta(minutes=15)
        assert dev2.connected_at is None
        token = s.get(RefreshToken, b"\x04" * 32)
        assert token is not None and token.used_at is None
        pref = s.get(UserPreference, u2.id)
        assert pref is not None
        assert (pref.share_task_category, pref.share_stack_tags) == (False, True)
        assert pref.consented_at == now


def test_timestamps_come_back_aware_utc(session_factory: SessionFactory) -> None:
    plus_two = timezone(timedelta(hours=2))
    with session_factory() as s:
        user = User(
            email_canonical="tz@example.com",
            created_at=datetime(2026, 10, 13, 14, 0, tzinfo=plus_two),
        )
        s.add(user)
        s.commit()
        uid = user.id
    with session_factory() as s:
        got = s.get(User, uid)
        assert got is not None
        assert got.created_at == datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
        assert got.created_at.utcoffset() == timedelta(0)


def test_naive_timestamp_is_rejected(session_factory: SessionFactory) -> None:
    with session_factory() as s:
        s.add(User(email_canonical="n@example.com", created_at=datetime(2026, 1, 1)))
        with pytest.raises((ValueError, StatementError)):
            s.commit()


def _commit_fails(session_factory: SessionFactory, *rows: object) -> None:
    with session_factory() as s:
        s.add_all(list(rows))
        with pytest.raises(IntegrityError):
            s.commit()


def test_email_is_unique(session_factory: SessionFactory) -> None:
    with session_factory() as s:
        s.add(_user("dup@example.com"))
        s.commit()
    _commit_fails(session_factory, _user("dup@example.com"))


def test_nickname_skeleton_is_unique_but_may_be_null(
    session_factory: SessionFactory,
) -> None:
    with session_factory() as s:
        s.add_all([_user("a@example.com"), _user("b@example.com")])  # two NULLs
        s.add(_user("c@example.com", nickname="Ada", nickname_skeleton="ada"))
        s.commit()
    _commit_fails(
        session_factory,
        _user("d@example.com", nickname="ADA", nickname_skeleton="ada"),
    )


def test_invite_code_hmac_is_unique(session_factory: SessionFactory) -> None:
    def invite() -> BetaInvite:
        return BetaInvite(
            email_canonical="a@example.com",
            code_hmac=b"\x07" * 32,
            expires_at=START_TIME,
            created_at=START_TIME,
        )

    with session_factory() as s:
        s.add(invite())
        s.commit()
    _commit_fails(session_factory, invite())


def test_device_access_hash_is_unique_but_may_be_null(
    session_factory: SessionFactory,
) -> None:
    with session_factory() as s:
        user = _user()
        s.add(user)
        s.commit()
        uid = user.id

    def device(access_hash: bytes | None) -> Device:
        return Device(
            user_id=uid, name="d", access_hash=access_hash, created_at=START_TIME
        )

    with session_factory() as s:
        s.add_all([device(None), device(None), device(b"\x09" * 32)])
        s.commit()
    _commit_fails(session_factory, device(b"\x09" * 32))


def test_status_and_attempts_checks(session_factory: SessionFactory) -> None:
    _commit_fails(session_factory, _user(status="banned"))
    _commit_fails(
        session_factory,
        AuthChallenge(
            email_canonical="a@example.com",
            code_hmac=b"\x00" * 32,
            expires_at=START_TIME,
            attempts=-1,
            created_at=START_TIME,
        ),
    )


def test_foreign_keys_are_enforced(session_factory: SessionFactory) -> None:
    from uuid import uuid4

    _commit_fails(
        session_factory,
        Device(user_id=uuid4(), name="orphan", created_at=START_TIME),
    )


def test_challenge_index_exists(engine: Engine) -> None:
    indexes = inspect(engine).get_indexes("auth_challenges")
    assert any(i["column_names"] == ["email_canonical", "consumed_at"] for i in indexes)


def test_sqlite_in_memory_engine_is_shared_and_enforces_foreign_keys() -> None:
    engine = make_engine("sqlite://")
    with engine.connect() as c:
        assert c.execute(text("PRAGMA foreign_keys")).scalar() == 1
    engine.dispose()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("a@example.com", "a@example.com"),
        ("  Ada.Lovelace@Example.COM \n", "ada.lovelace@example.com"),
        ("a+tag@sub.example.co", "a+tag@sub.example.co"),
        ("a@b@example.com", None),
        ("no-at.example.com", None),
        ("@example.com", None),
        ("a@", None),
        ("a@localhost", None),
        ("a@.example.com", None),
        ("a@example.com.", None),
        ("a b@example.com", None),
        ("a@exa mple.com", None),
        ("", None),
        ("   ", None),
        ("a@example.com\x00", None),
        (("x" * 242) + "@example.com", ("x" * 242) + "@example.com"),  # 254
        (("x" * 243) + "@example.com", None),  # 255
    ],
)
def test_email_canonical(raw: str, expected: str | None) -> None:
    assert email_canonical(raw) == expected

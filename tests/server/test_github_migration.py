"""Migration 0005: GitHub accounts, invites and challenges, up and down.

SQLite always; PostgreSQL when BREAKROOM_TEST_DATABASE_URL points at it (the
test drops the app's tables first, like test_migrations).
"""

import uuid
from collections.abc import Mapping
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import IntegrityError

from breakroom_server.db import alembic_config, make_engine
from breakroom_server.models import Base
from tests.server.conftest import START_TIME, TEST_DATABASE_URL, requires_postgres

USERS = sa.table(
    "users",
    sa.column("id", sa.Uuid()),
    sa.column("email_canonical", sa.String()),
    sa.column("github_id", sa.BigInteger()),
    sa.column("status", sa.String()),
    sa.column("created_at", sa.DateTime(timezone=True)),
)
OLD_USERS = sa.table(  # the columns that exist at revision 0004
    "users",
    sa.column("id", sa.Uuid()),
    sa.column("email_canonical", sa.String()),
    sa.column("status", sa.String()),
    sa.column("created_at", sa.DateTime(timezone=True)),
)
INVITES = sa.table(
    "beta_invites",
    sa.column("id", sa.Uuid()),
    sa.column("email_canonical", sa.String()),
    sa.column("github_id", sa.BigInteger()),
    sa.column("code_hmac", sa.LargeBinary()),
    sa.column("expires_at", sa.DateTime(timezone=True)),
    sa.column("created_at", sa.DateTime(timezone=True)),
)
CHALLENGES = sa.table(
    "auth_challenges",
    sa.column("id", sa.Uuid()),
    sa.column("email_canonical", sa.String()),
    sa.column("code_hmac", sa.LargeBinary()),
    sa.column("github_device_code", sa.String()),
    sa.column("github_interval_s", sa.Integer()),
    sa.column("github_next_poll_at", sa.DateTime(timezone=True)),
    sa.column("expires_at", sa.DateTime(timezone=True)),
    sa.column("attempts", sa.Integer()),
    sa.column("created_at", sa.DateTime(timezone=True)),
)
GITHUB_COLUMNS = {
    "users": {"github_id"},
    "beta_invites": {"github_id"},
    "auth_challenges": {
        "github_device_code",
        "github_interval_s",
        "github_next_poll_at",
    },
}


def _diff(engine: Engine) -> list[object]:
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True})
        return list(compare_metadata(ctx, Base.metadata))


def _columns(engine: Engine, table: str) -> set[str]:
    return {c["name"] for c in inspect(engine).get_columns(table)}


def _user(**values: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": uuid.uuid4(),
        "email_canonical": None,
        "github_id": None,
        "status": "active",
        "created_at": START_TIME,
    }
    return base | values


def _invite(**values: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": uuid.uuid4(),
        "email_canonical": None,
        "github_id": None,
        "code_hmac": None,
        "expires_at": START_TIME,
        "created_at": START_TIME,
    }
    return base | values


def _insert_fails(
    engine: Engine, table: sa.TableClause, row: Mapping[str, object]
) -> bool:
    try:
        with engine.begin() as conn:
            conn.execute(sa.insert(table).values(**row))
    except IntegrityError:
        return True
    return False


def _round_trip(url: str) -> None:
    config = alembic_config(url)
    command.upgrade(config, "0004")
    engine = make_engine(url)
    try:
        # An email account that exists before the upgrade.
        with engine.begin() as conn:
            conn.execute(
                sa.insert(OLD_USERS).values(
                    id=uuid.uuid4(),
                    email_canonical="old@example.com",
                    status="active",
                    created_at=START_TIME,
                )
            )
        command.upgrade(config, "head")
        assert _diff(engine) == []
        for table, names in GITHUB_COLUMNS.items():
            assert names <= _columns(engine, table)
        with engine.connect() as conn:
            kept = conn.execute(
                text("SELECT email_canonical, github_id FROM users")
            ).all()
        assert [tuple(r) for r in kept] == [("old@example.com", None)]

        # Users: a GitHub-only account, uniqueness and the identity check.
        with engine.begin() as conn:
            conn.execute(sa.insert(USERS).values(**_user(github_id=9_000_000_001)))
            conn.execute(sa.insert(USERS).values(**_user(github_id=2)))
        assert _insert_fails(engine, USERS, _user(github_id=2))
        assert _insert_fails(engine, USERS, _user())  # neither email nor GitHub

        # Invites: exactly one of email and GitHub id.
        with engine.begin() as conn:
            conn.execute(sa.insert(INVITES).values(**_invite(github_id=7)))
            conn.execute(
                sa.insert(INVITES).values(
                    **_invite(email_canonical="x@example.com", code_hmac=b"c" * 32)
                )
            )
        assert _insert_fails(engine, INVITES, _invite())
        assert _insert_fails(
            engine,
            INVITES,
            _invite(email_canonical="y@example.com", github_id=8, code_hmac=b"d" * 32),
        )

        # Challenges: an email one or a GitHub one, nothing else.
        github_challenge = {
            "id": uuid.uuid4(),
            "email_canonical": None,
            "code_hmac": None,
            "github_device_code": "d" * 40,
            "github_interval_s": 5,
            "github_next_poll_at": START_TIME,
            "expires_at": START_TIME,
            "attempts": 0,
            "created_at": START_TIME,
        }
        with engine.begin() as conn:
            conn.execute(sa.insert(CHALLENGES).values(**github_challenge))
        assert _insert_fails(
            engine,
            CHALLENGES,
            github_challenge
            | {
                "id": uuid.uuid4(),
                "github_interval_s": None,
                "github_device_code": None,
            },
        )

        # Down with GitHub accounts present is refused and changes nothing.
        with pytest.raises(Exception):  # noqa: B017 - alembic wraps it
            command.downgrade(config, "0004")
        assert "github_id" in _columns(engine, "users")

        # Remove the GitHub rows; the downgrade then works and keeps the email.
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM users WHERE github_id IS NOT NULL"))
        command.downgrade(config, "0004")
        for table, names in GITHUB_COLUMNS.items():
            assert not names & _columns(engine, table)
        with engine.connect() as conn:
            emails = conn.execute(text("SELECT email_canonical FROM users")).all()
            invites = conn.execute(
                text("SELECT email_canonical FROM beta_invites")
            ).all()
            challenges = conn.execute(
                text("SELECT count(*) FROM auth_challenges")
            ).one()
        assert [tuple(r) for r in emails] == [("old@example.com",)]
        # GitHub invites and challenges are dropped; the email invite stays.
        assert [tuple(r) for r in invites] == [("x@example.com",)]
        assert challenges[0] == 0

        command.upgrade(config, "head")
        assert _diff(engine) == []
    finally:
        engine.dispose()


def test_sqlite_0005_round_trip(tmp_path: Path) -> None:
    _round_trip(f"sqlite:///{tmp_path / 'github.db'}")


@requires_postgres
def test_postgres_0005_round_trip() -> None:
    assert TEST_DATABASE_URL is not None
    engine = make_engine(TEST_DATABASE_URL)
    Base.metadata.drop_all(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
    try:
        _round_trip(TEST_DATABASE_URL)
    finally:
        Base.metadata.drop_all(engine)
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
        engine.dispose()

"""Shared server fixtures; later M3 tickets reuse these.

- `engine`: a fresh database with the schema created from the models. SQLite in
  a temp *file* by default (several threads can share it); PostgreSQL when
  BREAKROOM_TEST_DATABASE_URL is set (its tables are dropped and recreated).
- `session_factory`, `server_config`, `clock` build on it.

BREAKROOM_TEST_DATABASE_URL is read once at import time. tests/conftest.py
deletes every BREAKROOM_* variable before each test, so a fixture reading the
variable later would never see it. Import `TEST_DATABASE_URL` instead.
"""

import os
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import sessionmaker

from breakroom_server.app import create_app
from breakroom_server.config import ServerConfig
from breakroom_server.db import SessionFactory, make_engine
from breakroom_server.mailer import RecordingMailer
from breakroom_server.models import Base, BetaInvite, User
from breakroom_server.tokens import hmac_code, new_code

TEST_DATABASE_URL: str | None = os.environ.get("BREAKROOM_TEST_DATABASE_URL") or None
TEST_SECRET = b"breakroom-test-secret-0123456789"  # 32 bytes, tests only
START_TIME = datetime(2026, 10, 13, 12, 0, 0, tzinfo=UTC)

requires_postgres = pytest.mark.skipif(
    TEST_DATABASE_URL is None or "postgres" not in TEST_DATABASE_URL,
    reason="set BREAKROOM_TEST_DATABASE_URL to a PostgreSQL URL",
)


class FixedClock:
    """Callable clock that only moves when the test says so."""

    def __init__(self, now: datetime = START_TIME) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> datetime:
        self.now += timedelta(seconds=seconds)
        return self.now


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock()


@pytest.fixture
def database_url(tmp_path: Path) -> str:
    if TEST_DATABASE_URL is not None:
        return TEST_DATABASE_URL
    return f"sqlite:///{tmp_path / 'breakroom-test.db'}"


@pytest.fixture
def engine(database_url: str) -> Iterator[Engine]:
    engine = make_engine(database_url)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> SessionFactory:
    return sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def server_config(database_url: str) -> ServerConfig:
    return ServerConfig(database_url=database_url, secret=TEST_SECRET)


@pytest.fixture
def server_env(
    monkeypatch: pytest.MonkeyPatch, engine: Engine, database_url: str
) -> None:
    """Environment for code that calls ServerConfig.from_env()."""
    import base64

    monkeypatch.setenv("BREAKROOM_DATABASE_URL", database_url)
    monkeypatch.setenv(
        "BREAKROOM_SERVER_SECRET", base64.urlsafe_b64encode(TEST_SECRET).decode()
    )


# --- app fixtures and sign-in helpers (M3-02; reused by later tickets) ---------

CODE = "ABCD2345"  # a valid invite code for tests


@pytest.fixture
def mailer() -> RecordingMailer:
    return RecordingMailer()


@pytest.fixture
def app(
    server_config: ServerConfig,
    engine: Engine,
    mailer: RecordingMailer,
    clock: FixedClock,
) -> FastAPI:
    return create_app(server_config, mailer=mailer, clock=clock)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


def add_invite(
    session_factory: SessionFactory,
    email: str,
    *,
    code: str = CODE,
    now: datetime = START_TIME,
    ttl_s: int = 604_800,
) -> None:
    with session_factory() as s:
        s.add(
            BetaInvite(
                email_canonical=email,
                code_hmac=hmac_code(TEST_SECRET, code),
                expires_at=now + timedelta(seconds=ttl_s),
                created_at=now,
            )
        )
        s.commit()


def sign_in(
    client: TestClient,
    session_factory: SessionFactory,
    mailer: RecordingMailer,
    email: str = "ada@example.com",
    device_name: str = "laptop",
) -> dict[str, Any]:
    """Sign in through the API (inviting the email first if it has no account).

    Returns the SessionResponse JSON. Clears the mailer's outbox.
    """
    with session_factory() as s:
        known = s.scalars(select(User).where(User.email_canonical == email)).first()
    body: dict[str, Any] = {"email": email}
    if known is None:
        invite_code = new_code()
        add_invite(session_factory, email, code=invite_code)
        body["invite_code"] = invite_code
    mailer.sent.clear()
    started = client.post("/v1/auth/start", json=body)
    assert started.status_code == 202
    (_, _, code) = mailer.sent[-1]
    mailer.sent.clear()
    verified = client.post(
        "/v1/auth/verify",
        json={
            "challenge_id": started.json()["challenge_id"],
            "code": code,
            "device_name": device_name,
        },
    )
    assert verified.status_code == 200, verified.text
    result: dict[str, Any] = verified.json()
    return result


def bearer(session: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {session['tokens']['access_token']}"}


@pytest.fixture(autouse=True)
def _logs_hold_no_secrets(
    request: pytest.FixtureRequest, caplog: pytest.LogCaptureFixture
) -> Iterator[None]:
    """Rule 10, for every server test: no email, code or token in any log line."""
    caplog.set_level("DEBUG")
    mailer: RecordingMailer | None = (
        request.getfixturevalue("mailer") if "mailer" in request.fixturenames else None
    )
    yield
    text = caplog.text
    assert not re.search(r"[\w.+-]+@[\w-]+\.[a-z]{2,}", text), "email in log"
    assert "bra_" not in text and "brr_" not in text, "token in log"
    if mailer is not None:
        for _, _, code in mailer.sent:
            assert code not in text, "code in log"

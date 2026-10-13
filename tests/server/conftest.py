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
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import sessionmaker

from breakroom_server.config import ServerConfig
from breakroom_server.db import SessionFactory, make_engine
from breakroom_server.models import Base

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

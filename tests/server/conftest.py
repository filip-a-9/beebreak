"""Shared server fixtures; later M3 tickets reuse these.

- `engine`: a fresh database with the schema created from the models. SQLite in
  a temp *file* by default (several threads can share it); PostgreSQL when
  BEEBREAK_TEST_DATABASE_URL is set (its tables are dropped and recreated).
- `session_factory`, `server_config`, `clock` build on it.
- `app` is built with the matchmaker loop off (rule 15): tests call
  `app.state.matchmaker.run_once()` themselves.
- M4 helpers: `add_ready_user` (database only), `ready_user` (through the API),
  `seed_queued`, `seed_match`. Fakes for the notifier and matchmaker are in
  `tests/server/fakes.py`.
- M5 helpers: `seed_match` also fills the chat columns; `seed_message`,
  `seed_report`.

BEEBREAK_TEST_DATABASE_URL is read once at import time. tests/conftest.py
deletes every BEEBREAK_* variable before each test, so a fixture reading the
variable later would never see it. Import `TEST_DATABASE_URL` instead.
"""

import os
import re
import uuid
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, inspect, select, text
from sqlalchemy.orm import sessionmaker

from beebreak_server.app import create_app
from beebreak_server.config import ServerConfig
from beebreak_server.db import SessionFactory, make_engine
from beebreak_server.mailer import RecordingMailer
from beebreak_server.models import (
    Base,
    BetaInvite,
    Device,
    Match,
    MatchParticipant,
    Message,
    QueueEntry,
    Report,
    ReportMessage,
    User,
    UserPreference,
    join_tags,
)
from beebreak_server.nickname import skeleton
from beebreak_server.tokens import hmac_code, new_code
from beebreak_shared.api import Consent, ReportReason
from beebreak_shared.presence import RemainingTimeBucket, StackTag, TaskCategory

TEST_DATABASE_URL: str | None = os.environ.get("BEEBREAK_TEST_DATABASE_URL") or None
TEST_SECRET = b"beebreak-test-secret-01234567890"  # 32 bytes, tests only
START_TIME = datetime(2026, 10, 13, 12, 0, 0, tzinfo=UTC)

requires_postgres = pytest.mark.skipif(
    TEST_DATABASE_URL is None or "postgres" not in TEST_DATABASE_URL,
    reason="set BEEBREAK_TEST_DATABASE_URL to a PostgreSQL URL",
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
    return f"sqlite:///{tmp_path / 'beebreak-test.db'}"


def _reset_schema(engine: Engine) -> None:
    """A database with the model tables, all empty, and identities restarted.

    SQLite gets a fresh temp file per test, so it only needs `create_all`. On
    PostgreSQL the schema is built once and each test truncates it: dropping
    and recreating every table per test dominated the directory's time. Real
    commits stay possible (the race tests use several connections). If another
    test (the migration tests) left the tables missing or different, rebuild.
    """
    if engine.dialect.name != "postgresql":
        Base.metadata.create_all(engine)
        return
    wanted = {table.name for table in Base.metadata.sorted_tables}
    with engine.connect() as conn:
        present = set(inspect(conn).get_table_names())
    if not wanted <= present or "alembic_version" in present:
        Base.metadata.drop_all(engine)
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
        Base.metadata.create_all(engine)
        return
    names = ", ".join(f'"{name}"' for name in sorted(wanted))
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))


@pytest.fixture(scope="session", autouse=True)
def _drop_schema_when_done() -> Iterator[None]:
    yield
    if TEST_DATABASE_URL is not None and "postgres" in TEST_DATABASE_URL:
        engine = make_engine(TEST_DATABASE_URL)
        try:
            Base.metadata.drop_all(engine)
        finally:
            engine.dispose()


@pytest.fixture
def engine(database_url: str) -> Iterator[Engine]:
    engine = make_engine(database_url)
    _reset_schema(engine)
    try:
        yield engine
    finally:
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

    monkeypatch.setenv("BEEBREAK_DATABASE_URL", database_url)
    monkeypatch.setenv(
        "BEEBREAK_SERVER_SECRET", base64.urlsafe_b64encode(TEST_SECRET).decode()
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
    return create_app(server_config, mailer=mailer, clock=clock, run_matchmaker=False)


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


# --- M4 helpers: ready users, queue entries and matches ------------------------

ALL_CONSENT = Consent(
    version=1, share_task_category=True, share_stack_tags=True, share_time_bucket=True
)


@dataclass(frozen=True)
class SeededUser:
    user_id: uuid.UUID
    device_id: uuid.UUID
    nickname: str | None


@dataclass(frozen=True)
class ApiUser:
    session: dict[str, Any]  # SessionResponse JSON
    headers: dict[str, str]  # bearer header
    user_id: uuid.UUID
    device_id: uuid.UUID


def add_ready_user(
    session_factory: SessionFactory,
    nickname: str | None,
    *,
    consent: Consent | None = ALL_CONSENT,
    status: str = "active",
    now: datetime = START_TIME,
) -> SeededUser:
    """A user with one device, written straight to the database (no tokens).

    `nickname=None` or `consent=None` leave setup incomplete.
    """
    tag = nickname or uuid.uuid4().hex[:12]
    with session_factory() as s:
        user = User(
            email_canonical=f"{tag.lower()}@example.com",
            nickname=nickname,
            nickname_skeleton=skeleton(nickname) if nickname else None,
            status=status,
            created_at=now,
        )
        s.add(user)
        s.flush()
        device = Device(user_id=user.id, name="laptop", created_at=now)
        s.add(device)
        if consent is not None:
            s.add(
                UserPreference(
                    user_id=user.id,
                    consent_version=consent.version,
                    share_task_category=consent.share_task_category,
                    share_stack_tags=consent.share_stack_tags,
                    share_time_bucket=consent.share_time_bucket,
                    consented_at=now,
                )
            )
        s.commit()
        return SeededUser(user.id, device.id, nickname)


def ready_user(
    client: TestClient,
    session_factory: SessionFactory,
    mailer: RecordingMailer,
    email: str = "ada@example.com",
    nickname: str | None = "Ada",
    *,
    consent: Consent | None = ALL_CONSENT,
    device_name: str = "laptop",
) -> ApiUser:
    """Sign in through the API, then set nickname and consent (if given)."""
    session = sign_in(client, session_factory, mailer, email, device_name)
    headers = bearer(session)
    if nickname is not None:
        r = client.patch("/v1/me", json={"nickname": nickname}, headers=headers)
        assert r.status_code == 200, r.text
    if consent is not None:
        r = client.put(
            "/v1/me/consent", json=consent.model_dump(mode="json"), headers=headers
        )
        assert r.status_code == 200, r.text
    with session_factory() as s:
        user_id = s.scalars(select(User.id).where(User.email_canonical == email)).one()
        device_id = s.scalars(
            select(Device.id)
            .where(Device.user_id == user_id, Device.name == device_name)
            .order_by(Device.created_at.desc())
        ).first()
    assert device_id is not None
    return ApiUser(session, headers, user_id, device_id)


def seed_queued(
    session_factory: SessionFactory,
    user: SeededUser | ApiUser,
    *,
    now: datetime = START_TIME,
    ttl_s: int = 90,
    task_category: TaskCategory | None = None,
    stack_tags: Sequence[StackTag] = (),
    bucket: RemainingTimeBucket | None = None,
    idempotency_key: uuid.UUID | None = None,
) -> uuid.UUID:
    """A `queued` entry admitted at `now`; returns its id."""
    with session_factory() as s:
        entry = QueueEntry(
            user_id=user.user_id,
            device_id=user.device_id,
            idempotency_key=idempotency_key or uuid.uuid4(),
            status="queued",
            presence_id=uuid.uuid4(),
            task_category=task_category,
            stack_tags=join_tags(stack_tags),
            remaining_time_bucket=bucket,
            admitted_at=now,
            expires_at=now + timedelta(seconds=ttl_s),
        )
        s.add(entry)
        s.commit()
        return entry.id


def seed_match(
    session_factory: SessionFactory,
    a: SeededUser | ApiUser,
    b: SeededUser | ApiUser,
    *,
    now: datetime = START_TIME,
    ttl_s: int = 900,
    shared_tags: Sequence[StackTag] = (),
    shared_category: TaskCategory | None = None,
    extensions_used: int = 0,
    last_seq: int = 0,
    extension_requested_by: SeededUser | ApiUser | None = None,
    offline_since: dict[uuid.UUID, datetime] | None = None,
    status: str = "active",
    ended_at: datetime | None = None,
    end_reason: str | None = None,
) -> uuid.UUID:
    """A match between `a` and `b` (no queue entries); returns its id.

    Active by default. `offline_since` maps a participant's user id to the time
    the sweep saw them go offline. `status="ended"` makes both participants
    inactive; pass `ended_at` and `end_reason` with it.
    """
    active = status == "active"
    offline = offline_since or {}
    with session_factory() as s:
        match = Match(
            status=status,
            created_at=now,
            ends_at=now + timedelta(seconds=ttl_s),
            ended_at=ended_at,
            end_reason=end_reason,
            shared_tags=join_tags(shared_tags) if active else None,
            shared_category=shared_category if active else None,
            extensions_used=extensions_used,
            last_seq=last_seq,
            extension_requested_by=(
                extension_requested_by.user_id if extension_requested_by else None
            ),
        )
        s.add(match)
        s.flush()
        s.add_all(
            [
                MatchParticipant(
                    match_id=match.id,
                    user_id=user.user_id,
                    active=active,
                    offline_since=offline.get(user.user_id),
                )
                for user in (a, b)
            ]
        )
        s.commit()
        return match.id


def seed_message(
    session_factory: SessionFactory,
    match_id: uuid.UUID,
    author: SeededUser | ApiUser,
    *,
    seq: int,
    body: str,
    now: datetime = START_TIME,
    client_message_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """A stored message; raises the match's `last_seq` to at least `seq` and
    counts it in the author's `messages_sent`. Returns its client message id."""
    client_id = client_message_id or uuid.uuid4()
    with session_factory() as s:
        s.add(
            Message(
                match_id=match_id,
                seq=seq,
                author_id=author.user_id,
                client_message_id=client_id,
                body=body,
                created_at=now,
            )
        )
        match = s.get(Match, match_id)
        assert match is not None
        match.last_seq = max(match.last_seq, seq)
        participant = s.get(MatchParticipant, (match_id, author.user_id))
        assert participant is not None
        participant.messages_sent += 1
        s.commit()
    return client_id


def seed_report(
    session_factory: SessionFactory,
    reporter: SeededUser | ApiUser,
    reported: SeededUser | ApiUser,
    match_id: uuid.UUID,
    *,
    now: datetime = START_TIME,
    reason: ReportReason = "harassment",
    note: str | None = None,
    status: str = "open",
    resolved_at: datetime | None = None,
    evidence: Iterable[tuple[int, str, str]] = (),
) -> uuid.UUID:
    """A report created at `now`; `evidence` is (seq, author, body) with author
    "reporter" or "reported". Returns the report id."""
    with session_factory() as s:
        report = Report(
            reporter_id=reporter.user_id,
            reported_id=reported.user_id,
            match_id=match_id,
            reason=reason,
            note=note,
            status=status,
            created_at=now,
            resolved_at=resolved_at,
        )
        s.add(report)
        s.flush()
        s.add_all(
            ReportMessage(
                report_id=report.id, seq=seq, author=author, body=body, sent_at=now
            )
            for seq, author, body in evidence
        )
        s.commit()
        return report.id


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

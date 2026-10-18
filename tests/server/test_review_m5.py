"""M5 server review: reproducing tests for the review findings."""

import uuid
from datetime import timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from beebreak_server import chat
from beebreak_server.config import ServerConfig
from beebreak_server.db import SessionFactory
from beebreak_server.mailer import RecordingMailer
from beebreak_server.matchmaking import Matchmaker
from beebreak_server.models import Match
from beebreak_shared.api import WsMatchEnded
from tests.server.conftest import (
    TEST_SECRET,
    ApiUser,
    FixedClock,
    add_ready_user,
    ready_user,
    seed_match,
)
from tests.server.fakes import RecordingNotifier


@pytest.fixture
def server_config(database_url: str) -> ServerConfig:
    return ServerConfig(
        database_url=database_url, secret=TEST_SECRET, access_ttl_s=200_000
    )


@pytest.fixture
def notifier(app: FastAPI) -> RecordingNotifier:
    fake = RecordingNotifier()
    app.state.notifier = fake
    return fake


@pytest.fixture
def ada(
    client: TestClient, session_factory: SessionFactory, mailer: RecordingMailer
) -> ApiUser:
    return ready_user(client, session_factory, mailer, "ada@example.com", "Ada")


@pytest.fixture
def bob(
    client: TestClient, session_factory: SessionFactory, mailer: RecordingMailer
) -> ApiUser:
    return ready_user(client, session_factory, mailer, "bob@example.com", "Bob")


# --- 1. accept committed between the sweep's expiry SELECT and its UPDATE ------


def test_accept_racing_the_ttl_sweep_keeps_the_extended_chat(
    session_factory: SessionFactory, server_config: ServerConfig, clock: FixedClock
) -> None:
    """The extension UPDATE requires `ends_at > now`, but the sweep's expiry
    UPDATE does not re-check `ends_at <= now`: an accept that commits after the
    sweep read its due list is overwritten with `expired`."""
    notifier = RecordingNotifier()
    ada = add_ready_user(session_factory, "Ada")
    bob = add_ready_user(session_factory, "Bob")
    notifier.connected.update({ada.user_id, bob.user_id})
    match_id = seed_match(
        session_factory, ada, bob, now=clock.now, extension_requested_by=ada
    )
    mm = Matchmaker(session_factory, server_config, clock, notifier)
    with session_factory() as s:
        ends_at = s.get_one(Match, match_id).ends_at

    original = mm._end_match_ids
    raced = False

    def end_after_accept(now: Any, ids: list[uuid.UUID], reason: str, **kw: Any) -> Any:
        nonlocal raced
        if reason == "expired" and match_id in ids and not raced:
            raced = True
            # Bob's accept: his request was read just before the deadline and
            # its UPDATE commits between the sweep's SELECT and UPDATE.
            with session_factory() as s:
                status, change, _ = chat.extension(
                    s,
                    server_config,
                    ends_at - timedelta(milliseconds=5),
                    bob.user_id,
                    match_id,
                    "accept",
                )
            assert change == "extension_accepted"
            assert status.ends_at == ends_at + timedelta(seconds=600)
        return original(now, ids, reason, **kw)

    mm._end_match_ids = end_after_accept  # type: ignore[method-assign, assignment]
    clock.now = ends_at + timedelta(milliseconds=5)
    mm.run_once()
    assert raced
    with session_factory() as s:
        match = s.get_one(Match, match_id)
        # Both users were just told the chat runs 10 more minutes.
        assert match.ends_at == ends_at + timedelta(seconds=600)
        assert match.status == "active", "an accepted extension was expired"
    assert not any(isinstance(f, WsMatchEnded) for _, f in notifier.sent)


# --- 2. out-of-range integers reach the database --------------------------------

# 2**31 overflows the int4 `seq` bind on PostgreSQL; 2**63 overflows SQLite.
BIG = (2**31, 2**63)


@pytest.mark.parametrize("value", BIG)
def test_huge_after_seq_is_not_a_500(
    client: TestClient,
    session_factory: SessionFactory,
    ada: ApiUser,
    bob: ApiUser,
    notifier: RecordingNotifier,
    value: int,
) -> None:
    match_id = seed_match(session_factory, ada, bob)
    r = client.get(
        f"/v1/matches/{match_id}/messages",
        params={"after_seq": value},
        headers=ada.headers,
    )
    assert r.status_code in (200, 400), r.status_code


@pytest.mark.parametrize("value", BIG)
def test_huge_report_seq_is_400_not_500(
    client: TestClient,
    session_factory: SessionFactory,
    ada: ApiUser,
    bob: ApiUser,
    notifier: RecordingNotifier,
    value: int,
) -> None:
    match_id = seed_match(session_factory, ada, bob)
    r = client.post(
        "/v1/reports",
        json={"match_id": str(match_id), "reason": "spam", "message_seqs": [value]},
        headers=ada.headers,
    )
    assert r.status_code == 400, r.status_code

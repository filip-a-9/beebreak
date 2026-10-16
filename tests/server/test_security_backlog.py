"""M6-05: sign-in lockout, pushes after revocation, queue throttles, refresh grace."""

import dataclasses
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from starlette.testclient import WebSocketTestSession

from breakroom_server.config import ServerConfig
from breakroom_server.db import SessionFactory
from breakroom_server.mailer import RecordingMailer
from breakroom_server.models import Device, RefreshToken, User
from breakroom_server.notify import make_frame
from breakroom_server.tokens import derive_successor, hash_token
from breakroom_shared.api import WS_CLOSE, WsMatchEnded
from tests.server.conftest import (
    TEST_SECRET,
    FixedClock,
    ready_user,
    seed_match,
    sign_in,
)
from tests.server.test_realtime import auth_frame, make_account

EMAIL = "ada@example.com"
VICTIM = "victim@example.com"
BUDGET = 3
ACTION_LIMIT = 2
READ_LIMIT = 3
MATCH_CAP = 2


@pytest.fixture
def server_config(server_config: ServerConfig) -> ServerConfig:
    return dataclasses.replace(
        server_config,
        rate_verify_email=BUDGET,
        rate_queue_action_user=ACTION_LIMIT,
        rate_queue_read_user=READ_LIMIT,
        matches_per_window_user=MATCH_CAP,
        access_ttl_s=100_000,
        refresh_ttl_s=1_000_000,
        ws_idle_timeout_s=1,
    )


def start(client: TestClient, email: str = EMAIL) -> Any:
    return client.post("/v1/auth/start", json={"email": email})


def verify(client: TestClient, challenge_id: str, code: str) -> Any:
    return client.post(
        "/v1/auth/verify",
        json={"challenge_id": challenge_id, "code": code, "device_name": "laptop"},
    )


def wrong_code(real: str) -> str:
    return "22222222" if real != "22222222" else "33333333"


def refresh(client: TestClient, token: str) -> Any:
    return client.post("/v1/auth/refresh", json={"refresh_token": token})


# --- sign-in lockout -----------------------------------------------------------


def test_strangers_starts_do_not_stop_the_owners_code(
    client: TestClient, session_factory: SessionFactory, mailer: RecordingMailer
) -> None:
    sign_in(client, session_factory, mailer, VICTIM)
    owner = start(client, VICTIM).json()["challenge_id"]
    owner_code = mailer.sent[-1][2]
    for _ in range(3):  # an attacker asks for more codes for the same address
        assert start(client, VICTIM).status_code == 202
    assert verify(client, owner, owner_code).status_code == 200


def test_every_open_challenge_stays_usable(
    client: TestClient, session_factory: SessionFactory, mailer: RecordingMailer
) -> None:
    sign_in(client, session_factory, mailer, VICTIM)
    first = start(client, VICTIM).json()["challenge_id"]
    first_code = mailer.sent[-1][2]
    second = start(client, VICTIM).json()["challenge_id"]
    second_code = mailer.sent[-1][2]
    assert verify(client, second, second_code).status_code == 200
    assert verify(client, first, first_code).status_code == 200


def test_failed_guesses_across_challenges_share_one_email_budget(
    client: TestClient, session_factory: SessionFactory, mailer: RecordingMailer
) -> None:
    sign_in(client, session_factory, mailer, VICTIM)
    ids = []
    for _ in range(BUDGET):
        ids.append(start(client, VICTIM).json()["challenge_id"])
    right = mailer.sent[-1][2]
    for challenge_id in ids:  # one wrong guess on each: no challenge hits its cap
        assert verify(client, challenge_id, wrong_code(right)).status_code == 400
    blocked = verify(client, ids[-1], right)
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) >= 1


def test_email_budget_frees_after_the_window(
    client: TestClient,
    session_factory: SessionFactory,
    mailer: RecordingMailer,
    clock: FixedClock,
    server_config: ServerConfig,
) -> None:
    sign_in(client, session_factory, mailer, VICTIM)
    challenge_id = start(client, VICTIM).json()["challenge_id"]
    right = mailer.sent[-1][2]
    for _ in range(BUDGET):
        verify(client, challenge_id, wrong_code(right))
    assert verify(client, challenge_id, right).status_code == 429
    fresh = start(client, VICTIM).json()["challenge_id"]
    fresh_code = mailer.sent[-1][2]
    assert verify(client, fresh, fresh_code).status_code == 429
    clock.advance(server_config.rate_window_s + 1)
    again = start(client, VICTIM).json()["challenge_id"]
    assert verify(client, again, mailer.sent[-1][2]).status_code == 200


def test_successful_verifies_do_not_spend_the_budget(
    client: TestClient, session_factory: SessionFactory, mailer: RecordingMailer
) -> None:
    sign_in(client, session_factory, mailer, VICTIM)
    for _ in range(BUDGET + 1):
        challenge_id = start(client, VICTIM).json()["challenge_id"]
        assert verify(client, challenge_id, mailer.sent[-1][2]).status_code == 200


def test_the_budget_is_per_email(
    client: TestClient, session_factory: SessionFactory, mailer: RecordingMailer
) -> None:
    sign_in(client, session_factory, mailer, VICTIM)
    sign_in(client, session_factory, mailer, EMAIL)
    attacked = start(client, VICTIM).json()["challenge_id"]
    for _ in range(BUDGET):
        verify(client, attacked, "22222222")
    other = start(client, EMAIL).json()["challenge_id"]
    assert verify(client, other, mailer.sent[-1][2]).status_code == 200


# --- pushes after revocation ---------------------------------------------------


def _first_message_is_close(ws: WebSocketTestSession) -> int:
    """The very next message must be the close; a pushed frame fails here."""
    message = ws.receive()
    assert message["type"] == "websocket.close", "a frame was pushed"
    return int(message["code"])


@contextmanager
def _connect(client: TestClient, token: str) -> Iterator[WebSocketTestSession]:
    with client.websocket_connect("/v1/realtime") as ws:
        ws.send_json(auth_frame(token))
        assert ws.receive_json()["type"] == "session.ready"
        assert ws.receive_json()["type"] == "sync"
        yield ws


def _push(app: FastAPI, user_id: uuid.UUID, clock: FixedClock) -> None:
    app.state.hub.send_to_user(
        user_id,
        make_frame(WsMatchEnded, clock(), match_id=uuid.uuid4(), reason="ended"),
    )


def test_no_frame_is_pushed_after_the_device_is_revoked(
    app: FastAPI,
    client: TestClient,
    session_factory: SessionFactory,
    clock: FixedClock,
) -> None:
    account = make_account(session_factory, clock)
    with _connect(client, account.token) as ws:
        with session_factory() as session:  # revoked behind the hub's back
            device = session.get(Device, account.device_id)
            assert device is not None
            device.revoked_at = clock()
            session.commit()
        _push(app, account.user_id, clock)
        assert _first_message_is_close(ws) == WS_CLOSE["unauthorized"]


def test_no_frame_is_pushed_after_the_user_is_disabled(
    app: FastAPI,
    client: TestClient,
    session_factory: SessionFactory,
    clock: FixedClock,
) -> None:
    account = make_account(session_factory, clock)
    with _connect(client, account.token) as ws:
        with session_factory() as session:
            user = session.get(User, account.user_id)
            assert user is not None
            user.status = "disabled"
            session.commit()
        _push(app, account.user_id, clock)
        assert _first_message_is_close(ws) == WS_CLOSE["unauthorized"]


def test_no_frame_is_pushed_after_the_access_token_expired(
    app: FastAPI,
    client: TestClient,
    session_factory: SessionFactory,
    clock: FixedClock,
) -> None:
    account = make_account(session_factory, clock, expires_in=60)
    with _connect(client, account.token) as ws:
        clock.advance(61)
        _push(app, account.user_id, clock)
        assert _first_message_is_close(ws) == WS_CLOSE["unauthorized"]


def test_a_still_valid_device_keeps_receiving_pushes(
    app: FastAPI,
    client: TestClient,
    session_factory: SessionFactory,
    clock: FixedClock,
) -> None:
    account = make_account(session_factory, clock)
    with _connect(client, account.token) as ws:
        _push(app, account.user_id, clock)
        assert ws.receive_json()["type"] == "match.ended"


# --- queue throttles -----------------------------------------------------------


def test_queue_delete_end_and_block_are_throttled(
    client: TestClient, session_factory: SessionFactory, mailer: RecordingMailer
) -> None:
    ada = ready_user(client, session_factory, mailer, "ada@example.com", "Ada")
    bob = ready_user(client, session_factory, mailer, "bob@example.com", "Bob")
    match_id = seed_match(session_factory, ada, bob)
    calls = (
        ("DELETE", "/v1/queue/current"),
        ("POST", f"/v1/matches/{match_id}/end"),
        ("POST", f"/v1/matches/{match_id}/block"),
    )
    for _ in range(ACTION_LIMIT):
        assert client.request(*calls[0], headers=ada.headers).status_code == 204
    for call in calls:  # one shared budget across the three actions
        blocked = client.request(*call, headers=ada.headers)
        assert blocked.status_code == 429
        assert int(blocked.headers["Retry-After"]) >= 1
    # Another user is unaffected.
    assert client.delete("/v1/queue/current", headers=bob.headers).status_code == 204


def test_queue_get_is_throttled(
    client: TestClient, session_factory: SessionFactory, mailer: RecordingMailer
) -> None:
    ada = ready_user(client, session_factory, mailer, "ada@example.com", "Ada")
    for _ in range(READ_LIMIT):
        assert client.get("/v1/queue/current", headers=ada.headers).status_code == 200
    assert client.get("/v1/queue/current", headers=ada.headers).status_code == 429


def _join(client: TestClient, headers: dict[str, str]) -> Any:
    return client.post(
        "/v1/queue",
        json={
            "idempotency_key": str(uuid.uuid4()),
            "presence": {
                "schema_version": 1,
                "presence_id": str(uuid.uuid4()),
                "status": "available",
            },
        },
        headers=headers,
    )


def _seed_ended(
    session_factory: SessionFactory, a: Any, b: Any, clock: FixedClock
) -> None:
    seed_match(
        session_factory,
        a,
        b,
        now=clock(),
        status="ended",
        ended_at=clock(),
        end_reason="ended",
    )


def test_joining_is_capped_by_matches_per_window(
    client: TestClient,
    session_factory: SessionFactory,
    mailer: RecordingMailer,
    clock: FixedClock,
    server_config: ServerConfig,
) -> None:
    ada = ready_user(client, session_factory, mailer, "ada@example.com", "Ada")
    bob = ready_user(client, session_factory, mailer, "bob@example.com", "Bob")
    for _ in range(MATCH_CAP - 1):
        _seed_ended(session_factory, ada, bob, clock)
    assert _join(client, ada.headers).status_code == 201
    client.delete("/v1/queue/current", headers=ada.headers)
    _seed_ended(session_factory, ada, bob, clock)  # the cap is now reached
    blocked = _join(client, ada.headers)
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) >= 1
    assert _join(client, bob.headers).status_code == 429  # bob took part in them too
    clock.advance(server_config.rate_window_s + 1)
    assert _join(client, ada.headers).status_code == 201


# --- refresh grace -------------------------------------------------------------


def test_a_retried_refresh_inside_the_grace_returns_the_same_pair(
    client: TestClient,
    session_factory: SessionFactory,
    mailer: RecordingMailer,
    clock: FixedClock,
    server_config: ServerConfig,
) -> None:
    first = sign_in(client, session_factory, mailer)["tokens"]
    rotated = refresh(client, first["refresh_token"])
    clock.advance(server_config.refresh_grace_s)  # the boundary itself is inside
    retried = refresh(client, first["refresh_token"])
    assert retried.status_code == 200
    assert retried.json() == rotated.json()
    me = client.get(
        "/v1/me", headers={"Authorization": f"Bearer {retried.json()['access_token']}"}
    )
    assert me.status_code == 200
    again = refresh(client, retried.json()["refresh_token"])  # the pair still rotates
    assert again.status_code == 200


def test_reuse_after_the_grace_revokes_the_device(
    client: TestClient,
    session_factory: SessionFactory,
    mailer: RecordingMailer,
    clock: FixedClock,
    server_config: ServerConfig,
) -> None:
    first = sign_in(client, session_factory, mailer)["tokens"]
    rotated = refresh(client, first["refresh_token"]).json()
    clock.advance(server_config.refresh_grace_s + 1)
    assert refresh(client, first["refresh_token"]).status_code == 401
    with session_factory() as s:
        assert s.scalars(select(Device)).one().revoked_at is not None
    assert refresh(client, rotated["refresh_token"]).status_code == 401


def test_reuse_of_an_older_token_inside_the_grace_still_revokes(
    client: TestClient, session_factory: SessionFactory, mailer: RecordingMailer
) -> None:
    first = sign_in(client, session_factory, mailer)["tokens"]
    second = refresh(client, first["refresh_token"]).json()
    refresh(client, second["refresh_token"])  # the successor has moved on
    assert refresh(client, first["refresh_token"]).status_code == 401
    with session_factory() as s:
        assert s.scalars(select(Device)).one().revoked_at is not None


def test_grace_replay_is_not_accepted_for_a_revoked_device(
    client: TestClient, session_factory: SessionFactory, mailer: RecordingMailer
) -> None:
    session = sign_in(client, session_factory, mailer)
    first = session["tokens"]
    refresh(client, first["refresh_token"])
    with session_factory() as s:
        device = s.scalars(select(Device)).one()
        device.revoked_at = device.created_at + timedelta(seconds=1)
        s.commit()
    assert refresh(client, first["refresh_token"]).status_code == 401


def test_the_successor_cannot_be_computed_from_what_the_database_stores(
    client: TestClient, session_factory: SessionFactory, mailer: RecordingMailer
) -> None:
    first = sign_in(client, session_factory, mailer)["tokens"]
    rotated = refresh(client, first["refresh_token"]).json()
    with session_factory() as s:
        stored = s.get(RefreshToken, hash_token(first["refresh_token"]))
        assert stored is not None
        from_stored = derive_successor(TEST_SECRET, stored.token_hash.hex())
        from_hash_text = derive_successor(TEST_SECRET, str(stored.token_hash))
    assert rotated["refresh_token"] not in from_stored + from_hash_text
    assert rotated["access_token"] not in from_stored + from_hash_text


def test_an_already_matched_user_at_the_cap_gets_409_not_429(
    client: TestClient,
    session_factory: SessionFactory,
    mailer: RecordingMailer,
    clock: FixedClock,
) -> None:
    ada = ready_user(client, session_factory, mailer, "ada@example.com", "Ada")
    bob = ready_user(client, session_factory, mailer, "bob@example.com", "Bob")
    for _ in range(MATCH_CAP - 1):
        _seed_ended(session_factory, ada, bob, clock)
    seed_match(session_factory, ada, bob, now=clock())  # active: cap reached
    response = _join(client, ada.headers)
    assert (response.status_code, response.json()) == (
        409,
        {"error": "already_matched"},
    )

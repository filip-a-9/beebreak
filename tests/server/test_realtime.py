import dataclasses
import time
import uuid
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession

from breakroom_server import realtime
from breakroom_server.config import ServerConfig
from breakroom_server.db import SessionFactory
from breakroom_server.models import Device, User
from breakroom_server.tokens import hash_token, new_token
from breakroom_shared.api import MAX_WS_FRAME_BYTES, WS_CLOSE, ServerWsFrame
from tests.server.conftest import FixedClock, add_ready_user, seed_match, seed_queued

ACCESS_TTL = 900


@dataclasses.dataclass
class Account:
    user_id: uuid.UUID
    device_id: uuid.UUID
    token: str


def make_account(
    factory: SessionFactory,
    clock: FixedClock,
    nickname: str | None = "Filip",
    *,
    status: str = "active",
    expires_in: float = ACCESS_TTL,
) -> Account:
    token = new_token("bra_")
    user = User(
        email_canonical=f"{uuid.uuid4().hex}@example.test",
        nickname=nickname,
        status=status,
        created_at=clock(),
    )
    with factory() as session:
        session.add(user)
        session.flush()
        device = Device(
            user_id=user.id,
            name="test",
            access_hash=hash_token(token),
            access_expires_at=clock() + timedelta(seconds=expires_in),
            created_at=clock(),
        )
        session.add(device)
        session.commit()
        return Account(user.id, device.id, token)


def connected_at(factory: SessionFactory, device_id: uuid.UUID) -> Any:
    with factory() as session:
        device = session.get(Device, device_id)
        assert device is not None
        return device.connected_at


def wait_disconnected(factory: SessionFactory, device_id: uuid.UUID) -> None:
    """Wait (bounded) for the server to clear connected_at.

    The test client leaves the `with` block as soon as the socket closes, but
    the server clears the column in a shielded thread job that may still run.
    """
    deadline = time.monotonic() + 2.0
    while connected_at(factory, device_id) is not None:
        assert time.monotonic() < deadline, "connected_at was never cleared"
        time.sleep(0.01)


def build_app(
    config: ServerConfig, factory: SessionFactory, clock: FixedClock
) -> FastAPI:
    app = FastAPI()
    app.state.config = config
    app.state.session_factory = factory
    app.state.clock = clock
    hub = realtime.install(app)

    @app.post("/test/logout/{device_id}", status_code=204)
    def logout(device_id: uuid.UUID) -> None:  # stands in for POST /v1/auth/logout
        hub.disconnect_device(device_id)

    return app


@pytest.fixture
def fast_config(server_config: ServerConfig) -> ServerConfig:
    return dataclasses.replace(
        server_config, ws_auth_timeout_s=0.2, ws_idle_timeout_s=30
    )


@pytest.fixture
def app(
    fast_config: ServerConfig, session_factory: SessionFactory, clock: FixedClock
) -> FastAPI:
    return build_app(fast_config, session_factory, clock)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


def auth_frame(token: str) -> dict[str, Any]:
    return {"type": "auth", "version": 1, "access_token": token}


PING = {"type": "ping", "version": 1}


def close_code(ws: WebSocketTestSession) -> int:
    """Read until the server closes; return its close code."""
    while True:
        message = ws.receive()
        if message["type"] == "websocket.close":
            return int(message["code"])


def test_happy_path(
    client: TestClient,
    session_factory: SessionFactory,
    clock: FixedClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    account = make_account(session_factory, clock)
    with client.websocket_connect("/v1/realtime") as ws:
        ws.send_json(auth_frame(account.token))
        ready = ServerWsFrame.model_validate(ws.receive_json()).root
        assert ws.receive_json()["type"] == "sync"
        assert ready.type == "session.ready"
        assert ready.nickname == "Filip"
        assert ready.version == 1
        assert ready.server_time == clock()
        assert connected_at(session_factory, account.device_id) == clock()

        ws.send_json(PING)
        pong = ServerWsFrame.model_validate(ws.receive_json()).root
        assert pong.type == "pong"
        assert pong.version == 1
        assert pong.server_time == clock()
        assert pong.event_id != ready.event_id
    wait_disconnected(session_factory, account.device_id)
    assert client.app.state.hub.connected_count() == 0  # type: ignore[attr-defined]
    assert account.token not in caplog.text


def test_nickname_may_be_unset(
    client: TestClient, session_factory: SessionFactory, clock: FixedClock
) -> None:
    account = make_account(session_factory, clock, nickname=None)
    with client.websocket_connect("/v1/realtime") as ws:
        ws.send_json(auth_frame(account.token))
        assert ws.receive_json()["nickname"] is None
        assert ws.receive_json()["type"] == "sync"


def test_lone_surrogate_token_is_a_bad_frame(client: TestClient) -> None:
    with client.websocket_connect("/v1/realtime") as ws:
        ws.send_text('{"type":"auth","version":1,"access_token":"bra_\\ud800"}')
        assert close_code(ws) == WS_CLOSE["bad_frame"]


def test_no_auth_frame_in_time_closes_4408(client: TestClient) -> None:
    with client.websocket_connect("/v1/realtime") as ws:
        assert close_code(ws) == WS_CLOSE["auth_timeout"]


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        '{"type": "mystery", "version": 1}',
        '{"type": "ping", "version": 2}',
        '{"type": "auth", "version": 1, "access_token": "x", "extra": 1}',
        "[]",
    ],
)
def test_garbage_frames_close_4400(client: TestClient, payload: str) -> None:
    with client.websocket_connect("/v1/realtime") as ws:
        ws.send_text(payload)
        assert close_code(ws) == WS_CLOSE["bad_frame"]


def test_oversized_frame_closes_4400(client: TestClient) -> None:
    with client.websocket_connect("/v1/realtime") as ws:
        ws.send_json(auth_frame("x" * (MAX_WS_FRAME_BYTES + 1)))
        assert close_code(ws) == WS_CLOSE["bad_frame"]


def test_binary_frame_closes_4400(client: TestClient) -> None:
    with client.websocket_connect("/v1/realtime") as ws:
        ws.send_bytes(b"\x00\x01")
        assert close_code(ws) == WS_CLOSE["bad_frame"]


def test_ping_before_auth_closes_4401(client: TestClient) -> None:
    with client.websocket_connect("/v1/realtime") as ws:
        ws.send_json(PING)
        assert close_code(ws) == WS_CLOSE["unauthorized"]


def test_unknown_token_closes_4401(client: TestClient) -> None:
    with client.websocket_connect("/v1/realtime") as ws:
        ws.send_json(auth_frame(new_token("bra_")))
        assert close_code(ws) == WS_CLOSE["unauthorized"]


def test_revoked_device_closes_4401(
    client: TestClient, session_factory: SessionFactory, clock: FixedClock
) -> None:
    account = make_account(session_factory, clock)
    with session_factory() as session:
        device = session.get(Device, account.device_id)
        assert device is not None
        device.revoked_at = clock()
        session.commit()
    with client.websocket_connect("/v1/realtime") as ws:
        ws.send_json(auth_frame(account.token))
        assert close_code(ws) == WS_CLOSE["unauthorized"]


def test_expired_token_closes_4401(
    client: TestClient, session_factory: SessionFactory, clock: FixedClock
) -> None:
    account = make_account(session_factory, clock, expires_in=-1)
    with client.websocket_connect("/v1/realtime") as ws:
        ws.send_json(auth_frame(account.token))
        assert close_code(ws) == WS_CLOSE["unauthorized"]


def test_token_in_query_string_is_ignored(
    client: TestClient, session_factory: SessionFactory, clock: FixedClock
) -> None:
    account = make_account(session_factory, clock)
    with client.websocket_connect(f"/v1/realtime?access_token={account.token}") as ws:
        assert close_code(ws) == WS_CLOSE["auth_timeout"]
    assert connected_at(session_factory, account.device_id) is None


def test_token_in_header_is_ignored(
    client: TestClient, session_factory: SessionFactory, clock: FixedClock
) -> None:
    account = make_account(session_factory, clock)
    headers = {"Authorization": f"Bearer {account.token}"}
    with client.websocket_connect("/v1/realtime", headers=headers) as ws:
        assert close_code(ws) == WS_CLOSE["auth_timeout"]


def test_origin_header_closes_4401(
    client: TestClient, session_factory: SessionFactory, clock: FixedClock
) -> None:
    account = make_account(session_factory, clock)
    with client.websocket_connect(
        "/v1/realtime", headers={"Origin": "https://evil.example"}
    ) as ws:
        ws.send_json(auth_frame(account.token))
        assert close_code(ws) == WS_CLOSE["unauthorized"]
    assert connected_at(session_factory, account.device_id) is None


def test_second_connection_replaces_first(
    client: TestClient, session_factory: SessionFactory, clock: FixedClock
) -> None:
    account = make_account(session_factory, clock)
    with client.websocket_connect("/v1/realtime") as first:
        first.send_json(auth_frame(account.token))
        first.receive_json()
        assert first.receive_json()["type"] == "sync"
        with client.websocket_connect("/v1/realtime") as second:
            second.send_json(auth_frame(account.token))
            second.receive_json()
            assert second.receive_json()["type"] == "sync"
            assert close_code(first) == WS_CLOSE["replaced"]
            # the replaced socket closing must not clear the live one
            assert connected_at(session_factory, account.device_id) is not None
            second.send_json(PING)
            assert second.receive_json()["type"] == "pong"
            assert client.app.state.hub.connected_count() == 1  # type: ignore[attr-defined]
    wait_disconnected(session_factory, account.device_id)


def test_reauth_with_fresh_token_keeps_socket(
    client: TestClient, session_factory: SessionFactory, clock: FixedClock
) -> None:
    account = make_account(session_factory, clock)
    with client.websocket_connect("/v1/realtime") as ws:
        ws.send_json(auth_frame(account.token))
        ws.receive_json()
        assert ws.receive_json()["type"] == "sync"
        clock.advance(ACCESS_TTL - 60)
        fresh = new_token("bra_")
        with session_factory() as session:
            device = session.get(Device, account.device_id)
            assert device is not None
            device.access_hash = hash_token(fresh)
            device.access_expires_at = clock() + timedelta(seconds=ACCESS_TTL)
            session.commit()
        ws.send_json(auth_frame(fresh))
        assert ws.receive_json()["type"] == "session.ready"
        clock.advance(120)  # past the original expiry
        ws.send_json(PING)
        assert ws.receive_json()["type"] == "pong"


def test_reauth_with_another_devices_token_closes_4401(
    client: TestClient, session_factory: SessionFactory, clock: FixedClock
) -> None:
    mine = make_account(session_factory, clock, "Alice")
    other = make_account(session_factory, clock, "Bobby")
    with client.websocket_connect("/v1/realtime") as ws:
        ws.send_json(auth_frame(mine.token))
        ws.receive_json()
        assert ws.receive_json()["type"] == "sync"
        ws.send_json(auth_frame(other.token))
        assert close_code(ws) == WS_CLOSE["unauthorized"]
    assert connected_at(session_factory, mine.device_id) is None
    assert connected_at(session_factory, other.device_id) is None


def test_expired_access_without_reauth_closes_on_next_frame(
    client: TestClient, session_factory: SessionFactory, clock: FixedClock
) -> None:
    account = make_account(session_factory, clock)
    with client.websocket_connect("/v1/realtime") as ws:
        ws.send_json(auth_frame(account.token))
        ws.receive_json()
        assert ws.receive_json()["type"] == "sync"
        clock.advance(ACCESS_TTL + 1)
        ws.send_json(PING)
        assert close_code(ws) == WS_CLOSE["unauthorized"]
    assert connected_at(session_factory, account.device_id) is None


def test_device_revoked_while_connected_closes_on_next_frame(
    client: TestClient, session_factory: SessionFactory, clock: FixedClock
) -> None:
    account = make_account(session_factory, clock)
    with client.websocket_connect("/v1/realtime") as ws:
        ws.send_json(auth_frame(account.token))
        ws.receive_json()
        assert ws.receive_json()["type"] == "sync"
        with session_factory() as session:
            device = session.get(Device, account.device_id)
            assert device is not None
            device.revoked_at = clock()
            session.commit()
        ws.send_json(PING)
        assert close_code(ws) == WS_CLOSE["unauthorized"]


def test_disconnect_device_from_http_closes_live_socket(
    client: TestClient, session_factory: SessionFactory, clock: FixedClock
) -> None:
    account = make_account(session_factory, clock)
    with client.websocket_connect("/v1/realtime") as ws:
        ws.send_json(auth_frame(account.token))
        ws.receive_json()
        assert ws.receive_json()["type"] == "sync"
        assert client.post(f"/test/logout/{account.device_id}").status_code == 204
        assert close_code(ws) == WS_CLOSE["unauthorized"]
    assert connected_at(session_factory, account.device_id) is None
    assert client.app.state.hub.connected_count() == 0  # type: ignore[attr-defined]


def test_frame_flood_closes_4429(
    client: TestClient, session_factory: SessionFactory, clock: FixedClock
) -> None:
    account = make_account(session_factory, clock)
    with client.websocket_connect("/v1/realtime") as ws:
        ws.send_json(auth_frame(account.token))
        for _ in range(30):
            ws.send_json(PING)
        assert close_code(ws) == WS_CLOSE["rate_limited"]


def test_idle_connection_closes_normally(
    fast_config: ServerConfig, session_factory: SessionFactory, clock: FixedClock
) -> None:
    config = dataclasses.replace(fast_config, ws_idle_timeout_s=0.2)
    account = make_account(session_factory, clock)
    with TestClient(build_app(config, session_factory, clock)) as client:
        with client.websocket_connect("/v1/realtime") as ws:
            ws.send_json(auth_frame(account.token))
            ws.receive_json()
            assert ws.receive_json()["type"] == "sync"
            assert close_code(ws) == 1000
        assert connected_at(session_factory, account.device_id) is None


def test_two_users_connected_at_once(
    client: TestClient, session_factory: SessionFactory, clock: FixedClock
) -> None:
    alice = make_account(session_factory, clock, "Alice")
    bobby = make_account(session_factory, clock, "Bobby")
    with client.websocket_connect("/v1/realtime") as a:
        a.send_json(auth_frame(alice.token))
        assert a.receive_json()["nickname"] == "Alice"
        assert a.receive_json()["type"] == "sync"
        with client.websocket_connect("/v1/realtime") as b:
            b.send_json(auth_frame(bobby.token))
            assert b.receive_json()["nickname"] == "Bobby"
            assert b.receive_json()["type"] == "sync"
            assert client.app.state.hub.connected_count() == 2  # type: ignore[attr-defined]
    assert client.app.state.hub.connected_count() == 0  # type: ignore[attr-defined]


def test_startup_clears_stale_connected_at(
    fast_config: ServerConfig, session_factory: SessionFactory, clock: FixedClock
) -> None:
    account = make_account(session_factory, clock)
    with session_factory() as session:
        device = session.get(Device, account.device_id)
        assert device is not None
        device.connected_at = clock()
        session.commit()
    assert connected_at(session_factory, account.device_id) is not None
    with TestClient(build_app(fast_config, session_factory, clock)):
        pass
    assert connected_at(session_factory, account.device_id) is None


# --- M4-04: sync after the first auth ---------------------------------------


def give_token(factory: SessionFactory, clock: FixedClock, device_id: uuid.UUID) -> str:
    """Make `device_id` (e.g. of `add_ready_user`) able to authenticate."""
    token = new_token("bra_")
    with factory() as session:
        device = session.get(Device, device_id)
        assert device is not None
        device.access_hash = hash_token(token)
        device.access_expires_at = clock() + timedelta(seconds=ACCESS_TTL)
        session.commit()
    return token


def read(ws: WebSocketTestSession) -> Any:
    """The next frame, validated against the frozen server contract."""
    return ServerWsFrame.model_validate(ws.receive_json()).root


def test_first_auth_sends_ready_then_empty_sync(
    client: TestClient, session_factory: SessionFactory, clock: FixedClock
) -> None:
    user = add_ready_user(session_factory, "Alice")
    token = give_token(session_factory, clock, user.device_id)
    with client.websocket_connect("/v1/realtime") as ws:
        ws.send_json(auth_frame(token))
        ready = read(ws)
        sync = read(ws)
        assert ready.type == "session.ready"
        assert sync.type == "sync"
        assert sync.queue is None and sync.match is None
        assert sync.version == 1
        assert sync.server_time == clock()
        assert sync.event_id != ready.event_id


def test_sync_shows_the_queued_entry(
    client: TestClient, session_factory: SessionFactory, clock: FixedClock
) -> None:
    user = add_ready_user(session_factory, "Alice")
    entry_id = seed_queued(session_factory, user, now=clock(), ttl_s=90)
    token = give_token(session_factory, clock, user.device_id)
    with client.websocket_connect("/v1/realtime") as ws:
        ws.send_json(auth_frame(token))
        assert read(ws).type == "session.ready"
        sync = read(ws)
        assert sync.queue is not None
        assert sync.queue.entry_id == entry_id
        assert sync.queue.status == "queued"
        assert sync.queue.expires_at == clock() + timedelta(seconds=90)
        assert sync.match is None


def test_sync_shows_the_match_from_the_callers_side(
    client: TestClient, session_factory: SessionFactory, clock: FixedClock
) -> None:
    alice = add_ready_user(session_factory, "Alice")
    bobby = add_ready_user(session_factory, "Bobby")
    match_id = seed_match(
        session_factory,
        alice,
        bobby,
        now=clock(),
        ttl_s=900,
        shared_tags=("python", "rust"),
    )
    alice_token = give_token(session_factory, clock, alice.device_id)
    bobby_token = give_token(session_factory, clock, bobby.device_id)
    for token, peer in ((alice_token, "Bobby"), (bobby_token, "Alice")):
        with client.websocket_connect("/v1/realtime") as ws:
            ws.send_json(auth_frame(token))
            assert read(ws).type == "session.ready"
            sync = read(ws)
            assert sync.queue is None
            assert sync.match is not None
            assert sync.match.match_id == match_id
            assert sync.match.peer_nickname == peer
            assert set(sync.match.shared_tags) == {"python", "rust"}
            assert sync.match.ends_at == clock() + timedelta(seconds=900)


def test_reauth_sends_ready_only(
    client: TestClient, session_factory: SessionFactory, clock: FixedClock
) -> None:
    user = add_ready_user(session_factory, "Alice")
    token = give_token(session_factory, clock, user.device_id)
    with client.websocket_connect("/v1/realtime") as ws:
        ws.send_json(auth_frame(token))
        assert read(ws).type == "session.ready"
        assert read(ws).type == "sync"
        ws.send_json(auth_frame(token))
        assert read(ws).type == "session.ready"
        ws.send_json(PING)
        assert read(ws).type == "pong"  # no sync in between

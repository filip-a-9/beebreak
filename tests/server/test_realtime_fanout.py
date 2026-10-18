"""M4-04: user fan-out, outbound queue and flood limit, on the real app."""

import threading
import uuid
from datetime import timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession

from beebreak_server.db import SessionFactory
from beebreak_server.mailer import RecordingMailer
from beebreak_server.models import Device
from beebreak_server.realtime import OUTBOUND_MAX, Hub, make_frame
from beebreak_server.tokens import hash_token, new_token
from beebreak_shared.api import (
    WS_CLOSE,
    ServerWsFrame,
    WsMatchEnded,
    WsQueueEnded,
)
from tests.server.conftest import FixedClock, SeededUser, add_ready_user, ready_user
from tests.server.test_realtime import (
    PING,
    auth_frame,
    close_code,
    connected_at,
    give_token,
    wait_disconnected,
)


def read(ws: WebSocketTestSession) -> Any:
    return ServerWsFrame.model_validate(ws.receive_json()).root


def sign_on(ws: WebSocketTestSession, token: str) -> None:
    ws.send_json(auth_frame(token))
    assert read(ws).type == "session.ready"
    assert read(ws).type == "sync"


def second_device(
    factory: SessionFactory, clock: FixedClock, user: SeededUser
) -> tuple[uuid.UUID, str]:
    token = new_token("bra_")
    with factory() as session:
        device = Device(
            user_id=user.user_id,
            name="phone",
            access_hash=hash_token(token),
            access_expires_at=clock() + timedelta(seconds=900),
            created_at=clock(),
        )
        session.add(device)
        session.commit()
        return device.id, token


def ended(clock: FixedClock) -> WsQueueEnded:
    return make_frame(WsQueueEnded, clock(), entry_id=uuid.uuid4(), reason="expired")


def hub_of(app: FastAPI) -> Hub:
    hub = app.state.notifier
    assert isinstance(hub, Hub)
    return hub


def test_send_to_user_reaches_every_device_and_no_other_user(
    app: FastAPI, client: TestClient, session_factory: SessionFactory, clock: FixedClock
) -> None:
    alice = add_ready_user(session_factory, "Alice")
    bobby = add_ready_user(session_factory, "Bobby")
    alice_1 = give_token(session_factory, clock, alice.device_id)
    _, alice_2 = second_device(session_factory, clock, alice)
    bobby_1 = give_token(session_factory, clock, bobby.device_id)
    with (
        client.websocket_connect("/v1/realtime") as a1,
        client.websocket_connect("/v1/realtime") as a2,
        client.websocket_connect("/v1/realtime") as b1,
    ):
        sign_on(a1, alice_1)
        sign_on(a2, alice_2)
        sign_on(b1, bobby_1)
        frame = ended(clock)
        hub_of(app).send_to_user(alice.user_id, frame)
        for ws in (a1, a2):
            got = read(ws)
            assert got.type == "queue.ended"
            assert got.event_id == frame.event_id
            assert got.reason == "expired"
        # Bobby saw nothing: his next frame is the answer to his own ping.
        b1.send_json(PING)
        assert read(b1).type == "pong"


def test_disconnected_device_is_skipped(
    app: FastAPI, client: TestClient, session_factory: SessionFactory, clock: FixedClock
) -> None:
    alice = add_ready_user(session_factory, "Alice")
    first = give_token(session_factory, clock, alice.device_id)
    other_id, other_token = second_device(session_factory, clock, alice)
    with client.websocket_connect("/v1/realtime") as keep:
        sign_on(keep, first)
        with client.websocket_connect("/v1/realtime") as gone:
            sign_on(gone, other_token)
        wait_disconnected(session_factory, other_id)
        assert hub_of(app).is_user_connected(alice.user_id)
        hub_of(app).send_to_user(alice.user_id, ended(clock))
        assert read(keep).type == "queue.ended"


def test_send_to_user_from_a_plain_thread(
    app: FastAPI, client: TestClient, session_factory: SessionFactory, clock: FixedClock
) -> None:
    alice = add_ready_user(session_factory, "Alice")
    token = give_token(session_factory, clock, alice.device_id)
    ended_frame = make_frame(
        WsMatchEnded, clock(), match_id=uuid.uuid4(), reason="ended"
    )
    with client.websocket_connect("/v1/realtime") as ws:
        sign_on(ws, token)
        worker = threading.Thread(
            target=hub_of(app).send_to_user, args=(alice.user_id, ended_frame)
        )
        worker.start()
        worker.join(timeout=5)
        assert not worker.is_alive()
        got = read(ws)
        assert got.type == "match.ended"
        assert got.match_id == ended_frame.match_id
        assert got.reason == "ended"


def test_send_to_unconnected_user_is_a_no_op(
    app: FastAPI, session_factory: SessionFactory, clock: FixedClock
) -> None:
    stranger = add_ready_user(session_factory, "Alice")
    assert not hub_of(app).is_user_connected(stranger.user_id)
    hub_of(app).send_to_user(stranger.user_id, ended(clock))  # must not raise


def test_is_user_connected_follows_the_devices(
    app: FastAPI, client: TestClient, session_factory: SessionFactory, clock: FixedClock
) -> None:
    alice = add_ready_user(session_factory, "Alice")
    first = give_token(session_factory, clock, alice.device_id)
    other_id, other_token = second_device(session_factory, clock, alice)
    hub = hub_of(app)
    assert not hub.is_user_connected(alice.user_id)
    with client.websocket_connect("/v1/realtime") as one:
        sign_on(one, first)
        assert hub.is_user_connected(alice.user_id)
        with client.websocket_connect("/v1/realtime") as two:
            sign_on(two, other_token)
        wait_disconnected(session_factory, other_id)
        assert hub.is_user_connected(alice.user_id)  # one device left
    wait_disconnected(session_factory, alice.device_id)
    assert not hub.is_user_connected(alice.user_id)


def test_unauthenticated_socket_is_not_a_connected_user(
    app: FastAPI, client: TestClient, session_factory: SessionFactory
) -> None:
    alice = add_ready_user(session_factory, "Alice")
    with client.websocket_connect("/v1/realtime"):
        assert not hub_of(app).is_user_connected(alice.user_id)


def test_outbound_overflow_closes_only_that_socket(
    app: FastAPI, client: TestClient, session_factory: SessionFactory, clock: FixedClock
) -> None:
    alice = add_ready_user(session_factory, "Alice")
    bobby = add_ready_user(session_factory, "Bobby")
    alice_token = give_token(session_factory, clock, alice.device_id)
    bobby_token = give_token(session_factory, clock, bobby.device_id)
    hub = hub_of(app)
    with (
        client.websocket_connect("/v1/realtime") as a,
        client.websocket_connect("/v1/realtime") as b,
    ):
        sign_on(a, alice_token)
        sign_on(b, bobby_token)

        def burst() -> None:
            # In one event-loop turn, so the socket cannot drain in between.
            for _ in range(OUTBOUND_MAX + 8):
                hub.send_to_user(alice.user_id, ended(clock))
            hub.send_to_user(bobby.user_id, ended(clock))

        client.portal.call(burst)  # type: ignore[union-attr]
        assert close_code(a) == WS_CLOSE["rate_limited"]
        assert read(b).type == "queue.ended"
        b.send_json(PING)
        assert read(b).type == "pong"
    wait_disconnected(session_factory, alice.device_id)
    assert connected_at(session_factory, alice.device_id) is None
    assert not hub.is_user_connected(alice.user_id)


def test_outbound_frames_do_not_count_towards_the_flood_limit(
    app: FastAPI, client: TestClient, session_factory: SessionFactory, clock: FixedClock
) -> None:
    alice = add_ready_user(session_factory, "Alice")
    token = give_token(session_factory, clock, alice.device_id)
    hub = hub_of(app)
    with client.websocket_connect("/v1/realtime") as ws:
        sign_on(ws, token)
        pushed = 0
        for _ in range(3):  # 30 outbound plus 15 inbound frames, limit 20 inbound
            for _ in range(10):
                hub.send_to_user(alice.user_id, ended(clock))
                pushed += 1
            for _ in range(5):
                ws.send_json(PING)
            kinds: list[str] = []
            while kinds.count("pong") < 5 or kinds.count("queue.ended") < 10:
                kinds.append(read(ws).type)
        assert pushed == 30
        ws.send_json(PING)
        assert read(ws).type == "pong"


# --- end to end with the real matchmaker -------------------------------------


@pytest.mark.parametrize("action", ["end", "block"])
def test_join_match_and_end_reach_both_sockets(
    app: FastAPI,
    client: TestClient,
    session_factory: SessionFactory,
    mailer: RecordingMailer,
    action: str,
) -> None:
    ada = ready_user(client, session_factory, mailer, "ada@example.com", "Ada")
    bob = ready_user(client, session_factory, mailer, "bob@example.com", "Bob")
    ada_token = ada.session["tokens"]["access_token"]
    bob_token = bob.session["tokens"]["access_token"]
    presence = {
        "schema_version": 1,
        "presence_id": str(uuid.uuid4()),
        "status": "available",
    }
    with (
        client.websocket_connect("/v1/realtime") as wa,
        client.websocket_connect("/v1/realtime") as wb,
    ):
        sign_on(wa, ada_token)
        sign_on(wb, bob_token)
        for user in (ada, bob):
            body = {"idempotency_key": str(uuid.uuid4()), "presence": presence}
            posted = client.post("/v1/queue", json=body, headers=user.headers)
            assert posted.status_code == 201
        assert len(app.state.matchmaker.run_once()) == 1

        created = {}
        for name, ws, peer in (("ada", wa, "Bob"), ("bob", wb, "Ada")):
            joined = read(ws)
            assert joined.type == "queue.joined"
            frame = read(ws)
            assert frame.type == "match.created"
            assert frame.entry_id == joined.entry.entry_id
            assert frame.match.peer_nickname == peer
            created[name] = frame
        match_id = created["ada"].match.match_id
        assert created["bob"].match.match_id == match_id

        ended = client.post(f"/v1/matches/{match_id}/{action}", headers=ada.headers)
        assert ended.status_code == 204
        for ws in (wa, wb):
            gone = read(ws)
            assert gone.type == "match.ended"
            assert gone.match_id == match_id
            assert gone.reason == "ended"

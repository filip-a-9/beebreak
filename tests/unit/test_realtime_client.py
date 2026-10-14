from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Coroutine
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, TypeVar
from uuid import uuid4

import pytest
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from breakroom_companion.api_client import ApiClient
from breakroom_companion.realtime_client import (
    CLOSE_PROTOCOL_ERROR,
    RealtimeClient,
    RealtimeError,
    ServerFrame,
)
from breakroom_shared.api import (
    MAX_WS_FRAME_BYTES,
    WsMatchCreated,
    WsMatchEnded,
    WsQueueEnded,
    WsQueueJoined,
    WsReady,
    WsSync,
)
from tests.fakes.chat import chat_json

if TYPE_CHECKING:
    from tests.e2e_support.server import LiveServer

pytest_plugins = ["tests.e2e_support.server"]  # live_server fixtures

T = TypeVar("T")
TIMEOUT_S = 2.0
FAST_PING_S = 0.05
Handler = Callable[[ServerConnection], Awaitable[None]]


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


def ready_frame(nickname: str | None = None) -> str:
    return json.dumps(
        {
            "type": "session.ready",
            "version": 1,
            "event_id": str(uuid4()),
            "server_time": datetime.now(UTC).isoformat(),
            "nickname": nickname,
        }
    )


def pong_frame() -> str:
    return json.dumps(
        {
            "type": "pong",
            "version": 1,
            "event_id": str(uuid4()),
            "server_time": datetime.now(UTC).isoformat(),
        }
    )


async def with_fake_server[T](
    handler: Handler, scenario: Callable[[str], Awaitable[T]]
) -> T:
    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        return await scenario(f"http://127.0.0.1:{port}")


async def sign_in(api: ApiClient, server: LiveServer, email: str) -> Any:
    invite = server.invite(email)
    challenge = await api.start_auth(email, invite)
    return await api.verify(challenge.challenge_id, server.last_code(), "test box")


# --- live server ---


def test_connects_pings_and_reports_a_server_side_replacement(
    live_server: LiveServer,
) -> None:
    async def scenario() -> None:
        api = ApiClient(live_server.base_url, TIMEOUT_S)
        first = RealtimeClient(live_server.base_url, TIMEOUT_S, FAST_PING_S)
        second = RealtimeClient(live_server.base_url, TIMEOUT_S, FAST_PING_S)
        try:
            session = await sign_in(api, live_server, "ada@example.com")
            token = session.tokens.access_token
            await api.set_nickname(token, "ada_l")

            ready = await first.connect(token)
            assert ready.nickname == "ada_l"
            # Same device again: the server replaces the first socket.
            await second.connect(token)
            assert await asyncio.wait_for(first.wait_closed(), TIMEOUT_S) == 4409
        finally:
            await first.close()
            await second.close()
            await api.aclose()

    run(scenario())


def test_bad_token_is_closed_with_4401(live_server: LiveServer) -> None:
    async def scenario() -> None:
        client = RealtimeClient(live_server.base_url, TIMEOUT_S, FAST_PING_S)
        with pytest.raises(RealtimeError) as info:
            await client.connect("bra_not-a-token")
        assert info.value.code == 4401
        assert "bra_not-a-token" not in str(info.value)

    run(scenario())


def test_reauth_on_live_socket_keeps_it_open_and_logout_closes_4401(
    live_server: LiveServer,
) -> None:
    async def scenario() -> None:
        api = ApiClient(live_server.base_url, TIMEOUT_S)
        client = RealtimeClient(live_server.base_url, TIMEOUT_S, FAST_PING_S)
        try:
            session = await sign_in(api, live_server, "ada@example.com")
            await client.connect(session.tokens.access_token)
            fresh = await api.refresh(session.tokens.refresh_token)
            await client.reauth(fresh.access_token)
            # The server accepted the fresh token: pings keep the socket open.
            closing = asyncio.ensure_future(client.wait_closed())
            done, _ = await asyncio.wait({closing}, timeout=4 * FAST_PING_S)
            assert not done
            await api.logout(fresh.access_token)
            assert await asyncio.wait_for(closing, TIMEOUT_S) == 4401
        finally:
            await client.close()
            await api.aclose()

    run(scenario())


def test_connection_refused_is_a_codeless_error() -> None:
    async def scenario() -> None:
        client = RealtimeClient("http://127.0.0.1:1", TIMEOUT_S, FAST_PING_S)
        with pytest.raises(RealtimeError) as info:
            await client.connect("bra_x")
        assert info.value.code is None

    run(scenario())


# --- fake servers ---


def test_sends_no_origin_no_extra_headers_and_no_token_in_the_url() -> None:
    seen: dict[str, Any] = {}

    async def handler(ws: ServerConnection) -> None:
        assert ws.request is not None
        seen["headers"] = {k.lower() for k in ws.request.headers}
        seen["path"] = ws.request.path
        seen["first"] = json.loads(await ws.recv())
        await ws.send(ready_frame())
        await ws.wait_closed()

    async def scenario(origin: str) -> None:
        client = RealtimeClient(origin, TIMEOUT_S, FAST_PING_S)
        try:
            await client.connect("bra_secret-token")
        finally:
            await client.close()

    run(with_fake_server(handler, scenario))

    assert seen["path"] == "/v1/realtime"
    assert seen["first"] == {
        "type": "auth",
        "version": 1,
        "access_token": "bra_secret-token",
    }
    assert seen["headers"] <= {
        "host",
        "upgrade",
        "connection",
        "sec-websocket-key",
        "sec-websocket-version",
    }


def test_pings_repeat_and_reauth_sends_a_fresh_auth_frame() -> None:
    frames: list[dict[str, Any]] = []
    enough = asyncio.Event

    async def scenario_inner() -> None:
        got_pings = enough()
        got_reauth = enough()

        async def handler(ws: ServerConnection) -> None:
            try:
                async for raw in ws:
                    frame = json.loads(raw)
                    frames.append(frame)
                    if frame["type"] == "auth":
                        await ws.send(ready_frame())
                        if len(frames) > 1:
                            got_reauth.set()
                    else:
                        await ws.send(pong_frame())
                        if sum(f["type"] == "ping" for f in frames) >= 3:
                            got_pings.set()
            except ConnectionClosed:
                pass

        async def scenario(origin: str) -> None:
            client = RealtimeClient(origin, TIMEOUT_S, FAST_PING_S)
            try:
                await client.connect("bra_one")
                await asyncio.wait_for(got_pings.wait(), TIMEOUT_S)
                await client.reauth("bra_two")
                await asyncio.wait_for(got_reauth.wait(), TIMEOUT_S)
            finally:
                await client.close()

        await with_fake_server(handler, scenario)

    run(scenario_inner())

    assert frames[0]["access_token"] == "bra_one"
    assert any(f["type"] == "auth" and f["access_token"] == "bra_two" for f in frames)


def test_no_ready_frame_in_time_is_an_error() -> None:
    async def handler(ws: ServerConnection) -> None:
        await ws.recv()
        await ws.wait_closed()  # never answers

    async def scenario(origin: str) -> None:
        client = RealtimeClient(origin, 0.2, FAST_PING_S)
        with pytest.raises(RealtimeError) as info:
            await client.connect("bra_x")
        assert info.value.code is None

    run(with_fake_server(handler, scenario))


@pytest.mark.parametrize("first_reply", ["not json", pong_frame()])
def test_bad_first_reply_is_an_error(first_reply: str) -> None:
    async def handler(ws: ServerConnection) -> None:
        await ws.recv()
        await ws.send(first_reply)
        await ws.wait_closed()

    async def scenario(origin: str) -> None:
        client = RealtimeClient(origin, TIMEOUT_S, FAST_PING_S)
        with pytest.raises(RealtimeError):
            await client.connect("bra_x")

    run(with_fake_server(handler, scenario))


@pytest.mark.parametrize(
    "bad", ["not json", json.dumps({"type": "other"}), b"binary"], ids=str
)
def test_invalid_inbound_frame_closes_the_socket(bad: str | bytes) -> None:
    server_saw: list[int | None] = []

    async def handler(ws: ServerConnection) -> None:
        await ws.recv()
        await ws.send(ready_frame())
        await ws.send(bad)
        await ws.wait_closed()
        server_saw.append(ws.close_code)

    async def scenario(origin: str) -> None:
        client = RealtimeClient(origin, TIMEOUT_S, FAST_PING_S)
        try:
            await client.connect("bra_x")
            assert await asyncio.wait_for(client.wait_closed(), TIMEOUT_S) is None
        finally:
            await client.close()

    run(with_fake_server(handler, scenario))

    assert server_saw == [CLOSE_PROTOCOL_ERROR]


def test_oversized_inbound_frame_closes_the_socket() -> None:
    async def handler(ws: ServerConnection) -> None:
        await ws.recv()
        await ws.send(ready_frame())
        await ws.send("x" * (MAX_WS_FRAME_BYTES + 1))
        await ws.wait_closed()

    async def scenario(origin: str) -> None:
        client = RealtimeClient(origin, TIMEOUT_S, FAST_PING_S)
        try:
            await client.connect("bra_x")
            code = await asyncio.wait_for(client.wait_closed(), TIMEOUT_S)
            assert code is None or code == 1009  # message too big
            assert code != 1000
        finally:
            await client.close()

    run(with_fake_server(handler, scenario))


def test_a_redirect_is_never_followed_and_the_token_goes_nowhere_else() -> None:
    received: list[bytes] = []

    async def elsewhere(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        received.append(b"connected")
        received.append(await reader.read(65536))
        writer.close()

    async def scenario() -> None:
        other = await asyncio.start_server(elsewhere, "127.0.0.1", 0)
        other_port = other.sockets[0].getsockname()[1]

        async def redirector(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(
                b"HTTP/1.1 302 Found\r\n"
                b"Location: ws://127.0.0.1:%d/v1/realtime\r\n"
                b"Content-Length: 0\r\nConnection: close\r\n\r\n" % other_port
            )
            await writer.drain()
            writer.close()

        first = await asyncio.start_server(redirector, "127.0.0.1", 0)
        client = RealtimeClient(
            f"http://127.0.0.1:{first.sockets[0].getsockname()[1]}",
            TIMEOUT_S,
            FAST_PING_S,
        )
        try:
            with pytest.raises(RealtimeError) as info:
                await client.connect("bra_secret-token")
            assert info.value.code is None
        finally:
            await client.close()
            first.close()
            other.close()

    run(scenario())

    # connect() has returned: a followed redirect would already have arrived.
    assert received == []


# --- product frames (M4-05) ---

ENTRY = {
    "entry_id": str(uuid4()),
    "status": "queued",
    "expires_at": "2026-10-13T12:01:30+00:00",
}
_MATCH_ID = uuid4()
MATCH = {
    "match_id": str(_MATCH_ID),
    "peer_nickname": "bobby",
    "shared_tags": ["python"],
    "shared_category": None,
    "ends_at": "2026-10-13T12:15:00+00:00",
    "chat": chat_json(_MATCH_ID, "2026-10-13T12:15:00+00:00"),
    "last_seq": 0,
}


def frame(kind: str, **fields: Any) -> str:
    return json.dumps(
        {
            "type": kind,
            "version": 1,
            "event_id": str(uuid4()),
            "server_time": datetime.now(UTC).isoformat(),
            **fields,
        }
    )


SERVER_FRAMES = [
    frame("sync", queue=None, match=None),
    frame("queue.joined", entry=ENTRY),
    frame("pong"),
    frame("match.created", entry_id=ENTRY["entry_id"], match=MATCH),
    frame("queue.ended", entry_id=ENTRY["entry_id"], reason="expired"),
    frame("pong"),
    frame("match.ended", match_id=MATCH["match_id"], reason="ended"),
]


def test_frames_reach_on_frame_in_order_and_pong_does_not() -> None:
    got: list[ServerFrame] = []

    async def handler(ws: ServerConnection) -> None:
        await ws.recv()
        await ws.send(ready_frame())
        for raw in SERVER_FRAMES:
            await ws.send(raw)
        await ws.wait_closed()

    async def scenario(origin: str) -> None:
        done = asyncio.Event()

        def on_frame(f: ServerFrame) -> None:
            got.append(f)
            if isinstance(f, WsMatchEnded):
                done.set()

        client = RealtimeClient(origin, TIMEOUT_S, FAST_PING_S, on_frame)
        try:
            ready = await client.connect("bra_x")
            assert isinstance(ready, WsReady)
            await asyncio.wait_for(done.wait(), TIMEOUT_S)
        finally:
            await client.close()

    run(with_fake_server(handler, scenario))

    assert [type(f) for f in got] == [
        WsSync,
        WsQueueJoined,
        WsMatchCreated,
        WsQueueEnded,
        WsMatchEnded,
    ]
    assert isinstance(got[2], WsMatchCreated)
    assert got[2].match.peer_nickname == "bobby"


def test_a_failing_handler_does_not_stop_later_frames() -> None:
    seen: list[str] = []

    async def handler(ws: ServerConnection) -> None:
        await ws.recv()
        await ws.send(ready_frame())
        await ws.send(frame("sync", queue=None, match=None))
        await ws.send(frame("queue.joined", entry=ENTRY))
        await ws.wait_closed()

    async def scenario(origin: str) -> None:
        done = asyncio.Event()

        def on_frame(f: ServerFrame) -> None:
            seen.append(f.type)
            if isinstance(f, WsQueueJoined):
                done.set()
            else:
                raise RuntimeError("handler bug")

        client = RealtimeClient(origin, TIMEOUT_S, FAST_PING_S, on_frame)
        try:
            await client.connect("bra_x")
            await asyncio.wait_for(done.wait(), TIMEOUT_S)
        finally:
            await client.close()

    run(with_fake_server(handler, scenario))

    assert seen == ["sync", "queue.joined"]


def test_a_malformed_product_frame_closes_the_socket_and_is_not_delivered() -> None:
    got: list[ServerFrame] = []
    server_saw: list[int | None] = []

    async def handler(ws: ServerConnection) -> None:
        await ws.recv()
        await ws.send(ready_frame())
        await ws.send(frame("queue.joined", entry={"entry_id": "not a uuid"}))
        await ws.wait_closed()
        server_saw.append(ws.close_code)

    async def scenario(origin: str) -> None:
        client = RealtimeClient(origin, TIMEOUT_S, FAST_PING_S, got.append)
        try:
            await client.connect("bra_x")
            assert await asyncio.wait_for(client.wait_closed(), TIMEOUT_S) is None
        finally:
            await client.close()

    run(with_fake_server(handler, scenario))

    assert got == []
    assert server_saw == [CLOSE_PROTOCOL_ERROR]

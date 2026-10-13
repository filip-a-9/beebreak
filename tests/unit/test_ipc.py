import asyncio
import contextlib
import os
import shutil
import socket
import stat
import tempfile
import threading
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from breakroom_agent_bridge.ipc_client import request_status, send_event
from breakroom_companion.ipc_server import IpcServer
from breakroom_shared.events import NormalizedEvent
from breakroom_shared.ipc import MAX_FRAME_BYTES, StatusReply


def make_event() -> NormalizedEvent:
    return NormalizedEvent(
        event_version=1,
        event_id=uuid.uuid4(),
        local_session_key="s1",
        agent="claude_code",
        type="tool_started",
        observed_at=datetime.now(UTC),
        tool_class="shell",
    )


STATUS = StatusReply(kind="status", version=3, invitation="suggested")


@pytest.fixture
def sock_path() -> Iterator[Path]:
    d = tempfile.mkdtemp(dir="/tmp")
    yield Path(d) / "c.sock"
    shutil.rmtree(d, ignore_errors=True)


def make_server(path: Path, received: list[NormalizedEvent]) -> IpcServer:
    return IpcServer(path, on_event=received.append, status=lambda: STATUS)


def test_event_round_trip(sock_path: Path) -> None:
    received: list[NormalizedEvent] = []
    event = make_event()

    async def scenario() -> bool:
        server = make_server(sock_path, received)
        await server.start()
        try:
            ok = await asyncio.to_thread(send_event, event, socket_path=sock_path)
            await asyncio.sleep(0.1)
            return ok
        finally:
            await server.close()

    assert asyncio.run(scenario()) is True
    assert received == [event]


def test_status_round_trip(sock_path: Path) -> None:
    async def scenario() -> StatusReply | None:
        server = make_server(sock_path, [])
        await server.start()
        try:
            return await asyncio.to_thread(request_status, socket_path=sock_path)
        finally:
            await server.close()

    assert asyncio.run(scenario()) == STATUS


def test_socket_mode_is_0600_and_removed_on_close(sock_path: Path) -> None:
    async def scenario() -> int:
        server = make_server(sock_path, [])
        await server.start()
        mode = stat.S_IMODE(os.stat(sock_path).st_mode)
        await server.close()
        return mode

    assert asyncio.run(scenario()) == 0o600
    assert not sock_path.exists()


def test_stale_socket_file_is_replaced(sock_path: Path) -> None:
    stale = socket.socket(socket.AF_UNIX)
    stale.bind(str(sock_path))
    stale.close()  # file remains, nobody listening
    assert sock_path.exists()

    async def scenario() -> StatusReply | None:
        server = make_server(sock_path, [])
        await server.start()
        try:
            return await asyncio.to_thread(request_status, socket_path=sock_path)
        finally:
            await server.close()

    assert asyncio.run(scenario()) == STATUS


def test_regular_file_at_path_is_replaced(sock_path: Path) -> None:
    sock_path.write_text("x")

    async def scenario() -> StatusReply | None:
        server = make_server(sock_path, [])
        await server.start()
        try:
            return await asyncio.to_thread(request_status, socket_path=sock_path)
        finally:
            await server.close()

    assert asyncio.run(scenario()) == STATUS


def test_second_server_on_live_socket_raises(sock_path: Path) -> None:
    async def scenario() -> StatusReply | None:
        first = make_server(sock_path, [])
        await first.start()
        try:
            with pytest.raises(RuntimeError):
                await make_server(sock_path, []).start()
            # the live socket must survive the failed attempt
            return await asyncio.to_thread(request_status, socket_path=sock_path)
        finally:
            await first.close()

    assert asyncio.run(scenario()) == STATUS


def test_client_without_socket_fails_fast(sock_path: Path) -> None:
    start = time.monotonic()
    assert send_event(make_event(), socket_path=sock_path) is False
    assert request_status(socket_path=sock_path) is None
    assert time.monotonic() - start < 0.5


def test_client_returns_within_timeout_against_hung_server(sock_path: Path) -> None:
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(sock_path))
    listener.listen(1)
    listener.settimeout(3)  # never hang the suite if the client doesn't connect
    accepted: list[socket.socket] = []

    def accept_one() -> None:
        with contextlib.suppress(OSError):
            accepted.append(listener.accept()[0])

    t = threading.Thread(target=accept_one)
    t.start()
    try:
        start = time.monotonic()
        assert request_status(socket_path=sock_path, timeout=0.3) is None
        assert time.monotonic() - start < 0.3 + 0.3
    finally:
        t.join()
        for c in accepted:
            c.close()
        listener.close()


def raw_send(path: Path, data: bytes) -> None:
    with socket.socket(socket.AF_UNIX) as s:
        s.settimeout(2)
        s.connect(str(path))
        try:
            s.sendall(data)
            s.shutdown(socket.SHUT_WR)
            s.recv(1)
        except OSError:
            pass


@pytest.mark.parametrize(
    "payload",
    [
        b"{not json\n",
        b'{"kind":"event","event":{"cwd":"/secret"}}\n',
        b"x" * (MAX_FRAME_BYTES * 2) + b"\n",
        b"x" * (MAX_FRAME_BYTES * 2),
    ],
    ids=["malformed", "wrong-shape", "oversize-line", "oversize-no-newline"],
)
def test_bad_frames_are_dropped_and_server_keeps_serving(
    sock_path: Path, payload: bytes
) -> None:
    received: list[NormalizedEvent] = []

    async def scenario() -> bool:
        server = make_server(sock_path, received)
        await server.start()
        try:
            await asyncio.to_thread(raw_send, sock_path, payload)
            return await asyncio.to_thread(
                send_event, make_event(), socket_path=sock_path
            )
        finally:
            await asyncio.sleep(0.1)
            await server.close()

    assert asyncio.run(scenario()) is True
    assert len(received) == 1


def test_silent_connection_times_out_and_server_keeps_serving(
    sock_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BREAKROOM_IPC_READ_TIMEOUT_S", "0.2")

    async def scenario() -> StatusReply | None:
        server = make_server(sock_path, [])
        await server.start()
        idle = socket.socket(socket.AF_UNIX)
        idle.connect(str(sock_path))
        try:
            await asyncio.sleep(0.5)
            idle.settimeout(1)
            closed = await asyncio.to_thread(idle.recv, 1)  # EOF: server hung up
            assert closed == b""
            return await asyncio.to_thread(request_status, socket_path=sock_path)
        finally:
            idle.close()
            await server.close()

    assert asyncio.run(scenario()) == STATUS


def test_callback_exception_does_not_stop_server(sock_path: Path) -> None:
    calls: list[int] = []

    def boom(_: NormalizedEvent) -> None:
        calls.append(1)
        raise ValueError("callback failure")

    async def scenario() -> StatusReply | None:
        server = IpcServer(sock_path, on_event=boom, status=lambda: STATUS)
        await server.start()
        try:
            await asyncio.to_thread(send_event, make_event(), socket_path=sock_path)
            await asyncio.sleep(0.1)
            return await asyncio.to_thread(request_status, socket_path=sock_path)
        finally:
            await server.close()

    assert asyncio.run(scenario()) == STATUS
    assert calls == [1]

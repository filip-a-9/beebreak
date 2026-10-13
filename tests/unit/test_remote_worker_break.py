"""RemoteWorker break flows against a tiny in-test server (M4-05 part A).

The server speaks just enough of the wire contract (refresh, me, logout, the four
queue/match routes, `/v1/realtime`) to drive the real worker, API client and
realtime client. Starlette plus uvicorn stand in because the HTTP routes and the
WebSocket share one port, as the worker requires. The live-server tests are part B.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import uvicorn
from PySide6.QtCore import QObject, Slot
from pytestqt.qtbot import QtBot
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from breakroom_companion.break_state import BreakPhase, BreakState
from breakroom_companion.credentials import FileCredentialStore, StoredCredentials
from breakroom_companion.remote import RemoteWorker
from breakroom_companion.remote_config import RemoteConfig
from breakroom_shared.api import QueueJoinRequest
from breakroom_shared.presence import OutboundPresence

WAIT_MS = 10_000
NICKNAME = "ada_l"
PEER = "bobby"
PRESENCE = OutboundPresence(
    schema_version=1,
    presence_id=UUID(int=4),
    task_category="backend",
    stack_tags=("python", "docker"),
    remaining_time_bucket="5_10m",
    status="available",
)


def _now() -> datetime:
    return datetime.now(UTC)


def match_json(match_id: UUID) -> dict[str, Any]:
    return {
        "match_id": str(match_id),
        "peer_nickname": PEER,
        "shared_tags": ["python"],
        "shared_category": None,
        "ends_at": (_now() + timedelta(minutes=15)).isoformat(),
    }


def frame(kind: str, **fields: Any) -> dict[str, Any]:
    return {
        "type": kind,
        "version": 1,
        "event_id": str(uuid4()),
        "server_time": _now().isoformat(),
        **fields,
    }


class FakeServer:
    """Records every HTTP request and WebSocket frame; replies as configured."""

    def __init__(self) -> None:
        self.requests: list[
            tuple[str, str, str, bytes]
        ] = []  # method, path, auth, body
        self.valid_tokens: set[str] = set()
        self.refreshes = 0
        self.entries: dict[str, dict[str, Any]] = {}  # idempotency key -> entry
        self.queue_error: tuple[int, str] | None = None
        self.sync: dict[str, Any] = {"queue": None, "match": None}
        self.snapshot: dict[str, Any] = {"queue": None, "match": None}
        self.snapshot_error: tuple[int, str] | None = None
        # A frame sent just before the snapshot reply, which is then delayed.
        self.frame_before_snapshot: dict[str, Any] | None = None
        self.ws_connects = 0
        self._outbox: list[asyncio.Queue[str | None]] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._uvicorn: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self.port = 0

    # --- test controls ---

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def calls(self, method: str, path_prefix: str) -> list[tuple[str, str, str, bytes]]:
        return [
            r for r in self.requests if r[0] == method and r[1].startswith(path_prefix)
        ]

    def push(self, payload: dict[str, Any]) -> None:
        """Send a frame to every connected socket."""
        self._signal(json.dumps(payload))

    def drop_sockets(self) -> None:
        self._signal(None)

    def _signal(self, item: str | None) -> None:
        loop = self._loop
        assert loop is not None
        for box in list(self._outbox):
            loop.call_soon_threadsafe(box.put_nowait, item)

    # --- the app ---

    def _auth_ok(self, request: Request) -> bool:
        return request.headers.get("authorization", "")[7:] in self.valid_tokens

    async def _record(self, request: Request) -> bytes:
        body = await request.body()
        self.requests.append(
            (
                request.method,
                request.url.path,
                request.headers.get("authorization", ""),
                body,
            )
        )
        return body

    async def _refresh(self, request: Request) -> Response:
        await self._record(request)
        self.refreshes += 1
        token = f"bra_{self.refreshes}"
        self.valid_tokens.add(token)
        return JSONResponse(
            {
                "access_token": token,
                "access_expires_at": (_now() + timedelta(minutes=15)).isoformat(),
                "refresh_token": f"brr_{self.refreshes}",
                "refresh_expires_at": (_now() + timedelta(days=30)).isoformat(),
            }
        )

    async def _me(self, request: Request) -> Response:
        await self._record(request)
        if not self._auth_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(
            {"nickname": NICKNAME, "consent": None, "consented_at": None}
        )

    async def _logout(self, request: Request) -> Response:
        await self._record(request)
        return Response(status_code=204)

    async def _join(self, request: Request) -> Response:
        body = await self._record(request)
        if not self._auth_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if self.queue_error is not None:
            status, code = self.queue_error
            return JSONResponse({"error": code}, status_code=status)
        key = json.loads(body)["idempotency_key"]
        if key in self.entries:
            return JSONResponse(self.entries[key], status_code=200)
        entry = {
            "entry_id": str(UUID(int=100 + len(self.entries))),
            "status": "queued",
            "expires_at": (_now() + timedelta(seconds=90)).isoformat(),
        }
        self.entries[key] = entry
        return JSONResponse(entry, status_code=201)

    async def _current(self, request: Request) -> Response:
        await self._record(request)
        if not self._auth_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if self.snapshot_error is not None:
            status, code = self.snapshot_error
            return JSONResponse({"error": code}, status_code=status)
        reply = JSONResponse(self.snapshot)  # read before the frame goes out
        if self.frame_before_snapshot is not None:
            for box in list(self._outbox):
                box.put_nowait(json.dumps(self.frame_before_snapshot))
            await asyncio.sleep(0.3)  # the frame wins the race to the client
        return reply

    async def _no_content(self, request: Request) -> Response:
        await self._record(request)
        if not self._auth_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return Response(status_code=204)

    async def _realtime(self, ws: WebSocket) -> None:
        await ws.accept()
        self._loop = asyncio.get_running_loop()
        first = json.loads(await ws.receive_text())
        assert first["type"] == "auth"
        await ws.send_text(json.dumps(frame("session.ready", nickname=NICKNAME)))
        await ws.send_text(json.dumps(frame("sync", **self.sync)))
        self.ws_connects += 1
        box: asyncio.Queue[str | None] = asyncio.Queue()
        self._outbox.append(box)

        async def pump_out() -> None:
            while (item := await box.get()) is not None:
                await ws.send_text(item)
            await ws.close()

        async def pump_in() -> None:
            try:
                while True:
                    message = json.loads(await ws.receive_text())
                    kind = "pong" if message["type"] == "ping" else "session.ready"
                    await ws.send_text(json.dumps(frame_for(kind)))
            except WebSocketDisconnect:
                pass

        tasks = [asyncio.create_task(pump_out()), asyncio.create_task(pump_in())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            self._outbox.remove(box)

    def _app(self) -> Starlette:
        post, delete = ["POST"], ["DELETE"]
        return Starlette(
            routes=[
                Route("/v1/auth/refresh", self._refresh, methods=post),
                Route("/v1/auth/logout", self._logout, methods=post),
                Route("/v1/me", self._me, methods=["GET"]),
                Route("/v1/queue", self._join, methods=post),
                Route("/v1/queue/current", self._no_content, methods=delete),
                Route("/v1/queue/current", self._current, methods=["GET"]),
                Route("/v1/matches/{match_id}/end", self._no_content, methods=post),
                Route("/v1/matches/{match_id}/block", self._no_content, methods=post),
                WebSocketRoute("/v1/realtime", self._realtime),
            ]
        )

    def start(self) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        config = uvicorn.Config(
            self._app(),
            host="127.0.0.1",
            port=self.port,
            log_level="warning",
            access_log=False,
            timeout_graceful_shutdown=1,
        )
        server = uvicorn.Server(config)
        self._uvicorn = server
        self._thread = threading.Thread(target=server.run, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 10
        while not server.started:
            if time.monotonic() > deadline or not self._thread.is_alive():
                raise RuntimeError("fake server did not start")
            time.sleep(0.01)

    def stop(self) -> None:
        if self._uvicorn is not None:
            self._uvicorn.should_exit = True
        if self._thread is not None:
            self._thread.join(10)


def frame_for(kind: str) -> dict[str, Any]:
    return frame(kind, nickname=None) if kind == "session.ready" else frame(kind)


class BreakRecorder(QObject):
    def __init__(self, worker: RemoteWorker) -> None:
        super().__init__()
        self.states: list[BreakState] = []
        self.errors: list[str] = []
        self.connection: list[str] = []
        worker.break_changed.connect(self.on_break)
        worker.error.connect(self.on_error)
        worker.state_changed.connect(self.on_state)

    @Slot(object)
    def on_break(self, state: object) -> None:
        assert isinstance(state, BreakState)
        self.states.append(state)

    @Slot(str)
    def on_error(self, code: str) -> None:
        self.errors.append(code)

    @Slot(str)
    def on_state(self, state: str) -> None:
        self.connection.append(state)

    @property
    def phases(self) -> list[BreakPhase]:
        return [s.phase for s in self.states]

    @property
    def last(self) -> BreakState:
        return self.states[-1] if self.states else BreakState()


class Rig:
    def __init__(
        self, server: FakeServer, worker: RemoteWorker, rec: BreakRecorder
    ) -> None:
        self.server, self.worker, self.rec = server, worker, rec


@pytest.fixture
def server() -> Iterator[FakeServer]:
    fake = FakeServer()
    fake.start()
    yield fake
    fake.stop()


@pytest.fixture
def make_rig(
    server: FakeServer, tmp_path: Path, qtbot: QtBot
) -> Iterator[Callable[..., Rig]]:
    made: list[RemoteWorker] = []

    def make(*, start: bool = True, name: str = "a") -> Rig:
        config = RemoteConfig(
            server_url=server.base_url,
            http_timeout_s=2,
            ws_ping_interval_s=1.0,
            reconnect_min_s=0.05,
            reconnect_max_s=0.2,
        )
        store = FileCredentialStore(tmp_path / f"{name}.json", server.base_url)
        store.save(
            StoredCredentials(
                refresh_token="brr_seed",
                refresh_expires_at=_now() + timedelta(days=30),
            )
        )
        worker = RemoteWorker(config, store)
        made.append(worker)
        rig = Rig(server, worker, BreakRecorder(worker))
        if start:
            worker.start()
            wait(qtbot, lambda: rig.rec.connection[-1:] == ["connected"])
        return rig

    yield make
    for worker in made:
        worker.stop()


def wait(qtbot: QtBot, condition: Callable[[], bool]) -> None:
    qtbot.waitUntil(condition, timeout=WAIT_MS)


def wait_phase(qtbot: QtBot, rig: Rig, phase: BreakPhase) -> None:
    wait(qtbot, lambda: rig.rec.last.phase == phase)


def settle(qtbot: QtBot, rig: Rig, *, extra_ms: int = 150) -> None:
    """Let queued commands run: the worker handles them in order, then we wait out
    any signal still in flight (used only to prove that nothing happened)."""
    qtbot.wait(extra_ms)


def search(qtbot: QtBot, rig: Rig) -> None:
    rig.worker.join_queue(PRESENCE)
    wait_phase(qtbot, rig, "searching")


def match_up(qtbot: QtBot, rig: Rig, match_id: UUID | None = None) -> None:
    match_id = match_id or UUID(int=900)
    search(qtbot, rig)
    entry_id = rig.rec.last.entry.entry_id  # type: ignore[union-attr]
    rig.server.push(
        frame("match.created", entry_id=str(entry_id), match=match_json(match_id))
    )
    wait_phase(qtbot, rig, "matched")


# --- join ---


def test_join_sends_the_presence_once_and_goes_joining_then_searching(
    qtbot: QtBot, make_rig: Callable[..., Rig]
) -> None:
    rig = make_rig()

    rig.worker.join_queue(PRESENCE)
    wait_phase(qtbot, rig, "searching")

    assert rig.rec.phases == ["joining", "searching"]
    assert rig.rec.last.entry is not None and rig.rec.last.entry.status == "queued"
    (call,) = rig.server.calls("POST", "/v1/queue")
    assert call[2] == "Bearer bra_1"
    sent = QueueJoinRequest.model_validate_json(call[3])
    assert sent.presence == PRESENCE
    assert rig.rec.errors == []


def test_each_join_call_gets_its_own_idempotency_key(
    qtbot: QtBot, make_rig: Callable[..., Rig]
) -> None:
    rig = make_rig()
    search(qtbot, rig)
    rig.worker.leave_queue()
    wait_phase(qtbot, rig, "none")

    search(qtbot, rig)

    keys = [
        json.loads(c[3])["idempotency_key"]
        for c in rig.server.calls("POST", "/v1/queue")
    ]
    assert len(keys) == 2 and keys[0] != keys[1]


def test_join_before_sign_in_sends_nothing(
    qtbot: QtBot, make_rig: Callable[..., Rig], server: FakeServer
) -> None:
    rig = make_rig(start=False)

    rig.worker.join_queue(PRESENCE)
    settle(qtbot, rig)

    assert rig.rec.states == [] and rig.rec.errors == []
    assert server.requests == []


def test_join_while_offline_sends_nothing(
    qtbot: QtBot, make_rig: Callable[..., Rig], server: FakeServer
) -> None:
    rig = make_rig()
    server.stop()
    wait(qtbot, lambda: rig.rec.connection[-1:] == ["offline"])
    before = len(server.requests)

    rig.worker.join_queue(PRESENCE)
    settle(qtbot, rig)

    assert rig.rec.states == []
    assert len(server.requests) == before


def test_join_while_searching_or_matched_is_ignored(
    qtbot: QtBot, make_rig: Callable[..., Rig]
) -> None:
    rig = make_rig()
    search(qtbot, rig)
    rig.worker.join_queue(PRESENCE)
    settle(qtbot, rig)
    assert len(rig.server.calls("POST", "/v1/queue")) == 1

    match_up(qtbot, rig)
    rig.worker.join_queue(PRESENCE)
    settle(qtbot, rig)

    assert len(rig.server.calls("POST", "/v1/queue")) == 1
    assert rig.rec.phases == ["joining", "searching", "matched"]


@pytest.mark.parametrize("code", ["already_queued", "already_matched"])
def test_join_conflict_goes_idle_and_shows_the_existing_search_when_announced(
    qtbot: QtBot, make_rig: Callable[..., Rig], server: FakeServer, code: str
) -> None:
    rig = make_rig()
    server.queue_error = (409, code)

    rig.worker.join_queue(PRESENCE)
    wait(qtbot, lambda: len(server.calls("POST", "/v1/queue")) == 1)
    settle(qtbot, rig)

    # Never stuck in joining: the announcement may already have been dropped.
    assert rig.rec.phases == ["joining", "none"]
    assert rig.rec.errors == []

    entry = {
        "entry_id": str(UUID(int=55)),
        "status": "queued",
        "expires_at": (_now() + timedelta(seconds=90)).isoformat(),
    }
    server.push(frame("queue.joined", entry=entry))
    wait_phase(qtbot, rig, "searching")
    assert rig.rec.last.entry is not None
    assert rig.rec.last.entry.entry_id == UUID(int=55)


@pytest.mark.parametrize(
    ("status", "code"),
    [(409, "setup_incomplete"), (429, "rate_limited"), (400, "invalid_request")],
)
def test_other_join_failures_go_back_to_none_and_report_the_code(
    qtbot: QtBot,
    make_rig: Callable[..., Rig],
    server: FakeServer,
    status: int,
    code: str,
) -> None:
    rig = make_rig()
    server.queue_error = (status, code)

    rig.worker.join_queue(PRESENCE)
    wait(qtbot, lambda: rig.rec.errors == [code])

    assert rig.rec.phases == ["joining", "none"]


def test_a_401_during_join_refreshes_and_retries_with_the_same_key(
    qtbot: QtBot, make_rig: Callable[..., Rig], server: FakeServer
) -> None:
    rig = make_rig()
    server.valid_tokens.discard("bra_1")  # the access token just went stale

    rig.worker.join_queue(PRESENCE)
    wait_phase(qtbot, rig, "searching")

    calls = server.calls("POST", "/v1/queue")
    assert [c[2] for c in calls] == ["Bearer bra_1", "Bearer bra_2"]
    assert calls[0][3] == calls[1][3]  # same key, same body
    assert len(server.entries) == 1
    assert rig.rec.phases == ["joining", "searching"]


# --- frames ---


def test_match_created_then_end_match_posts_and_moves_to_ended(
    qtbot: QtBot, make_rig: Callable[..., Rig]
) -> None:
    rig = make_rig()
    match_id = UUID(int=901)
    match_up(qtbot, rig, match_id)
    assert rig.rec.last.match is not None
    assert rig.rec.last.match.peer_nickname == PEER

    rig.worker.end_match()
    wait_phase(qtbot, rig, "ended")

    (call,) = rig.server.calls("POST", f"/v1/matches/{match_id}/end")
    assert call[2] == "Bearer bra_1"
    # The frame that follows the 204 is idempotent: no further state change.
    count = len(rig.rec.states)
    rig.server.push(frame("match.ended", match_id=str(match_id), reason="ended"))
    settle(qtbot, rig)
    assert len(rig.rec.states) == count


def test_block_peer_posts_to_block_and_ends_the_match(
    qtbot: QtBot, make_rig: Callable[..., Rig]
) -> None:
    rig = make_rig()
    match_id = UUID(int=902)
    match_up(qtbot, rig, match_id)

    rig.worker.block_peer()
    wait_phase(qtbot, rig, "ended")

    assert len(rig.server.calls("POST", f"/v1/matches/{match_id}/block")) == 1
    assert rig.server.calls("POST", f"/v1/matches/{match_id}/end") == []


def test_peer_ending_the_match_is_announced_by_frame(
    qtbot: QtBot, make_rig: Callable[..., Rig]
) -> None:
    rig = make_rig()
    match_id = UUID(int=903)
    match_up(qtbot, rig, match_id)

    rig.server.push(frame("match.ended", match_id=str(match_id), reason="expired"))

    wait_phase(qtbot, rig, "ended")


def test_leave_queue_deletes_the_entry_and_goes_to_none(
    qtbot: QtBot, make_rig: Callable[..., Rig]
) -> None:
    rig = make_rig()
    search(qtbot, rig)

    rig.worker.leave_queue()
    wait_phase(qtbot, rig, "none")

    assert len(rig.server.calls("DELETE", "/v1/queue/current")) == 1
    assert rig.rec.phases == ["joining", "searching", "none"]


def test_expiry_frame_shows_nobody_available_until_dismissed(
    qtbot: QtBot, make_rig: Callable[..., Rig]
) -> None:
    rig = make_rig()
    search(qtbot, rig)
    entry_id = rig.rec.last.entry.entry_id  # type: ignore[union-attr]

    rig.server.push(frame("queue.ended", entry_id=str(entry_id), reason="expired"))
    wait_phase(qtbot, rig, "nobody_available")

    rig.worker.dismiss_break()
    wait_phase(qtbot, rig, "none")


def test_leave_end_and_block_are_no_ops_when_there_is_nothing_to_act_on(
    qtbot: QtBot, make_rig: Callable[..., Rig], server: FakeServer
) -> None:
    rig = make_rig()
    before = len(server.requests)

    rig.worker.leave_queue()
    rig.worker.end_match()
    rig.worker.block_peer()
    settle(qtbot, rig)

    assert len(server.requests) == before
    assert rig.rec.states == [] and rig.rec.errors == []


def test_end_and_block_do_nothing_while_only_searching(
    qtbot: QtBot, make_rig: Callable[..., Rig], server: FakeServer
) -> None:
    rig = make_rig()
    search(qtbot, rig)

    rig.worker.end_match()
    rig.worker.block_peer()
    settle(qtbot, rig)

    assert server.calls("POST", "/v1/matches") == []
    assert rig.rec.last.phase == "searching"


def test_leave_queue_does_nothing_while_matched(
    qtbot: QtBot, make_rig: Callable[..., Rig], server: FakeServer
) -> None:
    rig = make_rig()
    match_up(qtbot, rig)

    rig.worker.leave_queue()
    settle(qtbot, rig)

    assert server.calls("DELETE", "/v1/queue") == []
    assert rig.rec.last.phase == "matched"


# --- sync, reconnect, sign-out ---


def test_sync_with_a_queue_entry_shows_searching_with_that_entry(
    qtbot: QtBot, make_rig: Callable[..., Rig], server: FakeServer
) -> None:
    entry = {
        "entry_id": str(UUID(int=77)),
        "status": "queued",
        "expires_at": (_now() + timedelta(seconds=60)).isoformat(),
    }
    server.sync = {"queue": entry, "match": None}

    rig = make_rig()
    wait_phase(qtbot, rig, "searching")

    assert rig.rec.phases == ["searching"]
    assert rig.rec.last.entry is not None
    assert rig.rec.last.entry.entry_id == UUID(int=77)


def test_sync_with_a_match_shows_matched(
    qtbot: QtBot, make_rig: Callable[..., Rig], server: FakeServer
) -> None:
    server.sync = {"queue": None, "match": match_json(UUID(int=78))}

    rig = make_rig()
    wait_phase(qtbot, rig, "matched")

    assert rig.rec.last.match is not None
    assert rig.rec.last.match.match_id == UUID(int=78)


def test_the_state_is_kept_offline_and_the_next_sync_corrects_it(
    qtbot: QtBot, make_rig: Callable[..., Rig], server: FakeServer
) -> None:
    rig = make_rig()
    search(qtbot, rig)
    entry_id = rig.rec.last.entry.entry_id  # type: ignore[union-attr]
    emitted = len(rig.rec.states)
    connects = server.ws_connects
    # The server restarted and forgot the search; the reconnect's sync says so.
    server.sync = {"queue": None, "match": None}

    server.drop_sockets()
    wait(qtbot, lambda: "offline" in rig.rec.connection)
    assert rig.rec.last.phase == "searching"  # kept while offline
    assert len(rig.rec.states) == emitted
    wait(qtbot, lambda: server.ws_connects > connects)
    wait_phase(qtbot, rig, "none")

    assert rig.rec.phases[-1] == "none"
    assert entry_id == UUID(int=100)


def test_reconnect_with_the_same_entry_keeps_searching_without_a_new_emit(
    qtbot: QtBot, make_rig: Callable[..., Rig], server: FakeServer
) -> None:
    rig = make_rig()
    search(qtbot, rig)
    entry = rig.rec.last.entry
    assert entry is not None
    server.sync = {"queue": json.loads(entry.model_dump_json()), "match": None}
    connects = server.ws_connects

    server.drop_sockets()
    wait(qtbot, lambda: server.ws_connects > connects)
    wait(qtbot, lambda: rig.rec.connection[-1:] == ["connected"])
    settle(qtbot, rig)

    assert rig.rec.phases == ["joining", "searching"]
    assert rig.rec.last.entry == entry


def test_sign_out_clears_the_break_state(
    qtbot: QtBot, make_rig: Callable[..., Rig]
) -> None:
    rig = make_rig()
    match_up(qtbot, rig)

    rig.worker.sign_out()

    wait_phase(qtbot, rig, "none")
    assert rig.rec.last == BreakState()


def test_a_dead_login_during_join_clears_the_state(
    qtbot: QtBot, make_rig: Callable[..., Rig], server: FakeServer
) -> None:
    rig = make_rig()
    server.valid_tokens.clear()  # the refresh endpoint will mint a token we then reject
    server.queue_error = (401, "unauthorized")

    rig.worker.join_queue(PRESENCE)
    wait(qtbot, lambda: "unauthorized" in rig.rec.errors)

    assert rig.rec.last.phase == "none"
    assert rig.rec.connection[-1] == "signed_out"


# --- privacy ---


def test_no_log_line_carries_presence_nickname_or_tags(
    qtbot: QtBot,
    make_rig: Callable[..., Rig],
    server: FakeServer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    rig = make_rig()
    server.queue_error = (429, "rate_limited")
    rig.worker.join_queue(PRESENCE)
    wait(qtbot, lambda: rig.rec.errors == ["rate_limited"])
    server.queue_error = None
    match_up(qtbot, rig)
    rig.worker.block_peer()
    wait_phase(qtbot, rig, "ended")

    text = " ".join(
        r.getMessage()
        for r in caplog.records
        if r.name.startswith("breakroom_companion")
    )

    for secret in (
        "python",
        "docker",
        PEER,
        NICKNAME,
        str(PRESENCE.presence_id),
        "bra_",
    ):
        assert secret not in text


def test_block_confirmed_after_the_peer_ended_is_still_sent_and_stays_ended(
    qtbot: QtBot, make_rig: Callable[..., Rig]
) -> None:
    rig = make_rig()
    match_id = UUID(int=905)
    match_up(qtbot, rig, match_id)
    rig.server.push(frame("match.ended", match_id=str(match_id), reason="ended"))
    wait_phase(qtbot, rig, "ended")
    count = len(rig.rec.states)

    rig.worker.block_peer()
    path = f"/v1/matches/{match_id}/block"
    wait(qtbot, lambda: len(rig.server.calls("POST", path)) == 1)
    settle(qtbot, rig)

    assert rig.rec.last.phase == "ended" and rig.rec.last.match is not None
    assert len(rig.rec.states) == count


def test_end_match_does_nothing_once_ended(
    qtbot: QtBot, make_rig: Callable[..., Rig]
) -> None:
    rig = make_rig()
    match_id = UUID(int=906)
    match_up(qtbot, rig, match_id)
    rig.server.push(frame("match.ended", match_id=str(match_id), reason="ended"))
    wait_phase(qtbot, rig, "ended")

    rig.worker.end_match()
    settle(qtbot, rig)

    assert rig.server.calls("POST", f"/v1/matches/{match_id}/end") == []


def test_second_device_that_missed_queue_joined_does_not_stick_in_joining(
    qtbot: QtBot, make_rig: Callable[..., Rig], server: FakeServer
) -> None:
    # This device shows "ended"; the other device's search was announced and
    # dropped here.
    rig = make_rig()
    match_id = UUID(int=907)
    match_up(qtbot, rig, match_id)
    server.push(frame("match.ended", match_id=str(match_id), reason="ended"))
    wait_phase(qtbot, rig, "ended")
    entry = {
        "entry_id": str(UUID(int=56)),
        "status": "queued",
        "expires_at": (_now() + timedelta(seconds=90)).isoformat(),
    }
    server.push(frame("queue.joined", entry=entry))  # ignored in "ended"
    settle(qtbot, rig)
    server.queue_error = (409, "already_queued")

    rig.worker.join_queue(PRESENCE)
    wait(qtbot, lambda: rig.rec.phases[-1:] == ["none"])

    server.push(frame("queue.ended", entry_id=entry["entry_id"], reason="expired"))
    settle(qtbot, rig)
    assert rig.rec.last.phase == "none"


def test_conflict_already_queued_shows_the_other_devices_search(
    qtbot: QtBot, make_rig: Callable[..., Rig], server: FakeServer
) -> None:
    rig = make_rig()
    entry = {
        "entry_id": str(UUID(int=57)),
        "status": "queued",
        "expires_at": (_now() + timedelta(seconds=90)).isoformat(),
    }
    server.snapshot = {"queue": entry, "match": None}
    server.queue_error = (409, "already_queued")

    rig.worker.join_queue(PRESENCE)
    wait_phase(qtbot, rig, "searching")

    assert rig.rec.last.entry is not None
    assert rig.rec.last.entry.entry_id == UUID(int=57)
    assert len(server.calls("GET", "/v1/queue/current")) == 1
    assert rig.rec.errors == []


def test_conflict_already_matched_shows_the_existing_match(
    qtbot: QtBot, make_rig: Callable[..., Rig], server: FakeServer
) -> None:
    rig = make_rig()
    server.snapshot = {"queue": None, "match": match_json(UUID(int=58))}
    server.queue_error = (409, "already_matched")

    rig.worker.join_queue(PRESENCE)
    wait_phase(qtbot, rig, "matched")

    assert rig.rec.last.match is not None
    assert rig.rec.last.match.match_id == UUID(int=58)
    assert rig.rec.errors == []


def test_conflict_with_a_failing_snapshot_falls_back_to_idle(
    qtbot: QtBot, make_rig: Callable[..., Rig], server: FakeServer
) -> None:
    rig = make_rig()
    server.snapshot_error = (429, "rate_limited")
    server.queue_error = (409, "already_queued")

    rig.worker.join_queue(PRESENCE)
    wait(qtbot, lambda: len(server.calls("GET", "/v1/queue/current")) == 1)
    settle(qtbot, rig)

    assert rig.rec.phases == ["joining", "none"]
    assert rig.rec.errors == []


def test_a_snapshot_older_than_a_frame_is_not_applied(
    qtbot: QtBot, make_rig: Callable[..., Rig], server: FakeServer
) -> None:
    rig = make_rig()
    entry = {
        "entry_id": str(UUID(int=59)),
        "status": "queued",
        "expires_at": (_now() + timedelta(seconds=90)).isoformat(),
    }
    server.snapshot = {"queue": entry, "match": None}  # read before the match
    server.frame_before_snapshot = frame(
        "match.created", entry_id=entry["entry_id"], match=match_json(UUID(int=60))
    )
    server.queue_error = (409, "already_queued")

    rig.worker.join_queue(PRESENCE)
    wait_phase(qtbot, rig, "matched")
    wait(qtbot, lambda: len(server.calls("GET", "/v1/queue/current")) == 1)
    settle(qtbot, rig, extra_ms=600)  # the stale reply has been handled by now

    assert rig.rec.last.phase == "matched"
    assert "searching" not in rig.rec.phases

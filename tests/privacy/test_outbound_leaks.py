"""End-to-end leak test (M3-06, M3 README rule 13).

A real RemoteWorker is signed in and connected to a real server on a loopback
port while real hook payloads, full of protected strings, go through the real
reducer, the real IPC client and a real IpcServer/IpcWorker. Every outbound
byte is captured at three levels:

* raw TCP bytes through a recording proxy (HTTP requests, WebSocket handshake),
* every httpx request (method, URL, headers, body),
* every WebSocket frame the client sends (frames are masked on the wire, so
  the proxy cannot read them; the client's `send` is wrapped instead).

Then the server logs and every database row are scanned too. No canary may
appear anywhere. The `test_scanner_*` tests prove the capture and the scan do
catch a canary in each channel.
"""

import asyncio
import contextlib
import copy
import json
import logging
import socket
import tempfile
import threading
import urllib.parse
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from PySide6.QtCore import QObject, Slot
from pytestqt.qtbot import QtBot
from sqlalchemy import select
from websockets.asyncio.client import ClientConnection

from breakroom_agent_bridge.ipc_client import send_event
from breakroom_agent_bridge.reducer import reduce
from breakroom_agent_bridge.session_key import derive_key
from breakroom_companion.api_client import ApiClient
from breakroom_companion.break_state import BreakState
from breakroom_companion.credentials import FileCredentialStore
from breakroom_companion.realtime_client import RealtimeClient, RealtimeError
from breakroom_companion.remote import RemoteWorker
from breakroom_companion.remote_config import RemoteConfig
from breakroom_companion.worker import IpcWorker
from breakroom_server.models import Base, User
from breakroom_shared.api import Consent
from breakroom_shared.events import NormalizedEvent
from breakroom_shared.ipc import StatusReply
from breakroom_shared.presence import build_presence
from tests.e2e_support.server import LiveServer
from tests.fixtures.hook_payloads import PAYLOADS, RAW_SESSION_ID
from tests.fixtures.privacy_negative import SENSITIVE_STRINGS

pytest_plugins = ["tests.e2e_support.server"]  # live_server fixtures

WAIT_MS = 15_000
SALT = b"privacy-test-salt-0123456789abcd"
SECRET_CANARY = "sk-test-DO-NOT-LEAK-12345"
MARKER_CANARY = "ZEBRA-7731"
HOME_DIR = str(Path.home())
REPO_NAME = "acme-payments"  # the repo name used by the privacy-negative corpus

CANARIES: list[str] = [
    *SENSITIVE_STRINGS,
    RAW_SESSION_ID,
    SECRET_CANARY,
    MARKER_CANARY,
    REPO_NAME,
    HOME_DIR,
]
_MIX = (
    f"{MARKER_CANARY} {SECRET_CANARY} {REPO_NAME} {HOME_DIR}/src {SENSITIVE_STRINGS[4]}"
)
_KEPT_KEYS = {"hook_event_name", "session_id", "tool_name", "notification_type"}


# --- capture ---


def _variants(canary: str) -> set[bytes]:
    """The ways a string could be written into JSON, a URL or a header."""
    forms = {
        canary,
        json.dumps(canary)[1:-1],
        json.dumps(canary, ensure_ascii=False)[1:-1],
        urllib.parse.quote(canary, safe=""),
        urllib.parse.quote(canary),
    }
    return {f.encode("utf-8") for f in forms}


def find_leaks(channels: dict[str, list[bytes]], canaries: list[str]) -> list[str]:
    hits = []
    for channel, blobs in channels.items():
        for blob in blobs:
            for canary in canaries:
                if any(v in blob for v in _variants(canary)):
                    hits.append(f"{channel}: {canary!r}")
    return hits


class Capture:
    """Everything the companion side wrote to the network."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.http: list[bytes] = []
        self.ws: list[bytes] = []
        self.raw: list[bytes] = []

    def add(self, channel: str, data: bytes) -> None:
        with self._lock:
            getattr(self, channel).append(data)

    def channels(self) -> dict[str, list[bytes]]:
        with self._lock:
            return {"http": list(self.http), "ws": list(self.ws), "raw": list(self.raw)}

    def ws_ping_count(self) -> int:
        with self._lock:
            return sum(1 for f in self.ws if b'"type":"ping"' in f)

    def raw_text(self) -> bytes:
        with self._lock:
            return b"".join(self.raw)


class RecordingProxy:
    """TCP forwarder 127.0.0.1:<port> -> upstream that records client bytes."""

    def __init__(self, upstream_port: int, capture: Capture) -> None:
        self._upstream = upstream_port
        self._capture = capture
        self._listener = socket.socket()
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen()
        self.port: int = self._listener.getsockname()[1]
        self._open: list[socket.socket] = []
        self._lock = threading.Lock()
        self._closed = False
        self._acceptor = threading.Thread(target=self._accept, daemon=True)
        self._acceptor.start()

    def _accept(self) -> None:
        while True:
            try:
                client, _ = self._listener.accept()
                upstream = socket.create_connection(("127.0.0.1", self._upstream))
            except OSError:
                return
            with self._lock:
                if self._closed:
                    client.close()
                    upstream.close()
                    return
                self._open += [client, upstream]
            threading.Thread(
                target=self._pump, args=(client, upstream, True), daemon=True
            ).start()
            threading.Thread(
                target=self._pump, args=(upstream, client, False), daemon=True
            ).start()

    def _pump(self, src: socket.socket, dst: socket.socket, outbound: bool) -> None:
        try:
            while data := src.recv(65536):
                if outbound:
                    self._capture.add("raw", data)
                dst.sendall(data)
        except OSError:
            pass
        finally:
            for sock in (src, dst):
                with contextlib.suppress(OSError):
                    sock.shutdown(socket.SHUT_RDWR)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            socks = list(self._open)
        # close() alone leaves a blocked accept() running; shutdown wakes it.
        with contextlib.suppress(OSError):
            self._listener.shutdown(socket.SHUT_RDWR)
        self._listener.close()
        self._acceptor.join(timeout=2)
        for sock in socks:
            sock.close()


@pytest.fixture
def capture(monkeypatch: pytest.MonkeyPatch) -> Capture:
    cap = Capture()
    real_http_send = httpx.AsyncClient.send
    real_ws_send = ClientConnection.send

    async def http_send(
        self: httpx.AsyncClient, request: httpx.Request, **kwargs: Any
    ) -> httpx.Response:
        try:
            body = request.content
        except httpx.RequestNotRead:
            body = b"<stream>"
        head = f"{request.method} {request.url}\n{dict(request.headers)}\n"
        cap.add("http", head.encode("utf-8") + body)
        return await real_http_send(self, request, **kwargs)

    async def ws_send(self: ClientConnection, message: Any, *a: Any, **k: Any) -> None:
        cap.add("ws", message.encode("utf-8") if isinstance(message, str) else message)
        return await real_ws_send(self, message, *a, **k)

    monkeypatch.setattr(httpx.AsyncClient, "send", http_send)
    monkeypatch.setattr(ClientConnection, "send", ws_send)
    return cap


@pytest.fixture
def proxy(live_server: LiveServer, capture: Capture) -> Iterator[RecordingProxy]:
    p = RecordingProxy(live_server.port, capture)
    yield p
    p.close()


def dump_database(server: LiveServer) -> dict[str, list[bytes]]:
    """Every column of every row of every table, as searchable bytes."""
    out: dict[str, list[bytes]] = {}
    with server.session_factory() as session:
        for table in Base.metadata.sorted_tables:
            blobs: list[bytes] = []
            for row in session.execute(select(table)).all():
                for value in row:
                    if isinstance(value, bytes):
                        blobs += [value, value.hex().encode()]
                    elif value is not None:
                        blobs.append(str(value).encode("utf-8"))
            out[f"db.{table.name}"] = blobs
    return out


# --- driving the companion ---


class Recorder(QObject):
    def __init__(self, worker: RemoteWorker) -> None:
        super().__init__()
        self.states: list[str] = []
        self.errors: list[str] = []
        self.nickname: str | None = None
        self.consent: Consent | None = None
        self.breaks: list[BreakState] = []
        worker.break_changed.connect(self.on_break)
        worker.state_changed.connect(self.on_state)
        worker.me_changed.connect(self.on_me)
        worker.error.connect(self.on_error)

    @Slot(str)
    def on_state(self, state: str) -> None:
        self.states.append(state)

    @Slot(object)
    def on_me(self, me: Any) -> None:
        if me is not None:
            self.nickname, self.consent = me.nickname, me.consent

    @Slot(object)
    def on_break(self, state: Any) -> None:
        self.breaks.append(state)

    @property
    def phase(self) -> str:
        return self.breaks[-1].phase if self.breaks else "none"

    @Slot(str)
    def on_error(self, code: str) -> None:
        self.errors.append(code)


class EventSink(QObject):
    def __init__(self, worker: IpcWorker) -> None:
        super().__init__()
        self.events: list[NormalizedEvent] = []
        worker.event_received.connect(self.on_event)

    @Slot(object)
    def on_event(self, event: object) -> None:
        assert isinstance(event, NormalizedEvent)
        self.events.append(event)


@pytest.fixture
def worker_factory(
    tmp_path: Path, qtbot: QtBot
) -> Iterator[Callable[[int], tuple[RemoteWorker, Recorder]]]:
    made: list[RemoteWorker] = []

    def make(port: int) -> tuple[RemoteWorker, Recorder]:
        config = RemoteConfig(
            server_url=f"http://127.0.0.1:{port}",
            http_timeout_s=2,
            ws_ping_interval_s=1.0,  # the server rate-limits above 2 frames/s
            reconnect_min_s=0.05,
            reconnect_max_s=0.2,
        )
        worker = RemoteWorker(
            config,
            FileCredentialStore(tmp_path / f"creds{len(made)}.json", config.server_url),
        )
        made.append(worker)
        return worker, Recorder(worker)

    yield make
    for worker in made:
        worker.stop()


ALL_CONSENT = Consent(
    version=1,
    share_task_category=True,
    share_stack_tags=True,
    share_time_bucket=True,
)


def sign_in_worker(
    qtbot: QtBot,
    live_server: LiveServer,
    worker: RemoteWorker,
    rec: Recorder,
    email: str,
    nickname: str,
    consent: Consent,
) -> None:
    """Start the worker and take it through sign-in, nickname and consent."""
    mails = len(live_server.mailer.sent)
    worker.start()
    qtbot.waitUntil(lambda: rec.states[-1:] == ["signed_out"], timeout=WAIT_MS)
    worker.begin_sign_in(email, live_server.invite(email))
    qtbot.waitUntil(lambda: len(live_server.mailer.sent) > mails, timeout=WAIT_MS)
    worker.verify(live_server.last_code())
    qtbot.waitUntil(lambda: rec.states[-1:] == ["connected"], timeout=WAIT_MS)
    worker.set_nickname(nickname)
    qtbot.waitUntil(lambda: rec.nickname == nickname, timeout=WAIT_MS)
    worker.set_consent(consent)
    qtbot.waitUntil(lambda: rec.consent == consent, timeout=WAIT_MS)


def _protected_payloads() -> list[dict[str, Any]]:
    """The corpus, plus a copy with every free-text value replaced by canaries."""

    def scrub(value: Any, key: str = "") -> Any:
        if isinstance(value, dict):
            return {k: scrub(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [scrub(v) for v in value]
        if isinstance(value, str) and key not in _KEPT_KEYS:
            return _MIX
        return value

    base = [copy.deepcopy(p) for p in PAYLOADS.values()]
    return base + [scrub(p) for p in base]


def _reduced_events() -> list[NormalizedEvent]:
    events = []
    for payload in _protected_payloads():
        event = reduce(payload, salt=SALT, now=datetime.now(UTC), event_id=uuid4())
        if event is not None:
            events.append(event)
    return events


# --- the end-to-end test ---


def test_no_protected_string_leaves_the_machine(
    qtbot: QtBot,
    live_server: LiveServer,
    capture: Capture,
    proxy: RecordingProxy,
    worker_factory: Callable[[int], tuple[RemoteWorker, Recorder]],
    caplog: pytest.LogCaptureFixture,
    capfd: pytest.CaptureFixture[str],
) -> None:
    caplog.set_level(logging.DEBUG)
    worker, rec = worker_factory(proxy.port)
    email = "ada@example.com"

    # Sign in, set a nickname, consent to everything: the strongest case for a leak.
    worker.start()
    qtbot.waitUntil(lambda: rec.states[-1:] == ["signed_out"], timeout=WAIT_MS)
    worker.begin_sign_in(email, live_server.invite(email))
    qtbot.waitUntil(lambda: len(live_server.mailer.sent) > 0, timeout=WAIT_MS)
    worker.verify(live_server.last_code())
    qtbot.waitUntil(lambda: rec.states[-1:] == ["connected"], timeout=WAIT_MS)
    worker.set_nickname("ada_l")
    qtbot.waitUntil(lambda: rec.nickname == "ada_l", timeout=WAIT_MS)
    everything = Consent(
        version=1,
        share_task_category=True,
        share_stack_tags=True,
        share_time_bucket=True,
    )
    worker.set_consent(everything)
    qtbot.waitUntil(lambda: rec.consent == everything, timeout=WAIT_MS)

    # Real IPC path: reducer -> hook client -> Unix socket -> IpcWorker.
    events = _reduced_events()
    assert len(events) >= 20
    sock_dir = Path(tempfile.mkdtemp(dir="/tmp"))
    sock_dir.chmod(0o700)
    ipc = IpcWorker(
        sock_dir / "s",
        StatusReply(kind="status", version=4, invitation="idle"),
    )
    sink = EventSink(ipc)
    ipc.start()
    try:
        pings_before = capture.ws_ping_count()
        for event in events:
            assert send_event(event, socket_path=ipc.socket_path, timeout=2.0)
        qtbot.waitUntil(lambda: len(sink.events) == len(events), timeout=WAIT_MS)
        # Keep the connection live while the events are being handled.
        qtbot.waitUntil(
            lambda: capture.ws_ping_count() >= pings_before + 2, timeout=WAIT_MS
        )
    finally:
        ipc.stop()
        sock_dir.joinpath("s").unlink(missing_ok=True)
        sock_dir.rmdir()
    assert rec.states[-1] == "connected"
    assert rec.errors == []

    # Sanity: the capture saw the real conversation, so "no hits" means something.
    raw = capture.raw_text()
    assert b"POST /v1/auth/verify" in raw
    assert b"/v1/realtime" in raw
    assert any(b"Bearer bra_" in b for b in capture.channels()["http"])
    assert any(b'"type":"auth"' in f for f in capture.channels()["ws"])
    assert capture.ws_ping_count() >= 2

    # What stays local: the derived session keys and event ids never go out either.
    local_only = [e.local_session_key for e in events] + [
        str(e.event_id) for e in events
    ]
    assert derive_key(RAW_SESSION_ID, SALT) in local_only

    logs = [caplog.text.encode("utf-8")]
    out = capfd.readouterr()
    logs += [out.out.encode("utf-8"), out.err.encode("utf-8")]
    channels = capture.channels() | {"server_logs": logs} | dump_database(live_server)
    assert channels["db.users"], "database dump is empty"
    assert find_leaks(channels, CANARIES + local_only) == []


# --- the detector itself: it must see a canary in every channel ---


def test_scanner_sees_a_canary_in_each_outbound_and_server_channel(
    qtbot: QtBot,
    live_server: LiveServer,
    capture: Capture,
    proxy: RecordingProxy,
    caplog: pytest.LogCaptureFixture,
) -> None:
    canary = "sk-test-DO-NOT-LEAK-CANARY"
    base_url = f"http://127.0.0.1:{proxy.port}"

    async def leak() -> None:
        api = ApiClient(base_url, 2.0)
        try:
            # HTTP: body of a real request (account enumeration keeps it harmless).
            await api.start_auth(f"{canary}@example.com", None)
        finally:
            await api.aclose()
        rt = RealtimeClient(base_url, 2.0, 1.0)
        try:
            # WebSocket: the auth frame carries the canary as its token.
            await rt.connect(canary)
        except RealtimeError:
            pass
        finally:
            await rt.close()

    asyncio.run(leak())
    logging.getLogger("breakroom_server.test").warning("leaked %s", canary)
    with live_server.session_factory() as session:
        session.add(
            User(
                email_canonical=f"{canary}@example.com",
                created_at=datetime.now(UTC),
            )
        )
        session.commit()

    channels = (
        capture.channels()
        | {"server_logs": [caplog.text.encode("utf-8")]}
        | dump_database(live_server)
    )
    hits = find_leaks(channels, [canary])
    assert {h.split(":")[0] for h in hits} >= {
        "http",
        "ws",
        "raw",
        "server_logs",
        "db.users",
    }
    # And nothing is reported when the canary is absent.
    assert find_leaks(channels, ["never-sent-0xC0FFEE"]) == []


# --- M4: a full search -> match -> end between two real workers ---


def test_no_protected_string_leaves_during_search_match_and_end(
    qtbot: QtBot,
    server_factory: Callable[..., LiveServer],
    capture: Capture,
    worker_factory: Callable[[int], tuple[RemoteWorker, Recorder]],
    caplog: pytest.LogCaptureFixture,
    capfd: pytest.CaptureFixture[str],
) -> None:
    caplog.set_level(logging.DEBUG)
    live_server = server_factory(match_sweep_interval_s=0.05)
    proxy = RecordingProxy(live_server.port, capture)
    try:
        a, rec_a = worker_factory(proxy.port)
        b, rec_b = worker_factory(proxy.port)
        sign_in_worker(
            qtbot, live_server, a, rec_a, "ada@example.com", "ada_l", ALL_CONSENT
        )
        sign_in_worker(
            qtbot, live_server, b, rec_b, "bob@example.com", "bobby", ALL_CONSENT
        )

        def presence() -> Any:
            return build_presence(
                ALL_CONSENT,
                task_category="other",
                stack_tags=["python", "docker"],
                remaining_time_bucket="unknown",
            )

        # Hook traffic full of canaries reaches the companion while it searches.
        events = _reduced_events()
        sock_dir = Path(tempfile.mkdtemp(dir="/tmp"))
        sock_dir.chmod(0o700)
        ipc = IpcWorker(
            sock_dir / "s", StatusReply(kind="status", version=4, invitation="idle")
        )
        sink = EventSink(ipc)
        ipc.start()
        try:
            a.join_queue(presence())
            qtbot.waitUntil(lambda: rec_a.phase == "searching", timeout=WAIT_MS)
            for event in events:
                assert send_event(event, socket_path=ipc.socket_path, timeout=2.0)
            qtbot.waitUntil(lambda: len(sink.events) == len(events), timeout=WAIT_MS)
            b.join_queue(presence())
            qtbot.waitUntil(
                lambda: rec_a.phase == "matched" and rec_b.phase == "matched",
                timeout=WAIT_MS,
            )
            a.end_match()
            qtbot.waitUntil(
                lambda: rec_a.phase == "ended" and rec_b.phase == "ended",
                timeout=WAIT_MS,
            )
        finally:
            ipc.stop()
            sock_dir.joinpath("s").unlink(missing_ok=True)
            sock_dir.rmdir()

        # Sanity: the capture saw the break conversation.
        http = capture.channels()["http"]
        assert any(b"POST http://127.0.0.1" in h and b"/v1/queue" in h for h in http)
        assert any(b"/end" in h for h in http)

        local_only = [e.local_session_key for e in events] + [
            str(e.event_id) for e in events
        ]
        logs = [caplog.text.encode("utf-8")]
        out = capfd.readouterr()
        logs += [out.out.encode("utf-8"), out.err.encode("utf-8")]
        channels = (
            capture.channels() | {"server_logs": logs} | dump_database(live_server)
        )
        assert channels["db.queue_entries"], "no queue rows were written"
        assert find_leaks(channels, CANARIES + local_only) == []
    finally:
        proxy.close()

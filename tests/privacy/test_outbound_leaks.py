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

import ast
import asyncio
import base64
import contextlib
import copy
import json
import logging
import re
import socket
import tempfile
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
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
from breakroom_companion.controller import Controller
from breakroom_companion.credentials import FileCredentialStore, StoredCredentials
from breakroom_companion.privacy_preview import DEFAULT_DEVICE_NAME
from breakroom_companion.realtime_client import RealtimeClient, RealtimeError
from breakroom_companion.remote import RemoteWorker
from breakroom_companion.remote_config import RemoteConfig
from breakroom_companion.state_store import FileStateStore
from breakroom_companion.worker import IpcWorker
from breakroom_server.account_data import export_account
from breakroom_server.admin import main as admin_main
from breakroom_server.models import Base, Device, User
from breakroom_shared.api import Consent
from breakroom_shared.events import NormalizedEvent
from breakroom_shared.ipc import StatusReply
from breakroom_shared.presence import build_presence
from tests.e2e_support.server import LiveServer
from tests.fixtures.hook_payloads import PAYLOADS, RAW_SESSION_ID
from tests.fixtures.privacy_corpus import load_corpus, scan
from tests.fixtures.privacy_negative import SENSITIVE_STRINGS
from tests.server.conftest import TEST_SECRET

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


def find_leaks(channels: dict[str, list[bytes]], canaries: list[str]) -> list[str]:
    """Each canary, in any encoding the corpus scanner knows, in any channel."""
    return scan.find_leaks(channels, [(c, c) for c in canaries])


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
    device_name: str = "test box",
) -> None:
    """Start the worker and take it through sign-in, nickname and consent."""
    mails = len(live_server.mailer.sent)
    worker.start()
    qtbot.waitUntil(lambda: rec.states[-1:] == ["signed_out"], timeout=WAIT_MS)
    worker.begin_sign_in(email, live_server.invite(email))
    qtbot.waitUntil(lambda: len(live_server.mailer.sent) > mails, timeout=WAIT_MS)
    worker.verify(live_server.last_code(), device_name)
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
    worker.verify(live_server.last_code(), "test box")
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


# --- M5: a chat and a report between two real workers ---

CHAT_BODY_A = "hello from ada, chat canary 41c7"
CHAT_BODY_B = "hello from bob, chat canary 9e02"
REPORT_NOTE = "report note canary 5b3d"
_MESSAGE_PATH = re.compile(r"^POST http://\S+/v1/matches/[0-9a-f-]{36}/messages$")


def _requests(capture: Capture) -> list[tuple[str, dict[str, str], bytes]]:
    """(method + url, headers, body) of every captured httpx request."""
    out = []
    for blob in capture.channels()["http"]:
        head, headers, body = blob.split(b"\n", 2)
        out.append(
            (
                head.decode("utf-8"),
                {k.lower(): v for k, v in ast.literal_eval(headers.decode()).items()},
                body,
            )
        )
    return out


def test_chat_and_report_requests_carry_only_chat_fields(
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
        presence = build_presence(
            ALL_CONSENT,
            task_category="other",
            stack_tags=["python", "docker"],
            remaining_time_bucket="unknown",
        )
        a.join_queue(presence)
        b.join_queue(presence)
        qtbot.waitUntil(
            lambda: rec_a.phase == "matched" and rec_b.phase == "matched",
            timeout=WAIT_MS,
        )

        # Hook traffic full of canaries reaches the companion mid-chat.
        events = _reduced_events()
        sock_dir = Path(tempfile.mkdtemp(dir="/tmp"))
        sock_dir.chmod(0o700)
        ipc = IpcWorker(
            sock_dir / "s", StatusReply(kind="status", version=4, invitation="idle")
        )
        sink = EventSink(ipc)
        ipc.start()
        try:
            qtbot.waitUntil(
                lambda: (
                    a.chat_state.status is not None and b.chat_state.status is not None
                ),
                timeout=WAIT_MS,
            )
            a.send_message(CHAT_BODY_A)
            for event in events:
                assert send_event(event, socket_path=ipc.socket_path, timeout=2.0)
            qtbot.waitUntil(lambda: len(sink.events) == len(events), timeout=WAIT_MS)
            qtbot.waitUntil(lambda: len(b.chat_state.messages) == 1, timeout=WAIT_MS)
            b.send_message(CHAT_BODY_B)
            qtbot.waitUntil(lambda: len(a.chat_state.messages) == 2, timeout=WAIT_MS)
            a.request_extension()
            qtbot.waitUntil(
                lambda: (
                    b.chat_state.status is not None
                    and b.chat_state.status.extension == "requested_by_peer"
                ),
                timeout=WAIT_MS,
            )
            b.answer_extension(False)
            qtbot.waitUntil(
                lambda: (
                    a.chat_state.status is not None
                    and a.chat_state.status.extension == "none"
                ),
                timeout=WAIT_MS,
            )
            a.report_peer("spam", REPORT_NOTE, (2,))
            qtbot.waitUntil(
                lambda: rec_a.phase == "ended" and rec_b.phase == "ended",
                timeout=WAIT_MS,
            )
        finally:
            ipc.stop()
            sock_dir.joinpath("s").unlink(missing_ok=True)
            sock_dir.rmdir()

        sent = _requests(capture)
        messages = [r for r in sent if _MESSAGE_PATH.match(r[0])]
        assert len(messages) == 2
        for _head, headers, body in messages:
            assert set(json.loads(body)) == {"client_message_id", "body"}
            assert headers["content-type"] == "application/json"
        assert sorted(json.loads(m[2])["body"] for m in messages) == sorted(
            [CHAT_BODY_A, CHAT_BODY_B]
        )

        (report,) = [r for r in sent if r[0].endswith("/v1/reports")]
        assert set(json.loads(report[2])) == {
            "match_id",
            "reason",
            "note",
            "message_seqs",
        }
        extensions = [r for r in sent if r[0].endswith("/extension")]
        assert len(extensions) == 2
        for _head, _headers, body in extensions:
            assert set(json.loads(body)) == {"action"}

        # Every chat or report request is a bare API call: no query string on
        # the POSTs, the bearer token the only credential, nothing custom.
        allowed = {
            "host",
            "accept",
            "accept-encoding",
            "connection",
            "user-agent",
            "content-length",
            "content-type",
            "authorization",
        }
        for head, headers, _body in [*messages, report, *extensions]:
            assert "?" not in head
            assert set(headers) <= allowed
            assert headers["authorization"].startswith("Bearer bra_")

        # Sanity: the capture saw the chat, so "no hits" means something.
        assert any(CHAT_BODY_A.encode() in blob for blob in capture.channels()["raw"])

        local_only = [e.local_session_key for e in events] + [
            str(e.event_id) for e in events
        ]
        logs = [caplog.text.encode("utf-8")]
        out = capfd.readouterr()
        logs += [out.out.encode("utf-8"), out.err.encode("utf-8")]
        channels = (
            capture.channels() | {"server_logs": logs} | dump_database(live_server)
        )
        assert channels["db.messages"], "no chat rows were written"
        # No task metadata, path or hook field anywhere; the chat text itself
        # is not in the canary list because it legitimately leaves.
        assert find_leaks(channels, CANARIES + local_only) == []
    finally:
        proxy.close()


# --- M7: the versioned corpus through every channel (M6-07) ---

CORPUS = load_corpus()
LOCAL_HOSTNAME = socket.gethostname()
EDITED_DEVICE_NAME = "Work laptop 2"
PLANTED_DEVICE = CORPUS.values[0]  # replaced below by a short enough value
for _v in CORPUS.bases:
    if _v.id == "hostname-2":  # 29 characters: a legal device name
        PLANTED_DEVICE = _v
ADMIN_EMAIL_A = "ada@example.com"
ADMIN_EMAIL_B = "bob@example.com"


def _corpus_hook_payloads() -> list[dict[str, Any]]:
    """One real-shaped hook payload per corpus value, templates in rotation.

    Every free-text field carries the value; for session ids the raw
    `session_id` is the value too, since that is the field it arrives in.
    """
    templates = list(PAYLOADS.values())
    out = []
    for i, entry in enumerate(CORPUS.values):

        def fill(node: Any, key: str = "", entry: Any = entry) -> Any:
            if isinstance(node, dict):
                return {k: fill(v, k) for k, v in node.items()}
            if isinstance(node, list):
                return [fill(v) for v in node]
            if isinstance(node, str) and (
                key not in _KEPT_KEYS
                or (key == "session_id" and entry.category == "session_id")
            ):
                return entry.value
            return node

        out.append(fill(copy.deepcopy(templates[i % len(templates)])))
    return out


def _corpus_events() -> list[NormalizedEvent]:
    events = []
    for payload in _corpus_hook_payloads():
        event = reduce(payload, salt=SALT, now=datetime.now(UTC), event_id=uuid4())
        if event is not None:
            events.append(event)
    return events


@dataclass
class Plan:
    """What a scenario run feeds in. Chat text, the report note and the device
    name are typed on purpose, so the normal run keeps corpus values out of
    them; the planting run puts one in each to prove the scan sees them."""

    device_name_b: str = EDITED_DEVICE_NAME
    chat_a: str = CHAT_BODY_A
    chat_b: str = CHAT_BODY_B
    note: str = REPORT_NOTE
    hooks: bool = True


@dataclass
class Scenario:
    qtbot: QtBot
    server: LiveServer
    tmp_path: Path
    plan: Plan
    a: RemoteWorker
    b: RemoteWorker
    rec_a: Recorder
    rec_b: Recorder
    events: list[NormalizedEvent] = field(default_factory=list)
    exports: list[bytes] = field(default_factory=list)
    local_state: Path | None = None


def step_sign_in(s: Scenario) -> None:
    """Default device name for one worker, an edited one for the other (M6-06)."""
    sign_in_worker(
        s.qtbot, s.server, s.a, s.rec_a, ADMIN_EMAIL_A, "ada_l", ALL_CONSENT,
        DEFAULT_DEVICE_NAME,
    )  # fmt: skip
    sign_in_worker(
        s.qtbot, s.server, s.b, s.rec_b, ADMIN_EMAIL_B, "bobby", ALL_CONSENT,
        s.plan.device_name_b,
    )  # fmt: skip
    with s.server.session_factory() as session:
        names = set(session.scalars(select(Device.name)))
    assert names == {DEFAULT_DEVICE_NAME, s.plan.device_name_b}


def step_match(s: Scenario) -> None:
    presence = build_presence(
        ALL_CONSENT,
        task_category="other",
        stack_tags=["python", "docker"],
        remaining_time_bucket="unknown",
    )
    s.a.join_queue(presence)
    s.b.join_queue(presence)
    s.qtbot.waitUntil(
        lambda: s.rec_a.phase == "matched" and s.rec_b.phase == "matched",
        timeout=WAIT_MS,
    )
    s.qtbot.waitUntil(
        lambda: s.a.chat_state.status is not None and s.b.chat_state.status is not None,
        timeout=WAIT_MS,
    )


def step_hooks(s: Scenario) -> None:
    """Corpus-filled hook payloads through the real reducer, IPC client and
    server, then into a real Controller that persists its state to a file.

    These events reach a standalone IpcWorker and Controller, while the
    RemoteWorkers use a fixed presence, so a leak added in app.py's wire_break
    glue is not covered (inherited from M3-06)."""
    if not s.plan.hooks:
        return
    s.events = _corpus_events()
    assert len(s.events) >= len(CORPUS.values) // 2
    sock_dir = Path(tempfile.mkdtemp(dir="/tmp"))
    sock_dir.chmod(0o700)
    ipc = IpcWorker(
        sock_dir / "s", StatusReply(kind="status", version=4, invitation="idle")
    )
    sink = EventSink(ipc)
    ipc.start()
    try:
        for start in range(0, len(s.events), 10):
            for event in s.events[start : start + 10]:
                assert send_event(event, socket_path=ipc.socket_path, timeout=2.0)
            # Let the companion drain, or the socket backlog fills up.
            sent = min(start + 10, len(s.events))

            def caught_up(n: int = sent) -> bool:
                return len(sink.events) >= n

            s.qtbot.waitUntil(caught_up, timeout=WAIT_MS)
        s.qtbot.waitUntil(lambda: len(sink.events) == len(s.events), timeout=WAIT_MS)
    finally:
        ipc.stop()
        sock_dir.joinpath("s").unlink(missing_ok=True)
        sock_dir.rmdir()
    s.local_state = s.tmp_path / "companion-state.json"
    controller = Controller(store=FileStateStore(s.local_state))
    for event in sink.events:
        controller.on_event(event)
    controller.set_stack_tags(("python", "docker"))
    controller.set_enabled(True)
    assert s.local_state.is_file()


def step_chat(s: Scenario) -> None:
    s.a.send_message(s.plan.chat_a)
    s.qtbot.waitUntil(lambda: len(s.b.chat_state.messages) == 1, timeout=WAIT_MS)
    s.b.send_message(s.plan.chat_b)
    s.qtbot.waitUntil(lambda: len(s.a.chat_state.messages) == 2, timeout=WAIT_MS)
    s.a.request_extension()
    s.qtbot.waitUntil(
        lambda: (
            s.b.chat_state.status is not None
            and s.b.chat_state.status.extension == "requested_by_peer"
        ),
        timeout=WAIT_MS,
    )
    s.b.answer_extension(False)
    s.qtbot.waitUntil(
        lambda: (
            s.a.chat_state.status is not None
            and s.a.chat_state.status.extension == "none"
        ),
        timeout=WAIT_MS,
    )


def step_report(s: Scenario) -> None:
    s.a.report_peer("spam", s.plan.note, (2,))
    s.qtbot.waitUntil(
        lambda: s.rec_a.phase == "ended" and s.rec_b.phase == "ended",
        timeout=WAIT_MS,
    )


def collect_exports(
    server: LiveServer,
    monkeypatch: pytest.MonkeyPatch,
    out_dir: Path,
    emails: list[str],
) -> list[bytes]:
    """Account export (M6-04) both ways: `export_account` and the admin CLI."""
    monkeypatch.setenv("BREAKROOM_DATABASE_URL", server.config.database_url)
    monkeypatch.setenv(
        "BREAKROOM_SERVER_SECRET", base64.urlsafe_b64encode(TEST_SECRET).decode()
    )
    blobs: list[bytes] = []
    for n, email in enumerate(emails):
        with server.session_factory() as session:
            data = export_account(session, email)
        blobs.append(json.dumps(data, sort_keys=True).encode("utf-8"))
        target = out_dir / f"export{n}.json"
        assert admin_main(["account", "export", email, "--out", str(target)]) == 0
        blobs.append(target.read_bytes())
    return blobs


def step_export(s: Scenario) -> None:
    mp = pytest.MonkeyPatch()
    try:
        out = s.tmp_path / "exports"
        out.mkdir()
        s.exports = collect_exports(s.server, mp, out, [ADMIN_EMAIL_A, ADMIN_EMAIL_B])
    finally:
        mp.undo()


# The hook point for wave 3: add the uninstall step (M6-02) to this list.
SCENARIO_STEPS: list[Callable[[Scenario], None]] = [
    step_sign_in,
    step_match,
    step_hooks,
    step_chat,
    step_report,
    step_export,
]


def companion_file_blobs(root: Path) -> dict[str, list[bytes]]:
    """Every regular file the companion side wrote under `root`: state,
    credentials fallback, and the SQLite file of the in-process server."""
    out: dict[str, list[bytes]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            out[f"file.{path.relative_to(root)}"] = [path.read_bytes()]
    return out


def log_blobs(
    caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture[str]
) -> dict[str, list[bytes]]:
    out = capfd.readouterr()
    return {
        "server_logs": [
            caplog.text.encode("utf-8"),
            out.out.encode("utf-8"),
            out.err.encode("utf-8"),
        ]
    }


def run_scenario(
    qtbot: QtBot,
    server: LiveServer,
    tmp_path: Path,
    plan: Plan,
    worker_factory: Callable[[int], tuple[RemoteWorker, Recorder]],
    proxy_port: int,
) -> Scenario:
    a, rec_a = worker_factory(proxy_port)
    b, rec_b = worker_factory(proxy_port)
    scenario = Scenario(qtbot, server, tmp_path, plan, a, b, rec_a, rec_b)
    for step in SCENARIO_STEPS:
        step(scenario)
    return scenario


def all_channels(
    s: Scenario,
    capture: Capture,
    caplog: pytest.LogCaptureFixture,
    capfd: pytest.CaptureFixture[str],
) -> dict[str, list[bytes]]:
    return (
        capture.channels()
        | log_blobs(caplog, capfd)
        | dump_database(s.server)
        | {"export": s.exports}
        | companion_file_blobs(s.tmp_path)
    )


def test_corpus_never_leaves_through_any_channel(
    qtbot: QtBot,
    server_factory: Callable[..., LiveServer],
    capture: Capture,
    worker_factory: Callable[[int], tuple[RemoteWorker, Recorder]],
    caplog: pytest.LogCaptureFixture,
    capfd: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    caplog.set_level(logging.DEBUG)
    server = server_factory(match_sweep_interval_s=0.05)
    proxy = RecordingProxy(server.port, capture)
    try:
        scenario = run_scenario(
            qtbot, server, tmp_path, Plan(), worker_factory, proxy.port
        )
    finally:
        proxy.close()
    assert CORPUS.version == 1  # the corpus version this run used

    # Sanity: the scan looked at real traffic, state and exports.
    channels = all_channels(scenario, capture, caplog, capfd)
    assert any(b"/v1/queue" in h for h in channels["http"])
    assert any(b'"type":"auth"' in f for f in channels["ws"])
    assert channels["db.messages"] and channels["db.reports"]
    assert any(CHAT_BODY_A.encode() in blob for blob in channels["export"])
    assert any(EDITED_DEVICE_NAME.encode() in blob for blob in channels["export"])
    assert any(b"python" in blob for blob in channels["file.companion-state.json"])
    assert any(b"brr_" in blob for blob in channels["file.creds0.json"])
    assert len(scenario.events) >= len(CORPUS.values) // 2

    local_only = [e.local_session_key for e in scenario.events] + [
        str(e.event_id) for e in scenario.events
    ]
    assert scan.find_corpus_leaks(channels, CORPUS.values) == []
    assert scan.find_marker_leaks(channels, CORPUS.marker) == []
    protected = [*CANARIES, *local_only]
    if len(LOCAL_HOSTNAME) >= scan.MIN_NEEDLE:
        protected.append(LOCAL_HOSTNAME)
    assert find_leaks(channels, protected) == []


def test_scanner_sees_corpus_values_planted_in_every_new_channel(
    qtbot: QtBot,
    server_factory: Callable[..., LiveServer],
    capture: Capture,
    worker_factory: Callable[[int], tuple[RemoteWorker, Recorder]],
    caplog: pytest.LogCaptureFixture,
    capfd: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """The same run, with a corpus value typed into the chat, the report note
    and the device name: the scan must then find each where it travelled."""
    caplog.set_level(logging.DEBUG)
    chat, note = CORPUS.bases[0], CORPUS.bases[1]
    plan = Plan(
        device_name_b=PLANTED_DEVICE.value,
        chat_a=chat.value,
        note=note.value,
        hooks=False,
    )
    server = server_factory(match_sweep_interval_s=0.05)
    proxy = RecordingProxy(server.port, capture)
    try:
        scenario = run_scenario(
            qtbot, server, tmp_path, plan, worker_factory, proxy.port
        )
    finally:
        proxy.close()
    channels = all_channels(scenario, capture, caplog, capfd)
    planted = [chat, note, PLANTED_DEVICE]
    hits = set(scan.find_corpus_leaks(channels, planted))
    for channel, value in [
        ("http", chat),  # M5 message request
        ("raw", chat),  # the TCP bytes of it
        ("db.messages", chat),
        ("export", chat),  # M6-04 export carries the user's own message
        ("db.reports", note),  # M5 report
        ("export", note),
        ("http", PLANTED_DEVICE),  # M6-06 sign-in with an edited device name
        ("db.devices", PLANTED_DEVICE),
        ("export", PLANTED_DEVICE),
    ]:
        assert f"{channel}: {value.id}" in hits, (channel, value.id)


def test_scanner_sees_a_corpus_value_in_companion_files_and_logs(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    capfd: pytest.CaptureFixture[str],
) -> None:
    value = CORPUS.bases[0]
    # Credentials fallback: the real store writes the planted token.
    FileCredentialStore(tmp_path / "credentials.json", "http://x").save(
        StoredCredentials(
            refresh_token=value.value,
            refresh_expires_at=datetime.now(UTC) + timedelta(days=1),
        )
    )
    # Companion state: the real state file, with a planted tag.
    state = tmp_path / "companion-state.json"
    state.write_text(json.dumps({"version": 1, "note": value.value}))
    # Companion log and console output.
    caplog.set_level(logging.DEBUG)
    logging.getLogger("breakroom_companion.remote").warning("leaked %s", value.value)
    print(value.value)
    channels = companion_file_blobs(tmp_path) | log_blobs(caplog, capfd)
    hits = set(scan.find_corpus_leaks(channels, [value]))
    assert hits == {
        f"file.credentials.json: {value.id}",
        f"file.companion-state.json: {value.id}",
        f"server_logs: {value.id}",
    }


def test_scanner_sees_a_corpus_value_in_an_account_export(
    live_server: LiveServer,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    value = CORPUS.bases[0]
    with live_server.session_factory() as session:
        user = User(
            email_canonical=ADMIN_EMAIL_A,
            nickname="ada_l",
            created_at=datetime.now(UTC),
        )
        session.add(user)
        session.flush()
        session.add(
            Device(user_id=user.id, name=value.value, created_at=user.created_at)
        )
        session.commit()
    blobs = collect_exports(live_server, monkeypatch, tmp_path, [ADMIN_EMAIL_A])
    assert len(blobs) == 2  # in-process export and the admin CLI file
    assert scan.find_corpus_leaks({"export": blobs}, [value]) == [f"export: {value.id}"]
    assert scan.find_corpus_leaks({"export": blobs}, CORPUS.bases[1:]) == []

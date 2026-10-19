"""U10 follow-ups: readiness reason, activity line, quiet probes, big hook stdin."""

import asyncio
import contextlib
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, get_args

import pytest
from mcp import Client
from pytestqt.qtbot import QtBot

from beebreak_agent_bridge.ipc_client import request_leave_queue, request_status
from beebreak_agent_bridge.mcp_server import build_server
from beebreak_companion.app import BreakTracker, initial_status, wire, wire_break
from beebreak_companion.break_state import BreakState
from beebreak_companion.config import TriggerConfig
from beebreak_companion.controller import Controller
from beebreak_companion.ipc_server import IpcServer
from beebreak_companion.remote import ConnectionState
from beebreak_companion.state_store import FileStateStore
from beebreak_companion.window import CompanionWindow
from beebreak_companion.worker import IpcWorker
from beebreak_shared.api import Consent, Me
from beebreak_shared.ipc import ClientFrame, NotReadyReason, StatusReply
from tests.fakes.remote_worker import FakeRemoteWorker
from tests.fixtures.hook_payloads import PAYLOADS

CFG = TriggerConfig(
    activity_threshold_s=60,
    min_tool_events=2,
    cooldown_s=900,
    suggestion_timeout_s=60,
)
LATER = datetime(2030, 1, 1, tzinfo=UTC)
ALL = Consent(
    version=1,
    share_task_category=True,
    share_stack_tags=True,
    share_time_bucket=True,
)
REASONS = ["signed_out", "connecting", "offline", "no_nickname", "no_consent"]
NOW = datetime(2026, 10, 19, 12, 0, 0, tzinfo=UTC)


def _me(consent: Consent | None = ALL, nickname: str | None = "ada_l") -> Me:
    return Me(nickname=nickname, consent=consent, consented_at=LATER)


# --- shared contract (D3) ---


def test_status_reply_readiness_fields_are_optional_and_default_to_none() -> None:
    reply = StatusReply(kind="status", version=4, invitation="idle")
    assert reply.ready is None
    assert reply.not_ready_reason is None


def test_the_reason_vocabulary_is_the_four_values() -> None:
    assert sorted(get_args(NotReadyReason)) == sorted(REASONS)


def test_an_unknown_reason_is_refused() -> None:
    with pytest.raises(ValueError):
        StatusReply.model_validate(
            {
                "kind": "status",
                "version": 4,
                "invitation": "idle",
                "not_ready_reason": "banana",
            }
        )


# --- MCP tool (D3) ---


def _call(reply: StatusReply | None) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        async with Client(build_server(lambda: reply)) as client:
            result = await client.call_tool("beebreak_status", {})
            return result.structured_content or {}

    return asyncio.run(run())


@pytest.mark.parametrize("reason", REASONS)
def test_the_mcp_tool_reports_ready_and_the_reason(reason: str) -> None:
    out = _call(
        StatusReply.model_validate(
            {
                "kind": "status",
                "version": 4,
                "invitation": "idle",
                "ready": False,
                "not_ready_reason": reason,
            }
        )
    )
    assert out["ready"] is False
    assert out["not_ready_reason"] == reason


def test_the_mcp_tool_reports_null_readiness_from_an_older_companion() -> None:
    out = _call(StatusReply(kind="status", version=4, invitation="idle"))
    assert out["ready"] is None
    assert out["not_ready_reason"] is None


def test_the_mcp_tool_reports_null_readiness_when_not_running() -> None:
    out = _call(None)
    assert out["ready"] is None
    assert out["not_ready_reason"] is None


# --- window (D2, D4) ---


@pytest.fixture
def window(qtbot: QtBot) -> CompanionWindow:
    w = CompanionWindow()
    qtbot.addWidget(w)
    w.show()
    return w


def test_each_reason_replaces_the_idle_line_with_its_own(
    window: CompanionWindow,
) -> None:
    window.set_state("idle")
    idle = window.status_label.text()
    seen = set()
    for reason in REASONS:
        window.set_not_ready_reason(reason)  # type: ignore[arg-type]
        text = window.status_label.text()
        assert text != idle
        seen.add(text)
    assert len(seen) == len(REASONS)
    window.set_not_ready_reason(None)
    assert window.status_label.text() == idle


def test_a_pending_offer_wins_over_the_reason(window: CompanionWindow) -> None:
    window.set_state("idle")
    idle = window.status_label.text()
    window.set_not_ready_reason("offline")
    window.set_state("suggested", "10_20m")
    offered = window.status_label.text()
    assert offered != idle
    window.set_not_ready_reason("signed_out")
    assert window.status_label.text() == offered


def test_a_break_in_progress_wins_over_the_reason(window: CompanionWindow) -> None:
    window.set_state("idle")
    idle = window.status_label.text()
    window.set_not_ready_reason("offline")
    not_ready = window.status_label.text()
    window.set_break(BreakState(phase="searching"))
    assert window.status_label.text() == idle
    window.set_break(BreakState(phase="none"))
    assert window.status_label.text() == not_ready


def test_disabled_keeps_its_own_line(window: CompanionWindow) -> None:
    window.set_state("disabled")
    off = window.status_label.text()
    window.set_not_ready_reason("offline")
    assert window.status_label.text() == off


def test_activity_line_shows_in_the_idle_view_and_changes_with_time(
    window: CompanionWindow,
) -> None:
    window.set_state("idle")
    window.set_last_activity(None, NOW)
    assert window.activity_label.isVisibleTo(window)
    none_yet = window.activity_label.text()
    window.set_last_activity(NOW - timedelta(seconds=12), NOW)
    recent = window.activity_label.text()
    window.set_last_activity(NOW - timedelta(seconds=12), NOW + timedelta(minutes=5))
    later = window.activity_label.text()
    assert len({none_yet, recent, later}) == 3


def test_activity_line_also_shows_in_the_not_ready_view(
    window: CompanionWindow,
) -> None:
    window.set_state("idle")
    window.set_not_ready_reason("signed_out")
    window.set_last_activity(None, NOW)
    assert window.activity_label.isVisibleTo(window)


def test_activity_line_is_hidden_when_offered_disabled_or_on_a_break(
    window: CompanionWindow,
) -> None:
    window.set_last_activity(NOW, NOW)
    window.set_state("suggested", "10_20m")
    assert not window.activity_label.isVisibleTo(window)
    window.set_state("disabled")
    assert not window.activity_label.isVisibleTo(window)
    window.set_state("idle")
    assert window.activity_label.isVisibleTo(window)
    window.set_break(BreakState(phase="matched"))
    assert not window.activity_label.isVisibleTo(window)


def test_a_future_timestamp_does_not_show_a_negative_age(
    window: CompanionWindow,
) -> None:
    window.set_state("idle")
    window.set_last_activity(NOW + timedelta(seconds=30), NOW)
    assert "-" not in window.activity_label.text()


# --- app wiring (D2, D3, D4) ---


class Rig:
    def __init__(self, qtbot: QtBot, tmp_path: Path) -> None:
        self.controller = Controller(CFG, store=FileStateStore(tmp_path / "s.json"))
        self.window = CompanionWindow()
        qtbot.addWidget(self.window)
        self.remote = FakeRemoteWorker()
        self.tracker = BreakTracker()
        self.ipc = IpcWorker(Path("/nonexistent"), initial_status(self.controller))

        def publish() -> None:
            self.ipc.set_status(
                initial_status(
                    self.controller,
                    self.tracker.queue_state(),
                    readiness=self.tracker.readiness(),
                )
            )

        wire_break(self.controller, self.window, self.remote, self.tracker, publish)
        self.timer = wire(
            self.controller,
            self.window,
            self.ipc,
            queue=self.tracker.queue_state,
            readiness=self.tracker.readiness,
        )

    @property
    def status(self) -> StatusReply:
        return self.ipc._status


@pytest.fixture
def rig(qtbot: QtBot, tmp_path: Path) -> Rig:
    return Rig(qtbot, tmp_path)


@pytest.mark.parametrize(
    ("state", "me", "reason"),
    [
        ("signed_out", None, "signed_out"),
        ("signing_in", None, "connecting"),
        ("connecting", _me(), "connecting"),
        ("offline", _me(), "offline"),
        ("connected", _me(nickname=None, consent=None), "no_nickname"),
        ("connected", _me(consent=None), "no_consent"),
    ],
)
def test_the_status_carries_the_reason_from_the_app_inputs(
    rig: Rig, state: ConnectionState, me: Me | None, reason: str
) -> None:
    rig.remote.push_me(me)
    rig.remote.push_state(state)
    assert rig.status.ready is False
    assert rig.status.not_ready_reason == reason


def test_signed_out_wins_over_offline_and_the_rest(rig: Rig) -> None:
    rig.remote.push_me(_me(nickname=None, consent=None))
    rig.remote.push_state("signed_out")
    assert rig.status.not_ready_reason == "signed_out"


def test_offline_wins_over_a_missing_nickname(rig: Rig) -> None:
    rig.remote.push_me(_me(nickname=None, consent=None))
    rig.remote.push_state("offline")
    assert rig.status.not_ready_reason == "offline"


def test_ready_clears_the_reason_and_the_window_line(rig: Rig) -> None:
    rig.remote.push_state("offline")
    not_ready_line = rig.window.status_label.text()
    rig.remote.push_me(_me())
    rig.remote.push_state("connected")
    assert rig.status.ready is True
    assert rig.status.not_ready_reason is None
    assert rig.window.status_label.text() != not_ready_line


def test_the_window_shows_a_reason_once_the_app_is_not_ready(
    rig: Rig, qtbot: QtBot
) -> None:
    rig.remote.push_state("offline")
    plain = CompanionWindow()
    qtbot.addWidget(plain)
    assert rig.window.status_label.text() != plain.status_label.text()


def test_without_a_remote_worker_the_reason_is_signed_out(
    qtbot: QtBot, tmp_path: Path
) -> None:
    controller = Controller(CFG, store=FileStateStore(tmp_path / "s.json"))
    window = CompanionWindow()
    qtbot.addWidget(window)
    tracker = BreakTracker()
    wire_break(controller, window, None, tracker, lambda: None)
    assert tracker.readiness() == (False, "signed_out")


def test_readiness_is_unknown_until_wired() -> None:
    assert BreakTracker().readiness() == (None, None)


def test_the_activity_line_follows_the_controller_on_the_existing_timer(
    rig: Rig,
) -> None:
    rig.window.show()
    rig.timer.timeout.emit()
    none_yet = rig.window.activity_label.text()
    rig.controller.last_event_at = datetime.now(UTC) - timedelta(seconds=90)
    rig.timer.timeout.emit()
    assert rig.window.activity_label.text() != none_yet


# --- quiet zero-byte connects (fix 4) ---


@pytest.fixture
def sock_path() -> Iterator[Path]:
    d = tempfile.mkdtemp(dir="/tmp")
    yield Path(d) / "c.sock"
    shutil.rmtree(d, ignore_errors=True)


class _Collect(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.count = 0

    def emit(self, record: logging.LogRecord) -> None:
        self.count += 1


def _log_records(sock_path: Path, data: bytes | None) -> int:
    """Log records the server emits for one connection sending `data`."""
    handler = _Collect()
    log = logging.getLogger("beebreak.ipc")
    log.addHandler(handler)
    old_level = log.level
    log.setLevel(logging.DEBUG)

    def client() -> None:
        with socket.socket(socket.AF_UNIX) as s:
            s.settimeout(2)
            s.connect(str(sock_path))
            if data:
                s.sendall(data)
            s.shutdown(socket.SHUT_WR)
            with contextlib.suppress(OSError):
                s.recv(1)

    async def scenario() -> None:
        server = IpcServer(
            sock_path,
            on_event=lambda _e: None,
            status=lambda: StatusReply(kind="status", version=4, invitation="idle"),
        )
        await server.start()
        try:
            await asyncio.to_thread(client)
            await asyncio.sleep(0.1)
        finally:
            await server.close()

    try:
        asyncio.run(scenario())
    finally:
        log.removeHandler(handler)
        log.setLevel(old_level)
    return handler.count


def test_a_connect_and_close_logs_nothing(sock_path: Path) -> None:
    assert _log_records(sock_path, None) == 0


def test_a_partial_frame_without_a_newline_is_still_logged(sock_path: Path) -> None:
    assert _log_records(sock_path, b'{"kind":"status_re') >= 1


# --- big hook stdin (fix 5, D1) ---


def test_hook_stdin_between_one_and_sixteen_mib_still_yields_an_event() -> None:
    runtime = tempfile.mkdtemp(dir="/tmp")
    try:
        rdir = Path(runtime) / "beebreak"
        rdir.mkdir(mode=0o700)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(rdir / "companion.sock"))
        server.listen(1)
        server.settimeout(15)
        received: list[bytes] = []

        def accept() -> None:
            conn, _ = server.accept()
            with conn:
                conn.settimeout(10)
                received.append(conn.makefile("rb").readline())

        thread = threading.Thread(target=accept)
        thread.start()
        payload = PAYLOADS["UserPromptSubmit"] | {"prompt": "x" * (3 * 1024 * 1024)}
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "beebreak_agent_bridge.hook"],
                input=json.dumps(payload).encode(),
                capture_output=True,
                env={**os.environ, "XDG_RUNTIME_DIR": runtime},
                timeout=20,
                check=False,
            )
            thread.join(15)
        finally:
            server.close()
        assert proc.returncode == 0
        assert received
        event = json.loads(received[0])["event"]
        assert event["type"] == "turn_submitted"
    finally:
        shutil.rmtree(runtime, ignore_errors=True)


# --- old bridge, new companion: unknown reply keys are ignored (M1) ---


def _serve_once(path: Path, payload: bytes) -> threading.Thread:
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(path))
    listener.listen(1)

    def answer() -> None:
        with listener:
            conn, _ = listener.accept()
            with conn:
                conn.recv(1024)
                conn.sendall(payload)

    t = threading.Thread(target=answer)
    t.start()
    return t


@pytest.mark.parametrize("version", [2, 3, 4])
def test_a_status_reply_with_an_unknown_key_is_still_understood(
    sock_path: Path, version: int
) -> None:
    body = {"kind": "status", "version": version, "invitation": "idle", "future": 1}
    t = _serve_once(sock_path, json.dumps(body).encode() + b"\n")
    reply = request_status(socket_path=sock_path, timeout=2)
    t.join(5)
    assert reply is not None
    assert reply.invitation == "idle"


def test_a_leave_reply_with_an_unknown_key_is_still_understood(
    sock_path: Path,
) -> None:
    body = {"kind": "leave_queue_result", "result": "cancelled", "future": 1}
    t = _serve_once(sock_path, json.dumps(body).encode() + b"\n")
    result = request_leave_queue(socket_path=sock_path, timeout=2)
    t.join(5)
    assert result == "cancelled"


def test_the_mcp_result_has_only_the_known_keys_even_with_an_unknown_one() -> None:
    reply = StatusReply.model_validate_json(
        '{"kind":"status","version":4,"invitation":"idle","secret":"x"}'
    )
    out = _call(reply)
    assert "secret" not in out
    assert set(out) == {
        "companion",
        "invitation",
        "last_event_at",
        "activity",
        "estimate",
        "queue",
        "ready",
        "not_ready_reason",
    }


def test_what_the_companion_parses_from_clients_still_refuses_unknown_keys() -> None:
    with pytest.raises(ValueError):
        ClientFrame.model_validate_json('{"kind":"status_request","extra":1}')


def test_without_a_remote_worker_the_status_is_published_at_once(
    qtbot: QtBot, tmp_path: Path
) -> None:
    controller = Controller(CFG, store=FileStateStore(tmp_path / "s.json"))
    window = CompanionWindow()
    qtbot.addWidget(window)
    published: list[int] = []
    wire_break(controller, window, None, BreakTracker(), lambda: published.append(1))
    assert published

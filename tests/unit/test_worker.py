import shutil
import tempfile
import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pytestqt.qtbot import QtBot

from breakroom_agent_bridge.ipc_client import request_status, send_event
from breakroom_companion.worker import IpcWorker
from breakroom_shared.events import NormalizedEvent
from breakroom_shared.ipc import StatusReply


def make_event() -> NormalizedEvent:
    return NormalizedEvent(
        event_version=1,
        event_id=uuid.uuid4(),
        local_session_key="s1",
        agent="claude_code",
        type="turn_stopped",
        observed_at=datetime.now(UTC),
    )


def status(invitation: str) -> StatusReply:
    return StatusReply.model_validate(
        {"kind": "status", "version": 3, "invitation": invitation}
    )


@pytest.fixture
def sock_path() -> Iterator[Path]:
    d = tempfile.mkdtemp(dir="/tmp")
    yield Path(d) / "c.sock"
    shutil.rmtree(d, ignore_errors=True)


def test_event_arrives_via_signal_on_main_thread(qtbot: QtBot, sock_path: Path) -> None:
    worker = IpcWorker(sock_path, status("idle"))
    slot_threads: list[int] = []
    worker.event_received.connect(lambda _e: slot_threads.append(threading.get_ident()))
    worker.start()
    try:
        event = make_event()
        with qtbot.waitSignal(worker.event_received, timeout=3000) as blocker:
            assert send_event(event, socket_path=sock_path, timeout=1.0)
        assert blocker.args == [event]
        assert slot_threads == [threading.get_ident()]
    finally:
        worker.stop()


def test_set_status_changes_reply(sock_path: Path) -> None:
    worker = IpcWorker(sock_path, status("idle"))
    worker.start()
    try:
        first = request_status(socket_path=sock_path, timeout=1.0)
        assert first is not None and first.invitation == "idle"
        worker.set_status(status("suggested"))
        second = request_status(socket_path=sock_path, timeout=1.0)
        assert second is not None and second.invitation == "suggested"
    finally:
        worker.stop()


def test_stop_removes_socket(sock_path: Path) -> None:
    worker = IpcWorker(sock_path, status("idle"))
    worker.start()
    assert sock_path.exists()
    worker.stop()
    assert not sock_path.exists()
    worker.stop()  # idempotent


def test_second_worker_on_same_path_raises(sock_path: Path) -> None:
    first = IpcWorker(sock_path, status("idle"))
    first.start()
    try:
        second = IpcWorker(sock_path, status("idle"))
        with pytest.raises(RuntimeError):
            second.start()
        second.stop()
        assert request_status(socket_path=sock_path, timeout=1.0) is not None
    finally:
        first.stop()

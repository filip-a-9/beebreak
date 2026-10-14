import shutil
import tempfile
import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pytestqt.qtbot import QtBot

from breakroom_agent_bridge.ipc_client import (
    request_leave_queue,
    request_status,
    send_event,
)
from breakroom_companion.worker import IpcWorker
from breakroom_shared.events import NormalizedEvent
from breakroom_shared.ipc import LeaveResult, StatusReply


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
        {"kind": "status", "version": 4, "invitation": invitation}
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


# --- M4-07: leave_queue through IpcWorker ----------------------------------------


def test_leave_queue_callback_is_called_off_the_main_thread(sock_path: Path) -> None:
    threads: list[int] = []

    def leave() -> LeaveResult:
        threads.append(threading.get_ident())
        return "cancelled"

    worker = IpcWorker(sock_path, status("idle"), leave)
    worker.start()
    try:
        assert request_leave_queue(socket_path=sock_path, timeout=1.0) == "cancelled"
    finally:
        worker.stop()
    assert len(threads) == 1 and threads[0] != threading.get_ident()


@pytest.mark.parametrize("outcome", ["not_searching", "unavailable"])
def test_leave_queue_outcomes_pass_through_the_worker(
    sock_path: Path, outcome: LeaveResult
) -> None:
    worker = IpcWorker(sock_path, status("idle"), lambda: outcome)
    worker.start()
    try:
        assert request_leave_queue(socket_path=sock_path, timeout=1.0) == outcome
    finally:
        worker.stop()


def test_worker_without_a_leave_callback_answers_unavailable(sock_path: Path) -> None:
    worker = IpcWorker(sock_path, status("idle"))
    worker.start()
    try:
        assert request_leave_queue(socket_path=sock_path, timeout=1.0) == "unavailable"
    finally:
        worker.stop()


def test_hanging_leave_callback_neither_blocks_status_nor_stop(
    sock_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BREAKROOM_IPC_READ_TIMEOUT_S", "0.3")
    release = threading.Event()

    def hang() -> LeaveResult:
        release.wait(10)
        return "cancelled"

    worker = IpcWorker(sock_path, status("idle"), hang)
    worker.start()
    try:
        assert request_leave_queue(socket_path=sock_path, timeout=1.0) == "unavailable"
        assert request_status(socket_path=sock_path, timeout=1.0) is not None
    finally:
        release.set()
        worker.stop()
    assert not sock_path.exists()


def test_status_carries_the_queue_state(sock_path: Path) -> None:
    worker = IpcWorker(sock_path, status("idle"))
    worker.start()
    try:
        first = request_status(socket_path=sock_path, timeout=1.0)
        assert isinstance(first, StatusReply) and first.queue == "none"
        worker.set_status(
            StatusReply(kind="status", version=4, invitation="idle", queue="searching")
        )
        second = request_status(socket_path=sock_path, timeout=1.0)
        assert isinstance(second, StatusReply) and second.queue == "searching"
    finally:
        worker.stop()

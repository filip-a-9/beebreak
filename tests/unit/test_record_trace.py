import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest

from beebreak_shared.events import NormalizedEvent
from beebreak_shared.ipc import EventFrame, StatusReply, StatusRequest

TOOL = Path(__file__).parents[2] / "tools" / "record_trace.py"


@pytest.fixture
def runtime() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(dir="/tmp") as d:
        yield Path(d)


def _env(runtime: Path) -> dict[str, str]:
    return {**os.environ, "XDG_RUNTIME_DIR": str(runtime)}


def _sock(runtime: Path) -> Path:
    return runtime / "beebreak" / "companion.sock"


def _start(runtime: Path, out: Path) -> subprocess.Popen[bytes]:
    proc = subprocess.Popen(
        [sys.executable, str(TOOL), str(out)],
        env=_env(runtime),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 10
    while not _sock(runtime).exists():
        if proc.poll() is not None or time.monotonic() > deadline:
            proc.kill()
            raise AssertionError("recorder did not start")
        time.sleep(0.05)
    return proc


def _send(runtime: Path, line: bytes, *, expect_reply: bool = False) -> bytes:
    with socket.socket(socket.AF_UNIX) as s:
        s.connect(str(_sock(runtime)))
        s.sendall(line)
        s.shutdown(socket.SHUT_WR)
        if not expect_reply:
            return b""
        return s.makefile("rb").readline()


def _event() -> NormalizedEvent:
    return NormalizedEvent.model_validate(
        {
            "event_version": 1,
            "event_id": uuid4(),
            "local_session_key": "s1",
            "agent": "claude_code",
            "type": "tool_completed",
            "observed_at": "2026-10-12T12:00:00Z",
            "tool_class": "shell",
        }
    )


def test_records_events_and_answers_status(runtime: Path) -> None:
    out = runtime / "trace.jsonl"
    proc = _start(runtime, out)
    try:
        events = [_event(), _event()]
        for e in events:
            _send(
                runtime,
                EventFrame(kind="event", event=e).model_dump_json().encode() + b"\n",
            )
        _send(runtime, b"not json\n")  # garbage must not crash or be recorded
        reply = _send(
            runtime,
            StatusRequest(kind="status_request").model_dump_json().encode() + b"\n",
            expect_reply=True,
        )
        assert StatusReply.model_validate_json(reply).invitation == "idle"
        time.sleep(0.2)
    finally:
        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=10)
    lines = out.read_text().splitlines()
    parsed = [NormalizedEvent.model_validate_json(line) for line in lines]
    assert parsed == events
    assert not _sock(runtime).exists()


def test_refuses_when_companion_already_running(runtime: Path) -> None:
    first = _start(runtime, runtime / "a.jsonl")
    try:
        second = subprocess.run(
            [sys.executable, str(TOOL), str(runtime / "b.jsonl")],
            env=_env(runtime),
            capture_output=True,
            timeout=10,
        )
        assert second.returncode != 0
        assert _sock(runtime).exists()  # the first one's socket is untouched
    finally:
        first.send_signal(signal.SIGINT)
        first.wait(timeout=10)

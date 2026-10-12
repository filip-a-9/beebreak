"""The real companion process: start-up failures and signal shutdown."""

import os
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from breakroom_agent_bridge.ipc_client import request_status

START_WAIT_S = 10.0


@pytest.fixture
def runtime() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(dir="/tmp") as d:
        yield Path(d)


def _spawn(runtime: Path) -> subprocess.Popen[bytes]:
    env = {**os.environ, "XDG_RUNTIME_DIR": str(runtime)}
    return subprocess.Popen(
        [sys.executable, "-m", "breakroom_companion.app"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _wait_running(sock: Path) -> None:
    deadline = time.monotonic() + START_WAIT_S
    while time.monotonic() < deadline:
        if request_status(socket_path=sock) is not None:
            return
        time.sleep(0.05)
    pytest.fail("companion did not start")


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_signal_quits_cleanly_and_removes_socket(
    runtime: Path, signum: signal.Signals
) -> None:
    proc = _spawn(runtime)
    sock = runtime / "breakroom" / "companion.sock"
    try:
        _wait_running(sock)
        proc.send_signal(signum)
        assert proc.wait(timeout=5) == 0
    finally:
        proc.kill()
        proc.wait()
    assert not sock.exists()


def test_second_instance_exits_already_running(runtime: Path) -> None:
    first = _spawn(runtime)
    try:
        _wait_running(runtime / "breakroom" / "companion.sock")
        second = _spawn(runtime)
        assert second.wait(timeout=START_WAIT_S) == 2
    finally:
        first.kill()
        first.wait()


def test_unusable_socket_path_is_not_reported_as_already_running(
    runtime: Path,
) -> None:
    # A directory where the socket should be: binding fails for a reason other
    # than a live companion, so the exit code must differ from "already running".
    (runtime / "breakroom").mkdir(mode=0o700)
    (runtime / "breakroom" / "companion.sock").mkdir()
    proc = _spawn(runtime)
    code = proc.wait(timeout=START_WAIT_S)
    assert code not in (0, 2)

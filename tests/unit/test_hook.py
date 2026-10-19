import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.fixtures.hook_payloads import PAYLOADS

# Generous bound on interpreter start-up plus the 0.2 s send timeout.
MAX_SECONDS = 3.0


def _run(
    stdin: bytes, runtime: str, **env: str
) -> tuple[subprocess.CompletedProcess[bytes], float]:
    full_env = {**os.environ, "XDG_RUNTIME_DIR": runtime, **env}
    start = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-m", "beebreak_agent_bridge.hook"],
        input=stdin,
        capture_output=True,
        env=full_env,
        timeout=10,
        check=False,
    )
    return proc, time.monotonic() - start


@pytest.fixture
def runtime() -> Iterator[str]:
    with tempfile.TemporaryDirectory(dir="/tmp") as path:
        yield path


def _assert_silent_success(proc: subprocess.CompletedProcess[bytes]) -> None:
    assert proc.returncode == 0
    assert proc.stdout == b""
    assert proc.stderr == b""


@pytest.mark.parametrize("name", PAYLOADS)
def test_valid_payload_exits_silently_without_companion(
    name: str, runtime: str
) -> None:
    proc, elapsed = _run(json.dumps(PAYLOADS[name]).encode(), runtime)
    _assert_silent_success(proc)
    assert elapsed < MAX_SECONDS


@pytest.mark.parametrize(
    "stdin",
    [b"not json at all", b"\xff\xfe\x00", b"", b"[1,2,3]", b"null", b'"str"'],
)
def test_garbage_stdin_exits_silently(stdin: bytes, runtime: str) -> None:
    proc, elapsed = _run(stdin, runtime)
    _assert_silent_success(proc)
    assert elapsed < MAX_SECONDS


def test_oversized_stdin_exits_silently_and_quickly(runtime: str) -> None:
    payload = PAYLOADS["UserPromptSubmit"] | {"prompt": "x" * (16 * 1024 * 1024 + 1)}
    proc, elapsed = _run(json.dumps(payload).encode(), runtime)
    _assert_silent_success(proc)
    assert elapsed < MAX_SECONDS


def test_stdin_cap_comes_from_environment(runtime: str) -> None:
    # Cap below the payload size: the hook must give up before touching the socket.
    proc, _ = _run(
        json.dumps(PAYLOADS["Stop"]).encode(),
        runtime,
        BEEBREAK_HOOK_MAX_STDIN_BYTES="10",
    )
    _assert_silent_success(proc)
    assert not (Path(runtime) / "beebreak" / "salt").exists()


def test_valid_payload_creates_salt_with_private_runtime_dir(runtime: str) -> None:
    proc, _ = _run(json.dumps(PAYLOADS["Stop"]).encode(), runtime)
    _assert_silent_success(proc)
    assert (Path(runtime) / "beebreak" / "salt").stat().st_size == 32


def test_empty_salt_file_is_replaced_and_event_still_delivered(runtime: str) -> None:
    import socket
    import threading

    rdir = Path(runtime) / "beebreak"
    rdir.mkdir(mode=0o700)
    (rdir / "salt").write_bytes(b"")  # left by a hook killed mid-create
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(rdir / "companion.sock"))
    server.listen(1)
    server.settimeout(5)
    received: list[bytes] = []

    def accept() -> None:
        conn, _ = server.accept()
        with conn:
            conn.settimeout(5)
            received.append(conn.makefile("rb").readline())

    thread = threading.Thread(target=accept)
    thread.start()
    try:
        proc, _ = _run(json.dumps(PAYLOADS["Stop"]).encode(), runtime)
        thread.join(10)
    finally:
        server.close()
    _assert_silent_success(proc)
    assert (rdir / "salt").stat().st_size == 32
    assert received and received[0].endswith(b"\n")


def test_unsafe_runtime_dir_leaves_hook_silent(runtime: str) -> None:
    sub = Path(runtime) / "beebreak"
    sub.mkdir(mode=0o700)
    sub.chmod(0o770)
    proc, _ = _run(json.dumps(PAYLOADS["Stop"]).encode(), runtime)
    _assert_silent_success(proc)

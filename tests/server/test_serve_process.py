"""The real `breakroom-server serve` path in a subprocess (rule 10, proxy headers).

TestClient skips uvicorn's own logging and proxy handling, so these start the
CLI the way docker compose does.
"""

import base64
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest
from websockets.sync.client import connect

from tests.server.conftest import TEST_SECRET

DEADLINE_S = 15.0
POLL_S = 0.05


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


def _request(
    url: str,
    body: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
) -> int:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            status: int = response.status
            return status
    except urllib.error.HTTPError as error:
        return error.code


class Served:
    def __init__(self, proc: subprocess.Popen[str], port: int) -> None:
        self.proc = proc
        self.http = f"http://127.0.0.1:{port}"
        self.ws = f"ws://127.0.0.1:{port}"
        self._stderr: str | None = None

    def stop(self) -> str:
        """Terminate the server and return everything it wrote to stderr."""
        if self._stderr is not None:
            return self._stderr
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
        try:
            _, stderr = self.proc.communicate(timeout=DEADLINE_S)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            _, stderr = self.proc.communicate()
        self._stderr = stderr
        return stderr


@pytest.fixture
def served(tmp_path: Path) -> Iterator[Served]:
    port = _free_port()
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("BREAKROOM_")},
        "BREAKROOM_DATABASE_URL": f"sqlite:///{tmp_path / 'serve.db'}",
        "BREAKROOM_SERVER_SECRET": base64.urlsafe_b64encode(TEST_SECRET).decode(),
        "BREAKROOM_MAILER": "record",
    }
    cli = [sys.executable, "-m", "breakroom_server.cli"]
    subprocess.run([*cli, "migrate"], env=env, check=True, timeout=60)
    server = Served(
        subprocess.Popen(
            [*cli, "serve", "--port", str(port)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ),
        port,
    )
    try:
        deadline = time.monotonic() + DEADLINE_S
        while True:
            assert server.proc.poll() is None, "server exited early"
            try:
                if _request(f"{server.http}/v1/health") == 200:
                    break
            except OSError:
                pass
            assert time.monotonic() < deadline, "server did not start"
            time.sleep(POLL_S)
        yield server
    finally:
        server.stop()


def test_serve_logs_no_address_query_or_token(served: Served) -> None:
    with connect(f"{served.ws}/v1/realtime?access_token=bra_SECRETx"):
        pass  # accepted, then closed without an auth frame
    assert _request(f"{served.http}/v1/me") == 401
    stderr = served.stop()
    assert "GET /v1/me 401" in stderr  # the app's own access line is visible
    assert "127.0.0.1" not in stderr
    assert "bra_" not in stderr and "SECRETx" not in stderr
    assert "access_token" not in stderr


def test_serve_ignores_x_forwarded_for_for_rate_limits(served: Served) -> None:
    # One real client address: a forged header must not buy a fresh limiter key.
    codes = [
        _request(
            f"{served.http}/v1/auth/start",
            {"email": f"user{i}@example.com"},
            {"X-Forwarded-For": f"10.0.{i}.1"},
        )
        for i in range(21)
    ]
    assert codes == [202] * 20 + [429]

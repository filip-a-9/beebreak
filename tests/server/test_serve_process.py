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


def _env(tmp_path: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    return {
        **{k: v for k, v in os.environ.items() if not k.startswith("BREAKROOM_")},
        "BREAKROOM_DATABASE_URL": f"sqlite:///{tmp_path / 'serve.db'}",
        "BREAKROOM_SERVER_SECRET": base64.urlsafe_b64encode(TEST_SECRET).decode(),
        "BREAKROOM_MAILER": "record",
        **(extra or {}),
    }


CLI = [sys.executable, "-m", "breakroom_server.cli"]


def _start(env: dict[str, str]) -> Iterator[Served]:
    port = _free_port()
    subprocess.run([*CLI, "migrate"], env=env, check=True, timeout=60)
    server = Served(
        subprocess.Popen(
            [*CLI, "serve", "--port", str(port)],
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


@pytest.fixture
def served(tmp_path: Path) -> Iterator[Served]:
    yield from _start(_env(tmp_path))


@pytest.fixture
def served_behind_trusted_proxy(tmp_path: Path) -> Iterator[Served]:
    # The test client connects from 127.0.0.1, so it plays the proxy.
    yield from _start(_env(tmp_path, {"BREAKROOM_TRUSTED_PROXIES": "127.0.0.1/32"}))


@pytest.fixture
def served_other_proxy_trusted(tmp_path: Path) -> Iterator[Served]:
    # A proxy list that does not include the connecting peer.
    yield from _start(_env(tmp_path, {"BREAKROOM_TRUSTED_PROXIES": "10.99.0.0/16"}))


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


def _forged_start_codes(served: Served) -> list[int]:
    return [
        _request(
            f"{served.http}/v1/auth/start",
            {"email": f"user{i}@example.com"},
            {"X-Forwarded-For": f"203.0.{i}.1"},
        )
        for i in range(21)
    ]


def test_forwarded_for_from_a_trusted_peer_sets_the_limiter_key(
    served_behind_trusted_proxy: Served,
) -> None:
    # 21 different forwarded clients: none is over its own limit.
    assert _forged_start_codes(served_behind_trusted_proxy) == [202] * 21


def test_forwarded_for_from_a_trusted_peer_still_limits_that_client(
    served_behind_trusted_proxy: Served,
) -> None:
    codes = [
        _request(
            f"{served_behind_trusted_proxy.http}/v1/auth/start",
            {"email": f"user{i}@example.com"},
            {"X-Forwarded-For": "203.0.113.9"},
        )
        for i in range(21)
    ]
    assert codes == [202] * 20 + [429]


def test_forwarded_for_from_an_untrusted_peer_is_ignored(
    served_other_proxy_trusted: Served,
) -> None:
    codes = _forged_start_codes(served_other_proxy_trusted)
    assert codes == [202] * 20 + [429]


@pytest.mark.parametrize("workers", ["0", "2", "-1"])
def test_serve_rejects_any_worker_count_but_one(tmp_path: Path, workers: str) -> None:
    result = subprocess.run(
        [*CLI, "serve", "--port", str(_free_port()), "--workers", workers],
        env=_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2


def test_serve_rejects_web_concurrency_above_one(tmp_path: Path) -> None:
    result = subprocess.run(
        [*CLI, "serve", "--port", str(_free_port())],
        env=_env(tmp_path, {"WEB_CONCURRENCY": "3"}),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2


@pytest.mark.parametrize("command", ["serve", "migrate"])
def test_example_database_password_stops_the_command(
    tmp_path: Path, command: str
) -> None:
    env = _env(
        tmp_path,
        {"BREAKROOM_DATABASE_URL": "postgresql+psycopg://u:change-me@127.0.0.1:1/db"},
    )
    result = subprocess.run(
        [*CLI, command],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2
    assert "change-me" not in result.stderr + result.stdout

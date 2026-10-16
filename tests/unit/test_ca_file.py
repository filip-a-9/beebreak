"""BREAKROOM_CA_FILE: the companion trusts a private CA, for httpx and websockets.

Real TLS servers with a self-signed certificate (made with the openssl CLI);
the clients under test are the real ApiClient and RealtimeClient.
"""

import asyncio
import json
import os
import ssl
import subprocess
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from uuid import uuid4

import pytest
from websockets.asyncio.server import ServerConnection, serve

from breakroom_companion.api_client import ApiClient, ApiError
from breakroom_companion.realtime_client import RealtimeClient, RealtimeError
from breakroom_companion.remote_config import RemoteConfig

TIMEOUT_S = 3.0


def _make_cert(directory: Path) -> tuple[Path, Path]:
    cert, key = directory / "cert.pem", directory / "key.pem"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(key), "-out", str(cert), "-days", "2",
            "-subj", "/CN=localhost",
            "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
    )  # fmt: skip
    return cert, key


def _server_context(cert: Path, key: Path) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    return ctx


@pytest.fixture(scope="module")
def cert_pair(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    return _make_cert(tmp_path_factory.mktemp("ca"))


def _config(ca: Path | None) -> RemoteConfig:
    return RemoteConfig(server_url="https://127.0.0.1:1", ca_file=ca)


# --- config -------------------------------------------------------------------


def test_ca_file_defaults_to_none() -> None:
    config = RemoteConfig.from_env()
    assert config.ca_file is None and config.ssl_context() is None


def test_ca_file_from_env_builds_a_verifying_context(
    monkeypatch: pytest.MonkeyPatch, cert_pair: tuple[Path, Path]
) -> None:
    ca = cert_pair[0]
    ca.chmod(0o644)
    monkeypatch.setenv("BREAKROOM_CA_FILE", str(ca))
    config = RemoteConfig.from_env()
    assert config.ca_file == ca
    ctx = config.ssl_context()
    assert ctx is not None
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname


@pytest.mark.parametrize("mode", [0o666, 0o664, 0o646])
def test_ca_file_writable_by_others_is_refused(
    monkeypatch: pytest.MonkeyPatch, cert_pair: tuple[Path, Path], mode: int
) -> None:
    ca = cert_pair[0]
    ca.chmod(mode)
    monkeypatch.setenv("BREAKROOM_CA_FILE", str(ca))
    try:
        with pytest.raises(ValueError):
            RemoteConfig.from_env()
    finally:
        ca.chmod(0o644)


def test_ca_file_missing_directory_or_garbage_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    garbage = tmp_path / "garbage.pem"
    garbage.write_text("not a certificate\n")
    garbage.chmod(0o600)
    for bad in (tmp_path / "absent.pem", tmp_path, garbage):
        monkeypatch.setenv("BREAKROOM_CA_FILE", str(bad))
        with pytest.raises(ValueError) as info:
            RemoteConfig.from_env()
        assert str(bad) not in str(info.value)


def test_ca_file_symlink_is_followed_but_must_be_a_regular_file(
    monkeypatch: pytest.MonkeyPatch, cert_pair: tuple[Path, Path], tmp_path: Path
) -> None:
    cert_pair[0].chmod(0o644)
    good = tmp_path / "link-to-cert"
    good.symlink_to(cert_pair[0])
    monkeypatch.setenv("BREAKROOM_CA_FILE", str(good))
    assert RemoteConfig.from_env().ssl_context() is not None

    bad = tmp_path / "link-to-dir"
    bad.symlink_to(tmp_path)
    monkeypatch.setenv("BREAKROOM_CA_FILE", str(bad))
    with pytest.raises(ValueError):
        RemoteConfig.from_env()


def test_ca_file_fifo_is_refused_without_blocking(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    monkeypatch.setenv("BREAKROOM_CA_FILE", str(fifo))
    with pytest.raises(ValueError):
        RemoteConfig.from_env()


def test_ca_file_owned_by_another_user_is_refused(
    monkeypatch: pytest.MonkeyPatch, cert_pair: tuple[Path, Path]
) -> None:
    # Cannot chown without root, so the real file (ours) is judged by a pretend
    # current user: the file then belongs to someone else, and is not root's.
    cert_pair[0].chmod(0o644)
    monkeypatch.setattr(os, "getuid", lambda: os.stat(cert_pair[0]).st_uid + 1)
    monkeypatch.setenv("BREAKROOM_CA_FILE", str(cert_pair[0]))
    with pytest.raises(ValueError):
        RemoteConfig.from_env()


def test_ca_file_owned_by_root_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    system_bundle = Path("/etc/ssl/certs/ca-certificates.crt")
    if not system_bundle.is_file() or system_bundle.stat().st_uid != 0:
        pytest.skip("no root-owned PEM bundle on this machine")
    monkeypatch.setenv("BREAKROOM_CA_FILE", str(system_bundle))
    assert RemoteConfig.from_env().ssl_context() is not None


def test_ca_is_loaded_once_at_startup(
    https_origin: str, cert_pair: tuple[Path, Path], tmp_path: Path
) -> None:
    ca = tmp_path / "ca.pem"
    ca.write_bytes(cert_pair[0].read_bytes())
    ca.chmod(0o600)
    config = _config(ca)
    other, _ = _make_cert(tmp_path)
    ca.write_bytes(other.read_bytes())  # swapped after startup
    ctx = config.ssl_context()

    async def scenario() -> str:
        api = ApiClient(https_origin, TIMEOUT_S, ssl_context=ctx)
        try:
            await api.me("bra_x")
        except ApiError as error:
            return error.code
        finally:
            await api.aclose()
        raise AssertionError("expected an error")

    assert asyncio.run(scenario()) == "unauthorized"
    assert config.ssl_context() is ctx


# --- httpx ----------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = json.dumps({"error": "unauthorized"}).encode()
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def https_origin(cert_pair: tuple[Path, Path]) -> Iterator[str]:
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    server.socket = _server_context(*cert_pair).wrap_socket(
        server.socket, server_side=True
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"https://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def _api_code(origin: str, ca: Path | None) -> str:
    async def scenario() -> str:
        ctx = _config(ca).ssl_context() if ca else None
        api = ApiClient(origin, TIMEOUT_S, ssl_context=ctx)
        try:
            await api.me("bra_x")
        except ApiError as error:
            return error.code
        finally:
            await api.aclose()
        raise AssertionError("expected an error")

    return asyncio.run(scenario())


def test_https_with_the_private_ca_reaches_the_server(
    https_origin: str, cert_pair: tuple[Path, Path]
) -> None:
    # A TLS-level failure would be "network"; reaching the app gives its code.
    assert _api_code(https_origin, cert_pair[0]) == "unauthorized"


def test_https_without_the_private_ca_fails_verification(https_origin: str) -> None:
    assert _api_code(https_origin, None) == "network"


def test_https_with_a_different_ca_fails_verification(
    https_origin: str, tmp_path: Path
) -> None:
    other, _ = _make_cert(tmp_path)
    assert _api_code(https_origin, other) == "network"


# --- websockets -----------------------------------------------------------------


def _ready() -> str:
    return json.dumps(
        {
            "type": "session.ready",
            "version": 1,
            "event_id": str(uuid4()),
            "server_time": datetime.now(UTC).isoformat(),
            "nickname": None,
        }
    )


def _ws_connect(cert_pair: tuple[Path, Path], ca: Path | None) -> int | str | None:
    async def handler(ws: ServerConnection) -> None:
        await ws.recv()
        await ws.send(_ready())
        await ws.wait_closed()

    async def scenario() -> int | str | None:
        async with serve(
            handler, "127.0.0.1", 0, ssl=_server_context(*cert_pair)
        ) as server:
            port = server.sockets[0].getsockname()[1]
            ctx = _config(ca).ssl_context() if ca else None
            client = RealtimeClient(
                f"https://127.0.0.1:{port}", TIMEOUT_S, 1.0, ssl_context=ctx
            )
            try:
                await client.connect("bra_x")
            except RealtimeError as error:
                return error.code
            finally:
                await client.close()
            return "connected"

    return asyncio.run(scenario())


def test_wss_with_the_private_ca_connects(cert_pair: tuple[Path, Path]) -> None:
    assert _ws_connect(cert_pair, cert_pair[0]) == "connected"


def test_wss_without_the_private_ca_fails_verification(
    cert_pair: tuple[Path, Path],
) -> None:
    assert _ws_connect(cert_pair, None) is None  # RealtimeError(None): no close code


def test_plain_ws_ignores_the_ca_context(cert_pair: tuple[Path, Path]) -> None:
    # The context only applies to wss; a loopback ws:// origin must still work.
    async def handler(ws: ServerConnection) -> None:
        await ws.recv()
        await ws.send(_ready())
        await ws.wait_closed()

    async def scenario() -> str:
        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            ctx = _config(cert_pair[0]).ssl_context()
            client = RealtimeClient(
                f"http://127.0.0.1:{port}", TIMEOUT_S, 1.0, ssl_context=ctx
            )
            try:
                await client.connect("bra_x")
            finally:
                await client.close()
            return "connected"

    assert asyncio.run(scenario()) == "connected"

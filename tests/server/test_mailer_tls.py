"""SmtpMailer TLS modes and login against a real TLS-speaking SMTP sink.

The sink's certificate is self-signed; a test trusts it by pointing
SSL_CERT_FILE at it, so the mailer's own verifying context is what is tested.
"""

import base64
import queue
import socket
import socketserver
import ssl
import subprocess
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Literal

import pytest

from beebreak_server.mailer import SmtpMailer

ADDRESS = "someone.private@example.test"
CODE = "ABCD2345"
FROM = "Beebreak <no-reply@beebreak.test>"
PASSWORD = "pw-value-9876"


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


class _Handler(socketserver.StreamRequestHandler):
    def _reply(self, line: str) -> None:
        self.wfile.write(line.encode() + b"\r\n")
        self.wfile.flush()

    def handle(self) -> None:
        sink = self.server
        assert isinstance(sink, _TlsSink)
        self._reply("220 sink ready")
        secure = sink.mode == "implicit"
        while True:
            line = self.rfile.readline()
            if not line:
                return
            cmd = line[:4].upper()
            if cmd == b"EHLO":
                extra = "250-STARTTLS\r\n" if sink.mode == "starttls" else ""
                auth = "250 AUTH PLAIN" if secure else "250 SIZE 10000"
                self.wfile.write(f"250-sink\r\n{extra}{auth}\r\n".encode())
                self.wfile.flush()
            elif cmd == b"STAR":
                self._reply("220 go")
                self.connection = sink.context.wrap_socket(
                    self.connection, server_side=True
                )
                self.rfile = self.connection.makefile("rb")
                self.wfile = self.connection.makefile("wb")
                secure = True
            elif cmd == b"AUTH":
                sink.auth.put(base64.b64decode(line.split()[-1]))
                self._reply("235 ok" if secure else "538 tls first")
            elif cmd == b"DATA":
                self._reply("354 go ahead")
                while self.rfile.readline() not in (b".\r\n", b""):
                    pass
                sink.delivered.put(secure)
                self._reply("250 queued")
            elif cmd == b"QUIT":
                self._reply("221 bye")
                return
            else:
                self._reply("250 ok")


class _TlsSink(socketserver.ThreadingTCPServer):
    daemon_threads = True

    def __init__(
        self, mode: Literal["implicit", "starttls"], cert: Path, key: Path
    ) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.mode = mode
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(cert, key)
        self.delivered: queue.Queue[bool] = queue.Queue()
        self.auth: queue.Queue[bytes] = queue.Queue()

    def get_request(self) -> tuple[socket.socket, object]:
        sock, addr = super().get_request()
        if self.mode == "implicit":
            sock = self.context.wrap_socket(sock, server_side=True)
        return sock, addr


@pytest.fixture(scope="module")
def cert_pair(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    return _make_cert(tmp_path_factory.mktemp("cert"))


def _serve(
    mode: Literal["implicit", "starttls"], cert_pair: tuple[Path, Path]
) -> Iterator[_TlsSink]:
    server = _TlsSink(mode, *cert_pair)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def implicit_sink(cert_pair: tuple[Path, Path]) -> Iterator[_TlsSink]:
    yield from _serve("implicit", cert_pair)


@pytest.fixture
def starttls_sink(cert_pair: tuple[Path, Path]) -> Iterator[_TlsSink]:
    yield from _serve("starttls", cert_pair)


def _port(server: _TlsSink) -> int:
    return int(server.server_address[1])


def test_implicit_tls_delivers_and_logs_in_when_cert_is_trusted(
    implicit_sink: _TlsSink,
    cert_pair: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SSL_CERT_FILE", str(cert_pair[0]))
    mailer = SmtpMailer(
        "127.0.0.1",
        _port(implicit_sink),
        FROM,
        tls="implicit",
        username="mailer",
        password=PASSWORD,
    )
    try:
        mailer.send_code(ADDRESS, CODE)
        assert implicit_sink.delivered.get(timeout=5) is True
    finally:
        mailer.close()
    assert implicit_sink.auth.get_nowait() == b"\0mailer\0" + PASSWORD.encode()


def test_implicit_tls_refuses_an_untrusted_certificate_and_logs_no_secret(
    implicit_sink: _TlsSink,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    other, _ = _make_cert(tmp_path)  # trust a different certificate
    monkeypatch.setenv("SSL_CERT_FILE", str(other))
    caplog.set_level("DEBUG")
    mailer = SmtpMailer(
        "127.0.0.1",
        _port(implicit_sink),
        FROM,
        tls="implicit",
        username="mailer",
        password=PASSWORD,
    )
    mailer.send_code(ADDRESS, CODE)
    mailer.close()
    assert implicit_sink.delivered.empty() and implicit_sink.auth.empty()
    assert PASSWORD not in caplog.text and ADDRESS not in caplog.text


def test_starttls_upgrades_before_login_and_delivery(
    starttls_sink: _TlsSink,
    cert_pair: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SSL_CERT_FILE", str(cert_pair[0]))
    mailer = SmtpMailer(
        "127.0.0.1",
        _port(starttls_sink),
        FROM,
        tls="starttls",
        username="mailer",
        password=PASSWORD,
    )
    try:
        mailer.send_invite(ADDRESS, CODE)
        assert starttls_sink.delivered.get(timeout=5) is True
    finally:
        mailer.close()
    assert starttls_sink.auth.get_nowait() == b"\0mailer\0" + PASSWORD.encode()


def test_unknown_tls_mode_is_rejected() -> None:
    with pytest.raises(ValueError):
        SmtpMailer("127.0.0.1", 1, FROM, tls="ssl")

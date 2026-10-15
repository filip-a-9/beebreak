import logging
import queue
import smtplib
import socket
import socketserver
import ssl
import threading
from collections.abc import Iterator
from email import message_from_bytes
from email.message import EmailMessage, Message

import pytest

from breakroom_server.mailer import RecordingMailer, SmtpMailer

ADDRESS = "someone.private@example.test"
CODE = "ABCD2345"


class _Handler(socketserver.StreamRequestHandler):
    def _reply(self, line: str) -> None:
        self.wfile.write(line.encode() + b"\r\n")
        self.wfile.flush()

    def handle(self) -> None:
        sink = self.server
        assert isinstance(sink, _Sink)
        self._reply("220 sink ready")
        while True:
            line = self.rfile.readline()
            if not line:
                return
            cmd = line[:4].upper()
            if cmd in (b"EHLO", b"HELO"):
                self._reply("250 sink")
            elif cmd == b"DATA":
                self._reply("354 go ahead")
                data = b""
                while True:
                    chunk = self.rfile.readline()
                    if chunk in (b".\r\n", b""):
                        break
                    data += chunk
                sink.received.put(message_from_bytes(data))
                self._reply("250 queued")
            elif cmd == b"QUIT":
                self._reply("221 bye")
                return
            else:  # MAIL, RCPT, RSET, NOOP
                self._reply("250 ok")


class _Sink(socketserver.ThreadingTCPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.received: queue.Queue[Message] = queue.Queue()


@pytest.fixture
def sink() -> Iterator[_Sink]:
    server = _Sink()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def _port(server: _Sink) -> int:
    return int(server.server_address[1])


def _payload(msg: Message) -> str:
    raw = msg.get_payload(decode=True)
    assert isinstance(raw, bytes)
    return raw.decode()


def test_code_mail_arrives_with_fixed_template(sink: _Sink) -> None:
    mailer = SmtpMailer("127.0.0.1", _port(sink), "Breakroom <no-reply@breakroom.test>")
    try:
        mailer.send_code(ADDRESS, CODE)
        msg = sink.received.get(timeout=5)
    finally:
        mailer.close()
    assert msg["Subject"] == "Your Breakroom sign-in code"
    assert msg["To"] == ADDRESS
    body = _payload(msg)
    assert "ABCD-2345" in body
    assert "10 minutes" in body
    assert "ignore this email" in body


def test_invite_mail_carries_the_invite_code(sink: _Sink) -> None:
    mailer = SmtpMailer("127.0.0.1", _port(sink), "no-reply@breakroom.test")
    try:
        mailer.send_invite(ADDRESS, CODE)
        msg = sink.received.get(timeout=5)
    finally:
        mailer.close()
    assert "ABCD-2345" in _payload(msg)
    assert "companion" in _payload(msg)


def test_close_drains_queued_mail(sink: _Sink) -> None:
    mailer = SmtpMailer("127.0.0.1", _port(sink), "no-reply@breakroom.test")
    for _ in range(3):
        mailer.send_code(ADDRESS, CODE)
    mailer.close()
    assert sink.received.qsize() == 3


def test_dead_port_drops_mail_without_raising_or_leaking(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        dead_port = int(s.getsockname()[1])
    caplog.set_level(logging.DEBUG)
    mailer = SmtpMailer("127.0.0.1", dead_port, "no-reply@breakroom.test", timeout_s=1)
    mailer.send_code(ADDRESS, CODE)
    mailer.send_invite(ADDRESS, CODE)
    mailer.close()
    assert any(r.name == "breakroom_server.mailer" for r in caplog.records)
    assert ADDRESS not in caplog.text
    assert CODE not in caplog.text
    assert "ABCD-2345" not in caplog.text


def test_bad_address_is_dropped_quietly(
    sink: _Sink, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    mailer = SmtpMailer("127.0.0.1", _port(sink), "no-reply@breakroom.test")
    mailer.send_code("a@b.test\r\nBcc: evil@x.test", CODE)
    mailer.send_code(ADDRESS, CODE)
    try:
        msg = sink.received.get(timeout=5)
    finally:
        mailer.close()
    assert msg["To"] == ADDRESS
    assert "evil@x.test" not in caplog.text
    assert CODE not in caplog.text


def test_full_queue_drops_without_raising(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    gate = threading.Event()

    class Blocked(SmtpMailer):
        def _deliver(self, msg: EmailMessage) -> None:
            gate.wait(timeout=5)

    mailer = Blocked("127.0.0.1", 1, "no-reply@breakroom.test")
    try:
        for _ in range(250):
            mailer.send_code(ADDRESS, CODE)
    finally:
        gate.set()
        mailer.close()
    assert "queue full" in caplog.text
    assert ADDRESS not in caplog.text


def test_send_after_close_does_not_raise() -> None:
    mailer = SmtpMailer("127.0.0.1", 1, "no-reply@breakroom.test")
    mailer.close()
    mailer.send_code(ADDRESS, CODE)


def test_recording_mailer_records() -> None:
    mailer = RecordingMailer()
    mailer.send_code("a@b.test", CODE)
    mailer.send_invite("c@d.test", "WXYZ6789")
    mailer.close()
    assert mailer.sent == [
        ("code", "a@b.test", CODE),
        ("invite", "c@d.test", "WXYZ6789"),
    ]


def test_starttls_verifies_the_server_certificate(
    sink: _Sink, monkeypatch: pytest.MonkeyPatch
) -> None:
    contexts: list[ssl.SSLContext | None] = []

    class _RecordingSMTP(smtplib.SMTP):
        def starttls(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            # The sink speaks no TLS: record the context, skip the handshake.
            contexts.append(kwargs.get("context"))
            return (220, b"skipped")

    monkeypatch.setattr(smtplib, "SMTP", _RecordingSMTP)
    mailer = SmtpMailer(
        "127.0.0.1", _port(sink), "Breakroom <no-reply@breakroom.test>", starttls=True
    )
    try:
        mailer.send_code(ADDRESS, CODE)
        sink.received.get(timeout=5)
    finally:
        mailer.close()
    (context,) = contexts
    assert context is not None
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname

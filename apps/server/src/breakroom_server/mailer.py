"""Outbound email with fixed templates. Owned by M3-07.

Rule 10: no address or code ever reaches a log line or exception message.
"""

import logging
import queue
import smtplib
import ssl
import threading
from email.message import EmailMessage
from typing import Final, Literal, Protocol

_log = logging.getLogger(__name__)

QUEUE_SIZE: Final = 100
CLOSE_WAIT_S: Final = 5.0
_POLL_S: Final = 0.2

CODE_SUBJECT: Final = "Your Breakroom sign-in code"
INVITE_SUBJECT: Final = "Your Breakroom invitation"

Kind = Literal["code", "invite"]


class Mailer(Protocol):
    def send_code(self, to: str, code: str) -> None: ...
    def send_invite(self, to: str, code: str) -> None: ...
    def close(self) -> None: ...


def _grouped(code: str) -> str:
    return f"{code[:4]}-{code[4:]}" if len(code) == 8 else code


def _body(kind: Kind, code: str) -> str:
    grouped = _grouped(code)
    if kind == "code":
        return (
            f"Your Breakroom sign-in code is {grouped}\n\n"
            "It is valid for 10 minutes.\n\n"
            "If you did not ask for this, ignore this email.\n"
        )
    return (
        f"Your Breakroom invite code is {grouped}\n\n"
        "Enter it in the Breakroom companion to create your account.\n"
    )


def _build(kind: Kind, to: str, code: str, mail_from: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = mail_from
    msg["To"] = to
    msg["Subject"] = CODE_SUBJECT if kind == "code" else INVITE_SUBJECT
    msg.set_content(_body(kind, code))
    return msg


class SmtpMailer:
    def __init__(
        self,
        host: str,
        port: int,
        mail_from: str,
        tls: str = "none",
        username: str | None = None,
        password: str | None = None,
        timeout_s: float = 10,
    ) -> None:
        if tls not in ("none", "starttls", "implicit"):
            raise ValueError("tls must be none, starttls or implicit")
        self._host = host
        self._port = port
        self._mail_from = mail_from
        self._tls = tls
        self._username = username
        self._password = password
        self._timeout_s = timeout_s
        # Verifies the certificate chain and hostname; bare starttls() does not.
        # Used by both STARTTLS and implicit TLS.
        self._tls_context = ssl.create_default_context()
        self._queue: queue.Queue[tuple[Kind, str, str]] = queue.Queue(QUEUE_SIZE)
        self._closing = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="breakroom-mailer", daemon=True
        )
        self._thread.start()

    def send_code(self, to: str, code: str) -> None:
        self._enqueue("code", to, code)

    def send_invite(self, to: str, code: str) -> None:
        self._enqueue("invite", to, code)

    def close(self) -> None:
        """Stop accepting mail, give queued mail a bounded time to go out."""
        self._closing.set()
        self._thread.join(CLOSE_WAIT_S)

    def _enqueue(self, kind: Kind, to: str, code: str) -> None:
        if self._closing.is_set():
            _log.warning("mail dropped: mailer closed")
            return
        try:
            self._queue.put_nowait((kind, to, code))
        except queue.Full:
            _log.warning("mail dropped: queue full")

    def _run(self) -> None:
        while True:
            try:
                kind, to, code = self._queue.get(timeout=_POLL_S)
            except queue.Empty:
                if self._closing.is_set():
                    return
                continue
            try:
                self._deliver(_build(kind, to, code, self._mail_from))
            except Exception:
                _log.warning("mail dropped: send failed")

    def _deliver(self, msg: EmailMessage) -> None:
        smtp: smtplib.SMTP
        if self._tls == "implicit":
            smtp = smtplib.SMTP_SSL(
                self._host,
                self._port,
                timeout=self._timeout_s,
                context=self._tls_context,
            )
        else:
            smtp = smtplib.SMTP(self._host, self._port, timeout=self._timeout_s)
        with smtp:
            if self._tls == "starttls":
                smtp.starttls(context=self._tls_context)
            if self._username is not None and self._password is not None:
                smtp.login(self._username, self._password)
            smtp.send_message(msg)


class RecordingMailer:
    """Keeps (kind, to, code) in memory; tests and BREAKROOM_MAILER=record."""

    def __init__(self) -> None:
        self.sent: list[tuple[Kind, str, str]] = []

    def send_code(self, to: str, code: str) -> None:
        self.sent.append(("code", to, code))

    def send_invite(self, to: str, code: str) -> None:
        self.sent.append(("invite", to, code))

    def close(self) -> None:
        pass


class NullMailer:
    """Stands in when email sign-in is off (no SMTP host): nothing can be sent
    and no sender thread exists."""

    def send_code(self, to: str, code: str) -> None:
        pass

    def send_invite(self, to: str, code: str) -> None:
        pass

    def close(self) -> None:
        pass

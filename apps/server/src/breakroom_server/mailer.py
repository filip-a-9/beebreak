"""Outbound email with fixed templates. Owned by M3-07."""

from typing import Literal, Protocol


class Mailer(Protocol):
    def send_code(self, to: str, code: str) -> None: ...
    def send_invite(self, to: str, code: str) -> None: ...
    def close(self) -> None: ...


class SmtpMailer:
    def __init__(
        self,
        host: str,
        port: int,
        mail_from: str,
        starttls: bool = False,
        timeout_s: float = 10,
    ) -> None:
        raise NotImplementedError

    def send_code(self, to: str, code: str) -> None:
        raise NotImplementedError

    def send_invite(self, to: str, code: str) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


class RecordingMailer:
    """Keeps (kind, to, code) in memory; tests and BREAKROOM_MAILER=record."""

    def __init__(self) -> None:
        self.sent: list[tuple[Literal["code", "invite"], str, str]] = []

    def send_code(self, to: str, code: str) -> None:
        raise NotImplementedError

    def send_invite(self, to: str, code: str) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

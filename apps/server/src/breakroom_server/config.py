"""Typed server configuration read once from environment variables.

ServerConfig.from_env() rejects a missing or short secret and non-positive
numbers instead of falling back, so a misconfigured server refuses to start.
"""

import base64
import binascii
import os
from dataclasses import dataclass

MIN_SECRET_BYTES = 32


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ValueError(f"{name} is required")
    return value


def _positive(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer") from None
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def decode_secret(raw: str) -> bytes:
    """URL-safe base64 (as made by secrets.token_urlsafe), at least 32 bytes."""
    try:
        secret = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except (binascii.Error, ValueError):
        raise ValueError("BREAKROOM_SERVER_SECRET must be URL-safe base64") from None
    if len(secret) < MIN_SECRET_BYTES:
        raise ValueError("BREAKROOM_SERVER_SECRET is too short")
    return secret


@dataclass(frozen=True)
class ServerConfig:
    database_url: str
    secret: bytes
    smtp_host: str = "127.0.0.1"
    smtp_port: int = 1025
    mail_from: str = "Breakroom <no-reply@breakroom.local>"
    access_ttl_s: int = 900
    refresh_ttl_s: int = 2_592_000
    code_ttl_s: int = 600
    code_max_attempts: int = 5
    invite_ttl_s: int = 604_800
    ws_auth_timeout_s: float = 5
    ws_idle_timeout_s: float = 60

    def __post_init__(self) -> None:
        if len(self.secret) < MIN_SECRET_BYTES:
            raise ValueError("secret is too short")
        for name in (
            "smtp_port",
            "access_ttl_s",
            "refresh_ttl_s",
            "code_ttl_s",
            "code_max_attempts",
            "invite_ttl_s",
            "ws_auth_timeout_s",
            "ws_idle_timeout_s",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")

    @classmethod
    def from_env(cls) -> "ServerConfig":
        return cls(
            database_url=_required("BREAKROOM_DATABASE_URL"),
            secret=decode_secret(_required("BREAKROOM_SERVER_SECRET")),
            smtp_host=os.environ.get("BREAKROOM_SMTP_HOST", "127.0.0.1"),
            smtp_port=_positive("BREAKROOM_SMTP_PORT", 1025),
            mail_from=os.environ.get(
                "BREAKROOM_MAIL_FROM", "Breakroom <no-reply@breakroom.local>"
            ),
            access_ttl_s=_positive("BREAKROOM_ACCESS_TTL_S", 900),
            refresh_ttl_s=_positive("BREAKROOM_REFRESH_TTL_S", 2_592_000),
            code_ttl_s=_positive("BREAKROOM_CODE_TTL_S", 600),
            code_max_attempts=_positive("BREAKROOM_CODE_MAX_ATTEMPTS", 5),
            invite_ttl_s=_positive("BREAKROOM_INVITE_TTL_S", 604_800),
            ws_auth_timeout_s=_positive("BREAKROOM_WS_AUTH_TIMEOUT_S", 5),
            ws_idle_timeout_s=_positive("BREAKROOM_WS_IDLE_TIMEOUT_S", 60),
        )

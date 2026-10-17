"""Typed server configuration read once from environment variables.

ServerConfig.from_env() rejects a missing or short secret and non-positive
numbers instead of falling back, so a misconfigured server refuses to start.
"""

import base64
import binascii
import ipaddress
import os
from dataclasses import dataclass, field

from sqlalchemy.engine import make_url

from breakroom_server.logging_config import LOG_FORMATS

MIN_SECRET_BYTES = 32
MAILER_KINDS = ("smtp", "record")
SMTP_TLS_MODES = ("none", "starttls", "implicit")
# The value shipped in .env.example; a server using it is one anyone can guess.
EXAMPLE_PASSWORD = "change-me"
# Every placeholder .env.example ships for a value this process reads.
EXAMPLE_VALUES = frozenset({EXAMPLE_PASSWORD, "change-me-user", "change-me-too"})


def reject_weak_database_url(url: str) -> None:
    """Refuse the .env.example database password. Never echoes the URL."""
    try:
        password = make_url(url).password
    except Exception:
        raise ValueError("BREAKROOM_DATABASE_URL is not a valid URL") from None
    if password == EXAMPLE_PASSWORD:
        raise ValueError(
            "BREAKROOM_DATABASE_URL uses the example password; set a real secret"
        )


def _trusted_proxies() -> tuple[str, ...]:
    raw = os.environ.get("BREAKROOM_TRUSTED_PROXIES", "").strip()
    if not raw:
        return ()
    items = tuple(part.strip() for part in raw.split(","))
    for item in items:
        try:
            ipaddress.ip_network(item)  # strict: uvicorn ignores host bits silently
        except ValueError:
            raise ValueError(
                "BREAKROOM_TRUSTED_PROXIES must be a comma-separated list of "
                "IP addresses or CIDR ranges"
            ) from None
    return items


def _smtp_tls() -> str:
    mode = os.environ.get("BREAKROOM_SMTP_TLS")
    if mode is None:
        # Deprecated alias, kept for one release.
        return "starttls" if _flag("BREAKROOM_SMTP_STARTTLS") else "none"
    if mode not in SMTP_TLS_MODES:
        raise ValueError("BREAKROOM_SMTP_TLS must be none, starttls or implicit")
    return mode


def _log_format() -> str:
    value = os.environ.get("BREAKROOM_LOG_FORMAT", "text")
    if value not in LOG_FORMATS:
        raise ValueError("BREAKROOM_LOG_FORMAT must be text or json")
    return value


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


def _non_negative(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer") from None
    if value < 0:
        raise ValueError(f"{name} must not be negative")
    return value


def _positive_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number") from None
    if not value > 0:
        raise ValueError(f"{name} must be positive")
    return value


def _flag(name: str) -> bool:
    raw = os.environ.get(name, "0")
    if raw not in ("0", "1"):
        raise ValueError(f"{name} must be 0 or 1")
    return raw == "1"


def _mailer_kind() -> str:
    kind = os.environ.get("BREAKROOM_MAILER", "smtp")
    if kind not in MAILER_KINDS:
        raise ValueError("BREAKROOM_MAILER must be smtp or record")
    return kind


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
    smtp_tls: str = "none"  # none | starttls | implicit
    smtp_username: str | None = None
    smtp_password: str | None = field(default=None, repr=False)
    # Peers whose X-Forwarded-For is believed (M6-03); empty trusts none.
    trusted_proxies: tuple[str, ...] = ()
    mailer: str = "smtp"  # "record" keeps mail in memory (tests, local dev)
    rate_window_s: int = 900
    rate_start_email: int = 5
    rate_start_ip: int = 20
    rate_verify_ip: int = 30
    rate_refresh_device: int = 30
    # Matchmaking (M4).
    queue_ttl_s: int = 90
    match_ttl_s: int = 900
    match_sweep_interval_s: float = 1.0
    rematch_penalty_s: int = 86_400
    retention_s: int = 2_592_000
    rate_queue_user: int = 20
    # Chat and reports (M5).
    chat_grace_s: int = 120
    chat_extension_s: int = 600
    chat_max_extensions: int = 2  # 0 allowed: no extensions
    chat_disconnect_s: int = 60
    message_retention_s: int = 86_400
    report_window_s: int = 86_400
    report_retention_s: int = 7_776_000
    rate_message_user: int = 20  # per rate_message_window_s
    rate_message_window_s: int = 10
    rate_report_user: int = 5  # per rate_window_s
    rate_extension_user: int = 10  # per rate_window_s
    # Retention of sign-in rows (M6-04).
    challenge_retention_s: int = 86_400
    refresh_token_retention_s: int = 604_800
    invite_retention_s: int = 2_592_000
    # Sign-in, refresh and queue abuse limits (M6-05).
    rate_verify_email: int = 10  # failed verifies per email per rate_window_s
    refresh_grace_s: int = 30  # a just-rotated refresh token is accepted again
    rate_queue_action_user: int = 60  # queue DELETE, end, block per rate_window_s
    rate_queue_read_user: int = 300  # queue GET per rate_window_s
    matches_per_window_user: int = 10  # matches a user may join per rate_window_s
    # Logging (M6-08): text | json.
    log_format: str = "text"
    # Server limits that were literals (backlog fixes).
    max_page: int = 100  # messages per page
    extension_attempts: int = 4  # retries of an extension action under contention
    # Request body cap: a chat message is at most 1000 characters after
    # normalization, but a client that JSON-escapes non-ASCII (ensure_ascii)
    # sends up to 12 bytes per character.
    max_body_bytes: int = 16 * 1024
    # The matchmaker purge runs at most this often (injected-clock seconds).
    purge_interval_s: int = 60

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
            "rate_window_s",
            "rate_start_email",
            "rate_start_ip",
            "rate_verify_ip",
            "rate_refresh_device",
            "queue_ttl_s",
            "match_ttl_s",
            "match_sweep_interval_s",
            "rematch_penalty_s",
            "retention_s",
            "rate_queue_user",
            "chat_grace_s",
            "chat_extension_s",
            "chat_disconnect_s",
            "message_retention_s",
            "report_window_s",
            "report_retention_s",
            "rate_message_user",
            "rate_message_window_s",
            "rate_report_user",
            "rate_extension_user",
            "challenge_retention_s",
            "refresh_token_retention_s",
            "invite_retention_s",
            "rate_verify_email",
            "refresh_grace_s",
            "rate_queue_action_user",
            "rate_queue_read_user",
            "matches_per_window_user",
            "max_page",
            "extension_attempts",
            "max_body_bytes",
            "purge_interval_s",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.report_window_s > self.message_retention_s:
            raise ValueError("report_window_s must not exceed message_retention_s")
        if self.retention_s < self.rematch_penalty_s:
            raise ValueError("retention_s must not be below rematch_penalty_s")
        if self.chat_max_extensions < 0:
            raise ValueError("chat_max_extensions must not be negative")
        if self.log_format not in LOG_FORMATS:
            raise ValueError("log_format must be text or json")
        if self.mailer not in MAILER_KINDS:
            raise ValueError("mailer must be smtp or record")
        if self.smtp_tls not in SMTP_TLS_MODES:
            raise ValueError("smtp_tls must be none, starttls or implicit")
        if (self.smtp_username is None) != (self.smtp_password is None):
            raise ValueError("SMTP username and password must be set together")
        if self.smtp_username is not None and self.smtp_tls == "none":
            raise ValueError("SMTP credentials need BREAKROOM_SMTP_TLS")
        reject_weak_database_url(self.database_url)
        for value in (self.smtp_username, self.smtp_password):
            if value in EXAMPLE_VALUES:
                raise ValueError("an SMTP credential is still the example value")

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
            smtp_tls=_smtp_tls(),
            smtp_username=os.environ.get("BREAKROOM_SMTP_USERNAME") or None,
            smtp_password=os.environ.get("BREAKROOM_SMTP_PASSWORD") or None,
            trusted_proxies=_trusted_proxies(),
            mailer=_mailer_kind(),
            rate_window_s=_positive("BREAKROOM_RATE_WINDOW_S", 900),
            rate_start_email=_positive("BREAKROOM_RATE_START_EMAIL", 5),
            rate_start_ip=_positive("BREAKROOM_RATE_START_IP", 20),
            rate_verify_ip=_positive("BREAKROOM_RATE_VERIFY_IP", 30),
            rate_refresh_device=_positive("BREAKROOM_RATE_REFRESH_DEVICE", 30),
            queue_ttl_s=_positive("BREAKROOM_QUEUE_TTL_S", 90),
            match_ttl_s=_positive("BREAKROOM_MATCH_TTL_S", 900),
            match_sweep_interval_s=_positive_float(
                "BREAKROOM_MATCH_SWEEP_INTERVAL_S", 1.0
            ),
            rematch_penalty_s=_positive("BREAKROOM_REMATCH_PENALTY_S", 86_400),
            retention_s=_positive("BREAKROOM_RETENTION_S", 2_592_000),
            rate_queue_user=_positive("BREAKROOM_RATE_QUEUE_USER", 20),
            chat_grace_s=_positive("BREAKROOM_CHAT_GRACE_S", 120),
            chat_extension_s=_positive("BREAKROOM_CHAT_EXTENSION_S", 600),
            chat_max_extensions=_non_negative("BREAKROOM_CHAT_MAX_EXTENSIONS", 2),
            chat_disconnect_s=_positive("BREAKROOM_CHAT_DISCONNECT_S", 60),
            message_retention_s=_positive("BREAKROOM_MESSAGE_RETENTION_S", 86_400),
            report_window_s=_positive("BREAKROOM_REPORT_WINDOW_S", 86_400),
            report_retention_s=_positive("BREAKROOM_REPORT_RETENTION_S", 7_776_000),
            rate_message_user=_positive("BREAKROOM_RATE_MESSAGE_USER", 20),
            rate_message_window_s=_positive("BREAKROOM_RATE_MESSAGE_WINDOW_S", 10),
            rate_report_user=_positive("BREAKROOM_RATE_REPORT_USER", 5),
            rate_extension_user=_positive("BREAKROOM_RATE_EXTENSION_USER", 10),
            challenge_retention_s=_positive("BREAKROOM_CHALLENGE_RETENTION_S", 86_400),
            refresh_token_retention_s=_positive(
                "BREAKROOM_REFRESH_TOKEN_RETENTION_S", 604_800
            ),
            invite_retention_s=_positive("BREAKROOM_INVITE_RETENTION_S", 2_592_000),
            rate_verify_email=_positive("BREAKROOM_RATE_VERIFY_EMAIL", 10),
            refresh_grace_s=_positive("BREAKROOM_REFRESH_GRACE_S", 30),
            rate_queue_action_user=_positive("BREAKROOM_RATE_QUEUE_ACTION_USER", 60),
            rate_queue_read_user=_positive("BREAKROOM_RATE_QUEUE_READ_USER", 300),
            matches_per_window_user=_positive("BREAKROOM_MATCHES_PER_WINDOW_USER", 10),
            log_format=_log_format(),
            max_page=_positive("BREAKROOM_MAX_PAGE", 100),
            extension_attempts=_positive("BREAKROOM_EXTENSION_ATTEMPTS", 4),
            max_body_bytes=_positive("BREAKROOM_MAX_BODY_BYTES", 16 * 1024),
            purge_interval_s=_positive("BREAKROOM_PURGE_INTERVAL_S", 60),
        )

"""Server endpoint and network timing settings. Owned by M3-04."""

import math
import os
import ssl
import stat
from dataclasses import dataclass, field
from pathlib import Path

from beebreak_companion.endpoint import validate_server_url

DEFAULT_SERVER_URL = "http://127.0.0.1:8000"

SERVER_URL_ENV = "BEEBREAK_SERVER_URL"
HTTP_TIMEOUT_ENV = "BEEBREAK_HTTP_TIMEOUT_S"
WS_PING_INTERVAL_ENV = "BEEBREAK_WS_PING_INTERVAL_S"
RECONNECT_MIN_ENV = "BEEBREAK_RECONNECT_MIN_S"
RECONNECT_MAX_ENV = "BEEBREAK_RECONNECT_MAX_S"
CA_FILE_ENV = "BEEBREAK_CA_FILE"
REFRESH_LEAD_ENV = "BEEBREAK_REFRESH_LEAD_S"
NOMINAL_ACCESS_TTL_ENV = "BEEBREAK_NOMINAL_ACCESS_TTL_S"
MIN_REFRESH_DELAY_ENV = "BEEBREAK_MIN_REFRESH_DELAY_S"
GITHUB_POLL_RETRIES_ENV = "BEEBREAK_GITHUB_POLL_RETRIES"
GITHUB_MIN_POLL_INTERVAL_ENV = "BEEBREAK_GITHUB_MIN_POLL_INTERVAL_S"


def _positive_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number") from None
    if not (math.isfinite(value) and value > 0):
        raise ValueError(f"{name} must be positive and finite")
    return value


def _positive_int(name: str, default: int) -> int:
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


def _load_ca(raw: Path) -> ssl.SSLContext:
    """Check an optional private-CA bundle and load it. Raises ValueError.

    Refuses a file another user could replace: not a regular file, writable by
    group or others, or owned by anyone but this user or root. Messages never
    carry the path.
    """
    try:
        info = raw.stat()
    except OSError:
        raise ValueError(
            f"{CA_FILE_ENV} must be a readable PEM certificate file"
        ) from None
    # Check the type before reading: a FIFO would block the load.
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022:
        raise ValueError(f"{CA_FILE_ENV} must be a regular file not writable by others")
    if info.st_uid not in (os.getuid(), 0):
        raise ValueError(f"{CA_FILE_ENV} must be owned by you or root")
    try:
        # Only this CA is trusted when a file is given (not the system store).
        return ssl.create_default_context(cafile=str(raw))
    except (OSError, ssl.SSLError):
        raise ValueError(
            f"{CA_FILE_ENV} must be a readable PEM certificate file"
        ) from None


@dataclass(frozen=True)
class RemoteConfig:
    server_url: str  # canonical origin, already through validate_server_url
    http_timeout_s: float = 10
    ws_ping_interval_s: float = 25
    reconnect_min_s: float = 1
    reconnect_max_s: float = 60
    # Token refresh: this long before expiry (or at half a short lifetime).
    refresh_lead_s: float = 60
    # The server's default access lifetime; the fallback when our clock disagrees.
    nominal_access_ttl_s: float = 900
    min_refresh_delay_s: float = 0.05
    # Consecutive network failures a waiting GitHub sign-in survives.
    github_poll_retries: int = 3
    github_min_poll_interval_s: float = 1
    ca_file: Path | None = None  # trust only this CA instead of the system store
    # Built once from ca_file when the config is made; later changes to the file
    # on disk are not picked up (restart the companion to rotate the CA).
    _ca_context: ssl.SSLContext | None = field(
        default=None, init=False, repr=False, compare=False
    )

    def ssl_context(self) -> ssl.SSLContext | None:
        """A verifying context for the private CA, or None for the default store."""
        return self._ca_context

    def __post_init__(self) -> None:
        if self.ca_file is not None:
            object.__setattr__(self, "_ca_context", _load_ca(self.ca_file))
        for value in (
            self.http_timeout_s,
            self.ws_ping_interval_s,
            self.reconnect_min_s,
            self.reconnect_max_s,
            self.refresh_lead_s,
            self.nominal_access_ttl_s,
            self.min_refresh_delay_s,
            self.github_min_poll_interval_s,
        ):
            if not (math.isfinite(value) and value > 0):
                raise ValueError("remote config values must be positive")
        if self.github_poll_retries <= 0:
            raise ValueError("github_poll_retries must be positive")
        if self.reconnect_min_s > self.reconnect_max_s:
            raise ValueError("reconnect_min_s must not exceed reconnect_max_s")

    @classmethod
    def from_env(cls) -> "RemoteConfig":
        """Raises EndpointRefused for a disallowed BEEBREAK_SERVER_URL."""
        d = cls(server_url=DEFAULT_SERVER_URL)
        return cls(
            server_url=validate_server_url(
                os.environ.get(SERVER_URL_ENV, DEFAULT_SERVER_URL)
            ),
            http_timeout_s=_positive_float(HTTP_TIMEOUT_ENV, d.http_timeout_s),
            ws_ping_interval_s=_positive_float(
                WS_PING_INTERVAL_ENV, d.ws_ping_interval_s
            ),
            reconnect_min_s=_positive_float(RECONNECT_MIN_ENV, d.reconnect_min_s),
            reconnect_max_s=_positive_float(RECONNECT_MAX_ENV, d.reconnect_max_s),
            refresh_lead_s=_positive_float(REFRESH_LEAD_ENV, d.refresh_lead_s),
            nominal_access_ttl_s=_positive_float(
                NOMINAL_ACCESS_TTL_ENV, d.nominal_access_ttl_s
            ),
            min_refresh_delay_s=_positive_float(
                MIN_REFRESH_DELAY_ENV, d.min_refresh_delay_s
            ),
            github_poll_retries=_positive_int(
                GITHUB_POLL_RETRIES_ENV, d.github_poll_retries
            ),
            github_min_poll_interval_s=_positive_float(
                GITHUB_MIN_POLL_INTERVAL_ENV, d.github_min_poll_interval_s
            ),
            ca_file=Path(raw_ca) if (raw_ca := os.environ.get(CA_FILE_ENV)) else None,
        )

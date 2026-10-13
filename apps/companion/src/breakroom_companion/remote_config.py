"""Server endpoint and network timing settings. Owned by M3-04."""

import math
import os
from dataclasses import dataclass

from breakroom_companion.endpoint import validate_server_url

DEFAULT_SERVER_URL = "http://127.0.0.1:8000"

SERVER_URL_ENV = "BREAKROOM_SERVER_URL"
HTTP_TIMEOUT_ENV = "BREAKROOM_HTTP_TIMEOUT_S"
WS_PING_INTERVAL_ENV = "BREAKROOM_WS_PING_INTERVAL_S"
RECONNECT_MIN_ENV = "BREAKROOM_RECONNECT_MIN_S"
RECONNECT_MAX_ENV = "BREAKROOM_RECONNECT_MAX_S"


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


@dataclass(frozen=True)
class RemoteConfig:
    server_url: str  # canonical origin, already through validate_server_url
    http_timeout_s: float = 10
    ws_ping_interval_s: float = 25
    reconnect_min_s: float = 1
    reconnect_max_s: float = 60

    def __post_init__(self) -> None:
        for value in (
            self.http_timeout_s,
            self.ws_ping_interval_s,
            self.reconnect_min_s,
            self.reconnect_max_s,
        ):
            if not (math.isfinite(value) and value > 0):
                raise ValueError("remote config values must be positive")
        if self.reconnect_min_s > self.reconnect_max_s:
            raise ValueError("reconnect_min_s must not exceed reconnect_max_s")

    @classmethod
    def from_env(cls) -> "RemoteConfig":
        """Raises EndpointRefused for a disallowed BREAKROOM_SERVER_URL."""
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
        )

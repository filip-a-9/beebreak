"""Server endpoint and network timing settings. Owned by M3-04."""

from dataclasses import dataclass

DEFAULT_SERVER_URL = "http://127.0.0.1:8000"


@dataclass(frozen=True)
class RemoteConfig:
    server_url: str  # canonical origin, already through validate_server_url
    http_timeout_s: float = 10
    ws_ping_interval_s: float = 25
    reconnect_min_s: float = 1
    reconnect_max_s: float = 60

    @classmethod
    def from_env(cls) -> "RemoteConfig":
        """Raises EndpointRefused for a disallowed BREAKROOM_SERVER_URL."""
        raise NotImplementedError

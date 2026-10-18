"""Typed hook and MCP settings read from environment variables."""

import os
from dataclasses import dataclass

DEFAULT_MAX_STDIN_BYTES = 1024 * 1024
DEFAULT_SEND_TIMEOUT_S = 0.2
DEFAULT_MCP_STATUS_TIMEOUT_S = 0.5


@dataclass(frozen=True, slots=True)
class HookSettings:
    max_stdin_bytes: int = DEFAULT_MAX_STDIN_BYTES
    send_timeout_s: float = DEFAULT_SEND_TIMEOUT_S

    @classmethod
    def from_env(cls) -> "HookSettings":
        return cls(
            max_stdin_bytes=_positive(
                "BEEBREAK_HOOK_MAX_STDIN_BYTES", DEFAULT_MAX_STDIN_BYTES, int
            ),
            send_timeout_s=_positive(
                "BEEBREAK_HOOK_SEND_TIMEOUT_S", DEFAULT_SEND_TIMEOUT_S, float
            ),
        )


@dataclass(frozen=True, slots=True)
class McpSettings:
    status_timeout_s: float = DEFAULT_MCP_STATUS_TIMEOUT_S

    @classmethod
    def from_env(cls) -> "McpSettings":
        return cls(
            status_timeout_s=_positive(
                "BEEBREAK_MCP_STATUS_TIMEOUT_S",
                DEFAULT_MCP_STATUS_TIMEOUT_S,
                float,
            )
        )


def _positive[T: (int, float)](name: str, default: T, cast: type[T]) -> T:
    """Parse a positive number from the environment; bad values use the default."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = cast(raw)
    except ValueError:
        return default
    return value if value > 0 else default

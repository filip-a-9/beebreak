"""Typed companion configuration read from environment variables.

Timeouts are read at call time so tests and launchers can override them.
Invalid or non-positive values fall back to the default. Trigger settings are
read once into TriggerConfig, which rejects invalid values instead.
"""

import os
from dataclasses import dataclass

IPC_READ_TIMEOUT_ENV = "BREAKROOM_IPC_READ_TIMEOUT_S"
WORKER_START_TIMEOUT_ENV = "BREAKROOM_WORKER_START_TIMEOUT_S"
WORKER_STOP_TIMEOUT_ENV = "BREAKROOM_WORKER_STOP_TIMEOUT_S"
PROBE_TIMEOUT_ENV = "BREAKROOM_PROBE_TIMEOUT_S"


def _positive_seconds(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def ipc_read_timeout_s() -> float:
    """Seconds a client connection may take to deliver its one frame line."""
    return _positive_seconds(IPC_READ_TIMEOUT_ENV, 1.0)


def worker_start_timeout_s() -> float:
    """Seconds to wait for the IPC worker thread to bind its socket."""
    return _positive_seconds(WORKER_START_TIMEOUT_ENV, 5.0)


def worker_stop_timeout_s() -> float:
    """Seconds to wait for the IPC worker thread to finish on shutdown."""
    return _positive_seconds(WORKER_STOP_TIMEOUT_ENV, 5.0)


def probe_timeout_s() -> float:
    """Seconds to wait when probing an existing socket for a live companion."""
    return _positive_seconds(PROBE_TIMEOUT_ENV, 1.0)


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


@dataclass(frozen=True)
class TriggerConfig:
    activity_threshold_s: int = 60
    quiet_gap_s: int = 20
    min_tool_events: int = 2
    cooldown_s: int = 900
    suggestion_timeout_s: int = 60
    # Activity staleness (M2-02): silence after which a session is unknown.
    stale_after_s: int = 180
    open_tool_max_s: int = 1800
    # Shorter than open_tool_max_s: an Esc on a prompt fires no hook (M2 README).
    waiting_max_s: int = 300
    forget_after_s: int = 7200

    def __post_init__(self) -> None:
        for field in (
            self.activity_threshold_s,
            self.quiet_gap_s,
            self.min_tool_events,
            self.cooldown_s,
            self.suggestion_timeout_s,
            self.stale_after_s,
            self.open_tool_max_s,
            self.waiting_max_s,
            self.forget_after_s,
        ):
            if field <= 0:
                raise ValueError("trigger config values must be positive")

    @classmethod
    def from_env(cls) -> "TriggerConfig":
        d = cls()
        return cls(
            activity_threshold_s=_positive(
                "BREAKROOM_ACTIVITY_THRESHOLD_S", d.activity_threshold_s
            ),
            quiet_gap_s=_positive("BREAKROOM_QUIET_GAP_S", d.quiet_gap_s),
            min_tool_events=_positive("BREAKROOM_MIN_TOOL_EVENTS", d.min_tool_events),
            cooldown_s=_positive("BREAKROOM_COOLDOWN_S", d.cooldown_s),
            suggestion_timeout_s=_positive(
                "BREAKROOM_SUGGESTION_TIMEOUT_S", d.suggestion_timeout_s
            ),
            stale_after_s=_positive("BREAKROOM_STALE_AFTER_S", d.stale_after_s),
            open_tool_max_s=_positive("BREAKROOM_OPEN_TOOL_MAX_S", d.open_tool_max_s),
            waiting_max_s=_positive("BREAKROOM_WAITING_MAX_S", d.waiting_max_s),
            forget_after_s=_positive("BREAKROOM_FORGET_AFTER_S", d.forget_after_s),
        )

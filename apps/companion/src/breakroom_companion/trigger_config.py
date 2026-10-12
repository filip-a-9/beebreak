"""Typed trigger configuration, read from environment variables."""

import os
from dataclasses import dataclass


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

    def __post_init__(self) -> None:
        for field in (
            self.activity_threshold_s,
            self.quiet_gap_s,
            self.min_tool_events,
            self.cooldown_s,
            self.suggestion_timeout_s,
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
        )

"""Typed companion configuration read from environment variables.

Timeouts are read at call time so tests and launchers can override them.
Invalid or non-positive values fall back to the default. Trigger settings are
read once into TriggerConfig, which rejects invalid values instead.
"""

import math
import os
from dataclasses import dataclass

IPC_READ_TIMEOUT_ENV = "BEEBREAK_IPC_READ_TIMEOUT_S"
WORKER_START_TIMEOUT_ENV = "BEEBREAK_WORKER_START_TIMEOUT_S"
WORKER_STOP_TIMEOUT_ENV = "BEEBREAK_WORKER_STOP_TIMEOUT_S"
PROBE_TIMEOUT_ENV = "BEEBREAK_PROBE_TIMEOUT_S"
GC_INTERVAL_ENV = "BEEBREAK_GC_INTERVAL_S"
NOTIFY_INTERVAL_ENV = "BEEBREAK_NOTIFY_INTERVAL_S"
STOP_WAIT_ENV = "BEEBREAK_STOP_WAIT_S"
CONNECT_TIMEOUT_ENV = "BEEBREAK_CONNECT_TIMEOUT_S"
EXTENSION_INFLIGHT_ENV = "BEEBREAK_EXTENSION_INFLIGHT_S"
EXTENSION_SWAP_GUARD_ENV = "BEEBREAK_EXTENSION_SWAP_GUARD_S"
QUEUE_WAIT_ENV = "BEEBREAK_QUEUE_WAIT_S"
THEME_ENV = "BEEBREAK_THEME"
# Set by WSL to the user-chosen distro name; used to tell devices apart.
WSL_DISTRO_ENV = "WSL_DISTRO_NAME"
THEME_PROBE_TIMEOUT_ENV = "BEEBREAK_THEME_PROBE_TIMEOUT_S"
POWERSHELL_ENV = "BEEBREAK_POWERSHELL"
WINDOWS_TOAST_ENV = "BEEBREAK_WINDOWS_TOAST"
DEFAULT_POWERSHELL = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
TOAST_TIMEOUT_ENV = "BEEBREAK_TOAST_TIMEOUT_S"
THEME_SETTINGS = ("auto", "light", "dark")


def wsl_distro_name() -> str:
    """The WSL distro name, or "" when unset (read at call time)."""
    return os.environ.get(WSL_DISTRO_ENV, "").strip()


def _positive_seconds(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if 0 < value < math.inf else default


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


def gc_interval_s() -> float:
    """Seconds between the GUI thread's checks for due cyclic collections."""
    return _positive_seconds(GC_INTERVAL_ENV, 1.0)


def notify_interval_s() -> float:
    """Minimum seconds between desktop notifications for one chat."""
    return _positive_seconds(NOTIFY_INTERVAL_ENV, 10.0)


def extension_inflight_s() -> float:
    """Seconds the extension buttons stay disabled after a click while the
    next chat status is awaited; then they re-enable."""
    return _positive_seconds(EXTENSION_INFLIGHT_ENV, 5.0)


def extension_swap_guard_s() -> float:
    """Seconds a newly swapped-in Yes/No/Cancel pair ignores clicks, so a
    click aimed at the old button cannot land on the new one."""
    return _positive_seconds(EXTENSION_SWAP_GUARD_ENV, 0.5)


def theme_setting() -> str:
    """`auto`, `light` or `dark`; anything else means `auto`."""
    value = os.environ.get(THEME_ENV, "auto").strip().lower()
    return value if value in THEME_SETTINGS else "auto"


def theme_probe_timeout_s() -> float:
    """Seconds to wait for reg.exe when reading Windows' light/dark setting."""
    return _positive_seconds(THEME_PROBE_TIMEOUT_ENV, 2.0)


def powershell_path() -> str:
    """Windows PowerShell 5.1 as seen from WSL; empty or blank means default."""
    return os.environ.get(POWERSHELL_ENV, "").strip() or DEFAULT_POWERSHELL


def windows_toast_enabled() -> bool:
    """Whether the Windows toast may be used (default on). Only `0`, `false`,
    `no` and `off` turn it off; anything else keeps the default."""
    value = os.environ.get(WINDOWS_TOAST_ENV, "").strip().lower()
    return value not in ("0", "false", "no", "off")


def toast_timeout_s() -> float:
    """Seconds a toast PowerShell may run before it is killed."""
    return _positive_seconds(TOAST_TIMEOUT_ENV, 15.0)


def stop_wait_s() -> float:
    """Seconds uninstall waits for a running companion to exit."""
    return _positive_seconds(STOP_WAIT_ENV, 5.0)


def connect_timeout_s() -> float:
    """Seconds uninstall waits when connecting to the companion's socket."""
    return _positive_seconds(CONNECT_TIMEOUT_ENV, 1.0)


def queue_wait_s() -> float:
    """Seconds a search waits for a partner, as shown to the user. Mirrors the
    server's BEEBREAK_QUEUE_TTL_S (90 s by default); set it if yours differs."""
    return _positive_seconds(QUEUE_WAIT_ENV, 90.0)


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
    min_tool_events: int = 2
    cooldown_s: int = 900
    suggestion_timeout_s: int = 60
    # Activity staleness (M2-02): silence after which a session is unknown.
    stale_after_s: int = 180
    open_tool_max_s: int = 600
    # Shorter than open_tool_max_s: an Esc on a prompt fires no hook (M2 README).
    waiting_max_s: int = 300
    forget_after_s: int = 7200
    # Open tasks but none completed yet: wait this long from the run start for
    # a first interval before deciding without an estimate (M2-03 rule 4a).
    task_evidence_wait_s: int = 300

    def __post_init__(self) -> None:
        for field in (
            self.activity_threshold_s,
            self.min_tool_events,
            self.cooldown_s,
            self.suggestion_timeout_s,
            self.stale_after_s,
            self.open_tool_max_s,
            self.waiting_max_s,
            self.forget_after_s,
            self.task_evidence_wait_s,
        ):
            if field <= 0:
                raise ValueError("trigger config values must be positive")

    @classmethod
    def from_env(cls) -> "TriggerConfig":
        d = cls()
        return cls(
            activity_threshold_s=_positive(
                "BEEBREAK_ACTIVITY_THRESHOLD_S", d.activity_threshold_s
            ),
            min_tool_events=_positive("BEEBREAK_MIN_TOOL_EVENTS", d.min_tool_events),
            cooldown_s=_positive("BEEBREAK_COOLDOWN_S", d.cooldown_s),
            suggestion_timeout_s=_positive(
                "BEEBREAK_SUGGESTION_TIMEOUT_S", d.suggestion_timeout_s
            ),
            stale_after_s=_positive("BEEBREAK_STALE_AFTER_S", d.stale_after_s),
            open_tool_max_s=_positive("BEEBREAK_OPEN_TOOL_MAX_S", d.open_tool_max_s),
            waiting_max_s=_positive("BEEBREAK_WAITING_MAX_S", d.waiting_max_s),
            forget_after_s=_positive("BEEBREAK_FORGET_AFTER_S", d.forget_after_s),
            task_evidence_wait_s=_positive(
                "BEEBREAK_TASK_EVIDENCE_WAIT_S", d.task_evidence_wait_s
            ),
        )

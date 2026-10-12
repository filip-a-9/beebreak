"""Fail-open IPC client used by the hook and the MCP server. Owned by M1-03.

Neither function may raise or exceed its timeout: a missing, stale, or hung
companion is reported as False / None.
"""

from pathlib import Path

from breakroom_shared.events import NormalizedEvent
from breakroom_shared.ipc import StatusReply


def send_event(
    event: NormalizedEvent, *, socket_path: Path, timeout: float = 0.2
) -> bool:
    """Send one EventFrame. True if fully written, False on any failure."""
    return False  # M1-03 stub: behaves as "companion not running".


def request_status(*, socket_path: Path, timeout: float = 0.5) -> StatusReply | None:
    """Send a StatusRequest and return the reply, or None on any failure."""
    return None  # M1-03 stub: behaves as "companion not running".

"""Fail-open IPC client used by the hook and the MCP server.

No function here may raise or exceed its timeout: a missing, stale, or hung
companion is reported as False / None (request_leave_queue: see its docstring).
Nothing is ever printed or logged. This module only reads status and cancels a
search; it has no way to start one.
"""

import socket
import time
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict

from beebreak_shared.events import NormalizedEvent
from beebreak_shared.ipc import (
    MAX_FRAME_BYTES,
    ActivityState,
    EventFrame,
    InvitationState,
    LeaveQueueReply,
    LeaveQueueRequest,
    LeaveResult,
    StatusReply,
    StatusRequest,
)
from beebreak_shared.presence import RemainingTimeBucket


class LegacyStatusReply(BaseModel):
    """A version 2 StatusReply from a companion that predates the queue field."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["status"]
    version: Literal[2]
    invitation: InvitationState
    last_event_at: AwareDatetime | None = None
    activity: ActivityState = "unknown"
    estimate: RemainingTimeBucket | None = None


class _V3StatusReply(BaseModel):
    """A version 3 StatusReply (M4 companion): `matched` is today's `chatting`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["status"]
    version: Literal[3]
    invitation: InvitationState
    last_event_at: AwareDatetime | None = None
    activity: ActivityState = "unknown"
    estimate: RemainingTimeBucket | None = None
    queue: Literal["none", "searching", "matched"] = "none"

    def upgrade(self) -> StatusReply:
        return StatusReply(
            kind="status",
            version=4,
            invitation=self.invitation,
            last_event_at=self.last_event_at,
            activity=self.activity,
            estimate=self.estimate,
            queue="chatting" if self.queue == "matched" else self.queue,
        )


def _connect(path: Path, deadline: float) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(_remaining(deadline))
        sock.connect(str(path))
    except BaseException:
        sock.close()
        raise
    return sock


def _remaining(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise TimeoutError
    return left


def _send_frame(sock: socket.socket, frame: BaseModel, deadline: float) -> None:
    data = (frame.model_dump_json() + "\n").encode()
    # sendall applies the socket timeout per call; one call here, so it is bounded.
    sock.settimeout(_remaining(deadline))
    sock.sendall(data)


def send_event(
    event: NormalizedEvent, *, socket_path: Path, timeout: float = 0.2
) -> bool:
    """Send one EventFrame. True if fully written, False on any failure."""
    try:
        deadline = time.monotonic() + timeout
        with _connect(socket_path, deadline) as sock:
            _send_frame(sock, EventFrame(kind="event", event=event), deadline)
        return True
    except Exception:
        return False


def _read_line(sock: socket.socket, deadline: float) -> bytes | None:
    """One reply line (without the newline), or None if missing or oversize."""
    buf = b""
    while b"\n" not in buf and len(buf) <= MAX_FRAME_BYTES:
        sock.settimeout(_remaining(deadline))
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
    line, sep, _ = buf.partition(b"\n")
    if not sep or len(line) > MAX_FRAME_BYTES:
        return None
    return line


def request_status(
    *, socket_path: Path, timeout: float = 0.5
) -> StatusReply | LegacyStatusReply | None:
    """Send a StatusRequest and return the reply, or None on any failure.

    A version 3 reply (M4 companion) comes back as a version 4 StatusReply with
    `matched` mapped to `chatting`; a version 2 reply as LegacyStatusReply.
    """
    try:
        deadline = time.monotonic() + timeout
        with _connect(socket_path, deadline) as sock:
            _send_frame(sock, StatusRequest(kind="status_request"), deadline)
            line = _read_line(sock, deadline)
        if line is None:
            return None
        try:
            return StatusReply.model_validate_json(line)
        except ValueError:
            pass
        try:
            return _V3StatusReply.model_validate_json(line).upgrade()
        except ValueError:
            return LegacyStatusReply.model_validate_json(line)
    except Exception:
        return None


def request_leave_queue(
    *, socket_path: Path, timeout: float = 0.5
) -> LeaveResult | None:
    """Ask the companion to cancel the user's break-partner search.

    None: nobody answers on the socket (companion not running). Once connected,
    any failure, timeout or malformed reply is "unavailable". An old companion
    that does not know the frame drops it, which also reads as "unavailable".
    """
    try:
        deadline = time.monotonic() + timeout
        sock = _connect(socket_path, deadline)
    except Exception:
        return None
    try:
        with sock:
            _send_frame(sock, LeaveQueueRequest(kind="leave_queue"), deadline)
            line = _read_line(sock, deadline)
        if line is None:
            return "unavailable"
        return LeaveQueueReply.model_validate_json(line).result
    except Exception:
        return "unavailable"

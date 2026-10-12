"""Fail-open IPC client used by the hook and the MCP server.

Neither function may raise or exceed its timeout: a missing, stale, or hung
companion is reported as False / None. Nothing is ever printed or logged.
"""

import socket
import time
from pathlib import Path

from pydantic import BaseModel

from breakroom_shared.events import NormalizedEvent
from breakroom_shared.ipc import MAX_FRAME_BYTES, EventFrame, StatusReply, StatusRequest


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


def request_status(*, socket_path: Path, timeout: float = 0.5) -> StatusReply | None:
    """Send a StatusRequest and return the reply, or None on any failure."""
    try:
        deadline = time.monotonic() + timeout
        with _connect(socket_path, deadline) as sock:
            _send_frame(sock, StatusRequest(kind="status_request"), deadline)
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
        return StatusReply.model_validate_json(line)
    except Exception:
        return None

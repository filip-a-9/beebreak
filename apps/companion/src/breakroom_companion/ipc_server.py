"""Asyncio Unix-socket server for the companion. Owned by M1-03."""

import asyncio
import contextlib
import logging
import os
import socket
import stat
from collections.abc import Callable
from pathlib import Path

from breakroom_companion.config import ipc_read_timeout_s
from breakroom_shared.events import NormalizedEvent
from breakroom_shared.ipc import (
    MAX_FRAME_BYTES,
    ClientFrame,
    EventFrame,
    StatusReply,
)

# Logs record only the failure kind or exception type name, never frame content.
log = logging.getLogger("breakroom.ipc")

_PROBE_TIMEOUT_S = 1.0


def _claim_path(path: Path) -> None:
    """Remove a stale file at path; raise RuntimeError if a companion is live."""
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return
    if stat.S_ISSOCK(mode):
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(_PROBE_TIMEOUT_S)
        try:
            probe.connect(str(path))
        except (ConnectionRefusedError, FileNotFoundError):
            pass  # nobody listening: stale (T7)
        except OSError as exc:
            raise RuntimeError("socket is in use or unreachable") from exc
        else:
            raise RuntimeError("another companion is already running")
        finally:
            probe.close()
    path.unlink(missing_ok=True)


class IpcServer:
    """Serves the breakroom_shared.ipc protocol on a 0600 Unix socket.

    Callbacks run on the server's event-loop thread and must not block.
    """

    def __init__(
        self,
        socket_path: Path,
        *,
        on_event: Callable[[NormalizedEvent], None],
        status: Callable[[], StatusReply],
    ) -> None:
        self.socket_path = socket_path
        self._on_event = on_event
        self._status = status
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        """Bind the socket (replacing a stale one) and begin accepting."""
        _claim_path(self.socket_path)
        old_umask = os.umask(0o177)
        try:
            self._server = await asyncio.start_unix_server(
                self._handle,
                path=str(self.socket_path),
                limit=MAX_FRAME_BYTES + 1,
            )
        finally:
            os.umask(old_umask)
        os.chmod(self.socket_path, 0o600)

    async def close(self) -> None:
        """Stop accepting and remove the socket file."""
        server, self._server = self._server, None
        if server is not None:
            server.close()
            await server.wait_closed()
        self.socket_path.unlink(missing_ok=True)

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            await self._serve_one(reader, writer)
        except Exception as exc:
            log.warning("connection failed: %s", type(exc).__name__)
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _serve_one(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), ipc_read_timeout_s())
        except TimeoutError:
            log.warning("dropped frame: read timeout")
            return
        except ValueError:  # line longer than the stream limit
            log.warning("dropped frame: oversize")
            return
        if not line.endswith(b"\n") or len(line) > MAX_FRAME_BYTES + 1:
            log.warning("dropped frame: truncated or oversize")
            return
        try:
            frame = ClientFrame.model_validate_json(line).root
        except ValueError:
            log.warning("dropped frame: invalid")
            return
        if isinstance(frame, EventFrame):
            try:
                self._on_event(frame.event)
            except Exception as exc:
                log.warning("on_event callback failed: %s", type(exc).__name__)
            return
        try:
            reply = self._status()
        except Exception as exc:
            log.warning("status callback failed: %s", type(exc).__name__)
            return
        writer.write((reply.model_dump_json() + "\n").encode())
        await asyncio.wait_for(writer.drain(), ipc_read_timeout_s())

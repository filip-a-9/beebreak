"""Asyncio Unix-socket server for the companion. Owned by M1-03."""

import asyncio
import contextlib
import logging
import os
import socket
import stat
import threading
from collections.abc import Callable
from pathlib import Path

from beebreak_companion.config import ipc_read_timeout_s, probe_timeout_s
from beebreak_shared.events import NormalizedEvent
from beebreak_shared.ipc import (
    MAX_FRAME_BYTES,
    ClientFrame,
    EventFrame,
    LeaveQueueReply,
    LeaveQueueRequest,
    LeaveResult,
    StatusReply,
)

# Logs record only the failure kind or exception type name, never frame content.
log = logging.getLogger("beebreak.ipc")


class CompanionAlreadyRunning(RuntimeError):
    """A live companion already answers on the socket."""


def _claim_path(path: Path) -> None:
    """Remove a stale file at path; raise RuntimeError if a companion is live."""
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return
    if stat.S_ISSOCK(mode):
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(probe_timeout_s())
        try:
            probe.connect(str(path))
        except (ConnectionRefusedError, FileNotFoundError):
            pass  # nobody listening: stale (T7)
        except OSError as exc:
            raise RuntimeError("socket is in use or unreachable") from exc
        else:
            raise CompanionAlreadyRunning("another companion is already running")
        finally:
            probe.close()
    path.unlink(missing_ok=True)


class IpcServer:
    """Serves the beebreak_shared.ipc protocol on a 0600 Unix socket.

    The event and status callbacks run on the server's event-loop thread and
    must not block. `leave_queue` runs on its own daemon thread and is bounded
    by the IPC read timeout: a callback that hangs or raises is answered with
    "unavailable" and never stalls the server. None means "unavailable".
    """

    def __init__(
        self,
        socket_path: Path,
        *,
        on_event: Callable[[NormalizedEvent], None],
        status: Callable[[], StatusReply],
        leave_queue: Callable[[], LeaveResult] | None = None,
    ) -> None:
        self.socket_path = socket_path
        self._on_event = on_event
        self._status = status
        self._leave_queue = leave_queue
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
        if not line:
            return  # connect-and-close, e.g. a single-instance or stop probe
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
        reply: StatusReply | LeaveQueueReply
        if isinstance(frame, LeaveQueueRequest):
            reply = LeaveQueueReply(
                kind="leave_queue_result", result=await self._leave()
            )
        else:
            try:
                reply = self._status()
            except Exception as exc:
                log.warning("status callback failed: %s", type(exc).__name__)
                return
        writer.write((reply.model_dump_json() + "\n").encode())
        await asyncio.wait_for(writer.drain(), ipc_read_timeout_s())

    async def _leave(self) -> LeaveResult:
        callback = self._leave_queue
        if callback is None:
            return "unavailable"
        loop = asyncio.get_running_loop()
        done: asyncio.Future[LeaveResult] = loop.create_future()

        def deliver(result: LeaveResult | None) -> None:
            if not done.done():
                done.set_result(result if result is not None else "unavailable")

        def run() -> None:
            result: LeaveResult | None = None
            try:
                result = callback()
            except Exception as exc:
                log.warning("leave_queue callback failed: %s", type(exc).__name__)
            with contextlib.suppress(RuntimeError):  # loop already closed
                loop.call_soon_threadsafe(deliver, result)

        # A plain daemon thread, not the loop's executor: a hung callback must
        # not keep the loop (or the process) from shutting down.
        threading.Thread(target=run, name="beebreak-leave", daemon=True).start()
        try:
            return await asyncio.wait_for(done, ipc_read_timeout_s())
        except TimeoutError:
            log.warning("leave_queue callback timed out")
            return "unavailable"

"""Background asyncio thread hosting IpcServer for the Qt app. Owned by M1-03."""

import asyncio
import contextlib
import threading
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from breakroom_companion.config import worker_start_timeout_s, worker_stop_timeout_s
from breakroom_companion.ipc_server import IpcServer
from breakroom_shared.events import NormalizedEvent
from breakroom_shared.ipc import StatusReply


class IpcWorker(QObject):
    """Create on the Qt main thread; event_received is delivered there (queued)."""

    event_received = Signal(object)  # NormalizedEvent

    def __init__(self, socket_path: Path, initial_status: StatusReply) -> None:
        super().__init__()
        self.socket_path = socket_path
        self._status = initial_status
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: IpcServer | None = None
        self._stop_event: asyncio.Event | None = None
        self._ready = threading.Event()
        self._start_error: BaseException | None = None

    def start(self) -> None:
        """Start the loop thread; return once the socket is bound.

        Raises RuntimeError if another companion already owns the socket.
        """
        if self._thread is not None:
            raise RuntimeError("worker already started")
        self._thread = threading.Thread(
            target=self._run, name="breakroom-ipc", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(worker_start_timeout_s()):
            raise RuntimeError("IPC server did not start in time")
        if self._start_error is not None:
            error = self._start_error
            self._thread.join(worker_stop_timeout_s())
            self._thread = None
            if isinstance(error, RuntimeError):
                raise error
            raise RuntimeError("IPC server failed to start") from error

    def set_status(self, status: StatusReply) -> None:
        """Thread-safe snapshot read by the server's status callback."""
        with self._lock:
            self._status = status

    def stop(self) -> None:
        """Close the server and join the thread (bounded wait)."""
        thread, loop, stop_event = self._thread, self._loop, self._stop_event
        self._thread = None
        if thread is None:
            return
        if loop is not None and stop_event is not None and not loop.is_closed():
            with contextlib.suppress(RuntimeError):  # loop already finished
                loop.call_soon_threadsafe(stop_event.set)
        thread.join(worker_stop_timeout_s())

    def _get_status(self) -> StatusReply:
        with self._lock:
            return self._status

    def _on_event(self, event: NormalizedEvent) -> None:
        self.event_received.emit(event)

    def _run(self) -> None:
        asyncio.run(self._main())

    async def _main(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        server = IpcServer(
            self.socket_path, on_event=self._on_event, status=self._get_status
        )
        try:
            await server.start()
        except BaseException as exc:
            self._start_error = exc
            self._ready.set()
            return
        self._ready.set()
        try:
            await self._stop_event.wait()
        finally:
            await server.close()

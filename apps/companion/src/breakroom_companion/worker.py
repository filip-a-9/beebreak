"""Background asyncio thread hosting IpcServer for the Qt app. Owned by M1-03."""

from pathlib import Path

from PySide6.QtCore import QObject, Signal

from breakroom_shared.ipc import StatusReply


class IpcWorker(QObject):
    """Create on the Qt main thread; event_received is delivered there (queued)."""

    event_received = Signal(object)  # NormalizedEvent

    def __init__(self, socket_path: Path, initial_status: StatusReply) -> None:
        super().__init__()
        self.socket_path = socket_path
        self._status = initial_status

    def start(self) -> None:
        """Start the loop thread; return once the socket is bound.

        Raises RuntimeError if another companion already owns the socket.
        """
        raise NotImplementedError("M1-03")

    def set_status(self, status: StatusReply) -> None:
        """Thread-safe snapshot read by the server's status callback."""
        self._status = status

    def stop(self) -> None:
        """Close the server and join the thread (bounded wait)."""
        raise NotImplementedError("M1-03")

"""Asyncio Unix-socket server for the companion. Owned by M1-03."""

from collections.abc import Callable
from pathlib import Path

from breakroom_shared.events import NormalizedEvent
from breakroom_shared.ipc import StatusReply


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

    async def start(self) -> None:
        """Bind the socket (replacing a stale one) and begin accepting."""
        raise NotImplementedError("M1-03")

    async def close(self) -> None:
        """Stop accepting and remove the socket file."""
        raise NotImplementedError("M1-03")

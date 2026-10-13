"""WebSocket client for `/v1/realtime`. Owned by M3-05a.

No Origin header and no extra headers. M3 carries no product events: the
client authenticates, pings, and re-authenticates with a fresh access token.
The token is sent only in the first frame, never in the URL.
"""

import asyncio
import contextlib
import logging

from pydantic import ValidationError
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from breakroom_shared.api import (
    MAX_WS_FRAME_BYTES,
    ServerWsFrame,
    WsAuth,
    WsPing,
    WsReady,
)

_log = logging.getLogger(__name__)

# Our own close when the server sends something we do not understand.
CLOSE_PROTOCOL_ERROR = 1002


class RealtimeError(Exception):
    """Connecting failed or the socket closed. `code` is the WebSocket close
    code, or None when there was no close frame (refused, timeout, reset)."""

    def __init__(self, code: int | None) -> None:
        super().__init__("realtime connection failed")
        self.code = code


def _ws_url(origin: str) -> str:
    # origin is already validated: http -> ws, https -> wss.
    return "ws" + origin.removeprefix("http") + "/v1/realtime"


class RealtimeClient:
    def __init__(self, origin: str, timeout_s: float, ping_interval_s: float) -> None:
        self._url = _ws_url(origin)
        self._timeout_s = timeout_s
        self._ping_interval_s = ping_interval_s
        self._ws: ClientConnection | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._closed: asyncio.Future[int | None] | None = None

    async def connect(self, access_token: str) -> WsReady:
        """Open the socket, authenticate, and return the server's ready frame."""
        loop = asyncio.get_running_loop()
        try:
            ws = await connect(
                self._url,
                origin=None,
                additional_headers=None,
                user_agent_header=None,
                proxy=None,  # never pick up proxies from the environment
                compression=None,
                ping_interval=None,  # application-level pings only
                open_timeout=self._timeout_s,
                close_timeout=self._timeout_s,
                max_size=MAX_WS_FRAME_BYTES,
            )
        except Exception:
            raise RealtimeError(None) from None
        self._ws = ws
        try:
            await ws.send(self._auth_frame(access_token))
            raw = await asyncio.wait_for(ws.recv(), self._timeout_s)
            frame = ServerWsFrame.model_validate_json(raw).root
            if not isinstance(frame, WsReady):
                raise ValueError("unexpected frame")
        except ConnectionClosed:
            await self._discard(ws)
            raise RealtimeError(self._code(ws)) from None
        except Exception:
            await ws.close(CLOSE_PROTOCOL_ERROR)
            await self._discard(ws)
            raise RealtimeError(None) from None
        self._closed = loop.create_future()
        self._tasks = [
            asyncio.create_task(self._pump(ws)),
            asyncio.create_task(self._pinger(ws)),
        ]
        return frame

    async def reauth(self, access_token: str) -> None:
        """Send a fresh `auth` frame on the live socket."""
        if self._ws is None:
            raise RealtimeError(None)
        try:
            await self._ws.send(self._auth_frame(access_token))
        except ConnectionClosed:
            raise RealtimeError(self._code(self._ws)) from None

    async def wait_closed(self) -> int | None:
        """The close code once the socket is gone (None: no close frame)."""
        assert self._closed is not None
        return await asyncio.shield(self._closed)

    async def close(self) -> None:
        ws, self._ws = self._ws, None
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()
        self._finish(None)

    # --- internals ---

    @staticmethod
    def _auth_frame(token: str) -> str:
        return WsAuth(type="auth", version=1, access_token=token).model_dump_json()

    @staticmethod
    def _code(ws: ClientConnection) -> int | None:
        code = ws.close_code
        # 1006 is websockets' stand-in for "no close frame was received".
        return None if code in (None, 1006) else code

    @staticmethod
    async def _discard(ws: ClientConnection) -> None:
        with contextlib.suppress(Exception):
            await ws.close()

    def _finish(self, code: int | None) -> None:
        if self._closed is not None and not self._closed.done():
            self._closed.set_result(code)

    async def _pump(self, ws: ClientConnection) -> None:
        """Read until the socket closes; any bad frame closes it."""
        try:
            async for raw in ws:
                if isinstance(raw, bytes):
                    raise ValueError("binary frame")
                ServerWsFrame.model_validate_json(raw)
            self._finish(self._code(ws))  # a clean close ends the iteration
        except ConnectionClosed:
            self._finish(self._code(ws))
        except (ValidationError, ValueError):
            await self._discard_with(ws, CLOSE_PROTOCOL_ERROR)
            self._finish(None)
        except Exception:
            self._finish(None)

    @staticmethod
    async def _discard_with(ws: ClientConnection, code: int) -> None:
        with contextlib.suppress(Exception):
            await ws.close(code)

    async def _pinger(self, ws: ClientConnection) -> None:
        frame = WsPing(type="ping", version=1).model_dump_json()
        try:
            while True:
                await asyncio.sleep(self._ping_interval_s)
                await ws.send(frame)
        except ConnectionClosed:
            pass  # the pump reports the close

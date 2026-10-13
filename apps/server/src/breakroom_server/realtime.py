"""WebSocket `/v1/realtime`: authenticated presence of a device. Owned by M3-03.

M3 carries no product events. The first frame must be `auth`; authorization is
rechecked on every frame. Nothing about a user (email, token, nickname, IP) is
logged: only fixed strings.
"""

import asyncio
import contextlib
import logging
import threading
from collections import deque
from collections.abc import AsyncIterator, Callable, MutableMapping
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from fastapi import APIRouter, FastAPI, WebSocket
from pydantic import BaseModel, ValidationError
from sqlalchemy import select, update
from starlette.concurrency import run_in_threadpool
from starlette.websockets import WebSocketDisconnect, WebSocketState

from breakroom_server.auth import Principal, authenticate_access
from breakroom_server.config import ServerConfig
from breakroom_server.db import SessionFactory
from breakroom_server.models import Device, User
from breakroom_server.notify import make_frame as make_frame
from breakroom_server.queue import current_state
from breakroom_server.ratelimit import RateLimiter
from breakroom_shared.api import (
    MAX_WS_FRAME_BYTES,
    WS_CLOSE,
    ClientWsFrame,
    MatchView,
    QueueEntryView,
    WsAuth,
    WsPong,
    WsReady,
    WsSync,
)

_log = logging.getLogger(__name__)

# Frame flood limit: more than 20 frames per 10 s closes with 4429.
FLOOD_FRAMES: Final = 20
FLOOD_WINDOW_S: Final = 10.0
NORMAL_CLOSE: Final = 1000
OUTBOUND_MAX: Final = 32  # frames queued per socket; overflow closes with 4429

router = APIRouter()


class Connection:
    """One live socket. `kick` and `enqueue` may be called from any thread."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self.signal = asyncio.Event()  # set by kick and by new outbound frames
        self.kick_code: int | None = None
        self.outbound: deque[str] = deque()

    def _call(self, fn: Callable[..., None], *args: Any) -> None:
        # RuntimeError: loop already closed, the socket is gone anyway.
        with contextlib.suppress(RuntimeError):
            self._loop.call_soon_threadsafe(fn, *args)

    def kick(self, code: int) -> None:
        self._call(self._kick, code)

    def _kick(self, code: int) -> None:
        if self.kick_code is None:
            self.kick_code = code
        self.signal.set()

    def enqueue(self, text: str) -> None:
        self._call(self._enqueue, text)

    def _enqueue(self, text: str) -> None:
        if self.kick_code is not None:
            return
        if len(self.outbound) >= OUTBOUND_MAX:
            # The client re-syncs on reconnect.
            self.outbound.clear()
            self._kick(WS_CLOSE["rate_limited"])
            return
        self.outbound.append(text)
        self.signal.set()


class Hub:
    """In-memory `device_id -> connection` and `user_id -> {device_id}`.

    One connection per device. Implements `notify.Notifier`.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._connections: dict[UUID, Connection] = {}
        self._users: dict[UUID, set[UUID]] = {}
        self._owner: dict[UUID, UUID] = {}  # device_id -> user_id

    def register(self, device_id: UUID, user_id: UUID, conn: Connection) -> None:
        with self._lock:
            old = self._connections.get(device_id)
            self._connections[device_id] = conn
            self._owner[device_id] = user_id
            self._users.setdefault(user_id, set()).add(device_id)
        if old is not None:
            old.kick(WS_CLOSE["replaced"])

    def unregister(self, device_id: UUID, conn: Connection) -> bool:
        """True if `conn` was the current connection of the device."""
        with self._lock:
            if self._connections.get(device_id) is not conn:
                return False
            del self._connections[device_id]
            user_id = self._owner.pop(device_id)
            devices = self._users.get(user_id)
            if devices is not None:
                devices.discard(device_id)
                if not devices:
                    del self._users[user_id]
            return True

    def disconnect_device(self, device_id: UUID) -> None:
        """Close the live socket of a device with 4401 (logout, admin disable)."""
        with self._lock:
            conn = self._connections.get(device_id)
        if conn is not None:
            conn.kick(WS_CLOSE["unauthorized"])

    def send_to_user(self, user_id: UUID, frame: BaseModel) -> None:
        """Notifier: queue `frame` for every socket of the user. Best effort."""
        try:
            text = frame.model_dump_json()
            with self._lock:
                conns = [
                    self._connections[d]
                    for d in self._users.get(user_id, ())
                    if d in self._connections
                ]
            for conn in conns:
                conn.enqueue(text)
        except Exception:
            _log.warning("could not queue frame")

    def is_user_connected(self, user_id: UUID) -> bool:
        """Notifier: does the user have an authenticated socket?"""
        with self._lock:
            return bool(self._users.get(user_id))

    def connected_count(self) -> int:
        with self._lock:
            return len(self._connections)


def install(app: FastAPI) -> Hub:
    """Create the Hub, include the router, clear stale `connected_at` on start."""
    hub = Hub()
    app.state.hub = hub
    app.include_router(router)

    original = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(inner: FastAPI) -> AsyncIterator[Any]:
        factory: SessionFactory = inner.state.session_factory
        await run_in_threadpool(_clear_all_connected, factory)
        async with original(inner) as state:
            yield state

    app.router.lifespan_context = lifespan
    return hub


def _clear_all_connected(factory: SessionFactory) -> None:
    with factory() as session:
        session.execute(
            update(Device)
            .where(Device.connected_at.is_not(None))
            .values(connected_at=None)
        )
        session.commit()


def _authenticate(
    factory: SessionFactory, token: str, now: datetime
) -> tuple[Principal, str | None] | None:
    with factory() as session:
        principal = authenticate_access(session, token, now)
        if principal is None:
            return None
        nickname = session.scalar(
            select(User.nickname).where(User.id == principal.user_id)
        )
        return principal, nickname


def _mark_connected(factory: SessionFactory, device_id: UUID, at: datetime) -> None:
    with factory() as session:
        session.execute(
            update(Device).where(Device.id == device_id).values(connected_at=at)
        )
        session.commit()


def _clear_connected(factory: SessionFactory, device_id: UUID) -> None:
    with factory() as session:
        session.execute(
            update(Device).where(Device.id == device_id).values(connected_at=None)
        )
        session.commit()


def _still_authorized(factory: SessionFactory, principal: Principal) -> bool:
    """Device not revoked and user active."""
    with factory() as session:
        row = session.execute(
            select(Device.revoked_at, User.status)
            .join(User, User.id == Device.user_id)
            .where(Device.id == principal.device_id)
        ).first()
    if row is None:
        return False
    revoked_at, status = row
    return revoked_at is None and status == "active"


class _Close(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


async def _send(ws: WebSocket, frame: BaseModel) -> None:
    await ws.send_text(frame.model_dump_json())


async def _flush(ws: WebSocket, conn: Connection) -> None:
    while conn.outbound:
        await ws.send_text(conn.outbound.popleft())


def _read_state(
    factory: SessionFactory, now: datetime, user_id: UUID
) -> tuple[QueueEntryView | None, MatchView | None]:
    with factory() as session:
        return current_state(session, now, user_id)


def _parse(message: MutableMapping[str, Any]) -> Any:
    text = message.get("text")
    if not isinstance(text, str):  # binary frame
        raise _Close(WS_CLOSE["bad_frame"])
    if len(text.encode()) > MAX_WS_FRAME_BYTES:
        raise _Close(WS_CLOSE["bad_frame"])
    try:
        return ClientWsFrame.model_validate_json(text).root
    except ValidationError:
        raise _Close(WS_CLOSE["bad_frame"]) from None


@router.websocket("/v1/realtime")
async def realtime(websocket: WebSocket) -> None:
    state = websocket.app.state
    config: ServerConfig = state.config
    factory: SessionFactory = state.session_factory
    clock: Callable[[], datetime] = state.clock
    hub: Hub = state.hub

    await websocket.accept()
    loop = asyncio.get_running_loop()
    conn = Connection(loop)
    limiter = RateLimiter(FLOOD_FRAMES, FLOOD_WINDOW_S, clock)
    principal: Principal | None = None
    code = NORMAL_CLOSE
    recv: asyncio.Future[MutableMapping[str, Any]] | None = None
    deadline = loop.time() + config.ws_auth_timeout_s
    try:
        # Browsers send Origin; the desktop client does not. No browser clients.
        if websocket.headers.get("origin") is not None:
            raise _Close(WS_CLOSE["unauthorized"])
        while True:
            # Clear first: anything queued after this point sets it again.
            conn.signal.clear()
            if conn.kick_code is not None:
                raise _Close(conn.kick_code)
            if principal is not None:
                await _flush(websocket, conn)
            if recv is None:
                recv = asyncio.ensure_future(websocket.receive())
            wake = asyncio.ensure_future(conn.signal.wait())
            try:
                waiting: set[asyncio.Future[Any]] = {recv, wake}
                done, _ = await asyncio.wait(
                    waiting,
                    timeout=max(deadline - loop.time(), 0.0),
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                wake.cancel()
                await asyncio.gather(wake, return_exceptions=True)
            if not done:
                raise _Close(
                    NORMAL_CLOSE if principal is not None else WS_CLOSE["auth_timeout"]
                )
            if recv not in done:
                continue  # outbound frame or kick
            message: MutableMapping[str, Any] = recv.result()
            recv = None
            if message["type"] == "websocket.disconnect":
                code = -1
                return
            if limiter.hit("frames") is not None:
                raise _Close(WS_CLOSE["rate_limited"])
            frame = _parse(message)
            now = clock()

            if principal is not None:
                # Rule 12: recheck on every frame.
                if principal.access_expires_at <= now:
                    raise _Close(WS_CLOSE["unauthorized"])
                if not await run_in_threadpool(_still_authorized, factory, principal):
                    raise _Close(WS_CLOSE["unauthorized"])

            if isinstance(frame, WsAuth):
                auth = await run_in_threadpool(
                    _authenticate, factory, frame.access_token, now
                )
                if auth is None:
                    raise _Close(WS_CLOSE["unauthorized"])
                new_principal, nickname = auth
                first = principal is None
                if principal is None:
                    principal = new_principal
                    await run_in_threadpool(
                        _mark_connected, factory, principal.device_id, now
                    )
                    hub.register(principal.device_id, principal.user_id, conn)
                elif new_principal.device_id != principal.device_id:
                    raise _Close(WS_CLOSE["unauthorized"])
                else:
                    principal = new_principal
                await _send(websocket, make_frame(WsReady, now, nickname=nickname))
                if first:
                    queue_view, match_view = await run_in_threadpool(
                        _read_state, factory, now, principal.user_id
                    )
                    await _send(
                        websocket,
                        make_frame(WsSync, now, queue=queue_view, match=match_view),
                    )
            else:
                if principal is None:
                    raise _Close(WS_CLOSE["unauthorized"])
                await _send(websocket, make_frame(WsPong, now))
            deadline = loop.time() + (
                config.ws_idle_timeout_s
                if principal is not None
                else config.ws_auth_timeout_s
            )
    except _Close as closing:
        code = closing.code
    except WebSocketDisconnect:
        code = -1
    finally:
        if recv is not None:
            recv.cancel()
            await asyncio.gather(recv, return_exceptions=True)
        if principal is not None and hub.unregister(principal.device_id, conn):
            try:
                await asyncio.shield(
                    run_in_threadpool(_clear_connected, factory, principal.device_id)
                )
            except Exception:
                _log.warning("could not clear connected_at")
        if code >= 0 and websocket.application_state == WebSocketState.CONNECTED:
            with contextlib.suppress(RuntimeError):  # peer already gone
                await websocket.close(code)

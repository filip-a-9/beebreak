"""A real Breakroom server on a loopback port, for end-to-end tests (rule 11).

`create_app` on a temp SQLite file, RecordingMailer, real clock, run by uvicorn
in a thread on 127.0.0.1:0. `LiveServer.restart()` brings the same app back on
the same port and database. Request counting lets tests prove a client stayed
off the network.

Use the `live_server` fixture (defaults), or `server_factory(**config)` to
override ServerConfig fields such as `access_ttl_s`. Import the fixtures into a
test module; there is deliberately no conftest here.
"""

import socket
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI
from sqlalchemy.orm import sessionmaker
from starlette.types import ASGIApp, Receive, Scope, Send

from breakroom_server.app import create_app
from breakroom_server.config import ServerConfig
from breakroom_server.db import SessionFactory, make_engine
from breakroom_server.mailer import RecordingMailer
from breakroom_server.models import Base
from breakroom_server.tokens import new_code
from tests.server.conftest import TEST_SECRET, add_invite

START_TIMEOUT_S = 10.0


class _Counting:
    """ASGI wrapper that records every http / websocket scope it serves."""

    def __init__(self, app: ASGIApp, seen: list[str]) -> None:
        self._app = app
        self._seen = seen

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            self._seen.append(f"{scope['type']} {scope['path']}")
        await self._app(scope, receive, send)


@dataclass
class LiveServer:
    config: ServerConfig
    mailer: RecordingMailer
    session_factory: SessionFactory
    requests: list[str] = field(default_factory=list)
    port: int = 0
    _server: uvicorn.Server | None = None
    _thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def invite(self, email: str) -> str:
        """Create an invite for `email` (real-clock expiry); returns its code."""
        code = new_code()
        add_invite(self.session_factory, email, code=code, now=datetime.now(UTC))
        return code

    def last_code(self) -> str:
        """The most recent sign-in code the mailer was asked to send."""
        for kind, _to, code in reversed(self.mailer.sent):
            if kind == "code":
                return code
        raise AssertionError("no sign-in code was sent")

    def _run(self) -> None:
        app: FastAPI = create_app(self.config, mailer=self.mailer)
        config = uvicorn.Config(
            _Counting(app, self.requests),
            host="127.0.0.1",
            port=self.port,
            log_level="warning",
            timeout_graceful_shutdown=1,
            access_log=False,
        )
        server = uvicorn.Server(config)
        self._server = server
        server.run()

    def start(self) -> None:
        if self.port == 0:
            # Reserve a free port once; restart() reuses it.
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                self.port = probe.getsockname()[1]
        self._server = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + START_TIMEOUT_S
        while time.monotonic() < deadline:
            server = self._server
            if server is not None and server.started:
                return
            if not self._thread.is_alive():
                break
            time.sleep(0.01)
        raise RuntimeError("live server did not start")

    def stop(self) -> None:
        server, thread = self._server, self._thread
        self._server = self._thread = None
        if server is not None:
            server.should_exit = True
        if thread is not None:
            thread.join(START_TIMEOUT_S)

    def restart(self) -> None:
        self.stop()
        self.start()


def start_server(tmp_path: Path, **overrides: Any) -> LiveServer:
    url = f"sqlite:///{tmp_path / 'live.db'}"
    engine = make_engine(url)
    Base.metadata.create_all(engine)
    params: dict[str, Any] = {
        "database_url": url,
        "secret": TEST_SECRET,
        "mailer": "record",
        "ws_auth_timeout_s": 2,
        "ws_idle_timeout_s": 30,
    } | overrides
    live = LiveServer(
        config=ServerConfig(**params),
        mailer=RecordingMailer(),
        session_factory=sessionmaker(engine, expire_on_commit=False),
    )
    live.start()
    return live


@pytest.fixture
def server_factory(tmp_path: Path) -> Iterator[Callable[..., LiveServer]]:
    started: list[LiveServer] = []

    def make(**overrides: Any) -> LiveServer:
        live = start_server(tmp_path, **overrides)
        started.append(live)
        return live

    yield make
    for live in started:
        live.stop()


@pytest.fixture
def live_server(server_factory: Callable[..., LiveServer]) -> LiveServer:
    return server_factory()

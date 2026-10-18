"""R1: WebSocket limits, refresh grace, GitHub-only config, mail TTL, read/write
limiters, device-code length and the literals that moved into ServerConfig."""

import base64
import dataclasses
import re
import socket
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import pytest
import uvicorn
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from starlette.websockets import WebSocketDisconnect
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

from beebreak_server.admin import _mailer_from
from beebreak_server.app import _default_mailer, create_app
from beebreak_server.cli import serve_options
from beebreak_server.config import ServerConfig
from beebreak_server.db import SessionFactory
from beebreak_server.mailer import Mailer, RecordingMailer, SmtpMailer
from beebreak_server.models import AuthChallenge, Device
from beebreak_server.notify import make_frame
from beebreak_shared.api import MAX_WS_FRAME_BYTES, WS_CLOSE, WsMatchEnded
from tests.server.conftest import (
    TEST_SECRET,
    FixedClock,
    bearer,
    ready_user,
    seed_match,
    sign_in,
)
from tests.server.github_fake import FakeGitHub, fake_github
from tests.server.test_github_auth import github_config
from tests.server.test_mailer import ADDRESS, CODE, _payload, _port, _Sink, sink
from tests.server.test_realtime import PING, auth_frame, close_code, make_account
from tests.server.test_realtime_fanout import sign_on

__all__ = ["fake_github", "github_config", "sink"]

SECRET = base64.urlsafe_b64encode(b"s" * 48).decode().rstrip("=")


def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BEEBREAK_DATABASE_URL", "sqlite://")
    monkeypatch.setenv("BEEBREAK_SERVER_SECRET", SECRET)


def _app(
    config: ServerConfig, mailer: RecordingMailer, clock: FixedClock
) -> TestClient:
    return TestClient(
        create_app(config, mailer=mailer, clock=clock, run_matchmaker=False)
    )


# --- config: every new tunable has a default, an override and a positivity check

NEW_INT_DEFAULTS = {
    "ws_max_message_bytes": 8192,
    "ws_max_connections": 500,
    "rate_ws_connect_ip": 120,
    "ws_flood_frames": 20,
    "ws_outbound_max": 64,
    "last_seen_interval_s": 60,
    "rate_messages_read_user": 300,
    "rate_me_read_user": 120,
    "rate_me_write_user": 30,
    "refresh_grace_s": 90,
}


def test_new_limits_have_their_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch)
    config = ServerConfig.from_env()
    for name, value in NEW_INT_DEFAULTS.items():
        assert getattr(config, name) == value
    assert config.ws_flood_window_s == 10
    assert config.smtp_timeout_s == 10


@pytest.mark.parametrize(
    "name", [*sorted(NEW_INT_DEFAULTS), "ws_flood_window_s", "smtp_timeout_s"]
)
def test_new_limits_read_from_env_and_must_be_positive(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    _env(monkeypatch)
    var = f"BEEBREAK_{name.upper()}"
    monkeypatch.setenv(var, "9999")
    assert getattr(ServerConfig.from_env(), name) == 9999
    for bad in ("0", "-1", "x"):
        monkeypatch.setenv(var, bad)
        with pytest.raises(ValueError):
            ServerConfig.from_env()


def test_ws_message_cap_must_exceed_the_client_frame_cap(
    server_config: ServerConfig,
) -> None:
    with pytest.raises(ValueError):
        dataclasses.replace(server_config, ws_max_message_bytes=MAX_WS_FRAME_BYTES)
    dataclasses.replace(server_config, ws_max_message_bytes=MAX_WS_FRAME_BYTES + 1)


# --- F3: connect limiter ------------------------------------------------------


def test_realtime_connects_are_limited_per_address(
    server_config: ServerConfig,
    engine: Engine,
    mailer: RecordingMailer,
    clock: FixedClock,
) -> None:
    config = dataclasses.replace(server_config, rate_ws_connect_ip=2)
    with _app(config, mailer, clock) as client:
        for _ in range(2):
            with client.websocket_connect("/v1/realtime"):
                pass
        with (
            pytest.raises(WebSocketDisconnect) as refused,
            client.websocket_connect("/v1/realtime"),
        ):
            pass
        # TestClient reports the pre-accept close code; real uvicorn answers
        # the refused handshake with HTTP 403 instead (the companion backs off).
        assert refused.value.code == WS_CLOSE["rate_limited"]
        clock.advance(config.rate_window_s + 1)
        with client.websocket_connect("/v1/realtime"):
            pass


def test_open_sockets_are_capped_before_accept_and_freed_on_close(
    server_config: ServerConfig,
    engine: Engine,
    mailer: RecordingMailer,
    clock: FixedClock,
) -> None:
    config = dataclasses.replace(server_config, ws_max_connections=1)
    with _app(config, mailer, clock) as client:
        with (
            client.websocket_connect("/v1/realtime"),  # holds the only slot
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect("/v1/realtime"),
        ):
            pass
        # The slot frees once the server side of the first socket has ended.
        deadline = time.monotonic() + 3
        while True:
            try:
                with client.websocket_connect("/v1/realtime"):
                    break
            except WebSocketDisconnect:
                assert time.monotonic() < deadline, "the slot was never freed"
                time.sleep(0.05)


# --- F3: the uvicorn frame cap, against a real server --------------------------


@pytest.fixture
def live_port(
    server_config: ServerConfig,
    engine: Engine,
    mailer: RecordingMailer,
    clock: FixedClock,
) -> Iterator[int]:
    config = dataclasses.replace(server_config, ws_idle_timeout_s=5)
    app = create_app(config, mailer=mailer, clock=clock, run_matchmaker=False)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=0,
            log_level="warning",
            **serve_options(config),
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "server did not start"
        time.sleep(0.02)
    try:
        yield int(server.servers[0].sockets[0].getsockname()[1])
    finally:
        server.should_exit = True
        thread.join(10)


def test_an_oversized_frame_is_refused_by_the_server_and_a_normal_one_passes(
    live_port: int,
    session_factory: SessionFactory,
    clock: FixedClock,
    server_config: ServerConfig,
) -> None:
    account = make_account(session_factory, clock)
    url = f"ws://127.0.0.1:{live_port}/v1/realtime"
    with connect(url, max_size=None) as ws:
        ws.send(f'{{"type":"auth","version":1,"access_token":"{account.token}"}}')
        assert "session.ready" in str(ws.recv(timeout=5))
    with connect(url, max_size=None) as ws:
        ws.send("x" * (server_config.ws_max_message_bytes + 1))
        with pytest.raises(ConnectionClosed):
            ws.recv(timeout=5)
        assert ws.protocol.close_code == 1009


# --- F7: refresh grace --------------------------------------------------------


def _refresh(client: TestClient, token: str) -> Any:
    return client.post("/v1/auth/refresh", json={"refresh_token": token})


def test_the_default_grace_covers_a_slow_reconnect(
    client: TestClient,
    session_factory: SessionFactory,
    mailer: RecordingMailer,
    clock: FixedClock,
) -> None:
    first = sign_in(client, session_factory, mailer)["tokens"]
    rotated = _refresh(client, first["refresh_token"])
    clock.advance(80)
    again = _refresh(client, first["refresh_token"])
    assert again.status_code == 200
    assert again.json() == rotated.json()
    clock.advance(11)  # 91 s after the rotation
    assert _refresh(client, first["refresh_token"]).status_code == 401
    with session_factory() as s:
        assert s.scalars(select(Device)).one().revoked_at is not None


# --- F8: GitHub-only server with placeholder SMTP credentials ----------------


def _placeholder_smtp(**overrides: Any) -> ServerConfig:
    values: dict[str, Any] = {
        "database_url": "sqlite://",
        "secret": TEST_SECRET,
        "smtp_tls": "starttls",
        "smtp_username": "change-me-user",
        "smtp_password": "change-me-too",
    }
    values.update(overrides)
    return ServerConfig(**values)


def test_github_only_server_ignores_placeholder_smtp_credentials() -> None:
    config = _placeholder_smtp(smtp_host="", github_client_id="Iv1.abc")
    assert not config.email_enabled and config.github_enabled


def test_email_server_still_refuses_placeholder_smtp_credentials() -> None:
    with pytest.raises(ValueError):
        _placeholder_smtp(smtp_host="smtp.example.com", github_client_id="Iv1.abc")


# --- F9: the code TTL in the email -------------------------------------------


def _code_mail_body(sink: _Sink, **kwargs: Any) -> str:
    mailer = SmtpMailer("127.0.0.1", _port(sink), "no-reply@beebreak.test", **kwargs)
    try:
        mailer.send_code(ADDRESS, CODE)
        return _payload(sink.received.get(timeout=5))
    finally:
        mailer.close()


def test_the_mail_names_the_configured_ttl_in_minutes(sink: _Sink) -> None:
    body = _code_mail_body(sink, code_ttl_s=300)
    assert re.search(r"\b5\b", body)
    assert not re.search(r"\b10\b", body)


def test_a_partial_minute_rounds_up(sink: _Sink) -> None:
    assert re.search(r"\b2\b", _code_mail_body(sink, code_ttl_s=61))


def test_the_default_mailer_gets_the_configured_ttl(
    sink: _Sink, server_config: ServerConfig
) -> None:
    config = dataclasses.replace(
        server_config,
        mailer="smtp",
        smtp_host="127.0.0.1",
        smtp_port=_port(sink),
        code_ttl_s=180,
    )
    mailer = _default_mailer(config)
    try:
        mailer.send_code(ADDRESS, CODE)
        body = _payload(sink.received.get(timeout=5))
    finally:
        mailer.close()
    assert re.search(r"\b3\b", body)


# --- F12: the SMTP timeout reaches the mailer ----------------------------------


@pytest.mark.parametrize(
    "factory", [_default_mailer, _mailer_from], ids=["server", "admin-invite"]
)
def test_the_smtp_timeout_from_config_bounds_a_silent_server(
    server_config: ServerConfig, factory: Callable[[ServerConfig], Mailer]
) -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)  # accepts the connection, never says 220
    config = dataclasses.replace(
        server_config,
        mailer="smtp",
        smtp_host="127.0.0.1",
        smtp_port=int(listener.getsockname()[1]),
        smtp_timeout_s=0.3,
    )
    mailer = factory(config)
    try:
        mailer.send_invite(ADDRESS, CODE)
        started = time.monotonic()
        # close() waits up to 5 s for the sender; a 0.3 s timeout frees it at
        # once, while the default of 10 s would hold close() for the full wait.
        mailer.close()
        assert time.monotonic() - started < 3
    finally:
        listener.close()


# --- F10: per-user limiters ---------------------------------------------------


@pytest.fixture
def tight(
    server_config: ServerConfig,
    engine: Engine,
    mailer: RecordingMailer,
    clock: FixedClock,
) -> Iterator[TestClient]:
    config = dataclasses.replace(
        server_config,
        rate_messages_read_user=2,
        rate_me_read_user=2,
        rate_me_write_user=2,
    )
    with _app(config, mailer, clock) as client:
        yield client


def _assert_limited(response: Any) -> None:
    assert response.status_code == 429
    assert response.json() == {"error": "rate_limited"}
    assert int(response.headers["Retry-After"]) >= 1


def test_me_reads_are_limited_per_user(
    tight: TestClient, session_factory: SessionFactory, mailer: RecordingMailer
) -> None:
    headers = bearer(sign_in(tight, session_factory, mailer))
    for _ in range(2):
        assert tight.get("/v1/me", headers=headers).status_code == 200
    _assert_limited(tight.get("/v1/me", headers=headers))


def test_nickname_writes_are_limited_per_user(
    tight: TestClient, session_factory: SessionFactory, mailer: RecordingMailer
) -> None:
    headers = bearer(sign_in(tight, session_factory, mailer))
    for name in ("Ada", "Adab"):
        patched = tight.patch("/v1/me", json={"nickname": name}, headers=headers)
        assert patched.status_code == 200
    _assert_limited(tight.patch("/v1/me", json={"nickname": "Adac"}, headers=headers))


def test_consent_writes_are_limited_per_user(
    tight: TestClient, session_factory: SessionFactory, mailer: RecordingMailer
) -> None:
    headers = bearer(sign_in(tight, session_factory, mailer))
    consent = {
        "version": 1,
        "share_task_category": True,
        "share_stack_tags": True,
        "share_time_bucket": True,
    }
    for _ in range(2):
        put = tight.put("/v1/me/consent", json=consent, headers=headers)
        assert put.status_code == 200
    _assert_limited(tight.put("/v1/me/consent", json=consent, headers=headers))


def test_me_reads_and_writes_have_separate_budgets(
    tight: TestClient, session_factory: SessionFactory, mailer: RecordingMailer
) -> None:
    headers = bearer(sign_in(tight, session_factory, mailer))
    for _ in range(2):
        tight.get("/v1/me", headers=headers)
    patched = tight.patch("/v1/me", json={"nickname": "Ada"}, headers=headers)
    assert patched.status_code == 200


def test_message_history_reads_are_limited_per_user(
    tight: TestClient, session_factory: SessionFactory, mailer: RecordingMailer
) -> None:
    ada = ready_user(tight, session_factory, mailer, "ada@example.com", "Ada")
    bob = ready_user(tight, session_factory, mailer, "bob@example.com", "Bobby")
    match_id = seed_match(session_factory, ada, bob)
    url = f"/v1/matches/{match_id}/messages"
    for _ in range(2):
        assert tight.get(url, headers=ada.headers).status_code == 200
    _assert_limited(tight.get(url, headers=ada.headers))
    assert tight.get(url, headers=bob.headers).status_code == 200  # per user


# --- F11: device code length --------------------------------------------------


def _device_code_column_length() -> int:
    length = AuthChallenge.__table__.c.github_device_code.type.length  # type: ignore[attr-defined]
    assert isinstance(length, int)
    return length


def test_a_device_code_longer_than_the_column_is_a_github_failure(
    github_config: ServerConfig,
    fake_github: FakeGitHub,
    engine: Engine,
    mailer: RecordingMailer,
    clock: FixedClock,
    session_factory: SessionFactory,
) -> None:
    fake_github.device_code = "d" * (_device_code_column_length() + 1)
    with _app(github_config, mailer, clock) as gh:
        response = gh.post("/v1/auth/github/start")
    assert (response.status_code, response.json()) == (
        502,
        {"error": "github_unavailable"},
    )
    with session_factory() as s:
        assert s.scalars(select(AuthChallenge)).first() is None


def test_a_device_code_at_the_column_length_is_accepted(
    github_config: ServerConfig,
    fake_github: FakeGitHub,
    engine: Engine,
    mailer: RecordingMailer,
    clock: FixedClock,
) -> None:
    fake_github.device_code = "d" * _device_code_column_length()
    with _app(github_config, mailer, clock) as gh:
        assert gh.post("/v1/auth/github/start").status_code == 200


# --- F12: literals that moved into ServerConfig --------------------------------


def test_a_lower_flood_limit_closes_sooner(
    server_config: ServerConfig,
    engine: Engine,
    mailer: RecordingMailer,
    clock: FixedClock,
    session_factory: SessionFactory,
) -> None:
    config = dataclasses.replace(server_config, ws_flood_frames=3)
    account = make_account(session_factory, clock)
    with (
        _app(config, mailer, clock) as client,
        client.websocket_connect("/v1/realtime") as ws,
    ):
        ws.send_json(auth_frame(account.token))
        for _ in range(5):  # well under the default of 20
            ws.send_json(PING)
        assert close_code(ws) == WS_CLOSE["rate_limited"]


def test_a_lower_outbound_cap_closes_a_backed_up_socket(
    server_config: ServerConfig,
    engine: Engine,
    mailer: RecordingMailer,
    clock: FixedClock,
    session_factory: SessionFactory,
) -> None:
    config = dataclasses.replace(server_config, ws_outbound_max=2)
    account = make_account(session_factory, clock)
    app = create_app(config, mailer=mailer, clock=clock, run_matchmaker=False)
    with TestClient(app) as client, client.websocket_connect("/v1/realtime") as ws:
        sign_on(ws, account.token)

        def burst() -> None:  # one event-loop turn: the socket cannot drain
            for _ in range(3):
                app.state.hub.send_to_user(
                    account.user_id,
                    make_frame(
                        WsMatchEnded,
                        clock(),
                        match_id=uuid.uuid4(),
                        reason="ended",
                    ),
                )

        client.portal.call(burst)  # type: ignore[union-attr]
        assert close_code(ws) == WS_CLOSE["rate_limited"]


def test_last_seen_interval_comes_from_config(
    server_config: ServerConfig,
    engine: Engine,
    mailer: RecordingMailer,
    clock: FixedClock,
    session_factory: SessionFactory,
) -> None:
    config = dataclasses.replace(server_config, last_seen_interval_s=5)
    with _app(config, mailer, clock) as client:
        headers = bearer(sign_in(client, session_factory, mailer))
        clock.advance(5)  # the default of 60 s would not refresh yet
        client.get("/v1/me", headers=headers)
    with session_factory() as s:
        assert s.scalars(select(Device)).one().last_seen_at == clock()

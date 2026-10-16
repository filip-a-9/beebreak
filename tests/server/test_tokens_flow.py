import dataclasses
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from breakroom_server.app import create_app
from breakroom_server.auth import authenticate_access
from breakroom_server.config import ServerConfig
from breakroom_server.db import SessionFactory
from breakroom_server.mailer import RecordingMailer
from breakroom_server.models import Device, RefreshToken, User
from breakroom_server.tokens import hash_token
from tests.server.conftest import START_TIME, FixedClock, bearer, sign_in

UNAUTHORIZED = {"error": "unauthorized"}


def refresh(client: TestClient, token: str) -> Any:
    return client.post("/v1/auth/refresh", json={"refresh_token": token})


def me(client: TestClient, access_token: str) -> Any:
    return client.get("/v1/me", headers={"Authorization": f"Bearer {access_token}"})


def test_tokens_are_stored_only_as_hashes(
    client: TestClient, session_factory: SessionFactory, mailer: RecordingMailer
) -> None:
    session = sign_in(client, session_factory, mailer)
    tokens = session["tokens"]
    with session_factory() as s:
        device = s.scalars(select(Device)).one()
        row = s.scalars(select(RefreshToken)).one()
    assert device.access_hash == hash_token(tokens["access_token"])
    assert row.token_hash == hash_token(tokens["refresh_token"])
    assert tokens["access_expires_at"].startswith("2026-10-13T12:15:00")


def test_missing_or_malformed_authorization_is_401_with_challenge(
    client: TestClient,
) -> None:
    for headers in (
        {},
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer "},
        {"Authorization": "Basic abc"},
        {"Authorization": "Bearer nonsense"},
        {"Authorization": "Bearer brr_" + "a" * 43},
    ):
        response = client.get("/v1/me", headers=headers)
        assert (response.status_code, response.json()) == (401, UNAUTHORIZED)
        assert response.headers["WWW-Authenticate"] == "Bearer"


def test_token_in_a_query_string_is_ignored(
    client: TestClient,
    session_factory: SessionFactory,
    mailer: RecordingMailer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    session = sign_in(client, session_factory, mailer)
    token = session["tokens"]["access_token"]
    assert client.get(f"/v1/me?access_token={token}").status_code == 401
    caplog.clear()  # the test client itself logs the URL it was given


def test_rotate_twice_then_replay_revokes_the_device(
    client: TestClient,
    session_factory: SessionFactory,
    mailer: RecordingMailer,
    clock: FixedClock,
    server_config: ServerConfig,
) -> None:
    first = sign_in(client, session_factory, mailer)["tokens"]
    second = refresh(client, first["refresh_token"])
    assert second.status_code == 200
    second_tokens = second.json()
    third = refresh(client, second_tokens["refresh_token"])
    assert third.status_code == 200
    newest = third.json()

    # Every rotation replaces the access token.
    assert me(client, first["access_token"]).status_code == 401
    assert me(client, second_tokens["access_token"]).status_code == 401
    assert me(client, newest["access_token"]).status_code == 200

    clock.advance(server_config.refresh_grace_s + 1)
    replay = refresh(client, first["refresh_token"])
    assert (replay.status_code, replay.json()) == (401, UNAUTHORIZED)

    assert me(client, newest["access_token"]).status_code == 401
    assert refresh(client, newest["refresh_token"]).status_code == 401
    with session_factory() as s:
        assert s.scalars(select(Device)).one().revoked_at == clock()


def test_a_new_sign_in_after_reuse_detection_works(
    client: TestClient,
    session_factory: SessionFactory,
    mailer: RecordingMailer,
    clock: FixedClock,
    server_config: ServerConfig,
) -> None:
    first = sign_in(client, session_factory, mailer)["tokens"]
    refresh(client, first["refresh_token"])
    clock.advance(server_config.refresh_grace_s + 1)
    refresh(client, first["refresh_token"])  # replay
    again = sign_in(client, session_factory, mailer)
    assert me(client, again["tokens"]["access_token"]).status_code == 200


def test_expired_access_is_401_but_refresh_still_works(
    client: TestClient,
    session_factory: SessionFactory,
    mailer: RecordingMailer,
    clock: FixedClock,
    server_config: ServerConfig,
) -> None:
    tokens = sign_in(client, session_factory, mailer)["tokens"]
    clock.advance(server_config.access_ttl_s - 1)
    assert me(client, tokens["access_token"]).status_code == 200
    clock.advance(1)
    expired = me(client, tokens["access_token"])
    assert (expired.status_code, expired.json()) == (401, UNAUTHORIZED)
    renewed = refresh(client, tokens["refresh_token"])
    assert renewed.status_code == 200
    assert me(client, renewed.json()["access_token"]).status_code == 200


def test_expired_refresh_token_is_401(
    client: TestClient,
    session_factory: SessionFactory,
    mailer: RecordingMailer,
    clock: FixedClock,
    server_config: ServerConfig,
) -> None:
    tokens = sign_in(client, session_factory, mailer)["tokens"]
    clock.advance(server_config.refresh_ttl_s)
    response = refresh(client, tokens["refresh_token"])
    assert (response.status_code, response.json()) == (401, UNAUTHORIZED)


def test_unknown_or_wrong_kind_refresh_token_is_401(
    client: TestClient, session_factory: SessionFactory, mailer: RecordingMailer
) -> None:
    session = sign_in(client, session_factory, mailer)
    assert refresh(client, "brr_" + "x" * 43).status_code == 401
    assert refresh(client, session["tokens"]["access_token"]).status_code == 401
    assert refresh(client, "").status_code == 401


def test_refresh_for_a_disabled_user_is_401(
    client: TestClient, session_factory: SessionFactory, mailer: RecordingMailer
) -> None:
    tokens = sign_in(client, session_factory, mailer)["tokens"]
    with session_factory() as s:
        s.scalars(select(User)).one().status = "disabled"
        s.commit()
    assert refresh(client, tokens["refresh_token"]).status_code == 401
    assert me(client, tokens["access_token"]).status_code == 401


def test_logout_revokes_access_and_refresh(
    client: TestClient, session_factory: SessionFactory, mailer: RecordingMailer
) -> None:
    session = sign_in(client, session_factory, mailer)
    other = sign_in(client, session_factory, mailer, device_name="desktop")
    response = client.post("/v1/auth/logout", headers=bearer(session))
    assert response.status_code == 204 and response.content == b""

    assert me(client, session["tokens"]["access_token"]).status_code == 401
    assert refresh(client, session["tokens"]["refresh_token"]).status_code == 401
    assert client.post("/v1/auth/logout", headers=bearer(session)).status_code == 401
    # Only the calling device is signed out.
    assert me(client, other["tokens"]["access_token"]).status_code == 200
    with session_factory() as s:
        devices = {d.name: d for d in s.scalars(select(Device))}
    assert devices["laptop"].revoked_at is not None
    assert devices["desktop"].revoked_at is None


def test_logout_requires_authentication(client: TestClient) -> None:
    assert client.post("/v1/auth/logout").status_code == 401


class _SyncHub:
    def __init__(self) -> None:
        self.calls: list[UUID] = []

    def disconnect_device(self, device_id: UUID) -> None:
        self.calls.append(device_id)


class _AsyncHub(_SyncHub):
    async def disconnect_device(self, device_id: UUID) -> None:  # type: ignore[override]
        self.calls.append(device_id)


def _device_id(session_factory: SessionFactory) -> UUID:
    with session_factory() as s:
        return s.scalars(select(Device)).one().id


@pytest.mark.parametrize("hub_class", [_SyncHub, _AsyncHub])
def test_logout_notifies_the_hub_when_present(
    hub_class: type[_SyncHub],
    app: FastAPI,
    client: TestClient,
    session_factory: SessionFactory,
    mailer: RecordingMailer,
) -> None:
    hub = hub_class()
    app.state.hub = hub
    session = sign_in(client, session_factory, mailer)
    device_id = _device_id(session_factory)
    client.post("/v1/auth/logout", headers=bearer(session))
    assert hub.calls == [device_id]


@pytest.mark.parametrize("hub_class", [_SyncHub, _AsyncHub])
def test_refresh_reuse_notifies_the_hub(
    hub_class: type[_SyncHub],
    app: FastAPI,
    client: TestClient,
    session_factory: SessionFactory,
    mailer: RecordingMailer,
    clock: FixedClock,
    server_config: ServerConfig,
) -> None:
    hub = hub_class()
    app.state.hub = hub
    tokens = sign_in(client, session_factory, mailer)["tokens"]
    device_id = _device_id(session_factory)
    refresh(client, tokens["refresh_token"])
    assert hub.calls == []
    clock.advance(server_config.refresh_grace_s + 1)
    refresh(client, tokens["refresh_token"])  # replay
    assert hub.calls == [device_id]


def test_last_seen_is_updated_at_most_once_a_minute(
    client: TestClient,
    session_factory: SessionFactory,
    mailer: RecordingMailer,
    clock: FixedClock,
) -> None:
    token = sign_in(client, session_factory, mailer)["tokens"]["access_token"]

    def last_seen() -> Any:
        with session_factory() as s:
            return s.scalars(select(Device)).one().last_seen_at

    assert last_seen() == START_TIME
    clock.advance(59)
    me(client, token)
    assert last_seen() == START_TIME
    clock.advance(1)
    me(client, token)
    assert last_seen() == START_TIME + timedelta(seconds=60)


def test_authenticate_access_checks_state_and_expiry(
    client: TestClient, session_factory: SessionFactory, mailer: RecordingMailer
) -> None:
    tokens = sign_in(client, session_factory, mailer)["tokens"]
    now = START_TIME
    with session_factory() as s:
        principal = authenticate_access(s, tokens["access_token"], now)
        assert principal is not None
        assert principal.access_expires_at == now + timedelta(seconds=900)
        assert (
            authenticate_access(s, tokens["access_token"], principal.access_expires_at)
            is None
        )
        assert authenticate_access(s, "bra_unknown", now) is None
        assert authenticate_access(s, tokens["refresh_token"], now) is None
        assert authenticate_access(s, "", now) is None


def test_refresh_is_limited_per_device(
    server_config: ServerConfig,
    engine: object,
    session_factory: SessionFactory,
    mailer: RecordingMailer,
    clock: FixedClock,
) -> None:
    config = dataclasses.replace(server_config, rate_refresh_device=2)
    with TestClient(create_app(config, mailer=mailer, clock=clock)) as client:
        tokens = sign_in(client, session_factory, mailer)["tokens"]
        for _ in range(2):
            renewed = refresh(client, tokens["refresh_token"])
            assert renewed.status_code == 200
            tokens = renewed.json()
        blocked = refresh(client, tokens["refresh_token"])
        assert (blocked.status_code, blocked.json()) == (429, {"error": "rate_limited"})
        assert int(blocked.headers["Retry-After"]) > 0
        clock.advance(901)
        assert refresh(client, tokens["refresh_token"]).status_code == 200


def test_lone_surrogate_in_a_token_is_401_not_500(client: TestClient) -> None:
    # json.loads accepts the escape; the server must not crash encoding it.
    response = client.post(
        "/v1/auth/refresh",
        content=b'{"refresh_token": "brr_\\ud800"}',
        headers={"Content-Type": "application/json"},
    )
    assert (response.status_code, response.json()) == (401, {"error": "unauthorized"})

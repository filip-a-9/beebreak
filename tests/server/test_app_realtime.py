"""Realtime wired into create_app: the real HTTP routes reach the hub."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from breakroom_server.db import SessionFactory
from breakroom_server.mailer import RecordingMailer
from breakroom_shared.api import WS_CLOSE
from tests.server.conftest import bearer, sign_in


def test_create_app_installs_hub(app: FastAPI) -> None:
    assert app.state.hub.connected_count() == 0


def test_http_logout_closes_live_socket(
    client: TestClient,
    session_factory: SessionFactory,
    mailer: RecordingMailer,
) -> None:
    session = sign_in(client, session_factory, mailer)
    access = session["tokens"]["access_token"]
    with client.websocket_connect("/v1/realtime") as ws:
        ws.send_json({"type": "auth", "version": 1, "access_token": access})
        assert ws.receive_json()["type"] == "session.ready"
        assert client.app.state.hub.connected_count() == 1  # type: ignore[attr-defined]
        response = client.post("/v1/auth/logout", headers=bearer(session))
        assert response.status_code == 204
        with pytest.raises(WebSocketDisconnect) as info:
            ws.receive_json()
        assert info.value.code == WS_CLOSE["unauthorized"]


def test_refresh_reuse_closes_live_socket(
    client: TestClient,
    session_factory: SessionFactory,
    mailer: RecordingMailer,
) -> None:
    session = sign_in(client, session_factory, mailer)
    first_refresh = session["tokens"]["refresh_token"]
    rotated = client.post("/v1/auth/refresh", json={"refresh_token": first_refresh})
    assert rotated.status_code == 200
    access = rotated.json()["access_token"]
    with client.websocket_connect("/v1/realtime") as ws:
        ws.send_json({"type": "auth", "version": 1, "access_token": access})
        assert ws.receive_json()["type"] == "session.ready"
        replay = client.post("/v1/auth/refresh", json={"refresh_token": first_refresh})
        assert replay.status_code == 401
        with pytest.raises(WebSocketDisconnect) as info:
            ws.receive_json()
        assert info.value.code == WS_CLOSE["unauthorized"]

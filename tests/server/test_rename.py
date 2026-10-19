"""U9: renaming the device and the nickname after sign-in."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from beebreak_server.config import ServerConfig
from beebreak_server.db import SessionFactory
from beebreak_server.mailer import RecordingMailer
from beebreak_server.matchmaking import Matchmaker
from beebreak_server.models import Block, Device, QueueEntry
from tests.server.conftest import (
    START_TIME,
    FixedClock,
    SeededUser,
    ready_user,
    seed_queued,
)
from tests.server.fakes import RecordingNotifier


def test_me_carries_this_devices_name(
    client: TestClient, session_factory: SessionFactory, mailer: RecordingMailer
) -> None:
    ada = ready_user(client, session_factory, mailer, device_name="desk")
    assert client.get("/v1/me", headers=ada.headers).json()["device_name"] == "desk"


def test_rename_changes_only_the_callers_device(
    client: TestClient, session_factory: SessionFactory, mailer: RecordingMailer
) -> None:
    first = ready_user(client, session_factory, mailer, device_name="desk")
    # A second sign-in for the same account makes a second device.
    second = ready_user(client, session_factory, mailer, device_name="laptop")
    response = client.patch(
        "/v1/me/device", json={"device_name": "Renamed 2"}, headers=second.headers
    )
    assert response.status_code == 204
    with session_factory() as s:
        names = {d.id: d.name for d in s.scalars(select(Device))}
    assert names[second.device_id] == "Renamed 2"
    assert names[first.device_id] == "desk"
    me = client.get("/v1/me", headers=second.headers).json()
    assert me["device_name"] == "Renamed 2"


@pytest.mark.parametrize("name", ["", "x" * 41, "bad/name", "tab\t"])
def test_rename_rejects_invalid_names(
    client: TestClient,
    session_factory: SessionFactory,
    mailer: RecordingMailer,
    name: str,
) -> None:
    ada = ready_user(client, session_factory, mailer, device_name="desk")
    response = client.patch(
        "/v1/me/device", json={"device_name": name}, headers=ada.headers
    )
    assert response.status_code in (400, 422)
    with session_factory() as s:
        assert s.scalars(select(Device.name)).one() == "desk"


def test_rename_needs_a_login(client: TestClient) -> None:
    response = client.patch("/v1/me/device", json={"device_name": "x"})
    assert response.status_code == 401


def test_rename_shares_the_profile_write_limit(
    client: TestClient, session_factory: SessionFactory, mailer: RecordingMailer
) -> None:
    ada = ready_user(client, session_factory, mailer)
    codes = [
        client.patch(
            "/v1/me/device", json={"device_name": f"n{i}"}, headers=ada.headers
        ).status_code
        for i in range(200)
    ]
    assert 429 in codes


def test_a_blocked_pair_is_not_matched_after_both_rename(
    client: TestClient,
    session_factory: SessionFactory,
    mailer: RecordingMailer,
    server_config: ServerConfig,
    clock: FixedClock,
) -> None:
    ada = ready_user(client, session_factory, mailer, "ada@example.com", "Ada")
    bob = ready_user(client, session_factory, mailer, "bob@example.com", "Bob")
    with session_factory() as s:
        s.add(
            Block(blocker_id=ada.user_id, blocked_id=bob.user_id, created_at=START_TIME)
        )
        s.commit()
    for user, nick, device in ((ada, "Ada2", "new-a"), (bob, "Bob2", "new-b")):
        r = client.patch("/v1/me", json={"nickname": nick}, headers=user.headers)
        assert r.status_code == 200
        r = client.patch(
            "/v1/me/device", json={"device_name": device}, headers=user.headers
        )
        assert r.status_code == 204
    notifier = RecordingNotifier()
    for user in (ada, bob):
        notifier.connected.add(user.user_id)
        seed_queued(
            session_factory,
            SeededUser(user.user_id, user.device_id, None),
            now=clock.now,
            ttl_s=90,
            stack_tags=(),
            bucket=None,
            task_category=None,
        )
    mm = Matchmaker(session_factory, server_config, clock, notifier)
    assert mm.run_once() == []
    with session_factory() as s:
        assert {e.status for e in s.scalars(select(QueueEntry))} == {"queued"}

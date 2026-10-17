"""GitHub sign-in leaks nothing it should not keep.

A real sign-in (server app, fake GitHub over real HTTP) with planted values:

- the GitHub access token: never in any table, log line or response;
- the device code: only in its challenge row while the sign-in is open;
- the GitHub numeric id: only in `users` and `beta_invites`; not in logs, not in
  anything sent to the companion except through the account's own export, and
  not in the tombstone (a keyed hash only).

The scanner is the corpus scanner, so URL, JSON, base64 and hex re-encodings are
found too; `test_the_scanner_sees_...` proves it would catch a planted value.
"""

import base64
import dataclasses
import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from breakroom_server.admin import main as admin_main
from breakroom_server.app import create_app
from breakroom_server.config import ServerConfig
from breakroom_server.db import make_engine
from breakroom_server.mailer import RecordingMailer
from breakroom_server.models import (
    Base,
    BetaInvite,
    DeletedAccountTombstone,
    Device,
    User,
)
from breakroom_server.tokens import github_tombstone_hmac
from tests.fixtures.privacy_corpus import scan
from tests.server.conftest import TEST_SECRET, FixedClock
from tests.server.github_fake import DEVICE_CODE, FakeGitHub, fake_github

__all__ = ["fake_github"]

TOKEN = "gho_breakroom-canary-token-8841"
GITHUB_ID = 987_654_321_012  # past 32 bits and unmistakable in text
USERNAME = "canary-octo-user"


@dataclasses.dataclass
class World:
    client: TestClient
    fake: FakeGitHub
    clock: FixedClock
    factory: Any
    url: str
    responses: list[bytes]


@pytest.fixture
def world(
    tmp_path: Path,
    fake_github: FakeGitHub,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> Iterator[World]:
    fake_github.token = TOKEN
    fake_github.token_user_id = GITHUB_ID
    fake_github.users[USERNAME] = GITHUB_ID
    url = f"sqlite:///{tmp_path / 'privacy.db'}"
    engine = make_engine(url)
    Base.metadata.create_all(engine)
    config = ServerConfig(
        database_url=url,
        secret=TEST_SECRET,
        mailer="record",
        github_client_id=fake_github.client_id,
        github_oauth_url=fake_github.url,
        github_api_url=fake_github.url,
        github_timeout_s=3,
    )
    clock = FixedClock()
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("BREAKROOM_DATABASE_URL", url)
    monkeypatch.setenv(
        "BREAKROOM_SERVER_SECRET", base64.urlsafe_b64encode(TEST_SECRET).decode()
    )
    monkeypatch.setenv("BREAKROOM_MAILER", "record")
    monkeypatch.setenv("BREAKROOM_GITHUB_CLIENT_ID", fake_github.client_id)
    monkeypatch.setenv("BREAKROOM_GITHUB_OAUTH_URL", fake_github.url)
    monkeypatch.setenv("BREAKROOM_GITHUB_API_URL", fake_github.url)
    app = create_app(
        config, mailer=RecordingMailer(), clock=clock, run_matchmaker=False
    )
    with TestClient(app) as client:
        yield World(
            client,
            fake_github,
            clock,
            sessionmaker(engine, expire_on_commit=False),
            url,
            [],
        )
    engine.dispose()


def dump(world: World) -> dict[str, list[bytes]]:
    out: dict[str, list[bytes]] = {}
    with world.factory() as session:
        for table in Base.metadata.sorted_tables:
            blobs: list[bytes] = []
            for row in session.execute(select(table)).all():
                for value in row:
                    if isinstance(value, bytes):
                        blobs += [value, value.hex().encode()]
                    elif value is not None:
                        blobs.append(str(value).encode("utf-8"))
            out[f"db.{table.name}"] = blobs
    return out


def sign_in(world: World) -> None:
    """Invite by username through the admin CLI, then sign in over HTTP."""
    assert (
        admin_main(["invite", "create", "--github", USERNAME], clock=world.clock) == 0
    )
    started = world.client.post("/v1/auth/github/start")
    world.responses.append(started.content)
    challenge_id = started.json()["challenge_id"]
    world.fake.approve()
    world.clock.advance(world.fake.interval)
    polled = world.client.post(
        "/v1/auth/github/poll",
        json={"challenge_id": challenge_id, "device_name": "laptop"},
    )
    assert polled.status_code == 200, polled.text
    world.responses += [polled.content, str(polled.headers).encode()]
    access = polled.json()["session"]["tokens"]["access_token"]
    me = world.client.get("/v1/me", headers={"Authorization": f"Bearer {access}"})
    world.responses.append(me.content)


def channels(world: World, caplog: pytest.LogCaptureFixture) -> dict[str, list[bytes]]:
    return {
        "http.responses": world.responses,
        "server_logs": [caplog.text.encode()],
    } | dump(world)


def test_the_token_and_device_code_are_stored_and_sent_nowhere(
    world: World,
    caplog: pytest.LogCaptureFixture,
    capfd: pytest.CaptureFixture[str],
) -> None:
    sign_in(world)
    seen = channels(world, caplog) | {"console": [capfd.readouterr().out.encode()]}
    values = [("token", TOKEN), ("device_code", DEVICE_CODE)]
    assert scan.find_leaks(seen, values) == []


def test_the_github_id_and_username_stay_in_their_two_tables(
    world: World,
    caplog: pytest.LogCaptureFixture,
    capfd: pytest.CaptureFixture[str],
) -> None:
    sign_in(world)
    seen = channels(world, caplog) | {"console": [capfd.readouterr().out.encode()]}
    hits = scan.find_leaks(seen, [("github_id", str(GITHUB_ID))])
    assert sorted(hits) == [
        "db.beta_invites: github_id",
        "db.users: github_id",
    ]
    # The username is only ever used to find the id; it is stored nowhere.
    assert scan.find_leaks(seen, [("username", USERNAME)]) == []


def test_the_device_code_sits_in_its_row_only_while_the_sign_in_is_open(
    world: World,
) -> None:
    world.client.post("/v1/auth/github/start")
    assert scan.find_leaks(dump(world), [("code", DEVICE_CODE)]) == [
        "db.auth_challenges: code"
    ]


def test_a_deleted_disabled_github_account_leaves_a_keyed_hash_and_no_id(
    world: World,
    caplog: pytest.LogCaptureFixture,
    capfd: pytest.CaptureFixture[str],
) -> None:
    sign_in(world)
    with world.factory() as s:
        user = s.scalars(select(User)).one()
        user.status = "disabled"
        s.commit()
    capfd.readouterr()
    argv = [
        "account",
        "delete",
        "--github-id",
        str(GITHUB_ID),
        "--yes",
        "--disabled-ok",
    ]
    assert admin_main(argv, clock=world.clock) == 0
    out = capfd.readouterr()
    assert (
        scan.find_leaks(
            {"admin": [(out.out + out.err).encode()]}, [("id", str(GITHUB_ID))]
        )
        == []
    )
    assert scan.find_leaks(dump(world), [("id", str(GITHUB_ID))]) == []
    with world.factory() as s:
        (row,) = s.scalars(select(DeletedAccountTombstone)).all()
        assert row.email_hmac == github_tombstone_hmac(TEST_SECRET, GITHUB_ID)
        assert s.scalars(select(BetaInvite)).first() is None


def test_the_scanner_sees_planted_values_in_the_database_and_the_logs(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    sign_in(world)
    with world.factory() as s:
        user_id = s.scalars(select(User.id)).one()
        s.add(Device(user_id=user_id, name=TOKEN, created_at=datetime.now(UTC)))
        s.commit()
    logging.getLogger("breakroom_server.test").warning("leaked %s", TOKEN)
    hits = scan.find_leaks(channels(world, caplog), [("token", TOKEN)])
    assert sorted(hits) == ["db.devices: token", "server_logs: token"]

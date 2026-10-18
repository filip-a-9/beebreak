"""GitHub sign-in through the HTTP API, against a fake GitHub (real HTTP)."""

import dataclasses
import threading
from collections.abc import Iterator
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, text

from beebreak_server.app import create_app
from beebreak_server.config import ServerConfig
from beebreak_server.db import SessionFactory
from beebreak_server.mailer import RecordingMailer
from beebreak_server.models import AuthChallenge, BetaInvite, Device, User
from tests.server.conftest import START_TIME, FixedClock, bearer, sign_in
from tests.server.github_fake import ACCESS_TOKEN, DEVICE_CODE, FakeGitHub, fake_github

__all__ = ["fake_github"]

GITHUB_ID = 4242
PEER_ID = 777_000_111


@pytest.fixture
def github_config(server_config: ServerConfig, fake_github: FakeGitHub) -> ServerConfig:
    return dataclasses.replace(
        server_config,
        github_client_id=fake_github.client_id,
        github_oauth_url=fake_github.url,
        github_api_url=fake_github.url,
        github_timeout_s=3,
    )


@pytest.fixture
def gh(
    github_config: ServerConfig,
    engine: Engine,
    mailer: RecordingMailer,
    clock: FixedClock,
) -> Iterator[TestClient]:
    app = create_app(github_config, mailer=mailer, clock=clock, run_matchmaker=False)
    with TestClient(app) as client:
        yield client


def add_github_invite(
    factory: SessionFactory,
    github_id: int = GITHUB_ID,
    *,
    ttl_s: int = 604_800,
    revoked: bool = False,
) -> UUID:
    with factory() as s:
        invite = BetaInvite(
            github_id=github_id,
            expires_at=START_TIME + timedelta(seconds=ttl_s),
            revoked_at=START_TIME if revoked else None,
            created_at=START_TIME,
        )
        s.add(invite)
        s.commit()
        return invite.id


def start(client: TestClient) -> dict[str, Any]:
    response = client.post("/v1/auth/github/start")
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def poll(client: TestClient, challenge_id: str, device_name: str = "laptop") -> Any:
    return client.post(
        "/v1/auth/github/poll",
        json={"challenge_id": challenge_id, "device_name": device_name},
    )


def finish(
    client: TestClient, fake: FakeGitHub, clock: FixedClock, challenge_id: str
) -> Any:
    """The user approved on GitHub; the companion polls once the interval is up."""
    fake.approve()
    clock.advance(fake.interval)
    return poll(client, challenge_id)


# --- which methods are on -------------------------------------------------------


def test_methods_lists_what_the_server_offers(gh: TestClient) -> None:
    response = gh.get("/v1/auth/methods")
    assert (response.status_code, response.json()) == (
        200,
        {"email": True, "github": True},
    )


def test_a_github_only_server_says_so_and_refuses_email_routes(
    github_config: ServerConfig,
    engine: Engine,
    mailer: RecordingMailer,
    clock: FixedClock,
) -> None:
    config = dataclasses.replace(github_config, smtp_host="")
    with TestClient(create_app(config, clock=clock, run_matchmaker=False)) as client:
        assert client.get("/v1/auth/methods").json() == {
            "email": False,
            "github": True,
        }
        started = client.post("/v1/auth/start", json={"email": "ada@example.com"})
        assert (started.status_code, started.json()) == (404, {"error": "not_found"})
        verified = client.post(
            "/v1/auth/verify",
            json={"challenge_id": str(uuid4()), "code": "ABCD2345", "device_name": "x"},
        )
        assert verified.status_code == 404
        # No mail provider was set up: nothing is sending.
        assert "beebreak-mailer" not in {t.name for t in threading.enumerate()}


def test_an_email_only_server_has_no_github_routes(
    server_config: ServerConfig,
    engine: Engine,
    mailer: RecordingMailer,
    clock: FixedClock,
) -> None:
    app = create_app(server_config, mailer=mailer, clock=clock, run_matchmaker=False)
    with TestClient(app) as client:
        assert client.get("/v1/auth/methods").json() == {
            "email": True,
            "github": False,
        }
        assert client.post("/v1/auth/github/start").status_code == 404
        body = {"challenge_id": str(uuid4()), "device_name": "x"}
        assert client.post("/v1/auth/github/poll", json=body).status_code == 404


# --- start -----------------------------------------------------------------------


def test_start_returns_the_code_to_show_and_keeps_the_device_code_server_side(
    gh: TestClient,
    fake_github: FakeGitHub,
    session_factory: SessionFactory,
) -> None:
    response = gh.post("/v1/auth/github/start")
    body = response.json()
    assert set(body) == {
        "challenge_id",
        "user_code",
        "verification_uri",
        "expires_in_s",
        "interval_s",
    }
    assert body["verification_uri"] == f"{fake_github.url}/login/device"
    assert (body["expires_in_s"], body["interval_s"]) == (900, 5)
    assert DEVICE_CODE not in response.text
    (call,) = fake_github.calls("/login/device/code")
    assert call.form == {"client_id": fake_github.client_id}  # no scope
    with session_factory() as s:
        challenge = s.get(AuthChallenge, UUID(body["challenge_id"]))
    assert challenge is not None
    assert challenge.github_device_code == DEVICE_CODE
    assert challenge.email_canonical is None and challenge.code_hmac is None
    assert challenge.expires_at == START_TIME + timedelta(seconds=900)


def test_start_when_github_fails_is_502_and_stores_nothing(
    gh: TestClient, fake_github: FakeGitHub, session_factory: SessionFactory
) -> None:
    fake_github.device_code_status = 503
    response = gh.post("/v1/auth/github/start")
    assert (response.status_code, response.json()) == (
        502,
        {"error": "github_unavailable"},
    )
    with session_factory() as s:
        assert s.scalars(select(AuthChallenge)).first() is None


def test_start_is_rate_limited_per_address(
    github_config: ServerConfig,
    engine: Engine,
    mailer: RecordingMailer,
    clock: FixedClock,
) -> None:
    config = dataclasses.replace(github_config, rate_start_ip=2)
    app = create_app(config, mailer=mailer, clock=clock, run_matchmaker=False)
    with TestClient(app) as client:
        assert client.post("/v1/auth/github/start").status_code == 200
        assert client.post("/v1/auth/github/start").status_code == 200
        limited = client.post("/v1/auth/github/start")
    assert (limited.status_code, limited.json()) == (429, {"error": "rate_limited"})
    assert "Retry-After" in limited.headers


# --- polling ---------------------------------------------------------------------


def test_polling_inside_the_interval_does_not_reach_github(
    gh: TestClient, fake_github: FakeGitHub, clock: FixedClock
) -> None:
    challenge_id = start(gh)["challenge_id"]
    clock.advance(4)
    response = poll(gh, challenge_id)
    assert (response.status_code, response.json()) == (
        200,
        {"status": "pending", "interval_s": 5, "session": None},
    )
    assert fake_github.calls("/login/oauth/access_token") == []


def test_polling_once_the_interval_is_up_asks_github_and_stays_pending(
    gh: TestClient, fake_github: FakeGitHub, clock: FixedClock
) -> None:
    challenge_id = start(gh)["challenge_id"]
    clock.advance(5)
    assert poll(gh, challenge_id).json()["status"] == "pending"
    assert len(fake_github.calls("/login/oauth/access_token")) == 1
    # Asking again at once is inside the new interval: GitHub is not asked.
    assert poll(gh, challenge_id).json()["status"] == "pending"
    assert len(fake_github.calls("/login/oauth/access_token")) == 1
    clock.advance(5)
    poll(gh, challenge_id)
    assert len(fake_github.calls("/login/oauth/access_token")) == 2


def test_slow_down_adds_five_seconds_to_the_interval(
    gh: TestClient, fake_github: FakeGitHub, clock: FixedClock
) -> None:
    challenge_id = start(gh)["challenge_id"]
    fake_github.script_poll({"error": "slow_down"})  # an answer without an interval
    clock.advance(5)
    assert poll(gh, challenge_id).json()["interval_s"] == 10
    clock.advance(9)
    poll(gh, challenge_id)
    assert len(fake_github.calls("/login/oauth/access_token")) == 1  # still waiting
    clock.advance(1)
    poll(gh, challenge_id)
    assert len(fake_github.calls("/login/oauth/access_token")) == 2


def test_slow_down_with_an_interval_uses_githubs_number(
    gh: TestClient, fake_github: FakeGitHub, clock: FixedClock
) -> None:
    challenge_id = start(gh)["challenge_id"]
    fake_github.script_poll({"error": "slow_down", "interval": 20})
    clock.advance(5)
    assert poll(gh, challenge_id).json()["interval_s"] == 20


def test_expired_token_ends_the_attempt(
    gh: TestClient, fake_github: FakeGitHub, clock: FixedClock
) -> None:
    challenge_id = start(gh)["challenge_id"]
    fake_github.script_poll({"error": "expired_token"})
    clock.advance(5)
    response = poll(gh, challenge_id)
    assert (response.status_code, response.json()) == (400, {"error": "expired"})
    assert poll(gh, challenge_id).json() == {"error": "invalid_code"}  # used up


def test_access_denied_ends_the_attempt_and_clears_the_device_code(
    gh: TestClient,
    fake_github: FakeGitHub,
    clock: FixedClock,
    session_factory: SessionFactory,
) -> None:
    challenge_id = start(gh)["challenge_id"]
    fake_github.script_poll({"error": "access_denied"})
    clock.advance(5)
    response = poll(gh, challenge_id)
    assert (response.status_code, response.json()) == (400, {"error": "access_denied"})
    with session_factory() as s:
        challenge = s.get(AuthChallenge, UUID(challenge_id))
        assert challenge is not None and challenge.github_device_code is None
    assert poll(gh, challenge_id).status_code == 400


def test_our_own_deadline_ends_the_attempt(gh: TestClient, clock: FixedClock) -> None:
    challenge_id = start(gh)["challenge_id"]
    clock.advance(901)
    response = poll(gh, challenge_id)
    assert (response.status_code, response.json()) == (400, {"error": "expired"})


def test_a_setup_problem_at_github_is_502(
    gh: TestClient, fake_github: FakeGitHub, clock: FixedClock
) -> None:
    challenge_id = start(gh)["challenge_id"]
    fake_github.script_poll({"error": "device_flow_disabled"})
    clock.advance(5)
    response = poll(gh, challenge_id)
    assert (response.status_code, response.json()) == (
        502,
        {"error": "github_unavailable"},
    )


def test_unknown_and_email_challenges_are_invalid_code(
    gh: TestClient,
    session_factory: SessionFactory,
    mailer: RecordingMailer,
) -> None:
    assert poll(gh, str(uuid4())).json() == {"error": "invalid_code"}
    started = gh.post("/v1/auth/start", json={"email": "ada@example.com"})
    assert poll(gh, started.json()["challenge_id"]).json() == {"error": "invalid_code"}


def test_a_github_challenge_cannot_be_verified_as_an_email_code(
    gh: TestClient,
) -> None:
    challenge_id = start(gh)["challenge_id"]
    for code in ("ABCD2345", "00000000"):
        response = gh.post(
            "/v1/auth/verify",
            json={"challenge_id": challenge_id, "code": code, "device_name": "x"},
        )
        assert (response.status_code, response.json()) == (
            400,
            {"error": "invalid_code"},
        )


def test_a_bad_device_name_is_invalid_request(gh: TestClient) -> None:
    challenge_id = start(gh)["challenge_id"]
    assert poll(gh, challenge_id, device_name="no/slash").status_code == 400


# --- completing ------------------------------------------------------------------


def test_an_invited_github_user_signs_in_and_the_tokens_work(
    gh: TestClient,
    fake_github: FakeGitHub,
    clock: FixedClock,
    session_factory: SessionFactory,
) -> None:
    invite_id = add_github_invite(session_factory)
    challenge_id = start(gh)["challenge_id"]
    response = finish(gh, fake_github, clock, challenge_id)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "complete" and body["interval_s"] is None
    session = body["session"]
    assert gh.get("/v1/me", headers=bearer(session)).status_code == 200

    # GET /user was asked once, with the token; the token went nowhere else.
    (user_call,) = fake_github.calls("/user")
    assert user_call.headers["authorization"] == f"Bearer {ACCESS_TOKEN}"
    assert ACCESS_TOKEN not in response.text

    with session_factory() as s:
        user = s.scalars(select(User)).one()
        assert (user.github_id, user.email_canonical) == (GITHUB_ID, None)
        invite = s.get(BetaInvite, invite_id)
        assert invite is not None
        assert invite.redeemed_by == user.id and invite.redeemed_at is not None
        challenge = s.get(AuthChallenge, UUID(challenge_id))
        assert challenge is not None
        assert challenge.consumed_at is not None
        assert challenge.github_device_code is None
        device = s.scalars(select(Device)).one()
        assert device.name == "laptop" and device.user_id == user.id


def test_the_access_token_is_nowhere_in_the_database_or_the_logs(
    gh: TestClient,
    fake_github: FakeGitHub,
    clock: FixedClock,
    engine: Engine,
    session_factory: SessionFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    add_github_invite(session_factory)
    challenge_id = start(gh)["challenge_id"]
    assert finish(gh, fake_github, clock, challenge_id).status_code == 200
    dump = ""
    with engine.connect() as conn:
        for (table,) in conn.execute(
            text("SELECT name FROM sqlite_master WHERE type = 'table'")
            if engine.dialect.name == "sqlite"
            else text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        ):
            for row in conn.execute(text(f'SELECT * FROM "{table}"')):
                dump += repr(row)
    assert ACCESS_TOKEN not in dump
    assert DEVICE_CODE not in dump  # cleared once the challenge was used
    assert ACCESS_TOKEN not in caplog.text and DEVICE_CODE not in caplog.text
    assert str(GITHUB_ID) not in caplog.text.replace("4242ms", "")  # no id in logs


def test_without_an_invite_a_new_github_user_is_not_invited(
    gh: TestClient,
    fake_github: FakeGitHub,
    clock: FixedClock,
    session_factory: SessionFactory,
) -> None:
    add_github_invite(session_factory, PEER_ID)  # someone else's invite
    challenge_id = start(gh)["challenge_id"]
    response = finish(gh, fake_github, clock, challenge_id)
    assert (response.status_code, response.json()) == (403, {"error": "not_invited"})
    with session_factory() as s:
        assert s.scalars(select(User)).first() is None
        assert s.scalars(select(Device)).first() is None
        invite = s.scalars(select(BetaInvite)).one()
        assert invite.redeemed_at is None
    assert poll(gh, challenge_id).json() == {"error": "invalid_code"}  # used up


@pytest.mark.parametrize("case", ["expired", "revoked", "redeemed"])
def test_a_dead_invite_does_not_count(
    gh: TestClient,
    fake_github: FakeGitHub,
    clock: FixedClock,
    session_factory: SessionFactory,
    case: str,
) -> None:
    invite_id = add_github_invite(
        session_factory, ttl_s=1 if case == "expired" else 604_800
    )
    with session_factory() as s:
        invite = s.get(BetaInvite, invite_id)
        assert invite is not None
        if case == "revoked":
            invite.revoked_at = START_TIME
        if case == "redeemed":
            invite.redeemed_at = START_TIME
        s.commit()
    response = finish(gh, fake_github, clock, start(gh)["challenge_id"])
    assert response.status_code == 403


def test_an_existing_account_signs_in_again_without_an_invite(
    gh: TestClient,
    fake_github: FakeGitHub,
    clock: FixedClock,
    session_factory: SessionFactory,
) -> None:
    add_github_invite(session_factory)
    first = finish(gh, fake_github, clock, start(gh)["challenge_id"])
    assert first.status_code == 200
    second = finish(gh, fake_github, clock, start(gh)["challenge_id"])
    assert second.status_code == 200
    with session_factory() as s:
        assert len(s.scalars(select(User)).all()) == 1
        assert len(s.scalars(select(Device)).all()) == 2


def test_a_disabled_github_account_cannot_sign_in(
    gh: TestClient,
    fake_github: FakeGitHub,
    clock: FixedClock,
    session_factory: SessionFactory,
) -> None:
    with session_factory() as s:
        s.add(User(github_id=GITHUB_ID, status="disabled", created_at=START_TIME))
        s.commit()
    response = finish(gh, fake_github, clock, start(gh)["challenge_id"])
    assert (response.status_code, response.json()) == (403, {"error": "not_invited"})


def test_an_email_account_is_not_reached_by_a_github_sign_in(
    gh: TestClient,
    fake_github: FakeGitHub,
    clock: FixedClock,
    session_factory: SessionFactory,
    mailer: RecordingMailer,
) -> None:
    """No linking: the same person with GitHub is a different, uninvited account."""
    sign_in(gh, session_factory, mailer, "ada@example.com")
    response = finish(gh, fake_github, clock, start(gh)["challenge_id"])
    assert response.status_code == 403


def test_a_failure_of_the_user_lookup_is_502_and_creates_nothing(
    gh: TestClient,
    fake_github: FakeGitHub,
    clock: FixedClock,
    session_factory: SessionFactory,
) -> None:
    add_github_invite(session_factory)
    fake_github.user_status = 500
    response = finish(gh, fake_github, clock, start(gh)["challenge_id"])
    assert (response.status_code, response.json()) == (
        502,
        {"error": "github_unavailable"},
    )
    with session_factory() as s:
        assert s.scalars(select(User)).first() is None
        assert s.scalars(select(BetaInvite)).one().redeemed_at is None


def test_a_github_user_can_use_the_whole_api_like_any_other(
    gh: TestClient,
    fake_github: FakeGitHub,
    clock: FixedClock,
    session_factory: SessionFactory,
) -> None:
    add_github_invite(session_factory)
    session = finish(gh, fake_github, clock, start(gh)["challenge_id"]).json()[
        "session"
    ]
    headers = bearer(session)
    assert (
        gh.patch("/v1/me", json={"nickname": "octo"}, headers=headers).status_code
        == 200
    )
    refreshed = gh.post(
        "/v1/auth/refresh", json={"refresh_token": session["tokens"]["refresh_token"]}
    )
    assert refreshed.status_code == 200
    fresh = {"Authorization": f"Bearer {refreshed.json()['access_token']}"}
    assert gh.post("/v1/auth/logout", headers=fresh).status_code == 204

import dataclasses
import threading
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select

from breakroom_server.app import app_from_env, create_app
from breakroom_server.auth import ApiError, verify_challenge
from breakroom_server.config import ServerConfig
from breakroom_server.db import SessionFactory
from breakroom_server.mailer import RecordingMailer
from breakroom_server.models import (
    AuthChallenge,
    BetaInvite,
    Device,
    User,
)
from breakroom_server.tokens import hmac_code, normalize_code
from tests.server.conftest import (
    START_TIME,
    TEST_SECRET,
    FixedClock,
    add_invite,
    bearer,
    sign_in,
)

EMAIL = "ada@example.com"
OK_START = {"email": EMAIL}


def start(client: TestClient, email: str = EMAIL, invite: str | None = None) -> Any:
    body: dict[str, Any] = {"email": email}
    if invite is not None:
        body["invite_code"] = invite
    return client.post("/v1/auth/start", json=body)


def verify(client: TestClient, challenge_id: str, code: str) -> Any:
    return client.post(
        "/v1/auth/verify",
        json={"challenge_id": challenge_id, "code": code, "device_name": "laptop"},
    )


def make_client(
    server_config: ServerConfig,
    mailer: RecordingMailer,
    clock: FixedClock,
    **overrides: Any,
) -> TestClient:
    config = dataclasses.replace(server_config, **overrides)
    return TestClient(create_app(config, mailer=mailer, clock=clock))


def test_health(client: TestClient) -> None:
    response = client.get("/v1/health")
    assert (response.status_code, response.json()) == (200, {"status": "ok"})


def test_invite_flow_creates_user_redeems_invite_and_tokens_work(
    client: TestClient,
    session_factory: SessionFactory,
    mailer: RecordingMailer,
) -> None:
    add_invite(session_factory, EMAIL)
    response = start(client, invite="ABCD2345")
    assert response.status_code == 202
    challenge_id = response.json()["challenge_id"]
    UUID(challenge_id)
    assert len(mailer.sent) == 1
    kind, to, code = mailer.sent[0]
    assert (kind, to) == ("code", EMAIL)
    assert normalize_code(code) == code and len(code) == 8

    verified = verify(client, challenge_id, code)
    assert verified.status_code == 200
    body = verified.json()
    assert body["me"] == {"nickname": None, "consent": None, "consented_at": None}
    assert body["tokens"]["access_token"].startswith("bra_")
    assert body["tokens"]["refresh_token"].startswith("brr_")

    with session_factory() as s:
        user = s.scalars(select(User)).one()
        invite = s.scalars(select(BetaInvite)).one()
        device = s.scalars(select(Device)).one()
    assert user.email_canonical == EMAIL and user.status == "active"
    assert invite.redeemed_at == START_TIME and invite.redeemed_by == user.id
    assert device.user_id == user.id and device.name == "laptop"

    assert client.get("/v1/me", headers=bearer(body)).status_code == 200


def test_returning_user_gets_a_code_and_a_second_device(
    client: TestClient, session_factory: SessionFactory, mailer: RecordingMailer
) -> None:
    first = sign_in(client, session_factory, mailer, device_name="laptop")
    second = sign_in(client, session_factory, mailer, device_name="desktop")
    assert first["tokens"]["access_token"] != second["tokens"]["access_token"]
    with session_factory() as s:
        assert {d.name for d in s.scalars(select(Device))} == {"laptop", "desktop"}
        assert len(s.scalars(select(User)).all()) == 1


@pytest.mark.parametrize(
    "scenario", ["unknown", "disabled", "wrong_invite", "expired_invite", "other_email"]
)
def test_start_does_not_reveal_account_or_invite_state(
    scenario: str,
    client: TestClient,
    session_factory: SessionFactory,
    mailer: RecordingMailer,
    clock: FixedClock,
) -> None:
    invite: str | None = None
    if scenario == "disabled":
        with session_factory() as s:
            s.add(User(email_canonical=EMAIL, status="disabled", created_at=START_TIME))
            s.commit()
    elif scenario == "wrong_invite":
        add_invite(session_factory, EMAIL)
        invite = "ZZZZ2345"
    elif scenario == "expired_invite":
        add_invite(session_factory, EMAIL, ttl_s=60)
        clock.advance(61)
        invite = "ABCD2345"
    elif scenario == "other_email":
        add_invite(session_factory, "someone.else@example.com")
        invite = "ABCD2345"

    response = start(client, invite=invite)
    assert response.status_code == 202
    assert set(response.json()) == {"challenge_id"}
    UUID(response.json()["challenge_id"])
    assert mailer.sent == []
    with session_factory() as s:
        assert s.scalars(select(AuthChallenge)).one().consumed_at is None


def test_garbage_invite_code_is_just_no_email(
    client: TestClient, mailer: RecordingMailer
) -> None:
    assert start(client, invite="!!!!!!!!").status_code == 202
    assert mailer.sent == []


@pytest.mark.parametrize("email", ["nope", "a@b@c.de", "", "x" * 300 + "@a.bc"])
def test_start_rejects_bad_email(client: TestClient, email: str) -> None:
    response = start(client, email)
    assert (response.status_code, response.json()) == (
        400,
        {"error": "invalid_request"},
    )


def test_wrong_code_five_times_locks_out_even_the_right_code(
    client: TestClient, session_factory: SessionFactory, mailer: RecordingMailer
) -> None:
    sign_in(client, session_factory, mailer)  # create the user
    challenge_id = start(client).json()["challenge_id"]
    code = mailer.sent[-1][2]
    wrong = "0" * 8 if code != "0" * 8 else "1" * 8
    for _ in range(5):
        response = verify(client, challenge_id, wrong)
        assert (response.status_code, response.json()) == (
            400,
            {"error": "invalid_code"},
        )
    response = verify(client, challenge_id, code)
    assert (response.status_code, response.json()) == (400, {"error": "invalid_code"})
    with session_factory() as s:
        assert s.scalars(select(AuthChallenge)).all()[-1].attempts == 5


def test_attempt_cap_holds_under_parallel_guesses(
    client: TestClient,
    server_config: ServerConfig,
    session_factory: SessionFactory,
    mailer: RecordingMailer,
) -> None:
    sign_in(client, session_factory, mailer)
    challenge_id = UUID(start(client).json()["challenge_id"])
    right = mailer.sent[-1][2]
    wrong = "0" * 8 if right != "0" * 8 else "1" * 8
    cap = server_config.code_max_attempts
    with session_factory() as s:
        challenge = s.get(AuthChallenge, challenge_id)
        assert challenge is not None
        challenge.attempts = cap - 1
        s.commit()

    guessers = 8
    barrier = threading.Barrier(guessers)
    outcomes: list[str] = []

    def guess() -> None:
        barrier.wait(timeout=5)
        with session_factory() as s:
            try:
                verify_challenge(
                    s,
                    server_config,
                    START_TIME,
                    challenge_id=challenge_id,
                    raw_code=wrong,
                    device_name="laptop",
                )
            except ApiError as error:
                outcomes.append(error.code)

    threads = [threading.Thread(target=guess) for _ in range(guessers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert outcomes == ["invalid_code"] * guessers
    with session_factory() as s:
        locked = s.get(AuthChallenge, challenge_id)
        assert locked is not None and locked.attempts == cap
    assert verify(client, str(challenge_id), right).json() == {"error": "invalid_code"}


def test_malformed_code_counts_as_an_attempt(
    client: TestClient, session_factory: SessionFactory, mailer: RecordingMailer
) -> None:
    sign_in(client, session_factory, mailer)
    challenge_id = start(client).json()["challenge_id"]
    assert verify(client, challenge_id, "UUUUUUUU").json() == {"error": "invalid_code"}
    with session_factory() as s:
        assert max(c.attempts for c in s.scalars(select(AuthChallenge))) == 1


def test_code_expires_after_code_ttl(
    client: TestClient,
    session_factory: SessionFactory,
    mailer: RecordingMailer,
    clock: FixedClock,
    server_config: ServerConfig,
) -> None:
    sign_in(client, session_factory, mailer)
    challenge_id = start(client).json()["challenge_id"]
    code = mailer.sent[-1][2]
    clock.advance(server_config.code_ttl_s)
    response = verify(client, challenge_id, code)
    assert (response.status_code, response.json()) == (400, {"error": "expired"})


def test_code_is_still_valid_just_before_expiry(
    client: TestClient,
    session_factory: SessionFactory,
    mailer: RecordingMailer,
    clock: FixedClock,
    server_config: ServerConfig,
) -> None:
    sign_in(client, session_factory, mailer)
    challenge_id = start(client).json()["challenge_id"]
    code = mailer.sent[-1][2]
    clock.advance(server_config.code_ttl_s - 1)
    assert verify(client, challenge_id, code).status_code == 200


def test_second_start_invalidates_the_first_code(
    client: TestClient, session_factory: SessionFactory, mailer: RecordingMailer
) -> None:
    sign_in(client, session_factory, mailer)
    first = start(client).json()["challenge_id"]
    first_code = mailer.sent[-1][2]
    second = start(client).json()["challenge_id"]
    second_code = mailer.sent[-1][2]
    assert first != second
    assert verify(client, first, first_code).json() == {"error": "invalid_code"}
    assert verify(client, second, second_code).status_code == 200


def test_code_is_single_use(
    client: TestClient, session_factory: SessionFactory, mailer: RecordingMailer
) -> None:
    sign_in(client, session_factory, mailer)
    challenge_id = start(client).json()["challenge_id"]
    code = mailer.sent[-1][2]
    assert verify(client, challenge_id, code).status_code == 200
    assert verify(client, challenge_id, code).json() == {"error": "invalid_code"}


def test_code_accepts_lowercase_and_hyphen(
    client: TestClient, session_factory: SessionFactory, mailer: RecordingMailer
) -> None:
    sign_in(client, session_factory, mailer)
    challenge_id = start(client).json()["challenge_id"]
    code = mailer.sent[-1][2]
    typed = f"{code[:4]}-{code[4:]}".lower()
    assert verify(client, challenge_id, typed).status_code == 200


def test_unknown_challenge_is_invalid_code(client: TestClient) -> None:
    response = verify(client, str(uuid4()), "ABCD2345")
    assert (response.status_code, response.json()) == (400, {"error": "invalid_code"})


def test_code_for_a_user_disabled_meanwhile_is_rejected(
    client: TestClient, session_factory: SessionFactory, mailer: RecordingMailer
) -> None:
    sign_in(client, session_factory, mailer)
    challenge_id = start(client).json()["challenge_id"]
    code = mailer.sent[-1][2]
    with session_factory() as s:
        s.scalars(select(User)).one().status = "disabled"
        s.commit()
    assert verify(client, challenge_id, code).json() == {"error": "invalid_code"}


def test_invite_redeemed_concurrently_loses(
    client: TestClient,
    session_factory: SessionFactory,
    mailer: RecordingMailer,
) -> None:
    add_invite(session_factory, EMAIL)
    first_id = start(client, invite="ABCD2345").json()["challenge_id"]
    first_code = mailer.sent[-1][2]
    # A second live challenge on the same invite (a start would have consumed
    # the first), as a concurrent request would have created it.
    with session_factory() as s:
        invite_id = s.scalars(select(BetaInvite)).one().id
        known = hmac_code(TEST_SECRET, "23456789")
        s.add(
            AuthChallenge(
                email_canonical=EMAIL,
                code_hmac=known,
                invite_id=invite_id,
                expires_at=START_TIME + timedelta(minutes=10),
                created_at=START_TIME,
            )
        )
        s.commit()
        second_id = str(
            s.scalars(select(AuthChallenge).where(AuthChallenge.code_hmac == known))
            .one()
            .id
        )
    # start() above consumed nothing for the second row, but verifying the
    # first (still live) challenge redeems the invite ...
    assert verify(client, first_id, first_code).status_code == 200
    # ... so the second one loses.
    response = verify(client, second_id, "23456789")
    assert (response.status_code, response.json()) == (400, {"error": "invalid_code"})
    with session_factory() as s:
        assert len(s.scalars(select(User)).all()) == 1
        assert len(s.scalars(select(Device)).all()) == 1


# --- request handling ------------------------------------------------------------


def test_validation_errors_are_uniform_and_do_not_echo_input(
    client: TestClient,
) -> None:
    response = client.post(
        "/v1/auth/start", json={"email": "secret.person@example.com", "extra": 1}
    )
    assert (response.status_code, response.json()) == (
        400,
        {"error": "invalid_request"},
    )
    assert "secret.person" not in response.text
    assert client.post("/v1/auth/verify", json={}).json() == {
        "error": "invalid_request"
    }
    bad_json = client.post(
        "/v1/auth/start", content=b"{nope", headers={"content-type": "application/json"}
    )
    assert (bad_json.status_code, bad_json.json()) == (
        400,
        {"error": "invalid_request"},
    )


def test_unknown_route_and_method_use_the_error_shape(client: TestClient) -> None:
    assert client.get("/v1/nothing").json() == {"error": "invalid_request"}
    assert client.get("/v1/auth/start").status_code == 405


def test_docs_endpoints_are_off(client: TestClient) -> None:
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_request_body_is_capped_at_16_kib(client: TestClient) -> None:
    big = {"email": "a@example.com", "invite_code": "x" * 17_000}
    response = client.post("/v1/auth/start", json=big)
    assert (response.status_code, response.json()) == (
        400,
        {"error": "invalid_request"},
    )


def test_streamed_body_is_capped_too(client: TestClient) -> None:
    def chunks() -> Any:
        for _ in range(20):
            yield b" " * 1024

    response = client.post(
        "/v1/auth/start", content=chunks(), headers={"content-type": "application/json"}
    )
    assert (response.status_code, response.json()) == (
        400,
        {"error": "invalid_request"},
    )


# --- lifecycle and configuration ------------------------------------------------------


def test_mailer_is_closed_on_shutdown(
    server_config: ServerConfig, engine: object, clock: FixedClock
) -> None:
    class Spy(RecordingMailer):
        closed = 0

        def close(self) -> None:
            Spy.closed += 1

    with TestClient(create_app(server_config, mailer=Spy(), clock=clock)) as c:
        assert c.get("/v1/health").status_code == 200
        assert Spy.closed == 0
    assert Spy.closed == 1


def test_app_state_exposes_what_realtime_needs(app: FastAPI, clock: FixedClock) -> None:
    assert app.state.config.secret == TEST_SECRET
    assert app.state.clock is clock
    with app.state.session_factory() as s:
        assert s.scalars(select(User)).all() == []
    assert app.state.hub.connected_count() == 0


def test_mailer_selection_from_env(
    server_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BREAKROOM_MAILER", "record")
    assert isinstance(app_from_env().state.mailer, RecordingMailer)


def test_default_mailer_is_smtp_with_starttls_flag(
    server_config: ServerConfig, engine: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    created: list[dict[str, Any]] = []

    class FakeSmtp(RecordingMailer):
        def __init__(self, host: str, port: int, mail_from: str, **kw: Any) -> None:
            super().__init__()
            created.append({"host": host, "port": port, **kw})

    monkeypatch.setattr("breakroom_server.app.SmtpMailer", FakeSmtp)
    config = dataclasses.replace(server_config, smtp_starttls=True, smtp_port=2525)
    create_app(config)
    assert created == [{"host": "127.0.0.1", "port": 2525, "starttls": True}]


def test_config_rate_limit_fields_default_and_are_strict(
    server_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = ServerConfig.from_env()
    assert (config.rate_start_email, config.rate_start_ip) == (5, 20)
    assert (config.rate_verify_ip, config.rate_refresh_device) == (30, 30)
    assert config.rate_window_s == 900 and config.mailer == "smtp"
    assert config.smtp_starttls is False

    monkeypatch.setenv("BREAKROOM_RATE_START_EMAIL", "7")
    monkeypatch.setenv("BREAKROOM_SMTP_STARTTLS", "1")
    config = ServerConfig.from_env()
    assert config.rate_start_email == 7 and config.smtp_starttls is True

    for name, bad in [
        ("BREAKROOM_RATE_START_IP", "0"),
        ("BREAKROOM_RATE_VERIFY_IP", "-1"),
        ("BREAKROOM_RATE_REFRESH_DEVICE", "x"),
        ("BREAKROOM_RATE_WINDOW_S", "0"),
        ("BREAKROOM_SMTP_STARTTLS", "yes"),
        ("BREAKROOM_MAILER", "carrier-pigeon"),
    ]:
        with monkeypatch.context() as m:
            m.setenv(name, bad)
            with pytest.raises(ValueError):
                ServerConfig.from_env()


# --- rate limits ---------------------------------------------------------------------


def test_start_is_limited_per_email_with_retry_after(
    client: TestClient, clock: FixedClock
) -> None:
    for _ in range(5):
        assert start(client).status_code == 202
    blocked = start(client)
    assert (blocked.status_code, blocked.json()) == (429, {"error": "rate_limited"})
    assert int(blocked.headers["Retry-After"]) > 0
    # Another address is unaffected; the same one recovers after the window.
    assert start(client, "other@example.com").status_code == 202
    clock.advance(901)
    assert start(client).status_code == 202


def test_start_is_limited_per_client_ip(client: TestClient) -> None:
    for i in range(20):
        assert start(client, f"user{i}@example.com").status_code == 202
    blocked = start(client, "user99@example.com")
    assert blocked.status_code == 429 and "Retry-After" in blocked.headers


def test_start_limit_applies_to_unknown_and_known_emails_alike(
    server_config: ServerConfig,
    engine: object,
    session_factory: SessionFactory,
    mailer: RecordingMailer,
    clock: FixedClock,
) -> None:
    with make_client(server_config, mailer, clock, rate_start_email=2) as client:
        sign_in(client, session_factory, mailer)  # uses 1 hit for EMAIL
        outcomes = {
            email: [start(client, email).status_code for _ in range(3)]
            for email in (EMAIL, "unknown@example.com")
        }
    assert outcomes[EMAIL] == [202, 429, 429]
    assert outcomes["unknown@example.com"] == [202, 202, 429]


def test_verify_is_limited_per_client_ip(client: TestClient) -> None:
    for _ in range(30):
        assert verify(client, str(uuid4()), "ABCD2345").status_code == 400
    blocked = verify(client, str(uuid4()), "ABCD2345")
    assert (blocked.status_code, blocked.json()) == (429, {"error": "rate_limited"})
    assert "Retry-After" in blocked.headers


# --- logging (rule 10) ---------------------------------------------------------------


def test_logs_contain_no_email_code_or_token(
    client: TestClient,
    session_factory: SessionFactory,
    mailer: RecordingMailer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("DEBUG")
    start(client, "nobody@example.com")
    session = sign_in(client, session_factory, mailer, "secret.person@example.com")
    client.get("/v1/me", headers=bearer(session))
    client.post(
        "/v1/auth/refresh", json={"refresh_token": session["tokens"]["refresh_token"]}
    )
    text = caplog.text
    assert "/v1/auth/start" in text and "/v1/me" in text  # something was logged
    assert "example.com" not in text and "secret.person" not in text
    assert "bra_" not in text and "brr_" not in text
    assert "testclient" not in text  # the client address
    assert session["tokens"]["access_token"] not in text


def test_unexpected_error_is_a_bare_500_without_leaking_input(
    app: FastAPI,
    engine: Engine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    AuthChallenge.__table__.drop(engine)  # type: ignore[attr-defined]
    with TestClient(app, raise_server_exceptions=False) as client:
        response = start(client, "leaky.person@example.com")
    assert (response.status_code, response.json()) == (
        500,
        {"error": "invalid_request"},
    )
    assert "leaky.person" not in caplog.text and "example.com" not in caplog.text
    assert "unhandled error" in caplog.text

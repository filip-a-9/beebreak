from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from beebreak_server.db import SessionFactory
from beebreak_server.mailer import RecordingMailer
from beebreak_server.models import Device, User, UserPreference
from tests.server.conftest import START_TIME, FixedClock, bearer, sign_in

CONSENT = {
    "version": 1,
    "share_task_category": True,
    "share_stack_tags": False,
    "share_time_bucket": True,
}


def set_nickname(client: TestClient, session: dict[str, Any], nickname: str) -> Any:
    return client.patch("/v1/me", json={"nickname": nickname}, headers=bearer(session))


def test_me_is_empty_after_first_sign_in(
    client: TestClient, session_factory: SessionFactory, mailer: RecordingMailer
) -> None:
    session = sign_in(client, session_factory, mailer)
    response = client.get("/v1/me", headers=bearer(session))
    assert response.status_code == 200
    assert response.json() == {"nickname": None, "consent": None, "consented_at": None}


def test_set_nickname(
    client: TestClient, session_factory: SessionFactory, mailer: RecordingMailer
) -> None:
    session = sign_in(client, session_factory, mailer)
    response = set_nickname(client, session, "Robin_")
    assert response.status_code == 200 and response.json()["nickname"] == "Robin_"
    assert client.get("/v1/me", headers=bearer(session)).json()["nickname"] == "Robin_"
    with session_factory() as s:
        user = s.scalars(select(User)).one()
    assert (user.nickname, user.nickname_skeleton) == ("Robin_", "robln")


def test_nickname_is_nfkc_normalised(
    client: TestClient, session_factory: SessionFactory, mailer: RecordingMailer
) -> None:
    session = sign_in(client, session_factory, mailer)
    # Fullwidth letters are rejected by the wire pattern before they get here.
    response = set_nickname(client, session, "\uff21\uff22\uff23")
    assert (response.status_code, response.json()) == (
        400,
        {"error": "invalid_request"},
    )


@pytest.mark.parametrize(
    "nickname", ["ab", "x" * 25, "has space", "emoji\U0001f600x", ""]
)
def test_invalid_nickname_is_400(
    nickname: str,
    client: TestClient,
    session_factory: SessionFactory,
    mailer: RecordingMailer,
) -> None:
    session = sign_in(client, session_factory, mailer)
    response = set_nickname(client, session, nickname)
    assert (response.status_code, response.json()) == (
        400,
        {"error": "invalid_request"},
    )


@pytest.mark.parametrize("nickname", ["admin", "Adm1n", "r00t", "Beebreak_Team"])
def test_reserved_nickname_is_409(
    nickname: str,
    client: TestClient,
    session_factory: SessionFactory,
    mailer: RecordingMailer,
) -> None:
    session = sign_in(client, session_factory, mailer)
    response = set_nickname(client, session, nickname)
    assert (response.status_code, response.json()) == (
        409,
        {"error": "nickname_reserved"},
    )


def test_nickname_taken_by_skeleton_and_own_reset_allowed(
    client: TestClient, session_factory: SessionFactory, mailer: RecordingMailer
) -> None:
    first = sign_in(client, session_factory, mailer, "first@example.com")
    second = sign_in(client, session_factory, mailer, "second@example.com")
    assert set_nickname(client, first, "Robin_").status_code == 200

    taken = set_nickname(client, second, "rob1n")
    assert (taken.status_code, taken.json()) == (409, {"error": "nickname_taken"})
    assert client.get("/v1/me", headers=bearer(second)).json()["nickname"] is None

    # The owner may change case or punctuation of their own nickname.
    assert set_nickname(client, first, "ROBIN").status_code == 200
    assert set_nickname(client, first, "ROBIN").status_code == 200
    assert set_nickname(client, second, "Robin_").status_code == 409


def test_concurrent_claim_of_one_skeleton_has_one_winner(
    client: TestClient, session_factory: SessionFactory, mailer: RecordingMailer
) -> None:
    sessions = [
        sign_in(client, session_factory, mailer, f"user{i}@example.com")
        for i in range(2)
    ]
    names = ["Robin_", "rob1n"]
    with ThreadPoolExecutor(2) as pool:
        results = list(
            pool.map(
                lambda a: set_nickname(client, *a), zip(sessions, names, strict=True)
            )
        )
    assert sorted(r.status_code for r in results) == [200, 409]
    loser = next(r for r in results if r.status_code == 409)
    assert loser.json() == {"error": "nickname_taken"}
    with session_factory() as s:
        assert sorted(u.nickname is not None for u in s.scalars(select(User))) == [
            False,
            True,
        ]


def test_consent_is_none_before_and_stored_with_time_after(
    client: TestClient,
    session_factory: SessionFactory,
    mailer: RecordingMailer,
    clock: FixedClock,
) -> None:
    session = sign_in(client, session_factory, mailer)
    assert client.get("/v1/me", headers=bearer(session)).json()["consent"] is None

    clock.advance(30)
    response = client.put("/v1/me/consent", json=CONSENT, headers=bearer(session))
    assert response.status_code == 200
    body = response.json()
    assert body["consent"] == CONSENT
    assert body["consented_at"].startswith("2026-10-13T12:00:30")
    assert client.get("/v1/me", headers=bearer(session)).json() == body

    with session_factory() as s:
        pref = s.scalars(select(UserPreference)).one()
    assert pref.consent_version == 1 and pref.consented_at.second == 30
    assert (
        pref.share_task_category,
        pref.share_stack_tags,
        pref.share_time_bucket,
    ) == (
        True,
        False,
        True,
    )


def test_consent_can_be_changed_and_withdrawn(
    client: TestClient,
    session_factory: SessionFactory,
    mailer: RecordingMailer,
    clock: FixedClock,
) -> None:
    session = sign_in(client, session_factory, mailer)
    client.put("/v1/me/consent", json=CONSENT, headers=bearer(session))
    clock.advance(60)
    off = {"version": 1}
    response = client.put("/v1/me/consent", json=off, headers=bearer(session))
    assert response.json()["consent"] == {
        "version": 1,
        "share_task_category": False,
        "share_stack_tags": False,
        "share_time_bucket": False,
    }
    assert response.json()["consented_at"].startswith("2026-10-13T12:01:00")
    with session_factory() as s:
        assert len(s.scalars(select(UserPreference)).all()) == 1


@pytest.mark.parametrize(
    "body", [{"version": 2}, {"version": 0}, {}, {"version": 1, "extra": True}]
)
def test_wrong_consent_version_or_shape_is_400(
    body: dict[str, Any],
    client: TestClient,
    session_factory: SessionFactory,
    mailer: RecordingMailer,
) -> None:
    session = sign_in(client, session_factory, mailer)
    response = client.put("/v1/me/consent", json=body, headers=bearer(session))
    assert (response.status_code, response.json()) == (
        400,
        {"error": "invalid_request"},
    )
    assert client.get("/v1/me", headers=bearer(session)).json()["consent"] is None


def test_consent_is_per_user(
    client: TestClient, session_factory: SessionFactory, mailer: RecordingMailer
) -> None:
    first = sign_in(client, session_factory, mailer, "first@example.com")
    second = sign_in(client, session_factory, mailer, "second@example.com")
    client.put("/v1/me/consent", json=CONSENT, headers=bearer(first))
    assert client.get("/v1/me", headers=bearer(second)).json()["consent"] is None


ROUTES = [
    ("GET", "/v1/me", None),
    ("PATCH", "/v1/me", {"nickname": "Someone"}),
    ("PUT", "/v1/me/consent", CONSENT),
]


@pytest.mark.parametrize(("method", "path", "body"), ROUTES)
def test_every_profile_route_needs_a_valid_token(
    method: str,
    path: str,
    body: dict[str, Any] | None,
    client: TestClient,
    session_factory: SessionFactory,
    mailer: RecordingMailer,
) -> None:
    session = sign_in(client, session_factory, mailer)
    bad_headers = [
        {},
        {"Authorization": "Bearer bra_" + "a" * 43},
        bearer(session) | {"Authorization": "Bearer " + "x"},
    ]
    for headers in bad_headers:
        response = client.request(method, path, json=body, headers=headers)
        assert (response.status_code, response.json()) == (
            401,
            {"error": "unauthorized"},
        )
        assert response.headers["WWW-Authenticate"] == "Bearer"

    # Works with the real token, then not once the device is revoked.
    assert (
        client.request(method, path, json=body, headers=bearer(session)).status_code
        == 200
    )
    with session_factory() as s:
        s.scalars(select(Device)).one().revoked_at = START_TIME
        s.commit()
    revoked = client.request(method, path, json=body, headers=bearer(session))
    assert (revoked.status_code, revoked.json()) == (401, {"error": "unauthorized"})


@pytest.mark.parametrize(("method", "path", "body"), ROUTES)
def test_disabled_user_is_unauthorized_on_profile_routes(
    method: str,
    path: str,
    body: dict[str, Any] | None,
    client: TestClient,
    session_factory: SessionFactory,
    mailer: RecordingMailer,
) -> None:
    session = sign_in(client, session_factory, mailer)
    with session_factory() as s:
        s.scalars(select(User)).one().status = "disabled"
        s.commit()
    response = client.request(method, path, json=body, headers=bearer(session))
    assert response.status_code == 401

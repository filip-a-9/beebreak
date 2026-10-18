"""The GitHub client against a fake GitHub on a loopback port (real HTTP)."""

import dataclasses
import socket
from typing import Any

import pytest

from beebreak_server.config import ServerConfig
from beebreak_server.github import (
    Authorized,
    Denied,
    Expired,
    GitHubClient,
    GitHubUnavailable,
    Pending,
    SlowDown,
)
from tests.server.conftest import TEST_SECRET
from tests.server.github_fake import (
    ACCESS_TOKEN,
    DEVICE_CODE,
    USER_CODE,
    FakeGitHub,
    fake_github,
)

__all__ = ["fake_github"]


def make_client(fake: FakeGitHub, **overrides: Any) -> GitHubClient:
    config = ServerConfig(
        database_url="sqlite://",
        secret=TEST_SECRET,
        github_client_id=fake.client_id,
        github_oauth_url=fake.url,
        github_api_url=fake.url,
        github_timeout_s=2,
    )
    return GitHubClient(dataclasses.replace(config, **overrides))


def test_device_flow_start_sends_the_client_id_and_no_scope(
    fake_github: FakeGitHub,
) -> None:
    code = make_client(fake_github).start_device_flow()
    assert (code.device_code, code.user_code) == (DEVICE_CODE, USER_CODE)
    assert code.verification_uri == f"{fake_github.url}/login/device"
    assert (code.expires_in, code.interval) == (900, 5)
    (request,) = fake_github.calls("/login/device/code")
    assert request.form == {"client_id": fake_github.client_id}  # no scope, no secret
    assert request.headers["accept"] == "application/json"
    assert request.headers["user-agent"]


def test_a_wrong_client_id_is_unavailable(fake_github: FakeGitHub) -> None:
    client = make_client(fake_github, github_client_id="Iv1.other")
    with pytest.raises(GitHubUnavailable):
        client.start_device_flow()


def test_an_http_error_on_start_is_unavailable(fake_github: FakeGitHub) -> None:
    fake_github.device_code_status = 503
    with pytest.raises(GitHubUnavailable):
        make_client(fake_github).start_device_flow()


def test_a_verification_uri_outside_the_oauth_host_is_refused(
    fake_github: FakeGitHub,
) -> None:
    fake_github.verification_uri = "https://evil.example/login/device"
    with pytest.raises(GitHubUnavailable):
        make_client(fake_github).start_device_flow()


def test_poll_sends_the_documented_grant(fake_github: FakeGitHub) -> None:
    make_client(fake_github).poll(DEVICE_CODE)
    (request,) = fake_github.calls("/login/oauth/access_token")
    assert request.form == {
        "client_id": fake_github.client_id,
        "device_code": DEVICE_CODE,
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
    }
    assert request.headers["accept"] == "application/json"


def test_poll_maps_every_documented_answer(fake_github: FakeGitHub) -> None:
    fake_github.script_poll(
        {"error": "authorization_pending"},
        {"error": "slow_down", "interval": 10},
        {"error": "slow_down"},  # no interval in the answer
        {"error": "expired_token"},
        {"error": "access_denied"},
        {"access_token": ACCESS_TOKEN, "token_type": "bearer", "scope": ""},
    )
    client = make_client(fake_github)
    assert client.poll(DEVICE_CODE) == Pending()
    assert client.poll(DEVICE_CODE) == SlowDown(interval=10)
    assert client.poll(DEVICE_CODE) == SlowDown(interval=None)
    assert client.poll(DEVICE_CODE) == Expired()
    assert client.poll(DEVICE_CODE) == Denied()
    assert client.poll(DEVICE_CODE) == Authorized(access_token=ACCESS_TOKEN)


@pytest.mark.parametrize(
    "answer",
    [
        {"error": "device_flow_disabled"},
        {"error": "incorrect_client_credentials"},
        {"error": "incorrect_device_code"},
        {"error": "unsupported_grant_type"},
        {"error": "something_new"},
        {"token_type": "bearer"},
        {"access_token": ""},
        {"access_token": 7},
    ],
)
def test_other_answers_are_unavailable(
    fake_github: FakeGitHub, answer: dict[str, object]
) -> None:
    fake_github.script_poll(answer)
    with pytest.raises(GitHubUnavailable):
        make_client(fake_github).poll(DEVICE_CODE)


def test_the_docs_second_spelling_of_expired_is_accepted(
    fake_github: FakeGitHub,
) -> None:
    fake_github.script_poll({"error": "token_expired"})
    assert make_client(fake_github).poll(DEVICE_CODE) == Expired()


def test_the_authorized_token_is_not_in_the_repr() -> None:
    assert ACCESS_TOKEN not in repr(Authorized(access_token=ACCESS_TOKEN))


def test_user_id_returns_only_the_numeric_id(fake_github: FakeGitHub) -> None:
    fake_github.token_user_id = 9_000_000_001  # past 32 bits
    assert make_client(fake_github).user_id(ACCESS_TOKEN) == 9_000_000_001
    (request,) = fake_github.calls("/user")
    assert request.headers["authorization"] == f"Bearer {ACCESS_TOKEN}"
    assert request.headers["accept"] == "application/vnd.github+json"


def test_a_rejected_token_is_unavailable(fake_github: FakeGitHub) -> None:
    with pytest.raises(GitHubUnavailable):
        make_client(fake_github).user_id("gho_not_the_token")


def test_a_user_answer_without_a_numeric_id_is_unavailable(
    fake_github: FakeGitHub,
) -> None:
    fake_github.user_status = 200  # body is {"message": "forced"}
    with pytest.raises(GitHubUnavailable):
        make_client(fake_github).user_id(ACCESS_TOKEN)


def test_lookup_resolves_a_username_without_credentials(
    fake_github: FakeGitHub,
) -> None:
    fake_github.users["octocat"] = 583231
    assert make_client(fake_github).lookup_user_id("OctoCat") == 583231
    (request,) = fake_github.calls("/users/OctoCat")
    assert "authorization" not in request.headers


def test_lookup_of_an_unknown_name_is_none(fake_github: FakeGitHub) -> None:
    assert make_client(fake_github).lookup_user_id("nobody-here") is None


@pytest.mark.parametrize("name", ["", "-lead", "a b", "a/b", "x" * 40, "é"])
def test_lookup_rejects_a_name_that_cannot_be_a_username(
    fake_github: FakeGitHub, name: str
) -> None:
    assert make_client(fake_github).lookup_user_id(name) is None
    assert fake_github.requests == []  # nothing was sent


def test_nothing_listening_is_unavailable_and_the_message_has_no_url() -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    client = make_client(FakeGitHub(port=port))
    with pytest.raises(GitHubUnavailable) as info:
        client.lookup_user_id("someone")
    assert "someone" not in str(info.value) and str(port) not in str(info.value)


def test_a_redirect_is_not_followed(fake_github: FakeGitHub) -> None:
    fake_github.redirect_lookup = True
    fake_github.users["octocat"] = 583231
    with pytest.raises(GitHubUnavailable):
        make_client(fake_github).lookup_user_id("octocat")
    assert fake_github.calls("/user") == []  # the Location was never visited


def test_an_oversized_answer_is_unavailable(fake_github: FakeGitHub) -> None:
    fake_github.script_poll({"error": "authorization_pending", "pad": "x" * 100_000})
    with pytest.raises(GitHubUnavailable):
        make_client(fake_github).poll(DEVICE_CODE)

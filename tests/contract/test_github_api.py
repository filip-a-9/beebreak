"""Wire contract of GitHub sign-in (the shapes both sides parse)."""

from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from beebreak_shared.api import (
    AuthMethods,
    GitHubPollRequest,
    GitHubPollResponse,
    GitHubStartResponse,
)

TOKENS: dict[str, Any] = {
    "access_token": "bra_x",
    "access_expires_at": "2026-10-17T12:15:00Z",
    "refresh_token": "brr_x",
    "refresh_expires_at": "2026-11-16T12:00:00Z",
}
SESSION = {
    "tokens": TOKENS,
    "me": {"nickname": None, "consent": None, "consented_at": None},
}
START: dict[str, Any] = {
    "challenge_id": str(uuid4()),
    "user_code": "WDJB-MJHT",
    "verification_uri": "https://github.com/login/device",
    "expires_in_s": 900,
    "interval_s": 5,
}


def test_methods_are_two_booleans_and_nothing_else() -> None:
    assert AuthMethods.model_validate({"email": True, "github": False}).github is False
    with pytest.raises(ValidationError):
        AuthMethods.model_validate({"email": True, "github": True, "extra": 1})
    with pytest.raises(ValidationError):
        AuthMethods.model_validate({"email": True})


def test_start_response_carries_the_code_to_show_and_the_page_to_open() -> None:
    reply = GitHubStartResponse.model_validate(START)
    assert (reply.user_code, reply.interval_s) == ("WDJB-MJHT", 5)


def test_start_response_has_no_device_code_field() -> None:
    with pytest.raises(ValidationError):
        GitHubStartResponse.model_validate(START | {"device_code": "never-sent"})


def test_poll_request_takes_the_challenge_and_a_device_name() -> None:
    body = {"challenge_id": str(uuid4()), "device_name": "laptop"}
    GitHubPollRequest.model_validate(body)
    with pytest.raises(ValidationError):
        GitHubPollRequest.model_validate(body | {"device_name": "bad/name"})


def test_pending_needs_an_interval_and_no_session() -> None:
    pending = GitHubPollResponse.model_validate({"status": "pending", "interval_s": 10})
    assert pending.session is None
    with pytest.raises(ValidationError):
        GitHubPollResponse.model_validate({"status": "pending"})
    with pytest.raises(ValidationError):
        GitHubPollResponse.model_validate(
            {"status": "pending", "interval_s": 5, "session": SESSION}
        )


def test_complete_needs_a_session() -> None:
    done = GitHubPollResponse.model_validate({"status": "complete", "session": SESSION})
    assert done.session is not None and done.interval_s is None
    with pytest.raises(ValidationError):
        GitHubPollResponse.model_validate({"status": "complete"})

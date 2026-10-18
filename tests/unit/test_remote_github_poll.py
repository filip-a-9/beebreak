"""The GitHub poll loop honours its retry budget and minimum interval from env."""

import asyncio
import dataclasses
import uuid
from pathlib import Path

import pytest
from pytestqt.qtbot import QtBot

from beebreak_companion.api_client import ApiError
from beebreak_companion.credentials import FileCredentialStore
from beebreak_companion.remote import RemoteWorker
from beebreak_companion.remote_config import RemoteConfig
from beebreak_shared.api import GitHubPollResponse, GitHubStartResponse

STARTED = GitHubStartResponse(
    challenge_id=uuid.uuid4(),
    user_code="ABCD-1234",
    verification_uri="https://github.com/login/device",
    expires_in_s=900,
    interval_s=1,
)


class _ScriptedApi:
    """Answers each poll from a script: an ApiError code to raise, or pending."""

    def __init__(self, script: list[str | None]) -> None:
        self._script = list(script)

    async def github_poll(
        self, challenge_id: uuid.UUID, device_name: str
    ) -> GitHubPollResponse:
        step = self._script.pop(0)
        if step is not None:
            raise ApiError(step)
        return GitHubPollResponse(status="pending", interval_s=1)


def _run_poll(
    config: RemoteConfig,
    tmp_path: Path,
    script: list[str | None],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[str], list[float]]:
    """Errors the worker reported, and every wait it took between polls."""
    waits: list[float] = []
    real_sleep = asyncio.sleep

    async def recording_sleep(delay: float) -> None:
        waits.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", recording_sleep)
    worker = RemoteWorker(
        config, FileCredentialStore(tmp_path / "c.json", config.server_url)
    )
    errors: list[str] = []
    worker.error.connect(errors.append)
    worker._api = _ScriptedApi(script)  # type: ignore[assignment]
    asyncio.run(worker._github_poll(STARTED, "WSL device"))
    return errors, waits


@pytest.mark.parametrize(("retries", "outcome"), [(2, "expired"), (1, "network")])
def test_network_failures_are_retried_up_to_the_configured_budget(
    qtbot: QtBot,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    retries: int,
    outcome: str,
) -> None:
    monkeypatch.setenv("BEEBREAK_GITHUB_POLL_RETRIES", str(retries))
    config = RemoteConfig.from_env()
    # Two network failures, then a definite answer: only a budget of 2 reaches it.
    errors, _ = _run_poll(
        config, tmp_path, ["network", "network", "expired"], monkeypatch
    )
    assert errors == [outcome]


def test_polls_wait_at_least_the_configured_minimum(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BEEBREAK_GITHUB_MIN_POLL_INTERVAL_S", "2.5")
    config = RemoteConfig.from_env()
    _, waits = _run_poll(config, tmp_path, [None, "expired"], monkeypatch)
    # The server asks for 1 s; the configured floor of 2.5 s wins every time.
    assert waits == [2.5, 2.5]


def test_the_server_interval_wins_when_above_the_minimum(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = dataclasses.replace(
        RemoteConfig.from_env(), github_min_poll_interval_s=0.5
    )
    _, waits = _run_poll(config, tmp_path, ["expired"], monkeypatch)
    assert waits == [1.0]

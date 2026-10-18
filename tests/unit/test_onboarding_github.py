"""Onboarding: the choice between GitHub and email, and the GitHub page.

Part one drives the dialog with a fake worker (no network). Part two signs in
for real: dialog, RemoteWorker, a live Beebreak server and a fake GitHub.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from pytestqt.qtbot import QtBot
from sqlalchemy import select

from beebreak_companion.credentials import FileCredentialStore
from beebreak_companion.onboarding import (
    ERROR_TEXT,
    PAGE_EMAIL,
    PAGE_GITHUB,
    PAGE_METHOD,
    PAGE_NICKNAME,
    PAGE_PRIVACY,
    OnboardingDialog,
    safe_verification_uri,
)
from beebreak_companion.remote import RemoteWorker
from beebreak_companion.remote_config import RemoteConfig
from beebreak_server.models import AuthChallenge, BetaInvite, User
from beebreak_shared.api import AuthMethods, GitHubStartResponse
from tests.fakes.remote_worker import FakeRemoteWorker
from tests.server.github_fake import (
    ACCESS_TOKEN,
    DEVICE_CODE,
    USER_CODE,
    FakeGitHub,
    fake_github,
)

if TYPE_CHECKING:
    from tests.e2e_support.server import LiveServer

pytest_plugins = ["tests.e2e_support.server"]  # live_server fixtures
__all__ = ["fake_github"]

WAIT_MS = 15_000
BOTH = AuthMethods(email=True, github=True)
GITHUB_ONLY = AuthMethods(email=False, github=True)
EMAIL_ONLY = AuthMethods(email=True, github=False)
STARTED = GitHubStartResponse(
    challenge_id=uuid4(),
    user_code="WDJB-MJHT",
    verification_uri="https://github.com/login/device",
    expires_in_s=900,
    interval_s=5,
)


class Opened:
    def __init__(self) -> None:
        self.urls: list[str] = []

    def __call__(self, url: str) -> bool:
        self.urls.append(url)
        return True


def dialog_with(
    qtbot: QtBot, opened: Opened | None = None
) -> tuple[OnboardingDialog, FakeRemoteWorker]:
    worker = FakeRemoteWorker()
    dialog = OnboardingDialog(worker, open_url=opened or Opened())
    qtbot.addWidget(dialog)
    dialog.show()
    return dialog, worker


def error_text(dialog: OnboardingDialog, page: int) -> str | None:
    label = dialog._errors[page]
    return label.text() if label.isVisibleTo(dialog) else None


# --- part one: the dialog alone ----------------------------------------------------


def test_the_dialog_asks_for_the_methods_once_and_starts_on_email(
    qtbot: QtBot,
) -> None:
    dialog, worker = dialog_with(qtbot)
    assert worker.calls.method_fetches == 1
    assert dialog.page == PAGE_EMAIL


def test_a_dialog_for_a_signed_in_user_does_not_ask(qtbot: QtBot) -> None:
    from beebreak_shared.api import Me

    worker = FakeRemoteWorker()
    dialog = OnboardingDialog(
        worker, Me(nickname=None, consent=None, consented_at=None)
    )
    qtbot.addWidget(dialog)
    assert worker.calls.method_fetches == 0


def test_an_email_only_server_keeps_the_email_page(qtbot: QtBot) -> None:
    dialog, worker = dialog_with(qtbot)
    worker.push_methods(EMAIL_ONLY)
    assert dialog.page == PAGE_EMAIL


def test_a_server_with_both_methods_offers_the_choice(qtbot: QtBot) -> None:
    dialog, worker = dialog_with(qtbot)
    worker.push_methods(BOTH)
    assert dialog.page == PAGE_METHOD
    assert not dialog.next_button.isVisibleTo(dialog)  # the two buttons decide
    dialog.email_choice_button.click()
    assert dialog.page == PAGE_EMAIL
    assert worker.calls.sign_ins == []  # choosing is not signing in


def test_choosing_github_shows_the_github_page(qtbot: QtBot) -> None:
    dialog, worker = dialog_with(qtbot)
    worker.push_methods(BOTH)
    dialog.github_choice_button.click()
    assert dialog.page == PAGE_GITHUB
    assert dialog.next_button.isEnabled()
    assert not dialog.github_code_label.isVisibleTo(dialog)  # nothing to show yet
    assert dialog.back_button.isEnabled()
    dialog.back_button.click()
    assert dialog.page == PAGE_METHOD


def test_a_github_only_server_goes_straight_to_github_with_no_way_back(
    qtbot: QtBot,
) -> None:
    dialog, worker = dialog_with(qtbot)
    worker.push_methods(GITHUB_ONLY)
    assert dialog.page == PAGE_GITHUB
    assert not dialog.back_button.isEnabled()


def test_methods_arriving_after_the_user_typed_do_not_move_them(qtbot: QtBot) -> None:
    dialog, worker = dialog_with(qtbot)
    dialog.email_edit.setText("ada@example.com")
    worker.push_methods(BOTH)
    assert dialog.page == PAGE_EMAIL
    assert dialog.email_edit.text() == "ada@example.com"


def test_next_starts_github_with_the_device_name_and_the_code_appears(
    qtbot: QtBot,
) -> None:
    opened = Opened()
    dialog, worker = dialog_with(qtbot, opened)
    worker.push_methods(GITHUB_ONLY)
    dialog.github_device_name_edit.setText("Work laptop")
    dialog.next_button.click()
    assert worker.calls.github_sign_ins == ["Work laptop"]
    assert not dialog.next_button.isEnabled()  # one attempt at a time

    worker.push_github_started(STARTED)
    assert dialog.github_code_label.text() == "WDJB-MJHT"
    assert dialog.github_open_button.isVisibleTo(dialog)
    assert opened.urls == []  # nothing opens by itself
    dialog.github_open_button.click()
    assert opened.urls == ["https://github.com/login/device"]


def test_a_bad_device_name_blocks_the_github_start(qtbot: QtBot) -> None:
    dialog, worker = dialog_with(qtbot)
    worker.push_methods(GITHUB_ONLY)
    dialog.github_device_name_edit.setText("no/slash")
    assert not dialog.next_button.isEnabled()


@pytest.mark.parametrize(
    ("uri", "ok"),
    [
        ("https://github.com/login/device", True),
        ("http://127.0.0.1:8123/login/device", True),
        ("http://github.com/login/device", False),
        ("file:///etc/passwd", False),
        ("javascript:alert(1)", False),
        ("https://", False),
        ("", False),
    ],
)
def test_only_safe_links_are_opened(uri: str, ok: bool, qtbot: QtBot) -> None:
    assert safe_verification_uri(uri) is ok
    opened = Opened()
    dialog, worker = dialog_with(qtbot, opened)
    worker.push_methods(GITHUB_ONLY)
    dialog.next_button.click()
    worker.push_github_started(STARTED.model_copy(update={"verification_uri": uri}))
    dialog.github_open_button.click()
    assert bool(opened.urls) is ok


@pytest.mark.parametrize(
    "code", ["not_invited", "access_denied", "expired", "github_unavailable"]
)
def test_a_failed_attempt_shows_the_reason_and_can_be_retried(
    qtbot: QtBot, code: str
) -> None:
    dialog, worker = dialog_with(qtbot)
    worker.push_methods(GITHUB_ONLY)
    dialog.next_button.click()
    worker.push_github_started(STARTED)
    worker.push_state("signed_out")
    worker.push_error(code)
    assert dialog.page == PAGE_GITHUB
    assert error_text(dialog, PAGE_GITHUB) == ERROR_TEXT[code]
    assert not dialog.github_code_label.isVisibleTo(dialog)  # a dead code is gone
    assert dialog.next_button.isEnabled()
    dialog.next_button.click()
    assert len(worker.calls.github_sign_ins) == 2


def test_cancelling_a_waiting_attempt_signs_out_so_polling_stops(
    qtbot: QtBot,
) -> None:
    dialog, worker = dialog_with(qtbot)
    worker.push_methods(GITHUB_ONLY)
    dialog.next_button.click()
    worker.push_github_started(STARTED)
    dialog.cancel_button.click()
    assert worker.calls.sign_outs == 1


def test_going_back_from_a_waiting_attempt_stops_it(qtbot: QtBot) -> None:
    dialog, worker = dialog_with(qtbot)
    worker.push_methods(BOTH)
    dialog.github_choice_button.click()
    dialog.next_button.click()
    worker.push_github_started(STARTED)
    assert dialog.back_button.isEnabled()
    dialog.back_button.click()
    assert dialog.page == PAGE_METHOD
    assert worker.calls.sign_outs == 1
    dialog.github_choice_button.click()
    assert not dialog.github_code_label.isVisibleTo(dialog)


def test_a_session_arriving_moves_on_to_the_nickname_page(qtbot: QtBot) -> None:
    from beebreak_shared.api import Me

    dialog, worker = dialog_with(qtbot)
    worker.push_methods(GITHUB_ONLY)
    dialog.next_button.click()
    worker.push_github_started(STARTED)
    worker.push_me(Me(nickname=None, consent=None, consented_at=None))
    assert dialog.page == PAGE_NICKNAME
    assert not dialog.back_button.isEnabled()


# --- part two: a real sign-in --------------------------------------------------------


@pytest.fixture
def github_server(
    server_factory: Callable[..., LiveServer], fake_github: FakeGitHub
) -> Callable[..., LiveServer]:
    fake_github.interval = 1  # the companion waits this long between polls

    def make(**overrides: object) -> LiveServer:
        return server_factory(
            github_client_id=fake_github.client_id,
            github_oauth_url=fake_github.url,
            github_api_url=fake_github.url,
            github_timeout_s=3,
            **overrides,
        )

    return make


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Callable[[LiveServer], tuple[RemoteWorker, Path]]]:
    workers: list[RemoteWorker] = []

    def make(server: LiveServer) -> tuple[RemoteWorker, Path]:
        path = tmp_path / "credentials.json"
        worker = RemoteWorker(
            RemoteConfig(
                server_url=server.base_url,
                http_timeout_s=3,
                ws_ping_interval_s=1.0,
                reconnect_min_s=0.05,
                reconnect_max_s=0.2,
            ),
            FileCredentialStore(path, server.base_url),
        )
        workers.append(worker)
        return worker, path

    yield make
    for worker in workers:
        worker.stop()


def invite_github_id(server: LiveServer, github_id: int) -> None:
    now = datetime.now(UTC)
    with server.session_factory() as s:
        s.add(
            BetaInvite(
                github_id=github_id,
                expires_at=now + timedelta(days=30),
                created_at=now,
            )
        )
        s.commit()


def test_a_full_github_sign_in_through_the_dialog(
    qtbot: QtBot,
    github_server: Callable[..., LiveServer],
    fake_github: FakeGitHub,
    rig: Callable[[LiveServer], tuple[RemoteWorker, Path]],
) -> None:
    server = github_server()
    invite_github_id(server, fake_github.token_user_id)
    worker, credentials = rig(server)
    opened = Opened()
    dialog = OnboardingDialog(worker, open_url=opened)
    qtbot.addWidget(dialog)
    dialog.show()

    qtbot.waitUntil(lambda: dialog.page == PAGE_METHOD, timeout=WAIT_MS)
    dialog.github_choice_button.click()
    dialog.next_button.click()
    qtbot.waitUntil(
        lambda: dialog.github_code_label.text() == USER_CODE, timeout=WAIT_MS
    )
    dialog.github_open_button.click()
    assert opened.urls == [f"{fake_github.url}/login/device"]

    # Still pending on GitHub's side: the dialog waits, the server is polled.
    qtbot.wait(1500)
    assert dialog.page == PAGE_GITHUB
    fake_github.approve()
    qtbot.waitUntil(lambda: dialog.page == PAGE_NICKNAME, timeout=WAIT_MS)

    # Signed in, and neither GitHub secret reached the companion's disk.
    saved = credentials.read_text()
    assert json.loads(saved)  # a login was stored
    assert ACCESS_TOKEN not in saved and DEVICE_CODE not in saved
    assert str(fake_github.token_user_id) not in saved
    with server.session_factory() as s:
        user = s.scalars(select(User)).one()
        assert (user.github_id, user.email_canonical) == (4242, None)
        challenge = s.scalars(select(AuthChallenge)).one()
        assert challenge.consumed_at is not None
        assert challenge.github_device_code is None

    # The rest of onboarding works as for any account.
    dialog.nickname_edit.setText("octo_cat")
    dialog.next_button.click()
    qtbot.waitUntil(lambda: dialog.page == PAGE_PRIVACY, timeout=WAIT_MS)


def test_an_uninvited_github_user_sees_not_invited_and_nothing_is_stored(
    qtbot: QtBot,
    github_server: Callable[..., LiveServer],
    fake_github: FakeGitHub,
    rig: Callable[[LiveServer], tuple[RemoteWorker, Path]],
) -> None:
    server = github_server()
    worker, credentials = rig(server)
    dialog = OnboardingDialog(worker, open_url=Opened())
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitUntil(lambda: dialog.page == PAGE_METHOD, timeout=WAIT_MS)
    dialog.github_choice_button.click()
    dialog.next_button.click()
    qtbot.waitUntil(
        lambda: dialog.github_code_label.text() == USER_CODE, timeout=WAIT_MS
    )
    fake_github.approve()
    qtbot.waitUntil(
        lambda: error_text(dialog, PAGE_GITHUB) == ERROR_TEXT["not_invited"],
        timeout=WAIT_MS,
    )
    assert dialog.page == PAGE_GITHUB
    assert dialog.next_button.isEnabled()
    assert not credentials.exists()
    with server.session_factory() as s:
        assert s.scalars(select(User)).first() is None


def test_declining_on_github_is_reported(
    qtbot: QtBot,
    github_server: Callable[..., LiveServer],
    fake_github: FakeGitHub,
    rig: Callable[[LiveServer], tuple[RemoteWorker, Path]],
) -> None:
    server = github_server()
    worker, _ = rig(server)
    dialog = OnboardingDialog(worker, open_url=Opened())
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitUntil(lambda: dialog.page == PAGE_METHOD, timeout=WAIT_MS)
    dialog.github_choice_button.click()
    dialog.next_button.click()
    qtbot.waitUntil(
        lambda: dialog.github_code_label.text() == USER_CODE, timeout=WAIT_MS
    )
    fake_github.script_poll({"error": "access_denied"})
    qtbot.waitUntil(
        lambda: error_text(dialog, PAGE_GITHUB) == ERROR_TEXT["access_denied"],
        timeout=WAIT_MS,
    )


def test_a_github_only_server_opens_on_the_github_page(
    qtbot: QtBot,
    github_server: Callable[..., LiveServer],
    rig: Callable[[LiveServer], tuple[RemoteWorker, Path]],
) -> None:
    server = github_server(mailer="smtp", smtp_host="")
    worker, _ = rig(server)
    dialog = OnboardingDialog(worker, open_url=Opened())
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitUntil(lambda: dialog.page == PAGE_GITHUB, timeout=WAIT_MS)
    assert not dialog.back_button.isEnabled()


def test_cancelling_stops_the_polling(
    qtbot: QtBot,
    github_server: Callable[..., LiveServer],
    fake_github: FakeGitHub,
    rig: Callable[[LiveServer], tuple[RemoteWorker, Path]],
) -> None:
    server = github_server()
    worker, _ = rig(server)
    dialog = OnboardingDialog(worker, open_url=Opened())
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitUntil(lambda: dialog.page == PAGE_METHOD, timeout=WAIT_MS)
    dialog.github_choice_button.click()
    dialog.next_button.click()
    qtbot.waitUntil(
        lambda: dialog.github_code_label.text() == USER_CODE, timeout=WAIT_MS
    )
    qtbot.waitUntil(lambda: len(server.requests) >= 4, timeout=WAIT_MS)
    dialog.cancel_button.click()
    qtbot.wait(300)  # let the cancel land
    seen = len(server.requests)
    qtbot.wait(2500)  # more than two intervals
    assert len(server.requests) == seen

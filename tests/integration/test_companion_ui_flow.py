"""Window + app wiring + onboarding + worker against a real loopback server."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot
from sqlalchemy import select

from breakroom_companion.app import setup_remote
from breakroom_companion.onboarding import (
    PAGE_DONE,
    PAGE_NICKNAME,
    PAGE_PRIVACY,
    OnboardingDialog,
)
from breakroom_companion.remote import RemoteWorker
from breakroom_companion.window import CompanionWindow
from breakroom_server.models import User, UserPreference

if TYPE_CHECKING:
    from tests.e2e_support.server import LiveServer

pytest_plugins = ["tests.e2e_support.server"]  # live_server fixtures

WAIT_MS = 15_000
EMAIL = "ada@example.com"


@pytest.fixture
def env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, live_server: LiveServer
) -> LiveServer:
    # Never touch the real state dir; a fresh dir has no stored login.
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("BREAKROOM_SERVER_URL", live_server.base_url)
    monkeypatch.setenv("BREAKROOM_RECONNECT_MIN_S", "0.05")
    monkeypatch.setenv("BREAKROOM_RECONNECT_MAX_S", "0.2")
    monkeypatch.setenv("BREAKROOM_WS_PING_INTERVAL_S", "1")
    return live_server


class App:
    def __init__(self, qtbot: QtBot) -> None:
        self.window = CompanionWindow()
        qtbot.addWidget(self.window)
        worker = setup_remote(self.window)
        assert worker is not None
        self.worker: RemoteWorker = worker
        self.qtbot = qtbot

    def dialog(self) -> OnboardingDialog:
        found = self.open_dialogs()
        assert len(found) == 1
        return found[0]

    def open_dialogs(self) -> list[OnboardingDialog]:
        # A closed dialog lingers until the event loop deletes it.
        return [d for d in self.window.findChildren(OnboardingDialog) if d.isVisible()]

    def wait_label(self, text: str) -> None:
        self.qtbot.waitUntil(
            lambda: self.window.account_label.text() == text, timeout=WAIT_MS
        )

    def wait_page(self, page: int) -> None:
        self.qtbot.waitUntil(lambda: self.dialog().page == page, timeout=WAIT_MS)


@pytest.fixture
def app(qtbot: QtBot, env: LiveServer) -> Iterator[App]:
    app = App(qtbot)
    yield app
    app.worker.stop()


def start_and_wait_signed_out(app: App) -> None:
    app.worker.start()
    app.wait_label("Not signed in")


def type_through_sign_in(app: App, server: LiveServer) -> None:
    """From a click on Sign in to the nickname page."""
    app.window.sign_in_button.click()
    dialog = app.dialog()
    dialog.email_edit.setText(EMAIL)
    dialog.invite_edit.setText(server.invite(EMAIL))
    dialog.next_button.click()
    app.qtbot.waitUntil(lambda: len(server.mailer.sent) > 0, timeout=WAIT_MS)
    dialog.code_edit.setText(server.last_code())
    dialog.next_button.click()
    app.wait_page(PAGE_NICKNAME)


def stored_consent(server: LiveServer) -> tuple[bool, bool, bool] | None:
    with server.session_factory() as session:
        pref = session.scalars(select(UserPreference)).first()
        if pref is None:
            return None
        return (
            pref.share_task_category,
            pref.share_stack_tags,
            pref.share_time_bucket,
        )


def test_full_flow_from_the_window(app: App, env: LiveServer) -> None:
    start_and_wait_signed_out(app)
    assert app.open_dialogs() == []

    type_through_sign_in(app, env)
    dialog = app.dialog()
    dialog.nickname_edit.setText("ada_l")
    dialog.next_button.click()
    app.wait_page(PAGE_PRIVACY)
    assert not any(
        box.isChecked()
        for box in (dialog.category_box, dialog.stack_box, dialog.time_box)
    )
    dialog.stack_box.setChecked(True)
    dialog.next_button.click()
    app.wait_page(PAGE_DONE)
    dialog.next_button.click()

    app.wait_label("Connected as ada_l")
    assert stored_consent(env) == (False, True, False)
    assert app.window.settings_button.isVisibleTo(app.window)
    assert not app.window.finish_setup_button.isVisibleTo(app.window)
    assert not app.window.sign_in_button.isVisibleTo(app.window)


def test_settings_round_trip_changes_consent_on_the_server(
    app: App, env: LiveServer
) -> None:
    start_and_wait_signed_out(app)
    type_through_sign_in(app, env)
    dialog = app.dialog()
    dialog.nickname_edit.setText("ada_l")
    dialog.next_button.click()
    app.wait_page(PAGE_PRIVACY)
    dialog.next_button.click()  # all off
    app.wait_page(PAGE_DONE)
    dialog.next_button.click()
    app.wait_label("Connected as ada_l")
    assert stored_consent(env) == (False, False, False)

    app.window.settings_button.click()
    settings = app.dialog()
    assert settings.page == PAGE_PRIVACY
    settings.category_box.setChecked(True)
    settings.time_box.setChecked(True)
    settings.next_button.click()
    app.wait_page(PAGE_DONE)

    assert stored_consent(env) == (True, False, True)


def test_cancel_after_verify_offers_finish_setup(app: App, env: LiveServer) -> None:
    start_and_wait_signed_out(app)
    type_through_sign_in(app, env)
    app.dialog().cancel_button.click()

    app.wait_label("Connected")
    assert app.window.finish_setup_button.isVisibleTo(app.window)
    assert not app.window.settings_button.isVisibleTo(app.window)
    app.window.finish_setup_button.click()
    app.wait_page(PAGE_NICKNAME)


def test_sign_out_and_resume(app: App, env: LiveServer, qtbot: QtBot) -> None:
    start_and_wait_signed_out(app)
    type_through_sign_in(app, env)
    dialog = app.dialog()
    dialog.nickname_edit.setText("ada_l")
    dialog.next_button.click()
    app.wait_page(PAGE_PRIVACY)
    dialog.next_button.click()
    app.wait_page(PAGE_DONE)
    dialog.next_button.click()
    app.wait_label("Connected as ada_l")

    # A second companion process with the same state dir resumes silently.
    second = App(qtbot)
    try:
        second.worker.start()
        second.wait_label("Connected as ada_l")
        assert not second.window.sign_in_button.isVisibleTo(second.window)
    finally:
        second.worker.stop()

    app.window.sign_out_button.click()
    app.wait_label("Not signed in")
    assert app.window.sign_in_button.isVisibleTo(app.window)
    with env.session_factory() as session:
        assert session.scalars(select(User)).first() is not None  # account stays


def test_cancel_before_verify_leaves_the_row_signed_out(
    app: App, env: LiveServer
) -> None:
    start_and_wait_signed_out(app)
    app.window.sign_in_button.click()
    dialog = app.dialog()
    dialog.email_edit.setText(EMAIL)
    dialog.invite_edit.setText(env.invite(EMAIL))
    dialog.next_button.click()
    dialog.cancel_button.click()

    app.wait_label("Not signed in")
    assert app.window.sign_in_button.isVisibleTo(app.window)


def test_agent_events_never_open_the_dialog(app: App) -> None:
    start_and_wait_signed_out(app)
    app.window.set_state("suggested", "5_10m")
    app.window.set_state("idle")
    QApplication.processEvents()
    assert app.open_dialogs() == []

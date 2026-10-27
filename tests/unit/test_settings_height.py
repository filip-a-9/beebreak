"""W8: the Settings dialog is short enough to stay on screen when centred.

Under WSLg the compositor centres a dialog on its parent and ignores move(),
so the only control is the dialog's height.
"""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QPushButton, QScrollArea
from pytestqt.qtbot import QtBot

from beebreak_companion.config import (
    DIALOG_MAX_HEIGHT_FRACTION_ENV,
    dialog_max_height_fraction,
)
from beebreak_companion.onboarding import PAGE_PRIVACY, OnboardingDialog
from beebreak_companion.window import CompanionWindow
from beebreak_shared.api import Consent, Me
from tests.fakes.remote_worker import FakeRemoteWorker

ME = Me(
    nickname="ada_l",
    consent=Consent(
        version=1,
        share_task_category=True,
        share_stack_tags=True,
        share_time_bucket=False,
    ),
    consented_at="2026-10-19T10:00:00Z",
    device_name="desk",
)


@pytest.fixture
def parent(qtbot: QtBot) -> CompanionWindow:
    window = CompanionWindow()
    qtbot.addWidget(window)
    window.show()
    return window


def settings(parent: CompanionWindow) -> OnboardingDialog:
    dialog = OnboardingDialog(FakeRemoteWorker(), ME, parent)
    dialog.show()
    return dialog


def cap_for(dialog: OnboardingDialog) -> int:
    screen = dialog.screen()
    assert screen is not None
    return int(screen.availableGeometry().height() * dialog_max_height_fraction())


def privacy_scroll(dialog: OnboardingDialog) -> QScrollArea:
    page = dialog.stack.widget(PAGE_PRIVACY)
    assert page is not None
    scrolls = page.findChildren(QScrollArea)
    assert len(scrolls) == 1
    return scrolls[0]


def test_settings_height_is_capped_by_the_screen(parent: CompanionWindow) -> None:
    dialog = settings(parent)
    assert dialog.page == PAGE_PRIVACY
    assert dialog.height() <= cap_for(dialog)
    assert dialog.sizeHint().height() <= cap_for(dialog)


def test_the_cap_follows_the_configured_fraction(
    parent: CompanionWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(DIALOG_MAX_HEIGHT_FRACTION_ENV, "0.3")
    dialog = settings(parent)
    assert dialog.height() <= cap_for(dialog)


def test_privacy_content_scrolls_and_every_field_is_inside(
    parent: CompanionWindow,
) -> None:
    dialog = settings(parent)
    scroll = privacy_scroll(dialog)
    inner = scroll.widget()
    assert inner is not None
    assert scroll.widgetResizable()
    for widget in (
        dialog.profile_nickname_edit,
        dialog.profile_device_edit,
        dialog.category_box,
        dialog.stack_box,
        dialog.time_box,
        dialog.tag_list,
    ):
        assert inner.isAncestorOf(widget)


def test_action_buttons_stay_outside_the_scroll_area(parent: CompanionWindow) -> None:
    dialog = settings(parent)
    inner = privacy_scroll(dialog).widget()
    assert inner is not None
    for button in (dialog.next_button, dialog.back_button, dialog.cancel_button):
        assert isinstance(button, QPushButton)
        assert not inner.isAncestorOf(button)
    assert not inner.isAncestorOf(dialog.privacy_error)


@pytest.mark.parametrize("raw", ["0", "-1", "1.5", "abc", "nan", "inf"])
def test_invalid_fraction_falls_back_to_the_default(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.delenv(DIALOG_MAX_HEIGHT_FRACTION_ENV, raising=False)
    default = dialog_max_height_fraction()
    monkeypatch.setenv(DIALOG_MAX_HEIGHT_FRACTION_ENV, raw)
    assert dialog_max_height_fraction() == default
    assert 0 < default <= 1


def test_settings_is_no_taller_than_its_window(
    parent: CompanionWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Centred on its window, a dialog no taller than the window stays inside
    # the window's span, so on screen wherever the compositor put the window.
    monkeypatch.setenv(DIALOG_MAX_HEIGHT_FRACTION_ENV, "1")
    dialog = settings(parent)
    assert dialog.frameGeometry().height() <= parent.frameGeometry().height()

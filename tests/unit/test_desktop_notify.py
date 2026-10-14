"""DesktopNotifier (M5-06)."""

import pytest
from PySide6.QtWidgets import QApplication, QWidget
from pytestqt.qtbot import QtBot

from breakroom_companion.desktop_notify import DesktopNotifier


class FakeTray:
    def __init__(self) -> None:
        self.shown = 0
        self.messages: list[tuple[str, str]] = []

    def show(self) -> None:
        self.shown += 1

    def showMessage(self, title: str, message: str) -> None:
        self.messages.append((title, message))


@pytest.fixture
def alerts(monkeypatch: pytest.MonkeyPatch) -> list[QWidget]:
    seen: list[QWidget] = []
    monkeypatch.setattr(QApplication, "alert", lambda widget, *a: seen.append(widget))
    return seen


def test_a_tray_shows_the_title_and_text_given(
    qtbot: QtBot, alerts: list[QWidget]
) -> None:
    window = QWidget()
    qtbot.addWidget(window)
    tray = FakeTray()
    made: list[FakeTray] = []

    def factory() -> FakeTray:
        made.append(tray)
        return tray

    notifier = DesktopNotifier(
        window, tray_available=lambda: True, tray_factory=factory
    )
    assert made == []  # created on first use only
    notifier.notify("Breakroom", "New message from bob")
    notifier.notify("Breakroom", "New message from bob")
    assert tray.messages == [("Breakroom", "New message from bob")] * 2
    assert tray.shown == 1 and len(made) == 1
    assert alerts == []


def test_without_a_tray_the_window_is_alerted_and_no_icon_is_made(
    qtbot: QtBot, alerts: list[QWidget]
) -> None:
    window = QWidget()
    qtbot.addWidget(window)

    def factory() -> FakeTray:
        raise AssertionError("no tray icon without a system tray")

    notifier = DesktopNotifier(
        window, tray_available=lambda: False, tray_factory=factory
    )
    notifier.notify("Breakroom", "New message from bob")
    assert alerts == [window]


def test_without_a_tray_or_window_nothing_happens(alerts: list[QWidget]) -> None:
    DesktopNotifier(tray_available=lambda: False).notify("t", "x")
    assert alerts == []


def test_the_real_tray_check_does_not_raise(qtbot: QtBot) -> None:
    window = QWidget()
    qtbot.addWidget(window)
    DesktopNotifier(window)  # the default availability check is lazy

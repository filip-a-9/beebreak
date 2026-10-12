from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot

from breakroom_companion.app import initial_status, wire
from breakroom_companion.config import TriggerConfig
from breakroom_companion.controller import Controller
from breakroom_companion.window import CompanionWindow
from breakroom_companion.worker import IpcWorker
from breakroom_shared.events import NormalizedEvent
from breakroom_shared.ipc import InvitationState


@pytest.fixture
def window(qtbot: QtBot) -> CompanionWindow:
    w = CompanionWindow()
    qtbot.addWidget(w)
    return w


def _event() -> NormalizedEvent:
    return NormalizedEvent(
        event_version=1,
        event_id=uuid4(),
        local_session_key="k",
        agent="claude_code",
        type="turn_stopped",
        observed_at=datetime.now(UTC),
    )


def test_buttons_visible_only_when_suggested(window: CompanionWindow) -> None:
    window.show()
    window.set_state("idle")
    assert window.accept_button.isHidden() and window.decline_button.isHidden()
    window.set_state("disabled")
    assert window.accept_button.isHidden() and window.decline_button.isHidden()
    window.set_state("suggested")
    assert window.accept_button.isVisible() and window.decline_button.isVisible()


@pytest.mark.parametrize(
    ("state", "checked"), [("disabled", False), ("idle", True), ("suggested", True)]
)
def test_checkbox_reflects_disabled(
    window: CompanionWindow, state: InvitationState, checked: bool
) -> None:
    window.set_state(state)
    assert window.enabled_checkbox.isChecked() is checked


def test_each_state_has_distinct_label(window: CompanionWindow) -> None:
    texts = set()
    for state in ("disabled", "idle", "suggested"):
        window.set_state(state)
        texts.add(window.status_label.text())
    assert len(texts) == 3 and "" not in texts


def test_label_is_plain_text(window: CompanionWindow) -> None:
    assert window.status_label.textFormat() == Qt.TextFormat.PlainText


def test_window_is_always_on_top_and_titled(window: CompanionWindow) -> None:
    assert window.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
    assert window.windowTitle() == "Breakroom"


def test_suggested_shows_the_window(window: CompanionWindow) -> None:
    assert not window.isVisible()
    window.set_state("suggested")
    assert window.isVisible()


def test_accept_and_decline_emit(window: CompanionWindow, qtbot: QtBot) -> None:
    window.set_state("suggested")
    with qtbot.waitSignal(window.accepted, timeout=1000):
        window.accept_button.click()
    with qtbot.waitSignal(window.declined, timeout=1000):
        window.decline_button.click()


def test_checkbox_toggle_emits_enabled_changed(
    window: CompanionWindow, qtbot: QtBot
) -> None:
    window.set_state("idle")
    with qtbot.waitSignal(window.enabled_changed, timeout=1000) as off:
        window.enabled_checkbox.click()
    assert off.args == [False]
    with qtbot.waitSignal(window.enabled_changed, timeout=1000) as on:
        window.enabled_checkbox.click()
    assert on.args == [True]


def test_set_state_does_not_emit_enabled_changed(
    window: CompanionWindow, qtbot: QtBot
) -> None:
    with qtbot.assertNotEmitted(window.enabled_changed):
        window.set_state("disabled")
        window.set_state("idle")


def _wired(
    qtbot: QtBot, controller: Controller
) -> tuple[CompanionWindow, IpcWorker, QTimer]:
    window = CompanionWindow()
    qtbot.addWidget(window)
    worker = IpcWorker(Path("/nonexistent"), initial_status(controller))
    timer = wire(controller, window, worker)
    return window, worker, timer


def test_disabling_reaches_controller_window_and_worker_status(
    qtbot: QtBot,
) -> None:
    controller = Controller()
    window, worker, _timer = _wired(qtbot, controller)
    window.enabled_checkbox.click()
    assert controller.state == "disabled"
    assert window.enabled_checkbox.isChecked() is False
    assert worker._status.invitation == "disabled"


def test_event_pushes_fresh_status_to_worker(qtbot: QtBot) -> None:
    _window, worker, _timer = _wired(qtbot, Controller())
    event = _event()
    worker.event_received.emit(event)
    QApplication.processEvents()
    assert worker._status.last_event_at == event.observed_at


# Short real thresholds: two tool events one second apart qualify.
FAST = TriggerConfig(
    activity_threshold_s=1,
    quiet_gap_s=20,
    min_tool_events=2,
    cooldown_s=900,
    suggestion_timeout_s=1,
)


def _emit_qualifying_run(worker: IpcWorker, start: datetime) -> None:
    for offset, type_ in (
        (0, "turn_submitted"),
        (1, "tool_completed"),
        (2, "tool_completed"),
    ):
        worker.event_received.emit(
            NormalizedEvent(
                event_version=1,
                event_id=uuid4(),
                local_session_key="k",
                agent="claude_code",
                type=type_,
                observed_at=start + timedelta(seconds=offset),
            )
        )
    QApplication.processEvents()


def test_qualifying_events_show_invitation(qtbot: QtBot) -> None:
    window, worker, _timer = _wired(qtbot, Controller(FAST))
    _emit_qualifying_run(worker, datetime.now(UTC))
    assert window.accept_button.isVisibleTo(window)
    assert worker._status.invitation == "suggested"


@pytest.mark.parametrize("button", ["accept_button", "decline_button"])
def test_outcome_buttons_return_to_idle(qtbot: QtBot, button: str) -> None:
    controller = Controller(FAST)
    window, worker, _timer = _wired(qtbot, controller)
    _emit_qualifying_run(worker, datetime.now(UTC))
    getattr(window, button).click()
    assert controller.state == "idle"
    assert window.accept_button.isHidden()
    assert worker._status.invitation == "idle"


def test_timer_expires_unanswered_invitation(qtbot: QtBot) -> None:
    controller = Controller(FAST)
    window, worker, timer = _wired(qtbot, controller)
    assert timer.interval() == 1000 and timer.isActive()
    _emit_qualifying_run(worker, datetime.now(UTC) - timedelta(seconds=10))
    assert controller.state == "suggested"
    timer.timeout.emit()
    assert worker._status.invitation == "idle"
    assert window.accept_button.isHidden()


@pytest.mark.parametrize("key", [Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter])
def test_keys_after_suggestion_do_not_answer_it(
    window: CompanionWindow, qtbot: QtBot, key: Qt.Key
) -> None:
    window.set_state("suggested")
    with (
        qtbot.assertNotEmitted(window.accepted),
        qtbot.assertNotEmitted(window.declined),
    ):
        qtbot.keyClick(window, key)  # type: ignore[no-untyped-call]
        focus = QApplication.focusWidget()
        if focus is not None:
            qtbot.keyClick(focus, key)  # type: ignore[no-untyped-call]


def test_mouse_click_on_each_button_still_answers(
    window: CompanionWindow, qtbot: QtBot
) -> None:
    window.set_state("suggested")
    with qtbot.waitSignal(window.accepted, timeout=1000):
        qtbot.mouseClick(window.accept_button, Qt.MouseButton.LeftButton)  # type: ignore[no-untyped-call]
    with qtbot.waitSignal(window.declined, timeout=1000):
        qtbot.mouseClick(window.decline_button, Qt.MouseButton.LeftButton)  # type: ignore[no-untyped-call]


def test_answer_buttons_cannot_take_focus_and_window_is_not_activated(
    window: CompanionWindow,
) -> None:
    for button in (window.accept_button, window.decline_button):
        assert button.focusPolicy() == Qt.FocusPolicy.NoFocus
    assert window.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

"""App-side chat wiring (M5-06): real windows, FakeRemoteWorker, fake clock."""

import logging
import re
from dataclasses import dataclass, field
from datetime import timedelta
from uuid import uuid4

import pytest
from PySide6.QtWidgets import QMessageBox
from pytestqt.qtbot import QtBot

from beebreak_companion.app import BreakTracker, wire_chat
from beebreak_companion.break_state import BreakState
from beebreak_companion.chat_state import ChatState, PendingMessage
from beebreak_companion.chat_window import ChatWindow
from beebreak_companion.desktop_notify import DesktopNotifier
from beebreak_companion.window import CompanionWindow
from beebreak_shared.api import MatchView
from tests.fakes.chat import T0, chat_state, chat_status, message_view
from tests.fakes.remote_worker import FakeRemoteWorker

CANARY = "CANARY-7c1d-do-not-leak"


class FakeTray:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def show(self) -> None:
        pass

    def showMessage(self, title: str, message: str) -> None:
        self.messages.append((title, message))


@dataclass
class Rig:
    window: CompanionWindow
    chat: ChatWindow
    remote: FakeRemoteWorker
    tray: FakeTray
    now: list[float] = field(default_factory=lambda: [100.0])
    active: list[bool] = field(default_factory=lambda: [False])

    def sent(self) -> list[tuple[str, str]]:
        return self.tray.messages


@pytest.fixture
def rig(qtbot: QtBot) -> Rig:
    window = CompanionWindow()
    chat = ChatWindow(now=lambda: T0)
    qtbot.addWidget(window)
    qtbot.addWidget(chat)
    remote = FakeRemoteWorker()
    tray = FakeTray()
    r = Rig(window, chat, remote, tray)
    notifier = DesktopNotifier(
        chat, tray_available=lambda: True, tray_factory=lambda: tray
    )
    wire_chat(
        window,
        chat,
        remote,
        notifier,
        clock=lambda: r.now[0],
        is_active=lambda: r.active[0],
    )
    return r


def _unread(rig: Rig) -> list[str]:
    """The numbers on the Open chat button: its unread count, if any."""
    return re.findall(r"\d+", rig.window.open_chat_button.text())


def _peer(seq: int, body: str = "hi"):  # type: ignore[no-untyped-def]
    return message_view(seq, body=body)


# --- opening ---


def test_a_new_match_does_not_open_the_window(rig: Rig) -> None:
    rig.window.show()
    rig.window.set_break(BreakState(phase="matched", match=_match()))
    rig.remote.push_chat(chat_state())
    assert not rig.chat.isVisible()
    assert rig.window.open_chat_button.isVisibleTo(rig.window)
    rig.window.open_chat_button.click()
    assert rig.chat.isVisible()


def test_the_window_the_user_opens_is_not_reopened_by_later_updates(
    rig: Rig,
) -> None:
    state = chat_state()
    rig.remote.push_chat(state)
    rig.chat.open_for_user()
    rig.chat.hide()
    rig.remote.push_chat(chat_state(match_id=state.match_id, messages=(_peer(1),)))
    assert not rig.chat.isVisible()


def test_a_match_does_not_raise_or_activate_the_window(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    real_raise, real_activate = rig.chat.raise_, rig.chat.activateWindow

    def spy_raise() -> None:
        calls.append("raise")
        real_raise()

    def spy_activate() -> None:
        calls.append("activate")
        real_activate()

    monkeypatch.setattr(rig.chat, "raise_", spy_raise)
    monkeypatch.setattr(rig.chat, "activateWindow", spy_activate)
    rig.remote.push_chat(chat_state())
    assert not rig.chat.isVisible()
    assert calls == []
    rig.chat.open_for_user()  # the user's own act still raises and focuses
    assert calls == ["raise", "activate"]


def test_the_chat_window_shows_the_pushed_state(rig: Rig) -> None:
    rig.remote.push_chat(chat_state(peer_nickname="bob_b", messages=(_peer(1),)))
    assert rig.chat.peer_label.text() == "bob_b"
    assert rig.chat.input.isEnabled()


def test_a_state_already_in_the_worker_is_shown_at_wiring_time(qtbot: QtBot) -> None:
    window = CompanionWindow()
    chat = ChatWindow(now=lambda: T0)
    qtbot.addWidget(window)
    qtbot.addWidget(chat)
    remote = FakeRemoteWorker()
    remote._chat = chat_state(peer_nickname="bob_b")
    remote.state = "connected"
    wire_chat(window, chat, remote, DesktopNotifier(chat))
    assert not chat.isVisible()
    assert chat.peer_label.text() == "bob_b"


def test_without_a_remote_worker_wiring_is_a_no_op(qtbot: QtBot) -> None:
    window = CompanionWindow()
    chat = ChatWindow()
    qtbot.addWidget(window)
    qtbot.addWidget(chat)
    wire_chat(window, chat, None, DesktopNotifier(chat))
    window.open_chat_requested.emit()
    assert not chat.isVisible()


def test_open_chat_button_opens_the_window(rig: Rig) -> None:
    rig.remote.push_chat(chat_state())
    rig.window.show()
    rig.window.set_break(BreakState(phase="matched", match=_match()))
    rig.window.open_chat_button.click()
    assert rig.chat.isVisible()


def _match() -> MatchView:
    match_id = uuid4()
    return MatchView(
        match_id=match_id,
        peer_nickname="bob",
        ends_at=T0 + timedelta(minutes=15),
        chat=chat_status(match_id, T0 + timedelta(minutes=15)),
        last_seq=0,
    )


def test_shared_labels_reach_the_chat_window(rig: Rig) -> None:
    match = _match().model_copy(
        update={"shared_tags": ("python",), "shared_category": "backend"}
    )
    rig.remote.push_chat(chat_state())
    rig.remote.push_break(BreakState(phase="matched", match=match))
    shown = rig.chat.shared_label.text()
    assert "Backend" in shown and "Python" in shown


def test_connection_state_reaches_the_chat_window(rig: Rig) -> None:
    rig.remote.push_chat(chat_state())
    rig.remote.push_state("offline")
    assert rig.chat.status_label.isVisibleTo(rig.chat)
    assert rig.chat.status_label.text() != ""
    rig.remote.push_state("connected")
    assert not rig.chat.status_label.isVisibleTo(rig.chat)


# --- window signals reach the worker ---


def test_window_signals_call_the_worker(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.remote.push_chat(chat_state())
    failed = uuid4()
    rig.chat.send_requested.emit("hello")
    rig.chat.retry_requested.emit(failed)
    rig.chat.extend_requested.emit()
    rig.chat.extension_answered.emit(True)
    rig.chat.extension_answered.emit(False)
    rig.chat.report_requested.emit("spam", "note", (2, 4))
    calls = rig.remote.calls
    assert calls.messages == ["hello"]
    assert calls.retries == [failed]
    assert calls.extension_requests == 1
    assert calls.extension_answers == [True, False]
    assert calls.reports == [("spam", "note", (2, 4))]
    assert calls.ends == calls.blocks == calls.chat_closes == 0
    monkeypatch.setattr(
        QMessageBox, "question", lambda *a: QMessageBox.StandardButton.Yes
    )
    rig.chat.end_button.click()
    rig.chat.block_button.click()
    assert calls.ends == 1 and calls.blocks == 1


def test_typing_and_enter_reaches_send_message(rig: Rig, qtbot: QtBot) -> None:
    from PySide6.QtCore import Qt

    rig.remote.push_chat(chat_state())
    rig.chat.input.setPlainText("hello there")
    qtbot.keyClick(rig.chat.input, Qt.Key.Key_Return)  # type: ignore[no-untyped-call]
    assert rig.remote.calls.messages == ["hello there"]


def test_closing_a_live_window_keeps_the_chat(rig: Rig) -> None:
    rig.remote.push_chat(chat_state())
    rig.chat.open_for_user()
    rig.chat.close()
    assert not rig.chat.isVisible()
    assert rig.remote.calls.chat_closes == 0


def test_closing_an_ended_window_forgets_the_chat(rig: Rig) -> None:
    rig.remote.push_chat(chat_state(ended="ended"))
    rig.chat.open_for_user()
    rig.chat.close()
    assert rig.remote.calls.chat_closes == 1
    rig.remote.push_chat(ChatState())  # the worker's answer
    assert not rig.chat.isVisible()


def test_the_close_button_of_an_ended_chat_forgets_it(rig: Rig) -> None:
    rig.remote.push_chat(chat_state(ended="expired"))
    rig.chat.open_for_user()
    rig.chat.close_button.click()
    assert rig.remote.calls.chat_closes == 1


def test_report_dialog_result_reaches_the_worker(rig: Rig) -> None:
    from PySide6.QtCore import Qt

    rig.remote.push_chat(chat_state(messages=(_peer(1), _peer(2))))
    rig.chat.report_button.click()
    dialog = rig.chat._report_dialog
    assert dialog is not None
    dialog.reason_buttons["harassment"].setChecked(True)
    dialog.message_list.item(1).setCheckState(Qt.CheckState.Checked)
    dialog.report_button.click()
    assert rig.remote.calls.reports == [("harassment", None, (2,))]


# --- unread count ---


def test_a_never_shown_window_still_counts_unread_and_notifies(rig: Rig) -> None:
    rig.window.show()
    rig.remote.push_break(BreakState(phase="matched", match=_match()))
    rig.remote.push_chat(chat_state(peer_nickname="bob_b", messages=(_peer(1),)))
    assert not rig.chat.isVisible()
    assert rig.window.open_chat_button.text() == "Open chat (1)"
    assert rig.sent() == [("Beebreak", "New message from bob_b")]


def test_unread_counts_peer_messages_while_the_window_is_not_active(
    rig: Rig,
) -> None:
    match_id = uuid4()
    rig.window.show()
    rig.remote.push_break(BreakState(phase="matched", match=_match()))
    rig.remote.push_chat(chat_state(match_id=match_id))
    assert _unread(rig) == []
    rig.remote.push_chat(
        chat_state(
            match_id=match_id,
            messages=(_peer(1), message_view(2, mine=True), _peer(3)),
        )
    )
    assert _unread(rig) == ["2"]
    rig.remote.push_chat(
        chat_state(
            match_id=match_id,
            messages=(_peer(1), message_view(2, mine=True), _peer(3), _peer(4)),
        )
    )
    assert _unread(rig) == ["3"]


def test_unread_resets_when_the_window_is_opened(rig: Rig) -> None:
    match_id = uuid4()
    rig.window.show()
    rig.remote.push_break(BreakState(phase="matched", match=_match()))
    rig.remote.push_chat(chat_state(match_id=match_id, messages=(_peer(1),)))
    assert _unread(rig) == ["1"]
    rig.window.open_chat_button.click()
    assert _unread(rig) == []
    # Only messages after that count again.
    rig.remote.push_chat(chat_state(match_id=match_id, messages=(_peer(1), _peer(2))))
    assert _unread(rig) == ["1"]


def test_unread_resets_when_the_window_becomes_active(rig: Rig) -> None:
    match_id = uuid4()
    rig.window.show()
    rig.remote.push_break(BreakState(phase="matched", match=_match()))
    rig.remote.push_chat(chat_state(match_id=match_id, messages=(_peer(1), _peer(2))))
    assert _unread(rig) == ["2"]
    rig.chat.activity_changed.emit(True)
    assert _unread(rig) == []
    rig.chat.activity_changed.emit(False)
    assert _unread(rig) == []


def test_nothing_is_unread_while_the_window_is_active(rig: Rig) -> None:
    rig.active[0] = True
    match_id = uuid4()
    rig.window.show()
    rig.remote.push_break(BreakState(phase="matched", match=_match()))
    rig.remote.push_chat(chat_state(match_id=match_id, messages=(_peer(1),)))
    assert _unread(rig) == []
    assert rig.sent() == []


def test_a_new_match_starts_from_zero(rig: Rig) -> None:
    rig.window.show()
    rig.remote.push_break(BreakState(phase="matched", match=_match()))
    rig.remote.push_chat(chat_state(messages=(_peer(1), _peer(2))))
    assert _unread(rig) == ["2"]
    rig.remote.push_chat(chat_state())
    assert _unread(rig) == []


def test_no_chat_means_no_unread(rig: Rig) -> None:
    rig.window.show()
    rig.remote.push_break(BreakState(phase="matched", match=_match()))
    rig.remote.push_chat(chat_state(messages=(_peer(1),)))
    rig.remote.push_chat(ChatState())
    assert _unread(rig) == []


# --- notifications ---


def test_a_peer_message_notifies_with_the_nickname_only(rig: Rig) -> None:
    match_id = uuid4()
    rig.remote.push_chat(chat_state(match_id=match_id, peer_nickname="bob_b"))
    assert rig.sent() == []
    rig.remote.push_chat(
        chat_state(
            match_id=match_id,
            peer_nickname="bob_b",
            messages=(_peer(1, CANARY),),
        )
    )
    assert rig.sent() == [("Beebreak", "New message from bob_b")]


def test_own_and_pending_messages_never_notify(rig: Rig) -> None:
    match_id = uuid4()
    rig.remote.push_chat(
        chat_state(
            match_id=match_id,
            messages=(message_view(1, mine=True, body=CANARY),),
            pending=(PendingMessage(uuid4(), CANARY, "sending"),),
        )
    )
    assert rig.sent() == []


def test_no_notification_while_the_window_is_active(rig: Rig) -> None:
    rig.active[0] = True
    rig.remote.push_chat(chat_state(messages=(_peer(1),)))
    assert rig.sent() == []


def test_notifications_are_throttled_per_chat(rig: Rig) -> None:
    match_id = uuid4()
    rig.remote.push_chat(chat_state(match_id=match_id, messages=(_peer(1),)))
    assert len(rig.sent()) == 1
    rig.now[0] += 5
    rig.remote.push_chat(chat_state(match_id=match_id, messages=(_peer(1), _peer(2))))
    assert len(rig.sent()) == 1  # within 10 s
    rig.now[0] += 5.1  # 10.1 s after the first
    rig.remote.push_chat(
        chat_state(match_id=match_id, messages=(_peer(1), _peer(2), _peer(3)))
    )
    assert len(rig.sent()) == 2


def test_the_notification_interval_follows_the_environment(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BEEBREAK_NOTIFY_INTERVAL_S", "2")
    match_id = uuid4()
    rig.remote.push_chat(chat_state(match_id=match_id, messages=(_peer(1),)))
    rig.now[0] += 2.5
    rig.remote.push_chat(chat_state(match_id=match_id, messages=(_peer(1), _peer(2))))
    assert len(rig.sent()) == 2


def test_a_state_without_a_new_peer_message_does_not_notify(rig: Rig) -> None:
    match_id = uuid4()
    messages = (_peer(1),)
    rig.remote.push_chat(chat_state(match_id=match_id, messages=messages))
    rig.now[0] += 60
    rig.remote.push_chat(
        chat_state(match_id=match_id, messages=messages, peer_nickname="bob")
    )
    assert len(rig.sent()) == 1


def test_a_new_chat_has_its_own_throttle(rig: Rig) -> None:
    rig.remote.push_chat(chat_state(messages=(_peer(1),)))
    rig.now[0] += 1
    rig.remote.push_chat(chat_state(messages=(_peer(1),)))
    assert len(rig.sent()) == 2


def test_a_hostile_nickname_is_validated_in_the_notification(rig: Rig) -> None:
    rig.remote.push_chat(chat_state(peer_nickname="<b>x</b>", messages=(_peer(1),)))
    assert rig.sent() == [("Beebreak", "New message from ?")]


def test_no_body_reaches_a_notification_or_a_log(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    match_id = uuid4()
    rig.remote.push_chat(
        chat_state(
            match_id=match_id,
            messages=tuple(_peer(i, CANARY) for i in range(1, 4)),
        )
    )
    rig.now[0] += 30
    rig.remote.push_chat(
        chat_state(
            match_id=match_id,
            messages=tuple(_peer(i, CANARY) for i in range(1, 6)),
        )
    )
    assert len(rig.sent()) == 2
    for title, text in rig.sent():
        assert CANARY not in title and CANARY not in text
    assert CANARY not in caplog.text
    assert CANARY not in rig.window.open_chat_button.text()


# --- BreakTracker (M5-07) ---


def test_the_tracker_reports_chatting_while_matched() -> None:
    tracker = BreakTracker()
    assert tracker.queue_state() == "none"
    tracker.set_phase("searching")
    assert tracker.queue_state() == "searching"
    tracker.set_phase("matched")
    assert tracker.queue_state() == "chatting"
    tracker.set_phase("ended")
    assert tracker.queue_state() == "none"


# --- review fixes ---


def test_remote_errors_reach_the_chat_window_as_fixed_lines(rig: Rig) -> None:
    rig.remote.push_chat(chat_state())
    rig.remote.push_error("report_window_closed")
    shown = rig.chat.error_label.text()
    assert shown != "" and rig.chat.error_label.isVisibleTo(rig.chat)
    rig.remote.push_error("unauthorized")
    assert rig.chat.error_label.text() == shown  # not an action error: unchanged


def test_an_ended_chat_stays_reachable_from_the_main_window(rig: Rig) -> None:
    rig.window.show()
    match_id = uuid4()
    rig.remote.push_break(BreakState(phase="matched", match=_match()))
    rig.remote.push_chat(chat_state(match_id=match_id))
    rig.chat.close()  # hidden during the live chat
    rig.remote.push_break(BreakState(phase="ended", match=_match()))  # chat still live
    assert not rig.window.open_chat_button.isVisibleTo(rig.window)
    rig.remote.push_chat(chat_state(match_id=match_id, ended="ended"))
    assert rig.window.open_chat_button.isVisibleTo(rig.window)
    rig.window.open_chat_button.click()
    assert rig.chat.isVisible()
    rig.chat.close_button.click()
    rig.remote.push_chat(ChatState())  # the worker forgot it
    assert not rig.window.open_chat_button.isVisibleTo(rig.window)

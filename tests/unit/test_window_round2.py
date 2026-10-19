"""Round-2 main-window fixes (R2-B): O1, O2, L1, U2, U3, U4, U7, X6, D2."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication, QMessageBox, QWidget
from pytestqt.qtbot import QtBot

from beebreak_companion.break_state import BreakState
from beebreak_companion.chat_window import ChatWindow
from beebreak_companion.config import TriggerConfig, queue_wait_s
from beebreak_companion.window import CompanionWindow
from beebreak_shared.api import MatchView, QueueEntryView
from tests.fakes.chat import T0, chat_state, chat_status

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(hours=1)


@pytest.fixture
def window(qtbot: QtBot) -> CompanionWindow:
    w = CompanionWindow()
    qtbot.addWidget(w)
    return w


def _match() -> MatchView:
    match_id = uuid4()
    return MatchView(
        match_id=match_id,
        peer_nickname="bob_b",
        ends_at=T0 + timedelta(minutes=15),
        chat=chat_status(match_id, T0 + timedelta(minutes=15)),
        last_seq=0,
    )


def _searching() -> BreakState:
    entry = QueueEntryView(entry_id=uuid4(), status="queued", expires_at=LATER)
    return BreakState(phase="searching", entry=entry)


def _matched() -> BreakState:
    return BreakState(phase="matched", match=_match())


def _inside(widget: QWidget, window: QWidget) -> bool:
    top_left = widget.mapTo(window, QPoint(0, 0))
    return window.rect().contains(QRect(top_left, widget.size()))


# --- O1 ---


def test_busy_agent_is_not_reported_as_idle_time(window: CompanionWindow) -> None:
    window.show()
    window.set_last_activity(NOW - timedelta(minutes=2), NOW)
    idle_text = window.activity_label.text()
    window.set_last_activity(NOW - timedelta(minutes=2), NOW, active=True)
    assert window.activity_label.isVisible()
    assert window.activity_label.text() != idle_text
    assert "ago" not in window.activity_label.text()


def test_idle_line_states_the_rule_from_the_config(qtbot: QtBot) -> None:
    config = TriggerConfig(activity_threshold_s=77, suggestion_timeout_s=33)
    window = CompanionWindow(config)
    qtbot.addWidget(window)
    window.show()
    window.set_state("idle")
    assert window.rule_label.isVisible()
    assert "77" in window.rule_label.text() and "33" in window.rule_label.text()


def test_rule_is_not_shown_while_an_offer_is_pending(window: CompanionWindow) -> None:
    window.show()
    window.set_state("suggested")
    assert not window.rule_label.isVisible()


def test_expired_offer_says_so_until_the_next_state(window: CompanionWindow) -> None:
    window.show()
    window.set_state("suggested")
    assert not window.expired_label.isVisible()
    window.set_state("idle", expired=True)
    assert window.expired_label.isVisible()
    window.set_state("suggested")
    assert not window.expired_label.isVisible()


def test_an_offer_withdrawn_without_expiry_says_nothing(
    window: CompanionWindow,
) -> None:
    window.show()
    window.set_state("suggested")
    window.set_state("idle")
    assert not window.expired_label.isVisible()


def test_the_expired_notice_clears_when_a_break_starts(
    window: CompanionWindow,
) -> None:
    window.show()
    window.set_state("idle", expired=True)
    window.set_break(_searching())
    assert not window.expired_label.isVisible()


# --- O2, L1 ---


def test_the_offer_appearing_does_not_move_find(window: CompanionWindow) -> None:
    window.set_ready(True)
    window.show()
    QApplication.processEvents()
    before = window.find_button.mapTo(window, QPoint(0, 0))
    window.set_state("suggested", "10_20m")
    QApplication.processEvents()
    assert window.find_button.mapTo(window, QPoint(0, 0)) == before
    window.set_state("idle", expired=True)  # the notice replaces the offer
    QApplication.processEvents()
    assert window.find_button.mapTo(window, QPoint(0, 0)) == before


def test_content_is_never_clipped_at_the_default_size(window: CompanionWindow) -> None:
    window.set_ready(True)
    window.show()
    window.set_state("suggested", "20m_plus")
    window.set_break(BreakState(phase="matched", match=_match()))
    window.set_chat_held(True)
    window.set_unread(3)
    QApplication.processEvents()
    for widget in (window.status_label, window.open_chat_button, window.account_label):
        assert widget.isVisibleTo(window)
        assert _inside(widget, window)
    assert window.size().expandedTo(window.minimumSizeHint()) == window.size()


# --- U2, U3, U4, U7 ---


def test_find_is_available_after_a_break_ended(window: CompanionWindow) -> None:
    window.set_ready(True)
    window.show()
    window.set_break(BreakState(phase="ended"))
    assert window.find_button.isVisible()


def test_open_chat_does_not_offer_a_blocked_chat(window: CompanionWindow) -> None:
    window.show()
    window.set_break(BreakState(phase="ended", match=_match(), blocked=False))
    window.set_chat_held(True)
    assert window.open_chat_button.isVisible()
    window.set_break(BreakState(phase="ended", match=_match(), blocked=True))
    assert not window.open_chat_button.isVisible()


def test_searching_shows_the_wait_from_config(
    window: CompanionWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BEEBREAK_QUEUE_WAIT_S", "47")
    window.show()
    window.set_break(BreakState(phase="joining"))  # no queue entry yet
    assert "47" in window.break_label.text()


def test_searching_shows_the_servers_real_expiry_over_the_config(
    window: CompanionWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BEEBREAK_QUEUE_WAIT_S", "47")
    entry = QueueEntryView(
        entry_id=uuid4(),
        status="queued",
        expires_at=datetime.now(UTC) + timedelta(seconds=71),
    )
    window.show()
    window.set_break(BreakState(phase="searching", entry=entry))
    text = window.break_label.text()
    assert "71" in text and "47" not in text


def test_a_huge_wait_is_not_printed_in_exponent_form(
    window: CompanionWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BEEBREAK_QUEUE_WAIT_S", "1000000000")
    window.show()
    window.set_break(BreakState(phase="joining"))
    assert "e+" not in window.break_label.text()


def test_queue_wait_default_and_invalid_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BEEBREAK_QUEUE_WAIT_S", raising=False)
    assert queue_wait_s() > 0
    monkeypatch.setenv("BEEBREAK_QUEUE_WAIT_S", "-3")
    assert queue_wait_s() > 0


def test_no_keyring_notice_says_what_to_do(window: CompanionWindow) -> None:
    assert "keyring" in window.account_note.text().lower()


# --- X6 ---


def test_declined_extension_cue_follows_the_flag(window: CompanionWindow) -> None:
    window.show()
    assert not window.extension_label.isVisible()
    window.set_extension_declined(True)
    assert window.extension_label.isVisible()
    window.set_extension_declined(False)
    assert not window.extension_label.isVisible()


# --- D2 ---


def _answer(
    monkeypatch: pytest.MonkeyPatch, button: QMessageBox.StandardButton
) -> list[str]:
    asked: list[str] = []

    def question(parent: object, title: str, text: str, *args: object) -> object:
        asked.append(title)
        return button

    monkeypatch.setattr(QMessageBox, "question", question)
    return asked


def test_closing_during_a_search_asks_and_stays_on_no(
    window: CompanionWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked = _answer(monkeypatch, QMessageBox.StandardButton.No)
    window.show()
    window.set_break(_searching())
    assert window.may_close() is False
    assert len(asked) == 1


def test_closing_during_a_search_quits_on_yes(
    window: CompanionWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked = _answer(monkeypatch, QMessageBox.StandardButton.Yes)
    window.show()
    window.set_break(_matched())
    assert window.may_close() is True
    assert len(asked) == 1


def test_closing_without_a_break_does_not_ask(
    window: CompanionWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked = _answer(monkeypatch, QMessageBox.StandardButton.No)
    window.show()
    assert window.may_close() is True
    assert asked == []


def test_closing_with_the_chat_window_open_does_not_ask(
    window: CompanionWindow, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked = _answer(monkeypatch, QMessageBox.StandardButton.No)
    chat = ChatWindow()
    qtbot.addWidget(chat)
    window.show()
    chat.show()
    window.set_break(_matched())
    assert window.may_close() is True
    assert asked == []


def test_a_close_made_by_code_never_asks(
    window: CompanionWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked = _answer(monkeypatch, QMessageBox.StandardButton.No)
    window.show()
    window.set_break(_searching())
    assert window.close() is True
    assert asked == []


class _UserClose(QCloseEvent):
    """A close the user made (title bar), which Qt marks as spontaneous."""

    def spontaneous(self) -> bool:
        return True


def test_a_user_close_during_a_search_ignores_on_no_and_accepts_on_yes(
    window: CompanionWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    window.show()
    window.set_break(_searching())
    _answer(monkeypatch, QMessageBox.StandardButton.No)
    event = _UserClose()
    window.closeEvent(event)
    assert not event.isAccepted()
    _answer(monkeypatch, QMessageBox.StandardButton.Yes)
    event = _UserClose()
    window.closeEvent(event)
    assert event.isAccepted()


def test_a_popup_is_not_another_window(
    window: CompanionWindow, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked = _answer(monkeypatch, QMessageBox.StandardButton.No)
    tip = QWidget(None, Qt.WindowType.ToolTip)
    qtbot.addWidget(tip)
    tip.show()
    window.show()
    window.set_break(_searching())
    assert window.may_close() is False
    assert len(asked) == 1


def _chat_window(qtbot: QtBot, busy: bool, ended: bool) -> ChatWindow:
    chat = ChatWindow(busy=lambda: busy)
    qtbot.addWidget(chat)
    chat.set_chat(chat_state(ended="ended" if ended else None))
    chat.show()
    return chat


@pytest.mark.parametrize(
    ("busy", "ended", "asks"),
    [
        (False, False, True),  # live chat
        (True, True, True),  # chat ended, a new search is running
        (False, True, False),  # nothing active
    ],
)
def test_the_last_chat_window_asks_only_when_something_is_active(
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
    busy: bool,
    ended: bool,
    asks: bool,
) -> None:
    chat = _chat_window(qtbot, busy, ended)
    asked = _answer(monkeypatch, QMessageBox.StandardButton.No)
    event = _UserClose()
    chat.closeEvent(event)
    assert len(asked) == (1 if asks else 0)
    assert event.isAccepted() is (not asks)
    _answer(monkeypatch, QMessageBox.StandardButton.Yes)
    event = _UserClose()
    chat.closeEvent(event)
    assert event.isAccepted()

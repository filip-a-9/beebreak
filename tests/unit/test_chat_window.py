"""ChatWindow (M5-06): hand-built ChatStates, a fake clock, offscreen Qt."""

import re
from datetime import timedelta
from uuid import uuid4

import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QMessageBox, QPushButton
from pytestqt.qtbot import QtBot

from beebreak_companion.chat_state import ChatEnd, ChatState, PendingMessage
from beebreak_companion.chat_window import ChatWindow, _BodyLabel
from beebreak_companion.report_dialog import ReportDialog
from beebreak_companion.window import CompanionWindow
from tests.fakes.chat import T0, chat_state, chat_status, message_view

FOOTER = "Not end-to-end encrypted. Messages are deleted 24 h after the chat ends."


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def window(qtbot: QtBot, clock: Clock) -> ChatWindow:
    w = ChatWindow(clock=clock, now=lambda: T0)
    qtbot.addWidget(w)
    w.show()
    return w


def _labels(window: ChatWindow) -> list[QLabel]:
    return window.transcript.findChildren(QLabel)


def _texts(window: ChatWindow) -> list[str]:
    return [label.text() for label in _labels(window)]


def _numbers(label: QLabel) -> list[str]:
    """The integers in a label, in order: the data a sentence is built from."""
    return re.findall(r"\d+", label.text())


def _visible(window: ChatWindow, *names: str) -> bool:
    return all(getattr(window, n).isVisibleTo(window) for n in names)


def _answer(
    monkeypatch: pytest.MonkeyPatch, button: QMessageBox.StandardButton
) -> list[str]:
    asked: list[str] = []

    def question(parent: object, title: str, text: str, *args: object) -> object:
        asked.append(text)
        return button

    monkeypatch.setattr(QMessageBox, "question", question)
    return asked


# --- header, footer, status line ---


def test_header_shows_the_nickname_and_the_footer_is_always_there(
    window: ChatWindow,
) -> None:
    assert window.footer_label.text() == FOOTER
    assert window.footer_label.isVisibleTo(window)
    window.set_chat(chat_state(peer_nickname="bob_b"))
    assert window.peer_label.text() == "bob_b"
    assert window.footer_label.isVisibleTo(window)
    window.set_chat(chat_state(peer_nickname="bob_b", ended="ended"))
    assert window.footer_label.isVisibleTo(window)


@pytest.mark.parametrize("nickname", ["<b>x</b>", "a b", "x" * 40])
def test_a_nickname_failing_the_pattern_shows_a_question_mark(
    window: ChatWindow, nickname: str
) -> None:
    window.set_chat(chat_state(peer_nickname=nickname))
    assert window.peer_label.text() == "?"


def test_shared_labels_show_while_chatting(window: ChatWindow) -> None:
    window.set_chat(chat_state())
    window.set_shared(["Backend", "Python"])
    shown = window.shared_label.text()
    assert "Backend" in shown and "Python" in shown
    assert window.shared_label.isVisibleTo(window)
    window.set_shared([])
    assert not window.shared_label.isVisibleTo(window)


def test_status_line_for_a_reconnecting_peer(window: ChatWindow) -> None:
    match_id = uuid4()
    window.set_chat(
        chat_state(
            match_id=match_id,
            status=chat_status(match_id, T0 + timedelta(minutes=15), peer_online=False),
        )
    )
    assert "bob" in window.status_label.text()
    assert window.status_label.isVisibleTo(window)
    window.set_chat(chat_state(match_id=match_id))
    assert not window.status_label.isVisibleTo(window)


def test_status_line_when_this_companion_is_offline(window: ChatWindow) -> None:
    window.set_chat(chat_state())
    window.set_connected(False)
    assert window.status_label.text() != ""
    assert not window.send_button.isEnabled()
    window.set_connected(True)
    assert not window.status_label.isVisibleTo(window)


def test_the_status_line_is_gone_once_the_chat_ended(window: ChatWindow) -> None:
    window.set_chat(chat_state(ended="ended"))
    window.set_connected(False)
    assert not window.status_label.isVisibleTo(window)


# --- transcript and plain text (rule 17) ---

HOSTILE = '<b>x</b> <a href="file:///etc/passwd">link</a> ‮evil‬'


def test_a_hostile_body_is_shown_literally_as_plain_text(window: ChatWindow) -> None:
    window.set_chat(chat_state(messages=(message_view(1, body=HOSTILE),)))
    (label,) = _labels(window)
    assert label.textFormat() == Qt.TextFormat.PlainText
    assert (
        label.text().replace("\u200b", "")
        == '<b>x</b> <a href="file:///etc/passwd">link</a> evil'
    )
    assert not label.openExternalLinks()
    flags = label.textInteractionFlags()
    assert flags & Qt.TextInteractionFlag.TextSelectableByMouse
    assert not flags & Qt.TextInteractionFlag.LinksAccessibleByMouse
    assert not flags & Qt.TextInteractionFlag.LinksAccessibleByKeyboard
    assert label.wordWrap()


def test_clicking_a_body_opens_nothing(
    window: ChatWindow, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    from PySide6.QtGui import QDesktopServices

    opened: list[object] = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url))
    window.set_chat(chat_state(messages=(message_view(1, body=HOSTILE),)))
    (label,) = _labels(window)
    activated: list[str] = []
    label.linkActivated.connect(activated.append)
    qtbot.mouseClick(label, Qt.MouseButton.LeftButton)  # type: ignore[no-untyped-call]
    assert opened == [] and activated == []


def test_own_messages_align_right_and_peer_messages_left(window: ChatWindow) -> None:
    window.set_chat(
        chat_state(
            messages=(
                message_view(1, body="from bob"),
                message_view(2, mine=True, body="from me"),
            )
        )
    )
    by_text = {label.text(): label for label in _labels(window)}
    assert by_text["from bob"].alignment() & Qt.AlignmentFlag.AlignLeft
    assert by_text["from me"].alignment() & Qt.AlignmentFlag.AlignRight
    assert _texts(window) == ["from bob", "from me"]


def test_transcript_follows_the_state(window: ChatWindow) -> None:
    match_id = uuid4()
    window.set_chat(chat_state(match_id=match_id, messages=(message_view(1),)))
    window.set_chat(
        chat_state(match_id=match_id, messages=(message_view(1), message_view(2)))
    )
    assert _texts(window) == ["message 1", "message 2"]
    window.set_chat(ChatState())
    assert _texts(window) == []


def test_pending_messages_show_their_mark(window: ChatWindow, qtbot: QtBot) -> None:
    sending, failed = uuid4(), uuid4()
    window.set_chat(
        chat_state(
            pending=(
                PendingMessage(sending, "one", "sending"),
                PendingMessage(failed, "two", "failed"),
            )
        )
    )
    texts = _texts(window)
    assert "one" in texts and "two" in texts
    assert len(texts) == 3  # two bodies and the one "sending" mark
    (retry,) = [b for b in window.transcript.findChildren(QPushButton)]
    assert retry.text() != ""
    with qtbot.waitSignal(window.retry_requested, timeout=1000) as blocker:
        retry.click()
    assert blocker.args == [failed]


# --- countdown ---


def _left(window: ChatWindow) -> str:
    window.tick()
    return window.time_label.text()


def test_countdown_in_whole_minutes(window: ChatWindow, clock: Clock) -> None:
    window.set_chat(chat_state(ends_at=T0 + timedelta(minutes=12)))
    assert _left(window) == "12 min left"
    clock.t += 30
    assert _left(window) == "12 min left"
    clock.t += 30  # exactly 11:00 remain
    assert _left(window) == "11 min left"
    clock.t += 1
    assert _left(window) == "11 min left"


def test_countdown_under_a_minute(window: ChatWindow, clock: Clock) -> None:
    window.set_chat(chat_state(ends_at=T0 + timedelta(seconds=90)))
    assert _left(window) == "2 min left"
    clock.t += 31
    assert _left(window) == "< 1 min left"
    clock.t += 1000
    assert _left(window) == "< 1 min left"


def test_countdown_uses_the_server_offset(window: ChatWindow) -> None:
    ends = T0 + timedelta(minutes=12)
    window.set_chat(chat_state(ends_at=ends, server_offset_s=0))
    assert _left(window) == "12 min left"
    # The server's clock is five minutes ahead of ours.
    window.set_chat(chat_state(ends_at=ends, server_offset_s=300))
    assert _left(window) == "7 min left"


def test_countdown_ignores_later_changes_of_the_local_wall_clock(
    qtbot: QtBot, clock: Clock
) -> None:
    now = [T0]
    w = ChatWindow(clock=clock, now=lambda: now[0])
    qtbot.addWidget(w)
    w.set_chat(chat_state(ends_at=T0 + timedelta(minutes=12)))
    now[0] = T0 + timedelta(hours=5)  # the user changed the system clock
    assert _left(w) == "12 min left"


def test_no_countdown_after_the_end(window: ChatWindow) -> None:
    window.set_chat(chat_state(ended="expired"))
    assert not window.time_label.isVisibleTo(window)


def test_the_timer_runs_only_while_live_and_visible(qtbot: QtBot, clock: Clock) -> None:
    w = ChatWindow(clock=clock, now=lambda: T0)
    qtbot.addWidget(w)
    w.set_chat(chat_state())
    assert not w._timer.isActive()  # hidden
    w.show()
    assert w._timer.isActive()
    assert w._timer.interval() == 1000
    w.set_chat(chat_state(ended="ended"))
    assert not w._timer.isActive()


# --- grace and extension banner ---


def _grace_state(
    *,
    extension: str = "none",
    extensions_left: int = 2,
    grace_s: int = 120,
    extension_s: int = 600,
    left_s: int = 100,
) -> ChatState:
    match_id = uuid4()
    return chat_state(
        match_id=match_id,
        status=chat_status(
            match_id,
            T0 + timedelta(seconds=left_s),
            grace_s=grace_s,
            extensions_left=extensions_left,
            extension_s=extension_s,
            extension=extension,  # type: ignore[arg-type]
        ),
    )


def _banner_buttons(window: ChatWindow) -> set[str]:
    named = {
        "extend": window.extend_button,
        "cancel": window.cancel_extension_button,
        "yes": window.yes_button,
        "no": window.no_button,
    }
    return {n for n, b in named.items() if b.isVisibleTo(window)}


def test_no_banner_before_the_grace_period(window: ChatWindow) -> None:
    window.set_chat(_grace_state(left_s=600))
    assert not window.banner.isVisibleTo(window)


def test_the_grace_banner_offers_to_extend(window: ChatWindow, qtbot: QtBot) -> None:
    window.set_chat(_grace_state())
    assert _numbers(window.banner_label) == ["2", "10"]
    assert _banner_buttons(window) == {"extend"}
    with qtbot.waitSignal(window.extend_requested, timeout=1000):
        window.extend_button.click()


def test_the_banner_appears_when_the_clock_reaches_the_grace_period(
    window: ChatWindow, clock: Clock
) -> None:
    window.set_chat(_grace_state(left_s=130))
    assert not window.banner.isVisibleTo(window)
    clock.t += 11
    window.tick()
    assert window.banner.isVisibleTo(window)
    assert _banner_buttons(window) == {"extend"}


def test_the_banner_minutes_come_from_the_status(window: ChatWindow) -> None:
    window.set_chat(_grace_state(grace_s=300, extension_s=1200, left_s=250))
    assert _numbers(window.banner_label) == ["5", "20"]
    window.set_chat(_grace_state(grace_s=60, extension_s=60, left_s=30))
    assert _numbers(window.banner_label) == ["1", "1"]


def test_banner_without_extensions_left(window: ChatWindow) -> None:
    window.set_chat(_grace_state(extensions_left=0))
    assert _numbers(window.banner_label) == ["2"]
    assert _banner_buttons(window) == set()
    assert window.banner.isVisibleTo(window)


def test_banner_when_i_asked(window: ChatWindow, qtbot: QtBot) -> None:
    window.set_chat(_grace_state(extension="requested_by_me"))
    assert "bob" in window.banner_label.text()
    assert _banner_buttons(window) == {"cancel"}
    with qtbot.waitSignal(window.extension_answered, timeout=1000) as blocker:
        window.cancel_extension_button.click()
    assert blocker.args == [False]


def test_banner_when_the_peer_asked_shows_at_any_time(
    window: ChatWindow, qtbot: QtBot
) -> None:
    window.set_chat(_grace_state(extension="requested_by_peer", left_s=800))
    assert "bob" in window.banner_label.text()
    assert _numbers(window.banner_label) == ["10"]
    assert _banner_buttons(window) == {"yes", "no"}
    with qtbot.waitSignal(window.extension_answered, timeout=1000) as yes:
        window.yes_button.click()
    assert yes.args == [True]
    with qtbot.waitSignal(window.extension_answered, timeout=1000) as no:
        window.no_button.click()
    assert no.args == [False]


def test_a_pending_request_shows_even_without_extensions_left(
    window: ChatWindow,
) -> None:
    window.set_chat(_grace_state(extension="requested_by_me", extensions_left=0))
    assert _banner_buttons(window) == {"cancel"}


def test_no_banner_while_ended(window: ChatWindow) -> None:
    state = _grace_state(extension="requested_by_peer")
    window.set_chat(
        ChatState(
            match_id=state.match_id,
            peer_nickname=state.peer_nickname,
            status=state.status,
            ended="expired",
        )
    )
    assert not window.banner.isVisibleTo(window)
    assert _banner_buttons(window) == set()


# --- input ---


def test_enter_sends_the_raw_text_and_clears_the_input(
    window: ChatWindow, qtbot: QtBot
) -> None:
    window.set_chat(chat_state())
    window.input.setPlainText("  hello  ")
    with qtbot.waitSignal(window.send_requested, timeout=1000) as blocker:
        qtbot.keyClick(window.input, Qt.Key.Key_Return)  # type: ignore[no-untyped-call]
    assert blocker.args == ["  hello  "]
    assert window.input.toPlainText() == ""


def test_shift_enter_inserts_a_newline(window: ChatWindow, qtbot: QtBot) -> None:
    window.set_chat(chat_state())
    window.input.setPlainText("a")
    window.input.moveCursor(window.input.textCursor().MoveOperation.End)
    with qtbot.assertNotEmitted(window.send_requested):
        qtbot.keyClick(  # type: ignore[no-untyped-call]
            window.input, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier
        )
    assert window.input.toPlainText() == "a\n"


def test_send_button_sends(window: ChatWindow, qtbot: QtBot) -> None:
    window.set_chat(chat_state())
    window.input.setPlainText("hi")
    assert window.send_button.isEnabled()
    with qtbot.waitSignal(window.send_requested, timeout=1000) as blocker:
        window.send_button.click()
    assert blocker.args == ["hi"]


@pytest.mark.parametrize("text", ["", "   ", "\n\n", "‮‏"])
def test_empty_after_normalizing_keeps_send_disabled(
    window: ChatWindow, qtbot: QtBot, text: str
) -> None:
    window.set_chat(chat_state())
    window.input.setPlainText(text)
    assert not window.send_button.isEnabled()
    with qtbot.assertNotEmitted(window.send_requested):
        qtbot.keyClick(window.input, Qt.Key.Key_Return)  # type: ignore[no-untyped-call]


def test_the_length_limit_and_counter(window: ChatWindow) -> None:
    window.set_chat(chat_state())
    window.input.setPlainText("x" * 899)
    assert not window.counter_label.isVisibleTo(window)
    window.input.setPlainText("x" * 900)
    assert window.counter_label.isVisibleTo(window)
    assert _numbers(window.counter_label) == ["900", "1000"]
    assert window.send_button.isEnabled()
    window.input.setPlainText("x" * 1000)
    assert window.send_button.isEnabled()
    window.input.setPlainText("x" * 1001)
    assert not window.send_button.isEnabled()
    assert _numbers(window.counter_label) == ["1001", "1000"]


def test_input_is_disabled_without_a_live_chat(window: ChatWindow) -> None:
    assert not window.input.isEnabled()
    window.set_chat(chat_state())
    assert window.input.isEnabled()
    window.set_chat(chat_state(ended="ended"))
    assert not window.input.isEnabled()
    assert not window.send_button.isEnabled()


def test_every_button_refuses_keyboard_focus(window: ChatWindow) -> None:
    for button in window.findChildren(QPushButton):
        assert button.focusPolicy() == Qt.FocusPolicy.NoFocus


def test_the_window_does_not_take_focus_when_shown(window: ChatWindow) -> None:
    assert window.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)


# --- end, block, report, close ---


def test_end_needs_confirmation(
    window: ChatWindow, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    window.set_chat(chat_state())
    asked = _answer(monkeypatch, QMessageBox.StandardButton.No)
    with qtbot.assertNotEmitted(window.end_requested):
        window.end_button.click()
    assert len(asked) == 1
    _answer(monkeypatch, QMessageBox.StandardButton.Yes)
    with qtbot.waitSignal(window.end_requested, timeout=1000):
        window.end_button.click()


def test_block_needs_confirmation(
    window: ChatWindow, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    window.set_chat(chat_state())
    asked = _answer(monkeypatch, QMessageBox.StandardButton.No)
    with qtbot.assertNotEmitted(window.block_requested):
        window.block_button.click()
    assert asked == ["You won't be matched with this person again."]
    _answer(monkeypatch, QMessageBox.StandardButton.Yes)
    with qtbot.waitSignal(window.block_requested, timeout=1000):
        window.block_button.click()


@pytest.mark.parametrize("reason", ["ended", "expired", "disconnected"])
def test_ended_chat_shows_one_line_and_disables_input(
    window: ChatWindow, qtbot: QtBot, reason: ChatEnd
) -> None:
    window.set_chat(chat_state(ended=reason, messages=(message_view(1),)))
    assert window.ended_label.text() != ""
    assert window.ended_label.isVisibleTo(window)
    assert not window.input.isEnabled()
    assert not window.end_button.isVisibleTo(window)
    assert window.report_button.isEnabled()
    assert window.close_button.isVisibleTo(window)
    assert _texts(window) == ["message 1"]  # the transcript stays
    with qtbot.waitSignal(window.close_requested, timeout=1000):
        window.close_button.click()


def test_live_chat_has_no_close_button_or_ended_line(window: ChatWindow) -> None:
    window.set_chat(chat_state())
    assert not window.ended_label.isVisibleTo(window)
    assert not window.close_button.isVisibleTo(window)
    assert _visible(window, "end_button", "block_button", "report_button")


def test_report_is_disabled_once_reported(window: ChatWindow) -> None:
    window.set_chat(chat_state(ended="ended"))
    assert window.report_button.isEnabled()
    window.set_chat(chat_state(ended="ended", reported=True))
    assert not window.report_button.isEnabled()
    assert not window.block_button.isVisibleTo(window)  # the report blocked


def test_report_button_opens_the_dialog_and_relays_its_result(
    window: ChatWindow, qtbot: QtBot
) -> None:
    window.set_chat(
        chat_state(
            messages=(message_view(1), message_view(2, mine=True), message_view(3)),
            pending=(PendingMessage(uuid4(), "unsent", "failed"),),
        )
    )
    window.report_button.click()
    dialog = window._report_dialog
    assert isinstance(dialog, ReportDialog)
    assert dialog.message_list.count() == 3  # pending messages are not listed
    dialog.reason_buttons["spam"].setChecked(True)
    dialog.message_list.item(2).setCheckState(Qt.CheckState.Checked)
    dialog.note_edit.setPlainText("rude")
    with qtbot.waitSignal(window.report_requested, timeout=1000) as blocker:
        dialog.report_button.click()
    assert blocker.args == ["spam", "rude", (3,)]


def test_cancelling_the_report_dialog_emits_nothing(
    window: ChatWindow, qtbot: QtBot
) -> None:
    window.set_chat(chat_state(messages=(message_view(1),)))
    window.report_button.click()
    dialog = window._report_dialog
    assert isinstance(dialog, ReportDialog)
    with qtbot.assertNotEmitted(window.report_requested):
        dialog.cancel_button.click()
    assert window._report_dialog is None


def test_closing_a_live_window_only_hides_it(window: ChatWindow, qtbot: QtBot) -> None:
    window.set_chat(chat_state())
    with qtbot.assertNotEmitted(window.close_requested):
        window.close()
    assert not window.isVisible()


def test_closing_an_ended_window_asks_to_forget_the_chat(
    window: ChatWindow, qtbot: QtBot
) -> None:
    window.set_chat(chat_state(ended="ended"))
    with qtbot.waitSignal(window.close_requested, timeout=1000):
        window.close()


def test_the_close_event_of_a_live_chat_is_ignored(
    window: ChatWindow, qtbot: QtBot
) -> None:
    main = CompanionWindow()
    qtbot.addWidget(main)
    main.show()  # with the main window on screen, closing only hides
    window.set_chat(chat_state())
    event = QCloseEvent()
    window.closeEvent(event)
    assert not event.isAccepted()
    main.hide()
    event = QCloseEvent()
    window.closeEvent(event)  # nothing else is visible: let the app quit
    assert event.isAccepted()


def test_an_empty_state_hides_the_window_and_clears_everything(
    window: ChatWindow,
) -> None:
    window.set_chat(chat_state(messages=(message_view(1),)))
    window.set_shared(["Go"])
    window.set_chat(ChatState())
    assert not window.isVisible()
    assert window.peer_label.text() == ""
    assert _texts(window) == []
    assert not window.report_button.isVisibleTo(window)


def test_a_new_match_clears_the_draft(window: ChatWindow) -> None:
    window.set_chat(chat_state())
    window.input.setPlainText("draft")
    window.set_chat(chat_state())  # another match id
    assert window.input.toPlainText() == ""


def test_the_same_chat_keeps_the_draft(window: ChatWindow) -> None:
    match_id = uuid4()
    window.set_chat(chat_state(match_id=match_id))
    window.input.setPlainText("draft")
    window.set_chat(chat_state(match_id=match_id, messages=(message_view(1),)))
    assert window.input.toPlainText() == "draft"


def test_open_for_user_shows_and_raises(window: ChatWindow, qtbot: QtBot) -> None:
    window.set_chat(chat_state())
    window.hide()
    window.open_for_user()
    assert window.isVisible()


def test_every_label_is_plain_text(window: ChatWindow) -> None:
    window.set_chat(chat_state(messages=(message_view(1),)))
    labels = window.findChildren(QLabel)
    assert labels
    for label in labels:
        assert label.textFormat() == Qt.TextFormat.PlainText
        assert not label.openExternalLinks()


# --- review fixes ---


def test_a_report_dialog_does_not_outlive_its_chat(
    window: ChatWindow, qtbot: QtBot
) -> None:
    window.set_chat(chat_state(messages=(message_view(1),), ended="ended"))
    emitted: list[object] = []
    window.report_requested.connect(lambda *args: emitted.append(args))
    window.report_button.click()
    dialog = window._report_dialog
    assert dialog is not None and dialog.isVisible()
    window.set_chat(chat_state())  # a new chat replaces it
    assert not dialog.isVisible()
    assert window._report_dialog is None
    dialog.reason_buttons["spam"].setChecked(True)
    dialog.report_button.click()  # even a late click reports nothing
    assert emitted == []


def test_the_report_dialog_closes_when_the_chat_is_forgotten(
    window: ChatWindow,
) -> None:
    window.set_chat(chat_state(ended="ended"))
    window.report_button.click()
    dialog = window._report_dialog
    assert dialog is not None
    window.set_chat(ChatState())
    assert not dialog.isVisible()


def test_the_report_dialog_stays_while_the_same_chat_updates(
    window: ChatWindow,
) -> None:
    match_id = uuid4()
    window.set_chat(chat_state(match_id=match_id))
    window.report_button.click()
    dialog = window._report_dialog
    assert dialog is not None
    window.set_chat(chat_state(match_id=match_id, messages=(message_view(1),)))
    assert dialog.isVisible()


CHAT_ERROR_CODES = [
    "invalid_request",
    "rate_limited",
    "report_window_closed",
    "already_reported",
    "chat_ended",
    "no_extensions_left",
    "no_extension_request",
    "not_found",
    "network",
]


@pytest.mark.parametrize("code", CHAT_ERROR_CODES)
def test_chat_action_errors_show_a_fixed_line(window: ChatWindow, code: str) -> None:
    window.set_chat(chat_state())
    assert not window.error_label.isVisibleTo(window)
    window.show_error(code)
    assert window.error_label.text() != ""
    assert window.error_label.isVisibleTo(window)
    assert window.error_label.textFormat() == Qt.TextFormat.PlainText


@pytest.mark.parametrize(
    "code", ["unauthorized", "invalid_code", "nickname_taken", "replaced", "<b>x</b>"]
)
def test_onboarding_and_unknown_codes_show_nothing(
    window: ChatWindow, code: str
) -> None:
    window.set_chat(chat_state())
    window.show_error(code)
    assert not window.error_label.isVisibleTo(window)


def test_no_error_line_without_a_chat(window: ChatWindow) -> None:
    window.show_error("network")
    assert not window.error_label.isVisibleTo(window)


def test_the_error_line_clears_by_timer_and_on_a_new_chat(
    window: ChatWindow, qtbot: QtBot
) -> None:
    window.set_chat(chat_state())
    window.show_error("network")
    assert window._error_timer.isActive()
    window._error_timer.timeout.emit()
    assert not window.error_label.isVisibleTo(window)
    window.show_error("network")
    window.set_chat(chat_state())  # another match
    assert not window.error_label.isVisibleTo(window)
    assert window.error_label.text() == ""


def test_closing_the_chat_window_quits_when_nothing_else_is_visible(
    qtbot: QtBot,
) -> None:
    """lastWindowClosed only fires inside a running `exec()`, so run one."""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    assert isinstance(app, QApplication)
    main = CompanionWindow()
    chat = ChatWindow()
    qtbot.addWidget(main)
    qtbot.addWidget(chat)
    chat.set_chat(chat_state())
    main.show()
    chat.show()
    fired: list[bool] = []
    after_main: list[bool] = []
    app.lastWindowClosed.connect(lambda: fired.append(True))
    QTimer.singleShot(100, main.close)
    QTimer.singleShot(200, lambda: after_main.append(bool(fired)))
    QTimer.singleShot(300, chat.close)
    QTimer.singleShot(1000, app.quit)  # safety net
    app.exec()
    assert after_main == [False]  # the chat window keeps the app alive
    assert not main.isVisible() and not chat.isVisible()
    assert fired == [True]


def test_closing_the_chat_window_only_hides_it_while_the_main_window_shows(
    qtbot: QtBot,
) -> None:
    main = CompanionWindow()
    chat = ChatWindow()
    qtbot.addWidget(main)
    qtbot.addWidget(chat)
    chat.set_chat(chat_state())
    main.show()
    chat.show()
    chat.close()
    assert main.isVisible() and not chat.isVisible()


def test_a_failed_message_in_an_ended_chat_is_not_clickable(
    window: ChatWindow,
) -> None:
    pending = PendingMessage(uuid4(), "lost", "failed")
    match_id = uuid4()
    window.set_chat(chat_state(match_id=match_id, pending=(pending,)))
    assert len(window.transcript.findChildren(QPushButton)) == 1
    window.set_chat(chat_state(match_id=match_id, pending=(pending,), ended="ended"))
    assert window.transcript.findChildren(QPushButton) == []
    assert len(_texts(window)) == 2  # the body and the one "not sent" mark


# --- X3: long messages are not clipped ---

C06_LINE = (
    '<b>bold</b> <a href="http://example.com">link</a> <img src="x"> '
    "&amp; CANARY-M5-7f3a"
)
LONG_WORDS = " ".join(["supercalifragilistic"] * 15)


def _settle(qtbot: QtBot, window: ChatWindow) -> None:
    qtbot.wait(50)  # layout and the deferred scroll run on the event loop
    layout = window.transcript.layout()
    assert layout is not None
    layout.activate()


@pytest.mark.parametrize("mine", [False, True])
@pytest.mark.parametrize("body", [C06_LINE, LONG_WORDS])
def test_a_long_message_gets_the_height_its_wrapped_text_needs(
    window: ChatWindow, qtbot: QtBot, body: str, mine: bool
) -> None:
    window.resize(window.minimumSizeHint().width(), 400)
    window.set_chat(chat_state(messages=(message_view(1, mine=mine, body=body),)))
    _settle(qtbot, window)
    (label,) = window._body_labels
    assert label.text().replace("\u200b", "") == body
    viewport = window.scroll_area.viewport()
    assert label.width() <= viewport.width()
    assert label.height() >= label.heightForWidth(label.width())


def test_scrolled_to_the_bottom_the_last_line_of_a_long_message_shows(
    window: ChatWindow, qtbot: QtBot
) -> None:
    window.resize(window.minimumSizeHint().width(), 300)
    earlier = tuple(message_view(seq) for seq in range(1, 15))
    last = message_view(15, body=LONG_WORDS)
    window.set_chat(chat_state(messages=(*earlier, last)))
    _settle(qtbot, window)
    bar = window.scroll_area.verticalScrollBar()
    bar.setValue(bar.maximum())
    label = window._body_labels[-1]
    bottom = label.mapTo(window.transcript, label.rect().bottomLeft()).y()
    visible_bottom = bar.value() + window.scroll_area.viewport().height()
    assert label.height() >= label.heightForWidth(label.width())
    assert bottom <= visible_bottom


@pytest.mark.parametrize("mine", [False, True])
def test_one_very_long_word_does_not_scroll_the_transcript_sideways(
    window: ChatWindow, qtbot: QtBot, mine: bool
) -> None:
    window.resize(window.minimumSizeHint().width(), 400)
    window.show()
    window.set_chat(chat_state(messages=(message_view(1, mine=mine, body="x" * 300),)))
    _settle(qtbot, window)
    (label,) = window._body_labels
    viewport = window.scroll_area.viewport()
    assert label.width() <= viewport.width()
    assert window.scroll_area.horizontalScrollBar().maximum() == 0
    assert label.height() >= label.heightForWidth(label.width())


def test_every_chat_error_code_has_its_own_line(window: ChatWindow) -> None:
    window.set_chat(chat_state())
    lines = set()
    for code in CHAT_ERROR_CODES:
        window.show_error(code)
        lines.add(window.error_label.text())
    assert len(lines) == len(CHAT_ERROR_CODES)


def test_each_way_a_chat_ends_has_its_own_line(window: ChatWindow) -> None:
    lines = set()
    for reason in ("ended", "expired", "disconnected"):
        window.set_chat(chat_state(ended=reason))
        lines.add(window.ended_label.text())
    assert len(lines) == 3


URL = "https://example.com/" + "a" * 60 + "?q=" + "b" * 60


def _select_all_and_copy(window: ChatWindow, qtbot: QtBot) -> str:
    QApplication.clipboard().setText("sentinel")
    window.show()
    window.set_chat(chat_state(messages=(message_view(1, body=URL),)))
    _settle(qtbot, window)
    (label,) = window._body_labels
    label.setFocus()
    QTest.keyClick(label, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    assert label.hasSelectedText()
    QTest.keyClick(label, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    return QApplication.clipboard().text()


def test_copying_a_long_message_gives_the_stored_body(
    window: ChatWindow, qtbot: QtBot
) -> None:
    assert _select_all_and_copy(window, qtbot) == URL


def test_the_context_menu_copy_gives_the_stored_body(
    window: ChatWindow, qtbot: QtBot
) -> None:
    window.show()
    window.set_chat(chat_state(messages=(message_view(1, body=URL),)))
    _settle(qtbot, window)
    (label,) = window._body_labels
    assert isinstance(label, _BodyLabel)
    actions = {a.objectName(): a for a in label.build_context_menu().actions()}
    assert not actions["copy"].isEnabled()  # nothing selected yet
    actions["select_all"].trigger()
    QApplication.clipboard().setText("sentinel")
    actions = {a.objectName(): a for a in label.build_context_menu().actions()}
    actions["copy"].trigger()
    assert QApplication.clipboard().text() == URL

"""Windows carry their state as a property the stylesheet keys on."""

from datetime import timedelta
from uuid import uuid4

import pytest
from PySide6.QtWidgets import QLabel
from pytestqt.qtbot import QtBot

from beebreak_companion.break_state import BreakPhase, BreakState
from beebreak_companion.chat_state import ChatState, PendingMessage
from beebreak_companion.chat_window import ChatWindow
from beebreak_companion.window import CompanionWindow
from beebreak_shared.api import MatchView
from beebreak_shared.ipc import InvitationState
from tests.fakes.chat import T0, chat_state, chat_status, message_view


@pytest.fixture
def window(qtbot: QtBot) -> CompanionWindow:
    w = CompanionWindow()
    qtbot.addWidget(w)
    return w


@pytest.fixture
def chat(qtbot: QtBot) -> ChatWindow:
    w = ChatWindow(clock=lambda: 1000.0, now=lambda: T0)
    qtbot.addWidget(w)
    w.show()
    return w


def _match() -> MatchView:
    match_id = uuid4()
    status = chat_status(match_id, T0 + timedelta(minutes=15))
    return MatchView(
        match_id=match_id,
        peer_nickname="bob",
        ends_at=status.ends_at,
        chat=status,
        last_seq=0,
    )


# --- main window ---


def test_header_shows_mark_and_wordmark(window: CompanionWindow) -> None:
    assert not window.mark_label.pixmap().isNull()
    assert window.wordmark_label.text() == "beebreak"
    assert window.wordmark_label.objectName() == "wordmark"


@pytest.mark.parametrize(
    ("invitation", "state"),
    [("disabled", "off"), ("idle", "connected"), ("suggested", "searching")],
)
def test_invitation_state_drives_the_status_pill(
    window: CompanionWindow, invitation: InvitationState, state: str
) -> None:
    window.set_state(invitation)
    assert window.status_label.property("state") == state


@pytest.mark.parametrize(
    ("phase", "state"),
    [
        ("joining", "searching"),
        ("searching", "searching"),
        ("matched", "connected"),
        ("nobody_available", "off"),
        ("ended", "off"),
    ],
)
def test_break_phase_drives_the_break_pill(
    window: CompanionWindow, phase: BreakPhase, state: str
) -> None:
    match = _match() if phase in ("matched", "ended") else None
    window.set_break(BreakState(phase=phase, match=match))
    assert window.break_label.property("state") == state


@pytest.mark.parametrize(
    ("connection", "state"),
    [
        ("signed_out", "off"),
        ("signing_in", "searching"),
        ("connecting", "searching"),
        ("connected", "connected"),
        ("offline", "offline"),
    ],
)
def test_connection_drives_the_account_pill(
    window: CompanionWindow, connection: str, state: str
) -> None:
    window.set_remote_state(connection)
    assert window.account_label.property("state") == state


def test_break_error_is_an_offline_state_line(window: CompanionWindow) -> None:
    window.show_error("offline")
    assert window.break_error_label.property("state") == "offline"


def test_buttons_have_roles(window: CompanionWindow) -> None:
    assert window.find_button.objectName() == "primary"
    assert window.accept_button.objectName() == "primary"


# --- chat window ---


def test_header_has_avatar_initial_and_name(chat: ChatWindow) -> None:
    chat.set_chat(chat_state(peer_nickname="bob"))
    assert chat.avatar_label.text().lower() == "b"
    assert chat.peer_label.objectName() == "peerName"
    assert chat.time_label.objectName() == "timePill"
    assert chat.footer_label.objectName() == "footer"


def test_reconnecting_and_ended_have_their_own_states(chat: ChatWindow) -> None:
    chat.set_chat(chat_state())
    chat.set_connected(False)
    assert chat.status_label.property("state") == "offline"
    chat.set_chat(chat_state(ended="ended"))
    assert chat.ended_label.property("state") == "off"


def test_chat_error_line_is_an_offline_state(chat: ChatWindow) -> None:
    chat.set_chat(chat_state())
    chat.show_error("network")
    assert chat.error_label.property("state") == "offline"


def _roles(chat: ChatWindow, state: ChatState) -> list[object]:
    chat.set_chat(state)
    return [
        label.property("role")
        for label in chat.transcript.findChildren(QLabel)
        if label.property("role") is not None
    ]


def test_bubbles_know_whose_they_are_and_whether_sent(chat: ChatWindow) -> None:
    sending, failed = uuid4(), uuid4()
    state = chat_state(
        messages=(message_view(1, mine=False), message_view(2, mine=True)),
        pending=(
            PendingMessage(sending, "a", "sending"),
            PendingMessage(failed, "b", "failed"),
        ),
    )
    assert _roles(chat, state) == ["peer", "mine", "pending", "pending"]


def test_sending_has_a_caption_and_failed_has_a_retry_button(chat: ChatWindow) -> None:
    sending, failed = uuid4(), uuid4()
    chat.set_chat(
        chat_state(
            pending=(
                PendingMessage(sending, "a", "sending"),
                PendingMessage(failed, "b", "failed"),
            )
        )
    )
    names = {label.objectName() for label in chat.transcript.findChildren(QLabel)}
    assert "caption" in names  # the sending caption
    assert any(b.objectName() == "retry" for b in chat._retry_buttons)


def test_action_buttons_have_roles(chat: ChatWindow) -> None:
    assert chat.send_button.objectName() == "primary"
    assert chat.yes_button.objectName() == "primary"
    assert chat.report_button.objectName() == "danger"
    assert chat.banner.objectName() == "banner"

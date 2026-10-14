from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, get_args
from uuid import uuid4

import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication, QMessageBox
from pytestqt.qtbot import QtBot

from breakroom_companion.app import initial_status, setup_remote, wire
from breakroom_companion.break_state import BreakPhase, BreakState
from breakroom_companion.config import TriggerConfig
from breakroom_companion.controller import Controller
from breakroom_companion.window import CATEGORY_LABELS, TAG_LABELS, CompanionWindow
from breakroom_companion.worker import IpcWorker
from breakroom_shared.api import Consent, MatchView, Me, QueueEntryView
from breakroom_shared.events import NormalizedEvent
from breakroom_shared.ipc import InvitationState
from breakroom_shared.presence import RemainingTimeBucket, StackTag, TaskCategory
from tests.fakes.chat import chat_status


@pytest.fixture(autouse=True)
def _state_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Controllers built here must not touch the real ~/.local/state."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


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


RANGE_LABELS = {
    "5_10m": "5\N{EN DASH}10",
    "10_20m": "10\N{EN DASH}20",
    "20m_plus": "20+",
}


@pytest.mark.parametrize(("bucket", "needle"), list(RANGE_LABELS.items()))
def test_suggested_shows_the_range_for_each_bucket(
    window: CompanionWindow, bucket: RemainingTimeBucket, needle: str
) -> None:
    window.show()
    window.set_state("suggested", bucket)
    assert window.estimate_label.isVisible()
    assert needle in window.estimate_label.text()


def test_range_labels_are_distinct(window: CompanionWindow) -> None:
    texts = set()
    for bucket in RANGE_LABELS:
        window.set_state("suggested", bucket)  # type: ignore[arg-type]
        texts.add(window.estimate_label.text())
    assert len(texts) == 3


@pytest.mark.parametrize("bucket", [None, "unknown", "under_5m"])
def test_estimate_label_hidden_without_a_usable_bucket(
    window: CompanionWindow, bucket: RemainingTimeBucket | None
) -> None:
    window.show()
    window.set_state("suggested", bucket)
    assert window.estimate_label.isHidden()


@pytest.mark.parametrize("state", ["idle", "disabled"])
def test_estimate_label_hidden_outside_suggested(
    window: CompanionWindow, state: InvitationState
) -> None:
    window.show()
    window.set_state("suggested", "5_10m")
    window.set_state(state, "5_10m")
    assert window.estimate_label.isHidden()


def test_estimate_label_is_plain_text(window: CompanionWindow) -> None:
    assert window.estimate_label.textFormat() == Qt.TextFormat.PlainText


def test_status_snapshot_carries_activity_and_estimate(qtbot: QtBot) -> None:
    controller = Controller(FAST)
    window, worker, _timer = _wired(qtbot, controller)
    assert worker._status.activity == "unknown"
    assert worker._status.estimate is None
    _emit_qualifying_run(worker, datetime.now(UTC))
    snap = worker._status
    assert snap.invitation == "suggested"
    assert snap.activity == "active"
    assert snap.estimate == "unknown"  # no task evidence in this run
    assert window.estimate_label.isHidden()


# --- account row (M3-05b) ---

ACCOUNT_TEXT = {
    "signed_out": "Not signed in",
    "signing_in": "Signing in\N{HORIZONTAL ELLIPSIS}",
    "connecting": "Connecting\N{HORIZONTAL ELLIPSIS}",
    "offline": "Offline \N{EN DASH} retrying",
}
FULL_ME = Me(
    nickname="ada_l",
    consent=Consent(version=1),
    consented_at=datetime(2026, 10, 13, tzinfo=UTC),
)


@pytest.mark.parametrize(("state", "text"), list(ACCOUNT_TEXT.items()))
def test_account_row_fixed_text_per_state(
    window: CompanionWindow, state: str, text: str
) -> None:
    window.set_remote_state(state)
    assert window.account_label.text() == text


def test_account_row_connected_shows_the_nickname(window: CompanionWindow) -> None:
    window.set_remote_state("connected")
    window.set_me(FULL_ME)
    assert window.account_label.text() == "Connected as ada_l"


@pytest.mark.parametrize(
    "nickname", ["<b>x</b>", "a b", "x" * 40, "\N{CYRILLIC SMALL LETTER A}bc", "ab\n"]
)
def test_hostile_nickname_is_never_rendered(
    window: CompanionWindow, nickname: str
) -> None:
    window.set_remote_state("connected")
    window.set_me(
        Me.model_construct(nickname=nickname, consent=None, consented_at=None)
    )
    assert window.account_label.text() == "Connected as ?"
    assert window.account_label.textFormat() == Qt.TextFormat.PlainText


def test_account_note_is_plain_text(window: CompanionWindow) -> None:
    assert window.account_note.textFormat() == Qt.TextFormat.PlainText


def _visible(window: CompanionWindow) -> set[str]:
    named = {
        "sign_in": window.sign_in_button,
        "settings": window.settings_button,
        "sign_out": window.sign_out_button,
        "finish": window.finish_setup_button,
    }
    return {name for name, button in named.items() if button.isVisibleTo(window)}


def test_buttons_follow_the_state(window: CompanionWindow) -> None:
    assert _visible(window) == {"sign_in"}
    window.set_remote_state("signing_in")
    assert _visible(window) == {"sign_out"}
    window.set_remote_state("connecting")
    window.set_me(FULL_ME)
    assert _visible(window) == {"settings", "sign_out"}
    window.set_remote_state("offline")
    assert _visible(window) == {"settings", "sign_out"}
    window.set_remote_state("connected")
    assert _visible(window) == {"settings", "sign_out"}
    window.set_remote_state("signed_out")
    window.set_me(None)
    assert _visible(window) == {"sign_in"}


@pytest.mark.parametrize(
    "me",
    [
        Me(nickname=None, consent=Consent(version=1), consented_at=None),
        Me(nickname="ada_l", consent=None, consented_at=None),
    ],
)
def test_finish_setup_when_nickname_or_consent_missing(
    window: CompanionWindow, me: Me
) -> None:
    window.set_remote_state("connected")
    window.set_me(me)
    assert _visible(window) == {"finish", "sign_out"}


def test_button_clicks_emit(window: CompanionWindow, qtbot: QtBot) -> None:
    window.show()
    with qtbot.waitSignal(window.sign_in_requested, timeout=1000):
        window.sign_in_button.click()
    window.set_remote_state("connected")
    window.set_me(FULL_ME)
    with qtbot.waitSignal(window.settings_requested, timeout=1000):
        window.settings_button.click()
    with qtbot.waitSignal(window.sign_out_requested, timeout=1000):
        window.sign_out_button.click()


def test_account_buttons_take_no_focus(window: CompanionWindow) -> None:
    for button in (
        window.sign_in_button,
        window.settings_button,
        window.sign_out_button,
        window.finish_setup_button,
    ):
        assert button.focusPolicy() == Qt.FocusPolicy.NoFocus


def test_unencrypted_note_only_when_signed_in_without_encryption(
    window: CompanionWindow,
) -> None:
    window.show()
    window.set_store_encrypted(False)
    assert window.account_note.isHidden()  # nothing stored while signed out
    window.set_remote_state("connected")
    assert window.account_note.isVisible()
    assert window.account_note.text() == "Stored unencrypted"
    window.set_store_encrypted(True)
    assert window.account_note.isHidden()


def test_unavailable_account_row_stays_disabled(window: CompanionWindow) -> None:
    window.show()
    window.set_remote_unavailable()
    assert window.account_label.text() == "Server address not allowed"
    window.set_remote_state("connected")
    window.set_me(FULL_ME)
    assert window.account_label.text() == "Server address not allowed"
    assert _visible(window) == set()


def test_invitation_area_still_works_with_the_account_row(
    window: CompanionWindow,
) -> None:
    window.show()
    window.set_state("suggested", "5_10m")
    assert window.accept_button.isVisible() and window.estimate_label.isVisible()
    assert window.accept_button.focusPolicy() == Qt.FocusPolicy.NoFocus


def test_refused_server_url_leaves_the_companion_running(
    window: CompanionWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BREAKROOM_SERVER_URL", "http://example.com")
    assert setup_remote(window) is None
    assert window.account_label.text() == "Server address not allowed"
    window.set_state("idle")  # the rest of the window keeps working
    assert window.status_label.text() != ""


def test_allowed_server_url_builds_a_worker_and_wires_the_row(
    window: CompanionWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BREAKROOM_SERVER_URL", "http://127.0.0.1:9")
    worker = setup_remote(window)
    assert worker is not None
    worker.state_changed.emit("connecting")
    assert window.account_label.text() == ACCOUNT_TEXT["connecting"]
    worker.me_changed.emit(FULL_ME)
    worker.state_changed.emit("connected")
    assert window.account_label.text() == "Connected as ada_l"
    assert worker.state == "signed_out"  # nothing started, no network
    worker.stop()


# --- M4-06: break area ---

LATER = datetime(2026, 10, 13, 13, 0, tzinfo=UTC)


def _match(
    nickname: str = "bob_b",
    tags: tuple[Any, ...] = (),
    category: Any = None,
) -> MatchView:
    match_id = uuid4()
    return MatchView.model_construct(
        match_id=match_id,
        peer_nickname=nickname,
        shared_tags=tags,
        shared_category=category,
        ends_at=LATER,
        chat=chat_status(match_id, LATER),
        last_seq=0,
    )


def _entry() -> QueueEntryView:
    return QueueEntryView(entry_id=uuid4(), status="queued", expires_at=LATER)


def _break_visible(window: CompanionWindow) -> set[str]:
    named = {
        "find": window.find_button,
        "cancel": window.cancel_button,
        "open_chat": window.open_chat_button,
        "block": window.block_button,
        "ok": window.ok_button,
        "text": window.break_label,
        "shared": window.shared_label,
    }
    return {name for name, w in named.items() if w.isVisibleTo(window)}


def test_nothing_in_the_break_area_until_ready(window: CompanionWindow) -> None:
    window.show()
    assert _break_visible(window) == set()
    window.set_ready(True)
    assert _break_visible(window) == {"find"}
    assert window.find_button.text() == "Find a break partner"
    window.set_ready(False)
    assert _break_visible(window) == set()


@pytest.mark.parametrize("phase", ["joining", "searching"])
def test_searching_shows_text_and_cancel_only(
    window: CompanionWindow, phase: BreakPhase
) -> None:
    window.show()
    window.set_ready(True)
    window.set_break(BreakState(phase=phase, entry=_entry()))
    assert _break_visible(window) == {"text", "cancel"}
    assert window.break_label.text().startswith("Looking for a break partner")


def test_matched_shows_nickname_shared_labels_and_open_chat(
    window: CompanionWindow,
) -> None:
    window.show()
    window.set_ready(True)
    window.set_break(
        BreakState(
            phase="matched",
            match=_match(tags=("python", "csharp"), category="backend"),
        )
    )
    assert _break_visible(window) == {"text", "shared", "open_chat"}
    assert window.break_label.text() == "Chatting with bob_b"
    assert "Python" in window.shared_label.text()
    assert "C#" in window.shared_label.text()
    assert "Backend" in window.shared_label.text()


def test_matched_without_shared_values_hides_the_shared_line(
    window: CompanionWindow,
) -> None:
    window.show()
    window.set_break(BreakState(phase="matched", match=_match()))
    assert _break_visible(window) == {"text", "open_chat"}


@pytest.mark.parametrize(
    "nickname", ["<b>x</b>", "a b", "x" * 40, "ab\n", "\N{CYRILLIC SMALL LETTER A}bc"]
)
def test_a_peer_nickname_failing_the_pattern_shows_a_question_mark(
    window: CompanionWindow, nickname: str
) -> None:
    window.set_break(BreakState(phase="matched", match=_match(nickname)))
    assert window.break_label.text() == "Chatting with ?"
    assert nickname not in window.break_label.text()


def test_unknown_tags_and_categories_never_render(window: CompanionWindow) -> None:
    window.show()
    window.set_break(
        BreakState(
            phase="matched",
            match=_match(tags=("<b>evil</b>", "python", 7), category="other"),
        )
    )
    assert window.shared_label.text().endswith("Python")
    assert "evil" not in window.shared_label.text()
    assert "Other" not in window.shared_label.text()
    window.set_break(
        BreakState(phase="matched", match=_match(tags=("zzz",), category="nope"))
    )
    assert window.shared_label.isHidden()


def test_labels_cover_the_whole_vocabulary() -> None:
    assert set(TAG_LABELS) == set(get_args(StackTag))
    assert "other" not in CATEGORY_LABELS
    assert set(CATEGORY_LABELS) | {"other"} == set(get_args(TaskCategory))


def test_break_labels_are_plain_text(window: CompanionWindow) -> None:
    for label in (window.break_label, window.shared_label):
        assert label.textFormat() == Qt.TextFormat.PlainText


def test_nobody_available_offers_ok_and_find(window: CompanionWindow) -> None:
    window.show()
    window.set_ready(True)
    window.set_break(BreakState(phase="nobody_available"))
    assert _break_visible(window) == {"text", "ok", "find"}
    assert window.break_label.text() == "Nobody is around right now. Try again later."


def test_ended_shows_text_and_ok_only(window: CompanionWindow) -> None:
    window.show()
    window.set_ready(True)
    window.set_break(BreakState(phase="ended"))
    assert _break_visible(window) == {"text", "ok"}
    assert window.break_label.text() == "Break ended."


def test_find_is_hidden_during_a_break_even_when_ready(
    window: CompanionWindow,
) -> None:
    window.show()
    window.set_ready(True)
    window.set_break(BreakState(phase="matched", match=_match()))
    assert not window.find_button.isVisibleTo(window)


def test_buttons_emit_their_signals(window: CompanionWindow, qtbot: QtBot) -> None:
    window.show()
    window.set_ready(True)
    with qtbot.waitSignal(window.find_requested, timeout=1000):
        window.find_button.click()
    window.set_break(BreakState(phase="searching", entry=_entry()))
    with qtbot.waitSignal(window.cancel_requested, timeout=1000):
        window.cancel_button.click()
    window.set_break(BreakState(phase="matched", match=_match()))
    with qtbot.waitSignal(window.open_chat_requested, timeout=1000):
        window.open_chat_button.click()
    window.set_break(BreakState(phase="ended"))
    with qtbot.waitSignal(window.dismiss_requested, timeout=1000):
        window.ok_button.click()


def _answer(
    monkeypatch: pytest.MonkeyPatch, button: QMessageBox.StandardButton
) -> list[str]:
    asked: list[str] = []

    def question(parent: object, title: str, text: str, *args: object) -> object:
        asked.append(text)
        return button

    monkeypatch.setattr(QMessageBox, "question", question)
    return asked


def test_block_emits_only_after_confirmation(
    window: CompanionWindow, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    window.show()
    window.set_break(BreakState(phase="ended", match=_match()))
    asked = _answer(monkeypatch, QMessageBox.StandardButton.No)
    with qtbot.assertNotEmitted(window.block_requested):
        window.block_button.click()
    assert asked == ["You won't be matched with this person again."]
    _answer(monkeypatch, QMessageBox.StandardButton.Yes)
    with qtbot.waitSignal(window.block_requested, timeout=1000):
        window.block_button.click()


def test_every_break_button_refuses_keyboard_focus(window: CompanionWindow) -> None:
    for button in (
        window.find_button,
        window.cancel_button,
        window.open_chat_button,
        window.block_button,
        window.ok_button,
    ):
        assert button.focusPolicy() == Qt.FocusPolicy.NoFocus


@pytest.mark.parametrize(
    "state",
    [
        BreakState(phase="searching", entry=_entry()),
        BreakState(phase="matched", match=_match()),
        BreakState(phase="nobody_available"),
        BreakState(phase="ended"),
    ],
)
def test_space_and_enter_after_a_state_change_emit_nothing(
    window: CompanionWindow, qtbot: QtBot, state: BreakState
) -> None:
    window.show()
    window.set_ready(True)
    window.set_break(state)
    signals = [
        window.find_requested,
        window.cancel_requested,
        window.open_chat_requested,
        window.block_requested,
        window.dismiss_requested,
        window.accepted,
        window.declined,
    ]
    spy = [QSignalSpy(s) for s in signals]
    for key in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
        qtbot.keyClick(window, key)  # type: ignore[no-untyped-call]
    assert [s.count() for s in spy] == [0] * len(signals)


def test_accept_is_hidden_while_a_break_is_active(window: CompanionWindow) -> None:
    window.show()
    window.set_state("suggested", "5_10m")
    assert window.accept_button.isVisible()
    window.set_break(BreakState(phase="searching", entry=_entry()))
    assert not window.accept_button.isVisibleTo(window)
    assert not window.decline_button.isVisibleTo(window)
    window.set_state("suggested", "5_10m")
    assert not window.accept_button.isVisibleTo(window)
    window.set_break(BreakState())
    assert window.accept_button.isVisibleTo(window)


@pytest.mark.parametrize(
    "state",
    [
        BreakState(phase="matched", match=_match()),
        BreakState(phase="nobody_available"),
    ],
)
def test_matched_and_nobody_available_show_and_raise_the_window(
    window: CompanionWindow, state: BreakState
) -> None:
    assert not window.isVisible()
    window.set_break(state)
    assert window.isVisible()
    assert window.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)


@pytest.mark.parametrize("phase", ["searching", "ended"])
def test_other_phases_do_not_pop_the_window_up(
    window: CompanionWindow, phase: BreakPhase
) -> None:
    window.set_break(BreakState(phase=phase))
    assert not window.isVisible()


# --- review fixes: Block after the peer ended (R5) ---


def test_ended_with_a_known_match_still_offers_block(window: CompanionWindow) -> None:
    window.show()
    window.set_break(BreakState(phase="ended", match=_match()))
    assert _break_visible(window) == {"text", "ok", "block"}
    assert window.block_button.focusPolicy() == Qt.FocusPolicy.NoFocus
    window.set_break(BreakState(phase="ended"))
    assert _break_visible(window) == {"text", "ok"}


def test_block_on_the_ended_screen_needs_confirmation_too(
    window: CompanionWindow, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    window.show()
    window.set_break(BreakState(phase="ended", match=_match()))
    asked = _answer(monkeypatch, QMessageBox.StandardButton.No)
    with qtbot.assertNotEmitted(window.block_requested):
        window.block_button.click()
    assert asked == ["You won't be matched with this person again."]
    _answer(monkeypatch, QMessageBox.StandardButton.Yes)
    with qtbot.waitSignal(window.block_requested, timeout=1000):
        window.block_button.click()


# --- M5-06: chat entry point in the break area ---


def test_matched_offers_open_chat_without_a_count_by_default(
    window: CompanionWindow,
) -> None:
    window.show()
    window.set_break(BreakState(phase="matched", match=_match()))
    assert window.open_chat_button.text() == "Open chat"
    assert not window.block_button.isVisibleTo(window)
    assert not hasattr(window, "end_button")


def test_open_chat_shows_the_unread_count(window: CompanionWindow) -> None:
    window.show()
    window.set_break(BreakState(phase="matched", match=_match()))
    window.set_unread(3)
    assert window.open_chat_button.text() == "Open chat (3)"
    window.set_unread(0)
    assert window.open_chat_button.text() == "Open chat"
    window.set_unread(-5)
    assert window.open_chat_button.text() == "Open chat"


def test_open_chat_is_only_offered_while_matched(window: CompanionWindow) -> None:
    window.show()
    window.set_ready(True)
    for phase in ("none", "searching", "nobody_available", "ended"):
        window.set_break(BreakState(phase=phase, entry=_entry()))
        assert not window.open_chat_button.isVisibleTo(window)

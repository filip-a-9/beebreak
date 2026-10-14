"""App-side break wiring (M4-06, M4-07 hookup): real window, controller, tracker."""

import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from PySide6.QtWidgets import QMessageBox
from pytestqt.qtbot import QtBot

from breakroom_companion.app import (
    BreakTracker,
    initial_status,
    wire,
    wire_break,
)
from breakroom_companion.break_state import BreakPhase, BreakState
from breakroom_companion.config import TriggerConfig
from breakroom_companion.controller import Controller
from breakroom_companion.state_store import FileStateStore
from breakroom_companion.window import CompanionWindow
from breakroom_companion.worker import IpcWorker
from breakroom_shared.api import Consent, MatchView, Me, QueueEntryView
from breakroom_shared.events import EventType, NormalizedEvent
from tests.fakes.chat import chat_status
from tests.fakes.remote_worker import FakeRemoteWorker

CFG = TriggerConfig(
    activity_threshold_s=60,
    min_tool_events=2,
    cooldown_s=900,
    suggestion_timeout_s=60,
)
LATER = datetime(2030, 1, 1, tzinfo=UTC)
ALL = Consent(
    version=1,
    share_task_category=True,
    share_stack_tags=True,
    share_time_bucket=True,
)
NONE = Consent(version=1)


def _me(consent: Consent | None = ALL, nickname: str | None = "ada_l") -> Me:
    return Me(nickname=nickname, consent=consent, consented_at=LATER)


def _entry() -> QueueEntryView:
    return QueueEntryView(entry_id=uuid4(), status="queued", expires_at=LATER)


def _match() -> MatchView:
    match_id = uuid4()
    return MatchView(
        match_id=match_id,
        peer_nickname="bob_b",
        ends_at=LATER,
        chat=chat_status(match_id, LATER),
        last_seq=0,
    )


@dataclass
class Rig:
    controller: Controller
    window: CompanionWindow
    remote: FakeRemoteWorker
    ipc: IpcWorker
    tracker: BreakTracker

    def ready(self, consent: Consent | None = ALL) -> None:
        self.remote.push_me(_me(consent))
        self.remote.push_state("connected")

    def status_queue(self) -> str:
        return self.ipc._status.queue

    def suggest(self) -> None:
        """Feed a long run so the controller suggests a break."""
        end = datetime.now(UTC)
        start = end - timedelta(seconds=200)
        self._send("turn_submitted", start)
        for i in range(1, 20):
            self._send("tool_completed", start + timedelta(seconds=10 * i))
        assert self.controller.state == "suggested"

    def _send(self, type_: EventType, when: datetime) -> None:
        self.ipc.event_received.emit(
            NormalizedEvent(
                event_version=1,
                event_id=uuid4(),
                local_session_key="k",
                agent="claude_code",
                type=type_,
                observed_at=when,
            )
        )


@pytest.fixture
def rig(qtbot: QtBot, tmp_path: Path) -> Rig:
    controller = Controller(CFG, store=FileStateStore(tmp_path / "state.json"))
    window = CompanionWindow()
    qtbot.addWidget(window)
    remote = FakeRemoteWorker()
    tracker = BreakTracker()
    ipc = IpcWorker(
        Path("/nonexistent"),
        initial_status(controller),
        leave_queue=tracker.leave_queue,
    )

    def publish() -> None:
        ipc.set_status(initial_status(controller, tracker.queue_state()))

    on_accepted = wire_break(controller, window, remote, tracker, publish)
    wire(
        controller,
        window,
        ipc,
        queue=tracker.queue_state,
        on_accepted=on_accepted,
    )
    window.show()
    return Rig(controller, window, remote, ipc, tracker)


# --- readiness ---


def test_before_connecting_there_is_no_find_button_and_no_invitation(
    rig: Rig,
) -> None:
    assert not rig.window.find_button.isVisibleTo(rig.window)
    for _ in range(3):
        rig.ipc.event_received.emit(
            NormalizedEvent(
                event_version=1,
                event_id=uuid4(),
                local_session_key="k",
                agent="claude_code",
                type="turn_submitted",
                observed_at=datetime.now(UTC) - timedelta(seconds=300),
            )
        )
    assert rig.controller.state == "idle"


@pytest.mark.parametrize(
    ("connected", "me"),
    [
        (False, _me()),
        (True, None),
        (True, _me(nickname=None)),
        (True, _me(consent=None)),
    ],
)
def test_each_missing_piece_keeps_the_app_not_ready(
    rig: Rig, connected: bool, me: Me | None
) -> None:
    rig.remote.push_me(me)
    rig.remote.push_state("connected" if connected else "offline")
    assert not rig.window.find_button.isVisibleTo(rig.window)
    assert rig.controller._ready is False


def test_connected_with_nickname_and_consent_is_ready(rig: Rig) -> None:
    rig.ready()
    assert rig.window.find_button.isVisibleTo(rig.window)
    assert rig.controller._ready is True


def test_losing_the_connection_withdraws_a_suggestion_and_hides_find(
    rig: Rig,
) -> None:
    rig.ready()
    rig.suggest()
    rig.remote.push_state("offline")
    assert rig.controller.state == "idle"
    assert rig.window.accept_button.isHidden()
    assert not rig.window.find_button.isVisibleTo(rig.window)
    assert rig.ipc._status.invitation == "idle"


def test_signing_out_makes_the_app_not_ready(rig: Rig) -> None:
    rig.ready()
    rig.remote.push_me(None)
    assert not rig.window.find_button.isVisibleTo(rig.window)


def test_without_a_remote_worker_nothing_is_offered(
    qtbot: QtBot, tmp_path: Path
) -> None:
    controller = Controller(CFG, store=FileStateStore(tmp_path / "s.json"))
    window = CompanionWindow()
    qtbot.addWidget(window)
    tracker = BreakTracker()
    hook = wire_break(controller, window, None, tracker, lambda: None)
    window.show()
    assert controller._ready is False
    assert not window.find_button.isVisibleTo(window)
    hook("5_10m")  # a no-op
    assert tracker.leave_queue() == "unavailable"


# --- find and accept: rule 13 ---


def test_find_sends_unknown_with_only_consented_fields(rig: Rig) -> None:
    rig.controller.set_stack_tags(("python", "docker"))
    rig.ready(ALL)
    rig.window.find_button.click()
    (sent,) = rig.remote.calls.joins
    assert sent.remaining_time_bucket == "unknown"
    assert sent.task_category == "other"
    assert sent.stack_tags == ("python", "docker")
    assert sent.status == "available"


def test_find_with_no_consent_sends_presence_alone(rig: Rig) -> None:
    rig.controller.set_stack_tags(("python",))
    rig.ready(NONE)
    rig.window.find_button.click()
    (sent,) = rig.remote.calls.joins
    assert sent.task_category is None
    assert sent.stack_tags == ()
    assert sent.remaining_time_bucket is None


def test_find_uses_the_consent_saved_now(rig: Rig) -> None:
    rig.ready(ALL)
    rig.remote.push_me(_me(Consent(version=1, share_task_category=True)))
    rig.window.find_button.click()
    (sent,) = rig.remote.calls.joins
    assert sent.task_category == "other"
    assert sent.remaining_time_bucket is None and sent.stack_tags == ()


def test_find_is_ignored_while_searching_or_matched(rig: Rig) -> None:
    rig.ready()
    rig.remote.push_break(BreakState(phase="searching", entry=_entry()))
    rig.window.find_requested.emit()
    rig.remote.push_break(BreakState(phase="matched", match=_match()))
    rig.window.find_requested.emit()
    assert rig.remote.calls.joins == []


def test_accept_joins_once_with_the_invitations_bucket(rig: Rig) -> None:
    rig.ready(ALL)
    rig.suggest()
    bucket = rig.controller.estimate
    assert bucket is not None
    rig.window.accept_button.click()
    (sent,) = rig.remote.calls.joins
    assert sent.remaining_time_bucket == bucket
    assert sent.task_category == "other"
    assert rig.controller.state == "idle"  # the controller handled it as before


def test_accept_with_time_not_consented_sends_no_bucket(rig: Rig) -> None:
    rig.ready(Consent(version=1, share_task_category=True))
    rig.suggest()
    rig.window.accept_button.click()
    (sent,) = rig.remote.calls.joins
    assert sent.remaining_time_bucket is None


def test_decline_sends_nothing(rig: Rig) -> None:
    rig.ready()
    rig.suggest()
    rig.window.decline_button.click()
    assert rig.remote.calls.joins == []


# --- break lifecycle ---


def test_break_changes_drive_window_controller_and_status(rig: Rig) -> None:
    rig.ready()
    rig.remote.push_break(BreakState(phase="joining"))
    assert rig.window.cancel_button.isVisibleTo(rig.window)
    assert rig.controller._break_active is True
    assert rig.status_queue() == "searching"
    rig.remote.push_break(BreakState(phase="searching", entry=_entry()))
    assert rig.status_queue() == "searching"
    rig.remote.push_break(BreakState(phase="matched", match=_match()))
    assert rig.window.break_label.text() == "Chatting with bob_b"
    assert rig.status_queue() == "chatting"
    rig.remote.push_break(BreakState(phase="ended"))
    assert rig.controller._break_active is False
    assert rig.status_queue() == "none"
    assert rig.controller._cooldown_until is not None  # the break's end


def test_a_break_withdraws_an_outstanding_suggestion(rig: Rig) -> None:
    rig.ready()
    rig.suggest()
    rig.remote.push_break(BreakState(phase="searching", entry=_entry()))
    assert rig.controller.state == "idle"
    assert rig.window.accept_button.isHidden()
    assert rig.ipc._status.invitation == "idle"


def test_buttons_call_the_worker(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    rig.ready()
    rig.remote.push_break(BreakState(phase="searching", entry=_entry()))
    rig.window.cancel_button.click()
    assert rig.remote.calls.leaves == 1
    rig.remote.push_break(BreakState(phase="matched", match=_match()))
    rig.remote.push_break(BreakState(phase="ended", match=_match()))
    monkeypatch.setattr(
        QMessageBox, "question", lambda *a: QMessageBox.StandardButton.No
    )
    rig.window.block_button.click()
    assert rig.remote.calls.blocks == 0
    monkeypatch.setattr(
        QMessageBox, "question", lambda *a: QMessageBox.StandardButton.Yes
    )
    rig.window.block_button.click()
    assert rig.remote.calls.blocks == 1
    assert rig.remote.calls.joins == []


@pytest.mark.parametrize("phase", ["nobody_available", "ended"])
def test_dismiss_clears_the_notice_without_a_worker_call(
    rig: Rig, phase: BreakPhase
) -> None:
    rig.ready()
    rig.remote.push_break(BreakState(phase=phase))
    rig.window.ok_button.click()
    assert rig.remote.calls.dismisses == 1
    assert rig.remote.calls.leaves == rig.remote.calls.ends == 0
    rig.remote.push_break(BreakState())  # the worker's answer
    assert rig.window.break_label.isHidden()
    assert rig.window.find_button.isVisibleTo(rig.window)
    assert rig.status_queue() == "none"


# --- the IPC leave-queue callback (M4-07) ---


def test_leave_is_unavailable_until_connected(rig: Rig) -> None:
    assert rig.tracker.leave_queue() == "unavailable"
    rig.remote.push_state("offline")
    rig.remote.push_break(BreakState(phase="searching", entry=_entry()))
    assert rig.tracker.leave_queue() == "unavailable"
    assert rig.remote.calls.leaves == 0


@pytest.mark.parametrize("phase", ["joining", "searching"])
def test_leave_cancels_a_search(rig: Rig, phase: BreakPhase) -> None:
    rig.ready()
    rig.remote.push_break(BreakState(phase=phase))
    assert rig.tracker.leave_queue() == "cancelled"
    assert rig.remote.calls.leaves == 1


@pytest.mark.parametrize("phase", ["none", "matched", "nobody_available", "ended"])
def test_leave_never_touches_anything_else(rig: Rig, phase: BreakPhase) -> None:
    rig.ready()
    rig.remote.push_break(BreakState(phase=phase, match=_match()))
    assert rig.tracker.leave_queue() == "not_searching"
    assert rig.remote.calls.leaves == rig.remote.calls.ends == 0
    assert rig.remote.calls.blocks == 0


def test_the_callback_works_from_another_thread(rig: Rig) -> None:
    rig.ready()
    rig.remote.push_break(BreakState(phase="searching", entry=_entry()))
    results: list[object] = []
    thread = threading.Thread(
        target=lambda: results.extend(
            [rig.tracker.queue_state(), rig.tracker.leave_queue()]
        )
    )
    thread.start()
    thread.join(5)
    assert results == ["searching", "cancelled"]


def test_the_status_reply_carries_only_the_fixed_queue_value(rig: Rig) -> None:
    rig.ready()
    rig.remote.push_break(BreakState(phase="matched", match=_match()))
    dumped = rig.ipc._status.model_dump_json()
    assert "bob_b" not in dumped
    assert rig.ipc._status.version == 4


# --- review fixes ---


def test_block_after_the_peer_ended_reaches_the_worker(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.ready()
    rig.remote.push_break(BreakState(phase="matched", match=_match()))
    rig.remote.push_break(BreakState(phase="ended", match=_match()))
    monkeypatch.setattr(
        QMessageBox, "question", lambda *a: QMessageBox.StandardButton.Yes
    )
    rig.window.block_button.click()
    assert rig.remote.calls.blocks == 1


def test_a_failed_join_starts_no_cooldown(rig: Rig) -> None:
    rig.ready()
    rig.remote.push_break(BreakState(phase="joining"))
    assert rig.controller._break_active is True
    rig.remote.push_break(BreakState(phase="none"))  # network error, 429, ...
    assert rig.controller._break_active is False
    assert rig.controller._cooldown_until is None


def test_a_search_that_ran_starts_the_cooldown_when_it_ends(rig: Rig) -> None:
    rig.ready()
    rig.remote.push_break(BreakState(phase="joining"))
    rig.remote.push_break(BreakState(phase="searching", entry=_entry()))
    rig.remote.push_break(BreakState(phase="none"))  # cancelled
    assert rig.controller._cooldown_until is not None


def test_a_failed_join_after_an_earlier_break_is_still_free(rig: Rig) -> None:
    rig.ready()
    rig.remote.push_break(BreakState(phase="searching", entry=_entry()))
    rig.remote.push_break(BreakState(phase="nobody_available"))
    rig.controller._cooldown_until = None
    rig.remote.push_break(BreakState(phase="joining"))
    rig.remote.push_break(BreakState(phase="none"))
    assert rig.controller._cooldown_until is None

"""App wiring for R2-B: X6 cue, U3 held chat, U8 read marker, O1 busy line."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pytestqt.qtbot import QtBot

from beebreak_companion.app import initial_status, wire, wire_chat
from beebreak_companion.break_state import BreakState
from beebreak_companion.chat_window import ChatWindow
from beebreak_companion.config import TriggerConfig
from beebreak_companion.controller import Controller
from beebreak_companion.desktop_notify import DesktopNotifier
from beebreak_companion.state_store import FileStateStore, PersistedState
from beebreak_companion.window import CompanionWindow
from beebreak_companion.worker import IpcWorker
from beebreak_shared.api import MatchView
from beebreak_shared.events import NormalizedEvent
from tests.fakes.chat import T0, chat_state, chat_status, message_view
from tests.fakes.remote_worker import FakeRemoteWorker


class _Tray:
    def show(self) -> None:
        pass

    def showMessage(self, title: str, message: str) -> None:
        pass


def _build(
    qtbot: QtBot, controller: Controller | None = None
) -> tuple[CompanionWindow, ChatWindow, FakeRemoteWorker]:
    window = CompanionWindow()
    chat = ChatWindow(now=lambda: T0)
    qtbot.addWidget(window)
    qtbot.addWidget(chat)
    remote = FakeRemoteWorker()
    wire_chat(
        window,
        chat,
        remote,
        DesktopNotifier(chat, tray_available=lambda: True, tray_factory=_Tray),
        is_active=lambda: False,
        main_window_active=lambda: False,
        read_marker=controller,
    )
    window.show()
    return window, chat, remote


def _match(match_id: UUID) -> MatchView:
    return MatchView(
        match_id=match_id,
        peer_nickname="bob_b",
        ends_at=T0,
        chat=chat_status(match_id, T0),
        last_seq=0,
    )


def test_declined_extension_shows_in_main_window_only_while_chat_is_hidden(
    qtbot: QtBot,
) -> None:
    window, chat, remote = _build(qtbot)
    match_id = uuid4()
    remote.push_chat(chat_state(match_id=match_id, extension_declined=True))
    assert window.extension_label.isVisible()
    remote.push_chat(chat_state(match_id=match_id, extension_declined=False))
    assert not window.extension_label.isVisible()
    chat.open_for_user()
    remote.push_chat(chat_state(match_id=match_id, extension_declined=True))
    assert not window.extension_label.isVisible()


def test_a_reported_chat_is_not_offered_by_open_chat(qtbot: QtBot) -> None:
    window, _chat, remote = _build(qtbot)
    match_id = uuid4()
    remote.push_break(BreakState(phase="ended", match=_match(match_id)))
    remote.push_chat(chat_state(match_id=match_id, ended="ended"))
    assert window.open_chat_button.isVisible()
    remote.push_chat(chat_state(match_id=match_id, ended="ended", reported=True))
    assert not window.open_chat_button.isVisible()


def _controller(path: Path) -> Controller:
    return Controller(TriggerConfig(), store=FileStateStore(path))


def test_read_marker_survives_a_restart(qtbot: QtBot, tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    match_id = uuid4()
    messages = (message_view(1), message_view(2))

    window, chat, remote = _build(qtbot, _controller(path))
    remote.push_chat(chat_state(match_id=match_id, messages=messages))
    assert "2" in window.open_chat_button.text()
    chat.open_for_user()  # the user reads them
    assert "2" not in window.open_chat_button.text()

    # Restart: a new controller reads the same file; the same chat arrives.
    window2, _chat2, remote2 = _build(qtbot, _controller(path))
    remote2.push_chat(chat_state(match_id=match_id, messages=messages))
    assert "2" not in window2.open_chat_button.text()
    # A message that arrived after the marker still counts.
    remote2.push_chat(
        chat_state(match_id=match_id, messages=(*messages, message_view(3)))
    )
    assert "1" in window2.open_chat_button.text()


def test_read_marker_for_another_match_is_ignored(qtbot: QtBot, tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    _controller(path).set_read_marker(uuid4(), 5)
    window, _chat, remote = _build(qtbot, _controller(path))
    remote.push_chat(chat_state(match_id=uuid4(), messages=(message_view(1),)))
    assert "1" in window.open_chat_button.text()


def test_read_marker_does_not_clobber_other_state(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    ctl = _controller(path)
    ctl.set_enabled(False)
    ctl.set_read_marker(uuid4(), 4)
    ctl.set_stack_tags(("python",))
    loaded = FileStateStore(path).load()
    assert loaded.enabled is False
    assert loaded.stack_tags == ("python",)
    assert loaded.read_seq == 4


def test_state_store_round_trips_the_read_marker(tmp_path: Path) -> None:
    store = FileStateStore(tmp_path / "s.json")
    state = PersistedState(read_match=uuid4(), read_seq=7)
    assert store.save(state)
    assert store.load() == state


@pytest.mark.parametrize(
    "extra",
    ['"read_match": "nope", "read_seq": 3', '"read_match": null, "read_seq": "x"'],
)
def test_a_bad_read_marker_is_just_no_marker(tmp_path: Path, extra: str) -> None:
    path = tmp_path / "s.json"
    head = '{"version": 1, "enabled": true, "cooldown_until": null, '
    path.write_text(head + extra + "}")
    loaded = FileStateStore(path).load()
    assert loaded.enabled is True
    assert loaded.read_match is None and loaded.read_seq == 0


def test_busy_agent_shows_no_idle_age(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller = _controller(tmp_path / "s.json")
    window = CompanionWindow()
    qtbot.addWidget(window)
    window.show()
    worker = IpcWorker(Path("/nonexistent"), initial_status(controller))
    wire(controller, window, worker)
    event = NormalizedEvent(
        event_version=1,
        event_id=uuid4(),
        local_session_key="k",
        agent="claude_code",
        type="turn_submitted",
        observed_at=datetime.now(UTC),
    )
    worker.event_received.emit(event)
    assert controller.activity(datetime.now(UTC)) == "active"
    assert "ago" not in window.activity_label.text()


def test_a_waiting_session_withdrawal_is_not_shown_as_expired(
    qtbot: QtBot, tmp_path: Path
) -> None:
    config = TriggerConfig(suggestion_timeout_s=60)
    controller = Controller(config, store=FileStateStore(tmp_path / "s.json"))
    window = CompanionWindow(config)
    qtbot.addWidget(window)
    window.show()
    worker = IpcWorker(Path("/nonexistent"), initial_status(controller))
    wire(controller, window, worker)
    now = datetime.now(UTC)

    def send(seconds_ago: float, kind: str, session: str = "a") -> None:
        worker.event_received.emit(
            NormalizedEvent(
                event_version=1,
                event_id=uuid4(),
                local_session_key=session,
                agent="claude_code",
                type=kind,
                observed_at=now - timedelta(seconds=seconds_ago),
            )
        )

    send(100, "turn_submitted")
    send(99, "tool_started")
    send(98, "tool_started")
    qtbot.waitUntil(lambda: controller.state == "suggested", timeout=4000)
    send(0, "waiting_input", "b")
    assert controller.state == "idle"
    assert not window.expired_label.isVisible()


def test_a_timed_out_offer_shows_expired_in_the_window(
    qtbot: QtBot, tmp_path: Path
) -> None:
    config = TriggerConfig(suggestion_timeout_s=1)
    controller = Controller(config, store=FileStateStore(tmp_path / "s.json"))
    window = CompanionWindow(config)
    qtbot.addWidget(window)
    window.show()
    worker = IpcWorker(Path("/nonexistent"), initial_status(controller))
    wire(controller, window, worker)
    now = datetime.now(UTC)
    for seconds_ago, kind in (
        (100, "turn_submitted"),
        (99, "tool_started"),
        (98, "tool_started"),
    ):
        worker.event_received.emit(
            NormalizedEvent(
                event_version=1,
                event_id=uuid4(),
                local_session_key="a",
                agent="claude_code",
                type=kind,
                observed_at=now - timedelta(seconds=seconds_ago),
            )
        )
    qtbot.waitUntil(window.expired_label.isVisible, timeout=6000)


def test_the_declined_cue_does_not_return_for_a_seen_decline(qtbot: QtBot) -> None:
    window, chat, remote = _build(qtbot)
    match_id = uuid4()
    status = chat_status(match_id, T0)
    declined = chat_state(match_id=match_id, status=status, extension_declined=True)
    remote.push_chat(declined)
    assert window.extension_label.isVisible()
    chat.open_for_user()  # the user sees it in the chat window
    assert not window.extension_label.isVisible()
    chat.hide()
    # A peer message arrives: same status object, the flag is still set.
    remote.push_chat(
        chat_state(
            match_id=match_id,
            status=status,
            extension_declined=True,
            messages=(message_view(1),),
        )
    )
    assert not window.extension_label.isVisible()
    # A newer status frame that declines again shows the cue again.
    remote.push_chat(
        chat_state(
            match_id=match_id,
            status=chat_status(match_id, T0),
            extension_declined=True,
            messages=(message_view(1),),
        )
    )
    assert window.extension_label.isVisible()

"""M5-08 items 2-3: no chat content leaks (README rules 14, 16, 17).

A full chat runs through the real server between two real `RemoteWorker`s, each
wired to a real `ChatWindow` and `CompanionWindow` by `app.wire_chat`, with a
recording notifier. Canary bodies and a canary report note are used throughout.

Scanned for canaries: every log record (server and companion, DEBUG), every
notifier call, every `StatusReply` the companion would answer and every MCP
`beebreak_status` result (these two also for nicknames), and finally the
database after the retention purges: bodies are gone after
`message_retention_s`, and nothing at all remains after `report_retention_s`
past a report's resolution.
"""

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from PySide6.QtCore import Qt
from pytestqt.qtbot import QtBot
from sqlalchemy import select, update

from beebreak_agent_bridge.mcp_server import build_server
from beebreak_companion.app import BreakTracker, initial_status, wire_chat
from beebreak_companion.break_state import BreakState
from beebreak_companion.chat_window import ChatWindow
from beebreak_companion.config import TriggerConfig
from beebreak_companion.controller import Controller
from beebreak_companion.desktop_notify import DesktopNotifier
from beebreak_companion.remote import RemoteWorker
from beebreak_companion.state_store import FileStateStore
from beebreak_companion.window import CompanionWindow
from beebreak_server.matchmaking import Matchmaker
from beebreak_server.models import Report, ReportMessage
from beebreak_shared.presence import build_presence
from tests.e2e_support.server import LiveServer
from tests.privacy.test_outbound_leaks import (
    ALL_CONSENT,
    Recorder,
    dump_database,
    find_leaks,
    sign_in_worker,
    worker_factory,  # noqa: F401  (fixture)
)
from tests.server.conftest import FixedClock
from tests.server.fakes import RecordingNotifier

pytest_plugins = ["tests.e2e_support.server"]  # live_server fixtures

WAIT_MS = 15_000
ADA, BOB = "ada_l", "bobby"
NICKNAMES = [ADA, BOB]
A1 = "CANARY-MSG-A1-5d0c1f"
A2 = "CANARY-MSG-A2-8e42b7"
B1 = "CANARY-MSG-B1-1a9f33"
B2 = "CANARY-MSG-B2-c07d58"  # ticked in the report
B3 = "CANARY-MSG-B3-62e1aa"
NOTE = "CANARY-NOTE-f4417b"
BODIES = [A1, A2, B1, B2, B3]
CANARIES = [*BODIES, NOTE]
TRIGGER = TriggerConfig(
    activity_threshold_s=60,
    min_tool_events=2,
    cooldown_s=900,
    suggestion_timeout_s=60,
)


class RecordingDesktopNotifier(DesktopNotifier):
    """A DesktopNotifier that records instead of showing anything."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, str]] = []

    def notify(self, title: str, text: str) -> None:
        self.calls.append((title, text))


@dataclass
class Side:
    name: str
    worker: RemoteWorker
    rec: Recorder
    window: CompanionWindow
    chat: ChatWindow
    notifier: RecordingDesktopNotifier
    tracker: BreakTracker
    controller: Controller
    replies: list[str] = field(default_factory=list)
    mcp: list[str] = field(default_factory=list)

    def snapshot(self) -> None:
        """What the companion would answer on IPC, and what MCP would return."""
        reply = initial_status(self.controller, self.tracker.queue_state())
        self.replies.append(reply.model_dump_json())

        async def call() -> dict[str, Any]:
            async with Client(build_server(lambda: reply)) as client:
                result = await client.call_tool("beebreak_status", {})
                assert not result.is_error and result.structured_content
                return dict(result.structured_content)

        self.mcp.append(json.dumps(asyncio.run(call())))

    def queue(self) -> str:
        return str(json.loads(self.mcp[-1])["queue"])


def make_side(
    qtbot: QtBot,
    tmp_path: Path,
    name: str,
    worker: RemoteWorker,
    rec: Recorder,
) -> Side:
    window = CompanionWindow()
    chat = ChatWindow()
    qtbot.addWidget(window)
    qtbot.addWidget(chat)
    notifier = RecordingDesktopNotifier()
    tracker = BreakTracker()
    controller = Controller(TRIGGER, store=FileStateStore(tmp_path / f"{name}.json"))
    tracker.attach(worker)

    def on_break(state: object) -> None:
        assert isinstance(state, BreakState)
        tracker.set_phase(state.phase)
        window.set_break(state)

    worker.break_changed.connect(on_break)
    wire_chat(window, chat, worker, notifier, is_active=lambda: False)
    return Side(name, worker, rec, window, chat, notifier, tracker, controller)


def chat_has(side: Side, bodies: list[str]) -> Callable[[], bool]:
    def check() -> bool:
        # The window gets its state through a queued signal, so wait on what
        # the window holds, not only on the worker thread's copy.
        worker = {m.body for m in side.worker.chat_state.messages}
        window = {m.body for m in side.chat._state.messages}
        return set(bodies) <= worker & window

    return check


def table_hits(
    channels: dict[str, list[bytes]], canaries: list[str]
) -> dict[str, set[str]]:
    """Which canaries appear in which channel."""
    hits: dict[str, set[str]] = {}
    for hit in find_leaks(channels, canaries):
        channel, canary = hit.split(": ", 1)
        hits.setdefault(channel, set()).add(canary.strip("'"))
    return hits


def test_a_full_chat_leaks_no_content(
    qtbot: QtBot,
    tmp_path: Path,
    server_factory: Callable[..., LiveServer],
    worker_factory: Callable[[int], tuple[RemoteWorker, Recorder]],  # noqa: F811
    caplog: pytest.LogCaptureFixture,
    capfd: pytest.CaptureFixture[str],
) -> None:
    caplog.set_level(logging.DEBUG)
    live = server_factory(match_sweep_interval_s=0.05)
    wa, rec_a = worker_factory(live.port)
    wb, rec_b = worker_factory(live.port)
    sign_in_worker(qtbot, live, wa, rec_a, "ada@example.com", ADA, ALL_CONSENT)
    sign_in_worker(qtbot, live, wb, rec_b, "bob@example.com", BOB, ALL_CONSENT)
    a = make_side(qtbot, tmp_path, "a", wa, rec_a)
    b = make_side(qtbot, tmp_path, "b", wb, rec_b)
    assert a.window is not b.window
    a.snapshot()
    assert a.queue() == "none"

    presence = build_presence(
        ALL_CONSENT,
        task_category="other",
        stack_tags=["python"],
        remaining_time_bucket="unknown",
    )
    wa.join_queue(presence)
    wb.join_queue(presence)
    qtbot.waitUntil(
        lambda: rec_a.phase == "matched" and rec_b.phase == "matched",
        timeout=WAIT_MS,
    )
    qtbot.waitUntil(
        lambda: wa.chat_state.status is not None and wb.chat_state.status is not None,
        timeout=WAIT_MS,
    )
    # A match no longer opens the window: each user opens it, as with a click.
    a.chat.open_for_user()
    b.chat.open_for_user()
    qtbot.waitUntil(lambda: a.chat.isVisible() and b.chat.isVisible(), timeout=WAIT_MS)

    # Chat both ways, through the windows' own input where it is a user action.
    a.chat.input.setPlainText(A1)
    qtbot.keyClick(a.chat.input, Qt.Key.Key_Return)  # type: ignore[no-untyped-call]
    qtbot.waitUntil(chat_has(b, [A1]), timeout=WAIT_MS)
    wb.send_message(B1)
    qtbot.waitUntil(chat_has(a, [B1]), timeout=WAIT_MS)
    wa.send_message(A2)
    wb.send_message(B2)
    wb.send_message(B3)
    qtbot.waitUntil(chat_has(a, BODIES), timeout=WAIT_MS)
    qtbot.waitUntil(chat_has(b, BODIES), timeout=WAIT_MS)

    # The peer got a notification that names the nickname only.
    assert b.notifier.calls, "B's window was inactive, a notification was due"
    assert {c[1] for c in b.notifier.calls} == {f"New message from {ADA}"}
    assert {c[1] for c in a.notifier.calls} <= {f"New message from {BOB}"}
    assert a.notifier.calls

    # Rule 14 while chatting: the fixed enum and nothing else.
    for side in (a, b):
        side.snapshot()
        assert side.queue() == "chatting"
    assert json.loads(a.mcp[-1]).keys() == {
        "companion",
        "invitation",
        "last_event_at",
        "activity",
        "estimate",
        "queue",
        "ready",
        "not_ready_reason",
    }

    # A reports B, ticking only B2, with a canary note, through the dialog.
    a.chat.report_button.click()
    dialog = a.chat._report_dialog
    assert dialog is not None
    dialog.reason_buttons["harassment"].setChecked(True)
    dialog.note_edit.setPlainText(NOTE)
    ticked = [
        row
        for row in range(dialog.message_list.count())
        if B2 in dialog.message_list.item(row).text()
    ]
    assert len(ticked) == 1
    dialog.message_list.item(ticked[0]).setCheckState(Qt.CheckState.Checked)
    dialog.report_button.click()
    qtbot.waitUntil(
        lambda: (
            wa.chat_state.reported
            and wa.chat_state.ended is not None
            and wb.chat_state.ended is not None
        ),
        timeout=WAIT_MS,
    )
    qtbot.waitUntil(
        lambda: rec_a.phase == "ended" and rec_b.phase == "ended", timeout=WAIT_MS
    )
    for side in (a, b):
        side.snapshot()
        assert side.queue() == "none"

    # --- rule 16: logs, notifications, IPC and MCP never carry a canary ---
    out = capfd.readouterr()
    logs = [caplog.text.encode(), out.out.encode(), out.err.encode()]
    assert caplog.records, "the capture saw nothing, so no hits would mean nothing"
    notified = [f"{t} {x}".encode() for s in (a, b) for t, x in s.notifier.calls]
    ipc = [r.encode() for s in (a, b) for r in s.replies + s.mcp]
    assert find_leaks({"logs": logs, "notifier": notified}, CANARIES) == []
    assert find_leaks({"ipc": ipc}, CANARIES + NICKNAMES) == []

    # --- the database: bodies exist while the chat is within retention ---
    now = datetime.now(UTC)
    before = table_hits(dump_database(live), CANARIES)
    assert set(before["db.messages"]) == set(BODIES)
    assert before["db.report_messages"] == {B2}
    assert before["db.reports"] == {NOTE}
    assert set(before) == {"db.messages", "db.report_messages", "db.reports"}

    # After message_retention_s and a purge pass: no body, except the one the
    # user ticked, which lives in the report evidence.
    clock = FixedClock(now + timedelta(seconds=live.config.message_retention_s + 120))
    sweep = Matchmaker(live.session_factory, live.config, clock, RecordingNotifier())
    sweep.run_once()
    after_messages = table_hits(dump_database(live), CANARIES)
    assert "db.messages" not in after_messages
    assert after_messages == {"db.report_messages": {B2}, "db.reports": {NOTE}}

    # The match row goes at retention_s; the report still stands.
    clock.advance(live.config.retention_s)
    Matchmaker(live.session_factory, live.config, clock, RecordingNotifier()).run_once()
    assert table_hits(dump_database(live), CANARIES) == after_messages

    # Open reports are kept until resolved, however old.
    clock.advance(live.config.report_retention_s + 120)
    Matchmaker(live.session_factory, live.config, clock, RecordingNotifier()).run_once()
    assert table_hits(dump_database(live), CANARIES) == after_messages

    # Resolve it; report_retention_s later nothing at all remains.
    with live.session_factory() as session:
        session.execute(
            update(Report).values(status="dismissed", resolved_at=clock.now)
        )
        session.commit()
    clock.advance(live.config.report_retention_s - 60)
    Matchmaker(live.session_factory, live.config, clock, RecordingNotifier()).run_once()
    assert table_hits(dump_database(live), CANARIES) == after_messages  # not yet
    clock.advance(180)
    Matchmaker(live.session_factory, live.config, clock, RecordingNotifier()).run_once()
    assert table_hits(dump_database(live), CANARIES) == {}
    with live.session_factory() as session:
        assert session.scalars(select(Report)).all() == []
        assert session.scalars(select(ReportMessage)).all() == []


def test_the_detector_sees_canaries_in_each_chat_channel(
    qtbot: QtBot,
    tmp_path: Path,
    live_server: LiveServer,
    worker_factory: Callable[[int], tuple[RemoteWorker, Recorder]],  # noqa: F811
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    worker, rec = worker_factory(live_server.port)
    side = make_side(qtbot, tmp_path, "d", worker, rec)
    side.notifier.notify("Beebreak", f"New message from {A1}")
    logging.getLogger("beebreak_companion.test").warning("body %s", A1)
    side.replies.append(f'{{"queue": "{A1}", "nick": "{ADA}"}}')
    notified = [f"{t} {x}".encode() for t, x in side.notifier.calls]
    channels = {
        "logs": [caplog.text.encode()],
        "notifier": notified,
        "ipc": [r.encode() for r in side.replies],
    }
    assert {h.split(":")[0] for h in find_leaks(channels, [A1])} == {
        "logs",
        "notifier",
        "ipc",
    }
    assert find_leaks({"ipc": channels["ipc"]}, NICKNAMES) != []
    assert find_leaks(channels, ["never-used-0xC0FFEE"]) == []

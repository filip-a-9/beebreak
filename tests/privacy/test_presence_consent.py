"""Presence beyond consent (M4-08 item 3, M4 rule 13).

For all 8 consent combinations the Find button is driven through the real app
wiring (window, controller, `wire_break`, a real RemoteWorker) against a real
server. The captured join body, and then the server row, carry exactly the
consented fields; once the match or the expiry ends the entry, the row holds
none of them.
"""

import itertools
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pytestqt.qtbot import QtBot
from sqlalchemy import select

from beebreak_companion.app import BreakTracker, wire_break
from beebreak_companion.config import TriggerConfig
from beebreak_companion.controller import Controller
from beebreak_companion.remote import RemoteWorker
from beebreak_companion.state_store import FileStateStore
from beebreak_companion.window import CompanionWindow
from beebreak_server.models import QueueEntry, User, split_tags
from beebreak_shared.api import Consent
from beebreak_shared.presence import StackTag, build_presence
from tests.e2e_support.server import LiveServer
from tests.privacy.test_outbound_leaks import (
    ALL_CONSENT,
    WAIT_MS,
    Capture,
    Recorder,
    sign_in_worker,
)

# live_server fixtures, and capture / worker_factory from the leak test
pytest_plugins = ["tests.e2e_support.server", "tests.privacy.test_outbound_leaks"]

TAGS: tuple[StackTag, ...] = ("python", "docker")
COMBOS = list(itertools.product([False, True], repeat=3))
PRESENCE_KEYS = {
    "schema_version",
    "presence_id",
    "task_category",
    "stack_tags",
    "remaining_time_bucket",
    "status",
}


def _consent(combo: tuple[bool, bool, bool]) -> Consent:
    category, tags, time = combo
    return Consent(
        version=1,
        share_task_category=category,
        share_stack_tags=tags,
        share_time_bucket=time,
    )


def _join_bodies(capture: Capture) -> list[dict[str, Any]]:
    bodies = []
    for blob in capture.channels()["http"]:
        first = blob.split(b"\n", 1)[0]
        if first.startswith(b"POST ") and first.endswith(b"/v1/queue"):
            bodies.append(json.loads(blob.rpartition(b"\n")[2]))
    return bodies


def _entries(server: LiveServer, email: str) -> list[QueueEntry]:
    with server.session_factory() as session:
        user = session.scalars(select(User).where(User.email_canonical == email)).one()
        return list(
            session.scalars(select(QueueEntry).where(QueueEntry.user_id == user.id))
        )


def _expected(consent: Consent) -> dict[str, Any]:
    return {
        "task_category": "other" if consent.share_task_category else None,
        "stack_tags": list(TAGS) if consent.share_stack_tags else [],
        "remaining_time_bucket": "unknown" if consent.share_time_bucket else None,
    }


class App:
    """Window, controller and break wiring around one real RemoteWorker."""

    def __init__(self, qtbot: QtBot, tmp_path: Path, remote: RemoteWorker) -> None:
        self.controller = Controller(
            TriggerConfig(
                activity_threshold_s=60,
                min_tool_events=2,
                cooldown_s=900,
                suggestion_timeout_s=60,
            ),
            store=FileStateStore(tmp_path / "state.json"),
        )
        self.controller.set_stack_tags(TAGS)
        self.window = CompanionWindow()
        qtbot.addWidget(self.window)
        self.tracker = BreakTracker()
        wire_break(self.controller, self.window, remote, self.tracker, lambda: None)
        self.window.show()


@pytest.mark.parametrize("combo", COMBOS)
def test_join_body_and_server_row_carry_exactly_the_consented_fields(
    qtbot: QtBot,
    tmp_path: Path,
    server_factory: Callable[..., LiveServer],
    capture: Capture,
    worker_factory: Callable[[int], tuple[RemoteWorker, Recorder]],
    combo: tuple[bool, bool, bool],
) -> None:
    server = server_factory(match_sweep_interval_s=0.05)
    consent = _consent(combo)
    a, rec_a = worker_factory(server.port)
    b, rec_b = worker_factory(server.port)
    app = App(qtbot, tmp_path, a)
    sign_in_worker(qtbot, server, a, rec_a, "ada@example.com", "ada_l", consent)
    sign_in_worker(qtbot, server, b, rec_b, "bob@example.com", "bobby", ALL_CONSENT)

    qtbot.waitUntil(lambda: app.window.find_button.isVisible(), timeout=WAIT_MS)
    app.window.find_button.click()
    qtbot.waitUntil(lambda: rec_a.phase == "searching", timeout=WAIT_MS)

    # The request body: exactly the consented fields, nothing else.
    (body,) = _join_bodies(capture)
    sent = body["presence"]
    assert set(sent) <= PRESENCE_KEYS
    want = _expected(consent)
    assert {k: sent[k] for k in want} == want
    assert sent["status"] == "available"

    # The server row: the same.
    (row,) = _entries(server, "ada@example.com")
    assert row.status == "queued"
    assert row.task_category == want["task_category"]
    assert list(split_tags(row.stack_tags)) == want["stack_tags"]
    assert row.remaining_time_bucket == want["remaining_time_bucket"]

    # After the match the row keeps status and times only.
    b.join_queue(
        build_presence(
            ALL_CONSENT,
            task_category="other",
            stack_tags=list(TAGS),
            remaining_time_bucket="unknown",
        )
    )
    qtbot.waitUntil(lambda: rec_a.phase == "matched", timeout=WAIT_MS)
    for email in ("ada@example.com", "bob@example.com"):
        (done,) = _entries(server, email)
        assert done.status == "matched"
        assert done.presence_id is None
        assert done.task_category is None
        assert done.stack_tags is None
        assert done.remaining_time_bucket is None


def test_an_expired_search_leaves_no_presence_in_the_row(
    qtbot: QtBot,
    tmp_path: Path,
    server_factory: Callable[..., LiveServer],
    worker_factory: Callable[[int], tuple[RemoteWorker, Recorder]],
) -> None:
    server = server_factory(match_sweep_interval_s=0.05, queue_ttl_s=1)
    a, rec_a = worker_factory(server.port)
    app = App(qtbot, tmp_path, a)
    sign_in_worker(qtbot, server, a, rec_a, "ada@example.com", "ada_l", ALL_CONSENT)
    qtbot.waitUntil(lambda: app.window.find_button.isVisible(), timeout=WAIT_MS)
    app.window.find_button.click()
    qtbot.waitUntil(lambda: rec_a.phase == "nobody_available", timeout=WAIT_MS)
    (row,) = _entries(server, "ada@example.com")
    assert row.status == "expired"
    assert row.presence_id is None
    assert row.task_category is None
    assert row.stack_tags is None
    assert row.remaining_time_bucket is None


def test_a_cancelled_search_leaves_no_presence_in_the_row(
    qtbot: QtBot,
    tmp_path: Path,
    server_factory: Callable[..., LiveServer],
    worker_factory: Callable[[int], tuple[RemoteWorker, Recorder]],
) -> None:
    server = server_factory(match_sweep_interval_s=0.05)
    a, rec_a = worker_factory(server.port)
    app = App(qtbot, tmp_path, a)
    sign_in_worker(qtbot, server, a, rec_a, "ada@example.com", "ada_l", ALL_CONSENT)
    qtbot.waitUntil(lambda: app.window.find_button.isVisible(), timeout=WAIT_MS)
    app.window.find_button.click()
    qtbot.waitUntil(lambda: rec_a.phase == "searching", timeout=WAIT_MS)
    app.window.cancel_button.click()
    qtbot.waitUntil(lambda: rec_a.phase == "none", timeout=WAIT_MS)
    (row,) = _entries(server, "ada@example.com")
    assert row.status == "cancelled"
    assert row.task_category is None and row.stack_tags is None
    assert row.remaining_time_bucket is None and row.presence_id is None

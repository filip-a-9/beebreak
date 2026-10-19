"""Cancelling my own extension request is not a decline by the peer."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from pytestqt.qtbot import QtBot

from tests.fakes.chat import chat_json
from tests.unit.test_remote_worker_break import frame
from tests.unit.test_remote_worker_chat import (  # noqa: F401  (fixtures)
    MATCH_ID,
    ChatServer,
    _now,
    make_rig,
    open_chat,
    server,
    settle,
    wait,
)


def _request_pending(qtbot: QtBot, rig, ends) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    status = chat_json(MATCH_ID, ends)
    status["extension"] = "requested_by_me"
    rig.server.push(frame("chat.status", status=status, change="extension_requested"))
    wait(
        qtbot,
        lambda: (
            rig.rec.last.status is not None
            and rig.rec.last.status.extension == "requested_by_me"
        ),
    )
    return status


def test_my_cancel_does_not_show_said_no(qtbot, make_rig, server: ChatServer) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    rig = make_rig()
    open_chat(qtbot, rig)
    ends = _now() + timedelta(minutes=15)
    status = _request_pending(qtbot, rig, ends)
    server.extension_status = chat_json(MATCH_ID, ends)

    rig.worker.answer_extension(False)  # Cancel
    status["extension"] = "none"
    rig.server.push(frame("chat.status", status=status, change="extension_declined"))
    wait(qtbot, lambda: rig.rec.last.status.extension == "none")
    settle(qtbot)

    assert not any(s.extension_declined for s in rig.rec.states)


def test_the_peers_real_decline_still_shows_said_no(qtbot, make_rig) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    rig = make_rig()
    open_chat(qtbot, rig)
    status = _request_pending(qtbot, rig, _now() + timedelta(minutes=15))
    status["extension"] = "none"

    rig.server.push(frame("chat.status", status=status, change="extension_declined"))

    wait(qtbot, lambda: rig.rec.last.extension_declined)

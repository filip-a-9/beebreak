"""RemoteWorker chat flows against a tiny in-test server (M5-05 part A).

Builds on the break tests' server (refresh, me, `/v1/realtime`) and adds the
chat routes. The live-server tests are part B.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from PySide6.QtCore import QObject, Slot
from pytestqt.qtbot import QtBot
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from beebreak_companion.chat_state import ChatState
from beebreak_companion.credentials import FileCredentialStore, StoredCredentials
from beebreak_companion.remote import RemoteWorker
from beebreak_companion.remote_config import RemoteConfig
from tests.fakes.chat import chat_json
from tests.unit.test_remote_worker_break import (
    PEER,
    WAIT_MS,
    FakeServer,
    frame,
    match_json,
)

MATCH_ID = UUID(int=910)
MESSAGES = f"/v1/matches/{MATCH_ID}/messages"
OTHER = UUID(int=920)
BODY_CANARY = "canary-body-text"


def _now() -> datetime:
    return datetime.now(UTC)


def message_json(
    seq: int,
    *,
    mine: bool = False,
    body: str | None = None,
    client_message_id: str | None = None,
) -> dict[str, Any]:
    return {
        "seq": seq,
        "mine": mine,
        "body": body if body is not None else f"message {seq}",
        "sent_at": (
            datetime(2026, 10, 14, 12, 0, tzinfo=UTC) + timedelta(seconds=seq)
        ).isoformat(),
        "client_message_id": client_message_id if mine else None,
    }


def chat_match_json(last_seq: int = 0) -> dict[str, Any]:
    return {**match_json(MATCH_ID), "last_seq": last_seq}


class ChatServer(FakeServer):
    """The break server plus the four chat routes."""

    def __init__(self) -> None:
        super().__init__()
        self.messages: list[dict[str, Any]] = []  # what GET returns, oldest first
        self.sent: dict[str, dict[str, Any]] = {}  # client_message_id -> message
        self.send_error: tuple[int, str] | None = None
        self.fail_sends = 0  # next N sends get an unparseable 502
        self.list_delay_s = 0.0
        self.extension_error: tuple[int, str] | None = None
        self.extension_status: dict[str, Any] | None = None
        self.report_error: tuple[int, str] | None = None
        self.list_requests: list[str] = []  # path?query of each GET

    def send_bodies(self) -> list[dict[str, Any]]:
        return [json.loads(r[3]) for r in self.calls("POST", MESSAGES)]

    def list_queries(self) -> list[str]:
        return list(self.list_requests)

    async def _post_message(self, request: Request) -> Response:
        body = await self._record(request)
        if not self._auth_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if self.fail_sends > 0:
            self.fail_sends -= 1
            return Response("bad gateway", status_code=502)
        if self.send_error is not None:
            status, code = self.send_error
            return JSONResponse({"error": code}, status_code=status)
        data = json.loads(body)
        cid = data["client_message_id"]
        if cid in self.sent:
            return JSONResponse(self.sent[cid], status_code=200)
        view = message_json(
            len(self.messages) + 1, mine=True, body=data["body"], client_message_id=cid
        )
        self.sent[cid] = view
        self.messages.append(view)
        return JSONResponse(view, status_code=201)

    async def _get_messages(self, request: Request) -> Response:
        await self._record(request)
        self.list_requests.append(f"{request.url.path}?{request.url.query}")
        if not self._auth_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        after = int(request.query_params["after_seq"])
        page = [m for m in self.messages if m["seq"] > after][:100]
        reply = JSONResponse(
            {"messages": page, "last_seq": len(self.messages)}
        )  # read before any delay
        if self.list_delay_s:
            await asyncio.sleep(self.list_delay_s)
        return reply

    async def _extension(self, request: Request) -> Response:
        await self._record(request)
        if not self._auth_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if self.extension_error is not None:
            status, code = self.extension_error
            return JSONResponse({"error": code}, status_code=status)
        status_json = self.extension_status or chat_json(
            MATCH_ID, _now() + timedelta(minutes=25)
        )
        return JSONResponse(status_json)

    async def _report(self, request: Request) -> Response:
        await self._record(request)
        if not self._auth_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if self.report_error is not None:
            status, code = self.report_error
            return JSONResponse({"error": code}, status_code=status)
        return Response(status_code=204)

    def _app(self) -> Starlette:
        app = super()._app()
        post, get = ["POST"], ["GET"]
        app.router.routes.extend(
            [
                Route(
                    "/v1/matches/{match_id}/messages", self._post_message, methods=post
                ),
                Route(
                    "/v1/matches/{match_id}/messages", self._get_messages, methods=get
                ),
                Route(
                    "/v1/matches/{match_id}/extension", self._extension, methods=post
                ),
                Route("/v1/reports", self._report, methods=post),
            ]
        )
        return app


class ChatRecorder(QObject):
    def __init__(self, worker: RemoteWorker) -> None:
        super().__init__()
        self.states: list[ChatState] = []
        self.errors: list[str] = []
        self.connection: list[str] = []
        worker.chat_changed.connect(self.on_chat)
        worker.error.connect(self.on_error)
        worker.state_changed.connect(self.on_state)

    @Slot(object)
    def on_chat(self, state: object) -> None:
        assert isinstance(state, ChatState)
        self.states.append(state)

    @Slot(str)
    def on_error(self, code: str) -> None:
        self.errors.append(code)

    @Slot(str)
    def on_state(self, state: str) -> None:
        self.connection.append(state)

    @property
    def last(self) -> ChatState:
        return self.states[-1] if self.states else ChatState()


class Rig:
    def __init__(
        self, server: ChatServer, worker: RemoteWorker, rec: ChatRecorder
    ) -> None:
        self.server, self.worker, self.rec = server, worker, rec


@pytest.fixture
def server() -> Iterator[ChatServer]:
    fake = ChatServer()
    fake.start()
    yield fake
    fake.stop()


@pytest.fixture
def make_rig(
    server: ChatServer, tmp_path: Path, qtbot: QtBot
) -> Iterator[Callable[..., Rig]]:
    made: list[RemoteWorker] = []

    def make() -> Rig:
        config = RemoteConfig(
            server_url=server.base_url,
            http_timeout_s=2,
            ws_ping_interval_s=1.0,
            reconnect_min_s=0.05,
            reconnect_max_s=0.2,
        )
        store = FileCredentialStore(tmp_path / "a.json", server.base_url)
        store.save(
            StoredCredentials(
                refresh_token="brr_seed",
                refresh_expires_at=_now() + timedelta(days=30),
            )
        )
        worker = RemoteWorker(config, store)
        made.append(worker)
        rig = Rig(server, worker, ChatRecorder(worker))
        worker.start()
        wait(qtbot, lambda: rig.rec.connection[-1:] == ["connected"])
        return rig

    yield make
    for worker in made:
        worker.stop()


def wait(qtbot: QtBot, condition: Callable[[], bool]) -> None:
    qtbot.waitUntil(condition, timeout=WAIT_MS)


def settle(qtbot: QtBot, extra_ms: int = 200) -> None:
    qtbot.wait(extra_ms)


def seqs(state: ChatState) -> list[int]:
    return [m.seq for m in state.messages]


def open_chat(qtbot: QtBot, rig: Rig, last_seq: int = 0) -> None:
    """The chat opens through a `match.created` frame."""
    rig.server.sync = {"queue": None, "match": chat_match_json(last_seq)}
    rig.server.push(
        frame("match.created", entry_id=str(uuid4()), match=chat_match_json(last_seq))
    )
    wait(qtbot, lambda: rig.rec.last.match_id == MATCH_ID)


# --- opening, offset, status ---


def test_match_created_opens_the_chat_and_sets_the_offset(
    qtbot: QtBot, make_rig: Callable[..., Rig]
) -> None:
    rig = make_rig()
    ahead = _now() + timedelta(seconds=30)

    rig.server.push(
        {
            **frame("match.created", entry_id=str(uuid4()), match=chat_match_json()),
            "server_time": ahead.isoformat(),
        }
    )
    wait(qtbot, lambda: rig.rec.last.match_id == MATCH_ID)

    state = rig.rec.last
    assert state.peer_nickname == PEER
    assert state.status is not None and state.status.match_id == MATCH_ID
    assert state.messages == () and state.pending == () and state.ended is None
    assert 29 < state.server_offset_s < 31  # emitted with the first state
    assert rig.worker.chat_state == state
    assert len(rig.rec.states) == 1


def test_sync_with_a_match_opens_the_chat(
    qtbot: QtBot, make_rig: Callable[..., Rig], server: ChatServer
) -> None:
    server.sync = {"queue": None, "match": chat_match_json()}

    rig = make_rig()
    wait(qtbot, lambda: rig.rec.last.match_id == MATCH_ID)

    assert rig.rec.last.peer_nickname == PEER


def test_a_status_frame_updates_the_status_and_an_offset_alone_does_not_emit(
    qtbot: QtBot, make_rig: Callable[..., Rig]
) -> None:
    rig = make_rig()
    open_chat(qtbot, rig)
    status = chat_json(MATCH_ID, _now() + timedelta(minutes=15))
    status["peer_online"] = False

    rig.server.push(frame("chat.status", status=status, change="peer_offline"))
    wait(
        qtbot,
        lambda: rig.rec.last.status is not None and not rig.rec.last.status.peer_online,
    )

    emitted = len(rig.rec.states)
    shifted = {
        **frame("chat.status", status=status, change="peer_offline"),
        "server_time": (_now() + timedelta(seconds=60)).isoformat(),
    }
    rig.server.push(shifted)
    wait(qtbot, lambda: 59 < rig.worker.chat_state.server_offset_s < 61)
    settle(qtbot)

    assert len(rig.rec.states) == emitted  # nothing else changed: no emission


# --- gap fill ---


def test_sync_with_missed_messages_fetches_them_once(
    qtbot: QtBot, make_rig: Callable[..., Rig], server: ChatServer
) -> None:
    server.messages = [message_json(1), message_json(2), message_json(3)]
    server.sync = {"queue": None, "match": chat_match_json(last_seq=3)}

    rig = make_rig()
    wait(qtbot, lambda: seqs(rig.rec.last) == [1, 2, 3])
    settle(qtbot)

    assert server.list_queries() == [f"{MESSAGES}?after_seq=0"]
    assert server.calls("GET", MESSAGES)[0][2] == "Bearer bra_1"


def test_a_sync_with_nothing_missed_fetches_nothing(
    qtbot: QtBot, make_rig: Callable[..., Rig], server: ChatServer
) -> None:
    server.sync = {"queue": None, "match": chat_match_json(last_seq=0)}

    rig = make_rig()
    wait(qtbot, lambda: rig.rec.last.match_id == MATCH_ID)
    settle(qtbot)

    assert server.list_queries() == []


def test_a_seq_gap_triggers_exactly_one_fetch(
    qtbot: QtBot, make_rig: Callable[..., Rig], server: ChatServer
) -> None:
    rig = make_rig()
    open_chat(qtbot, rig)
    server.messages = [message_json(n) for n in (1, 2, 3)]
    rig.server.push(
        frame("chat.message", match_id=str(MATCH_ID), message=message_json(1))
    )
    wait(qtbot, lambda: seqs(rig.rec.last) == [1])
    assert server.list_queries() == []  # in order: no fetch

    rig.server.push(
        frame("chat.message", match_id=str(MATCH_ID), message=message_json(3))
    )
    wait(qtbot, lambda: seqs(rig.rec.last) == [1, 2, 3])
    settle(qtbot)

    assert server.list_queries() == [f"{MESSAGES}?after_seq=1"]


def test_frames_during_a_fetch_do_not_start_a_second_one(
    qtbot: QtBot, make_rig: Callable[..., Rig], server: ChatServer
) -> None:
    rig = make_rig()
    open_chat(qtbot, rig)
    server.messages = [message_json(n) for n in range(1, 6)]
    server.list_delay_s = 0.4

    for n in (3, 5):
        rig.server.push(
            frame("chat.message", match_id=str(MATCH_ID), message=message_json(n))
        )
    wait(qtbot, lambda: seqs(rig.rec.last) == [1, 2, 3, 4, 5])
    settle(qtbot)

    assert len(server.list_queries()) == 1


def test_a_failed_fetch_leaves_the_messages_and_the_next_gap_retries(
    qtbot: QtBot, make_rig: Callable[..., Rig], server: ChatServer
) -> None:
    rig = make_rig()
    open_chat(qtbot, rig)
    # No messages on the server yet: the page is empty and makes no progress.
    rig.server.push(
        frame("chat.message", match_id=str(MATCH_ID), message=message_json(2))
    )
    wait(qtbot, lambda: seqs(rig.rec.last) == [2])
    wait(qtbot, lambda: len(server.list_queries()) == 1)

    server.messages = [message_json(1), message_json(2), message_json(3)]
    rig.server.push(
        frame("chat.message", match_id=str(MATCH_ID), message=message_json(3))
    )

    wait(qtbot, lambda: seqs(rig.rec.last) == [1, 2, 3])
    assert len(server.list_queries()) == 2
    assert rig.rec.errors == []


def test_chat_frames_for_another_match_are_ignored(
    qtbot: QtBot, make_rig: Callable[..., Rig], server: ChatServer
) -> None:
    rig = make_rig()
    open_chat(qtbot, rig)
    count = len(rig.rec.states)

    rig.server.push(
        frame("chat.message", match_id=str(UUID(int=1)), message=message_json(1))
    )
    settle(qtbot)

    assert len(rig.rec.states) == count and server.list_queries() == []


# --- sending ---


def test_send_normalizes_posts_once_and_confirms_by_the_reply(
    qtbot: QtBot, make_rig: Callable[..., Rig], server: ChatServer
) -> None:
    rig = make_rig()
    open_chat(qtbot, rig)

    rig.worker.send_message("  hello ‮ there  \n\n\n\nbye ")
    wait(qtbot, lambda: seqs(rig.rec.last) == [1])

    (body,) = server.send_bodies()
    assert body["body"] == "hello  there  \n\nbye"
    UUID(body["client_message_id"])
    assert server.calls("POST", MESSAGES)[0][2] == "Bearer bra_1"
    sending = [s for s in rig.rec.states if s.pending]
    assert sending and sending[0].pending[0].status == "sending"
    assert rig.rec.last.pending == ()
    assert rig.rec.last.messages[0].mine

    # The echo frame for the same message changes nothing.
    count = len(rig.rec.states)
    server.push(
        frame(
            "chat.message",
            match_id=str(MATCH_ID),
            message=server.messages[0],
        )
    )
    settle(qtbot)
    assert len(rig.rec.states) == count and seqs(rig.rec.last) == [1]


def test_send_of_empty_text_or_without_a_chat_posts_nothing(
    qtbot: QtBot, make_rig: Callable[..., Rig], server: ChatServer
) -> None:
    rig = make_rig()

    rig.worker.send_message("hello")  # no chat yet
    open_chat(qtbot, rig)
    rig.worker.send_message("  ‎ \n ")
    settle(qtbot)

    assert server.send_bodies() == []
    assert rig.rec.last.pending == ()


def test_f3_a_failed_post_with_the_socket_up_does_not_stay_sending(
    qtbot: QtBot, make_rig: Callable[..., Rig], server: ChatServer
) -> None:
    rig = make_rig()
    open_chat(qtbot, rig)
    server.fail_sends = 1  # one 502 from a proxy; the WebSocket stays up

    rig.worker.send_message("hello")

    wait(qtbot, lambda: [p.status for p in rig.rec.last.pending] == ["failed"])
    failed_id = rig.rec.last.pending[0].client_message_id
    rig.worker.retry_message(failed_id)  # Retry reuses the id
    wait(qtbot, lambda: seqs(rig.rec.last) == [1])
    first, second = server.send_bodies()
    assert first == second and rig.rec.last.pending == ()


def test_a_resent_message_does_not_overtake_a_later_send(
    qtbot: QtBot, make_rig: Callable[..., Rig], server: ChatServer
) -> None:
    rig = make_rig()
    open_chat(qtbot, rig)
    server.drop_sockets()
    wait(qtbot, lambda: "offline" in rig.rec.connection)
    rig.worker.send_message("old")
    wait(qtbot, lambda: [p.status for p in rig.rec.last.pending] == ["sending"])
    connected = rig.rec.connection.count("connected")
    wait(qtbot, lambda: rig.rec.connection.count("connected") > connected)

    rig.worker.send_message("new")

    wait(qtbot, lambda: len(server.messages) == 2)
    assert [m["body"] for m in server.messages] == ["old", "new"]


def test_a_sync_without_a_match_shows_the_reason_the_chat_ended(
    qtbot: QtBot, make_rig: Callable[..., Rig], server: ChatServer
) -> None:
    rig = make_rig()
    open_chat(qtbot, rig)
    server.sync = {
        "queue": None,
        "match": None,
        "ended_reason": "disconnected",
        "ended_match_id": str(MATCH_ID),
    }

    server.drop_sockets()

    wait(qtbot, lambda: rig.rec.last.ended == "disconnected")


def test_a_reason_for_another_chat_is_not_applied(
    qtbot: QtBot, make_rig: Callable[..., Rig], server: ChatServer
) -> None:
    rig = make_rig()
    open_chat(qtbot, rig)
    server.sync = {
        "queue": None,
        "match": None,
        "ended_reason": "disconnected",
        "ended_match_id": str(OTHER),
    }

    server.drop_sockets()

    wait(qtbot, lambda: rig.rec.last.ended == "ended")


def test_a_chat_that_ended_while_offline_still_fetches_what_it_missed(
    qtbot: QtBot, make_rig: Callable[..., Rig], server: ChatServer
) -> None:
    rig = make_rig()
    open_chat(qtbot, rig)
    server.messages = [message_json(1), message_json(2)]
    server.sync = {"queue": None, "match": None}  # the chat ended meanwhile

    server.drop_sockets()

    wait(qtbot, lambda: rig.rec.last.ended == "ended")
    wait(qtbot, lambda: seqs(rig.rec.last) == [1, 2])


def test_a_pending_message_the_server_stored_shows_once_after_the_chat_ended(
    qtbot: QtBot, make_rig: Callable[..., Rig], server: ChatServer
) -> None:
    rig = make_rig()
    open_chat(qtbot, rig)
    server.drop_sockets()
    wait(qtbot, lambda: "offline" in rig.rec.connection)
    rig.worker.send_message("old")
    wait(qtbot, lambda: [p.status for p in rig.rec.last.pending] == ["sending"])
    cid = str(rig.rec.last.pending[0].client_message_id)
    # The server stored it before the chat ended; the reply never arrived.
    stored = message_json(1, mine=True, body="old", client_message_id=cid)
    server.messages = [stored]
    server.sent[cid] = stored
    server.sync = {"queue": None, "match": None}

    connected = rig.rec.connection.count("connected")
    wait(qtbot, lambda: rig.rec.connection.count("connected") > connected)

    wait(qtbot, lambda: rig.rec.last.ended == "ended")
    wait(qtbot, lambda: seqs(rig.rec.last) == [1])
    qtbot.wait(200)
    assert seqs(rig.rec.last) == [1]
    assert rig.rec.last.pending == ()


def _ended_then_other_device_matches(qtbot: QtBot, rig: Rig) -> None:
    open_chat(qtbot, rig)
    rig.server.push(frame("match.ended", match_id=str(MATCH_ID), reason="ended"))
    wait(qtbot, lambda: rig.rec.last.ended == "ended")
    rig.server.push(
        frame("match.created", entry_id=str(uuid4()), match=match_json(OTHER))
    )
    wait(qtbot, lambda: rig.rec.last.match_id == OTHER)
    assert rig.rec.last.ended is None  # the chat window now shows live chat B


def test_f1_end_in_the_chat_window_ends_the_chat_it_shows(
    qtbot: QtBot, make_rig: Callable[..., Rig], server: ChatServer
) -> None:
    rig = make_rig()
    _ended_then_other_device_matches(qtbot, rig)

    rig.worker.end_match()

    wait(qtbot, lambda: rig.rec.last.ended == "ended")
    ends = [c[1] for c in server.calls("POST", "/v1/matches/") if c[1].endswith("/end")]
    assert ends == [f"/v1/matches/{OTHER}/end"]


def test_f1_block_in_the_chat_window_blocks_the_peer_it_shows(
    qtbot: QtBot, make_rig: Callable[..., Rig], server: ChatServer
) -> None:
    rig = make_rig()
    _ended_then_other_device_matches(qtbot, rig)

    rig.worker.block_peer()

    wait(qtbot, lambda: rig.rec.last.ended == "ended")
    blocked = [c[1] for c in server.calls("POST", "/v1/matches/")]
    assert blocked == [f"/v1/matches/{OTHER}/block"]


def test_f1_block_from_an_ended_chat_window_after_ok_still_blocks(
    qtbot: QtBot, make_rig: Callable[..., Rig], server: ChatServer
) -> None:
    rig = make_rig()
    open_chat(qtbot, rig)
    server.push(frame("match.ended", match_id=str(MATCH_ID), reason="ended"))
    wait(qtbot, lambda: rig.rec.last.ended == "ended")
    rig.worker.dismiss_break()  # OK in the main window; chat window still open
    settle(qtbot)

    rig.worker.block_peer()
    wait(qtbot, lambda: bool(server.calls("POST", "/v1/matches/")))

    assert [c[1] for c in server.calls("POST", "/v1/matches/")] == [
        f"/v1/matches/{MATCH_ID}/block"
    ]


def test_end_from_an_ended_chat_window_posts_nothing(
    qtbot: QtBot, make_rig: Callable[..., Rig], server: ChatServer
) -> None:
    rig = make_rig()
    open_chat(qtbot, rig)
    server.push(frame("match.ended", match_id=str(MATCH_ID), reason="ended"))
    wait(qtbot, lambda: rig.rec.last.ended == "ended")

    rig.worker.end_match()
    settle(qtbot)

    assert server.calls("POST", "/v1/matches/") == []


def test_a_send_while_offline_waits_for_the_next_session(
    qtbot: QtBot, make_rig: Callable[..., Rig], server: ChatServer
) -> None:
    rig = make_rig()
    open_chat(qtbot, rig)
    server.drop_sockets()
    wait(qtbot, lambda: "offline" in rig.rec.connection)

    rig.worker.send_message("hello")
    wait(qtbot, lambda: len(server.send_bodies()) >= 1)
    wait(qtbot, lambda: seqs(rig.rec.last) == [1])

    assert len(server.messages) == 1 and rig.rec.last.pending == ()
    assert len({b["client_message_id"] for b in server.send_bodies()}) == 1


@pytest.mark.parametrize(
    ("status", "code"),
    [(409, "chat_ended"), (400, "invalid_request"), (429, "rate_limited")],
)
def test_an_http_error_marks_the_message_failed(
    qtbot: QtBot,
    make_rig: Callable[..., Rig],
    server: ChatServer,
    status: int,
    code: str,
) -> None:
    rig = make_rig()
    open_chat(qtbot, rig)
    server.send_error = (status, code)

    rig.worker.send_message("hello")

    wait(qtbot, lambda: [p.status for p in rig.rec.last.pending] == ["failed"])
    assert rig.rec.last.messages == ()
    # A failed message is not resent by a reconnect.
    server.drop_sockets()
    wait(
        qtbot,
        lambda: rig.rec.connection[-1:] == ["connected"] and server.ws_connects >= 2,
    )
    settle(qtbot)
    assert len(server.send_bodies()) == 1


def test_retry_message_reuses_the_id(
    qtbot: QtBot, make_rig: Callable[..., Rig], server: ChatServer
) -> None:
    rig = make_rig()
    open_chat(qtbot, rig)
    server.send_error = (429, "rate_limited")
    rig.worker.send_message("hello")
    wait(qtbot, lambda: [p.status for p in rig.rec.last.pending] == ["failed"])
    failed_id = rig.rec.last.pending[0].client_message_id
    server.send_error = None

    rig.worker.retry_message(failed_id)
    wait(qtbot, lambda: seqs(rig.rec.last) == [1])

    first, second = server.send_bodies()
    assert first["client_message_id"] == second["client_message_id"] == str(failed_id)
    assert rig.rec.last.pending == ()
    assert any(
        s.pending and s.pending[0].status == "sending" for s in rig.rec.states[2:]
    )


def test_retry_of_an_unknown_id_does_nothing(
    qtbot: QtBot, make_rig: Callable[..., Rig], server: ChatServer
) -> None:
    rig = make_rig()
    open_chat(qtbot, rig)

    rig.worker.retry_message(uuid4())
    settle(qtbot)

    assert server.send_bodies() == []


def test_pending_messages_fail_when_the_chat_ends(
    qtbot: QtBot, make_rig: Callable[..., Rig], server: ChatServer
) -> None:
    rig = make_rig()
    open_chat(qtbot, rig)
    server.drop_sockets()
    wait(qtbot, lambda: "offline" in rig.rec.connection)
    rig.worker.send_message("hello")
    wait(qtbot, lambda: [p.status for p in rig.rec.last.pending] == ["sending"])
    # The chat ended while we were away: the resend is refused or never made.
    server.send_error = (409, "chat_ended")
    server.sync = {"queue": None, "match": None}

    wait(qtbot, lambda: rig.rec.last.ended == "ended")

    assert [p.status for p in rig.rec.last.pending] == ["failed"]


# --- end, sync without the match ---


def test_match_ended_frame_ends_the_chat_and_keeps_it_until_closed(
    qtbot: QtBot, make_rig: Callable[..., Rig]
) -> None:
    rig = make_rig()
    open_chat(qtbot, rig)

    rig.server.push(frame("match.ended", match_id=str(MATCH_ID), reason="disconnected"))
    wait(qtbot, lambda: rig.rec.last.ended == "disconnected")

    assert rig.rec.last.match_id == MATCH_ID


def test_end_match_by_this_user_ends_the_chat(
    qtbot: QtBot, make_rig: Callable[..., Rig]
) -> None:
    rig = make_rig()
    open_chat(qtbot, rig)

    rig.worker.end_match()

    wait(qtbot, lambda: rig.rec.last.ended == "ended")


def test_a_sync_without_the_match_after_a_reconnect_ends_the_chat(
    qtbot: QtBot, make_rig: Callable[..., Rig], server: ChatServer
) -> None:
    rig = make_rig()
    open_chat(qtbot, rig)
    server.sync = {"queue": None, "match": None}

    server.drop_sockets()

    wait(qtbot, lambda: rig.rec.last.ended == "ended")


def test_a_sync_with_the_match_after_a_reconnect_keeps_the_chat_and_fetches_missed(
    qtbot: QtBot, make_rig: Callable[..., Rig], server: ChatServer
) -> None:
    rig = make_rig()
    open_chat(qtbot, rig)
    server.push(frame("chat.message", match_id=str(MATCH_ID), message=message_json(1)))
    wait(qtbot, lambda: seqs(rig.rec.last) == [1])
    server.messages = [message_json(n) for n in (1, 2, 3)]
    server.sync = {"queue": None, "match": chat_match_json(last_seq=3)}

    server.drop_sockets()

    wait(qtbot, lambda: seqs(rig.rec.last) == [1, 2, 3])
    assert rig.rec.last.ended is None
    assert server.list_queries() == [f"{MESSAGES}?after_seq=1"]


# --- extension, report, close ---


@pytest.mark.parametrize(
    ("call", "action"),
    [
        (lambda w: w.request_extension(), "request"),
        (lambda w: w.answer_extension(True), "accept"),
        (lambda w: w.answer_extension(False), "decline"),
    ],
)
def test_extension_posts_the_action_and_applies_the_returned_status(
    qtbot: QtBot,
    make_rig: Callable[..., Rig],
    server: ChatServer,
    call: Callable[[RemoteWorker], None],
    action: str,
) -> None:
    rig = make_rig()
    open_chat(qtbot, rig)
    new_end = _now() + timedelta(minutes=25)
    server.extension_status = chat_json(MATCH_ID, new_end)

    call(rig.worker)

    wait(
        qtbot,
        lambda: (
            rig.rec.last.status is not None and rig.rec.last.status.ends_at == new_end
        ),
    )
    (posted,) = server.calls("POST", f"/v1/matches/{MATCH_ID}/extension")
    assert json.loads(posted[3]) == {"action": action}
    assert posted[2] == "Bearer bra_1"


def test_extension_errors_surface_as_error_codes(
    qtbot: QtBot, make_rig: Callable[..., Rig], server: ChatServer
) -> None:
    rig = make_rig()
    open_chat(qtbot, rig)
    server.extension_error = (409, "no_extensions_left")
    before = rig.rec.last

    rig.worker.request_extension()

    wait(qtbot, lambda: rig.rec.errors == ["no_extensions_left"])
    assert rig.rec.last == before


def test_extension_without_a_live_chat_posts_nothing(
    qtbot: QtBot, make_rig: Callable[..., Rig], server: ChatServer
) -> None:
    rig = make_rig()

    rig.worker.request_extension()
    open_chat(qtbot, rig)
    server.push(frame("match.ended", match_id=str(MATCH_ID), reason="ended"))
    wait(qtbot, lambda: rig.rec.last.ended == "ended")
    rig.worker.answer_extension(True)
    settle(qtbot)

    assert server.calls("POST", f"/v1/matches/{MATCH_ID}/extension") == []


def test_report_posts_the_request_and_marks_the_chat_reported(
    qtbot: QtBot, make_rig: Callable[..., Rig], server: ChatServer
) -> None:
    rig = make_rig()
    open_chat(qtbot, rig)

    rig.worker.report_peer("harassment", "  rude ‮ ", (2, 5))
    wait(qtbot, lambda: rig.rec.last.reported)

    (posted,) = server.calls("POST", "/v1/reports")
    assert json.loads(posted[3]) == {
        "match_id": str(MATCH_ID),
        "reason": "harassment",
        "note": "rude",
        "message_seqs": [2, 5],
    }
    assert rig.rec.last.ended is None  # the match.ended frame ends it
    server.push(frame("match.ended", match_id=str(MATCH_ID), reason="ended"))
    wait(qtbot, lambda: rig.rec.last.ended == "ended")
    assert rig.rec.last.reported


def test_report_without_a_note_sends_null(
    qtbot: QtBot, make_rig: Callable[..., Rig], server: ChatServer
) -> None:
    rig = make_rig()
    open_chat(qtbot, rig)

    rig.worker.report_peer("spam", "   ", ())
    wait(qtbot, lambda: rig.rec.last.reported)

    (posted,) = server.calls("POST", "/v1/reports")
    assert json.loads(posted[3])["note"] is None


def test_report_is_allowed_on_an_ended_chat_but_not_after_close(
    qtbot: QtBot, make_rig: Callable[..., Rig], server: ChatServer
) -> None:
    rig = make_rig()
    open_chat(qtbot, rig)
    server.push(frame("match.ended", match_id=str(MATCH_ID), reason="ended"))
    wait(qtbot, lambda: rig.rec.last.ended == "ended")

    rig.worker.report_peer("other", None, ())
    wait(qtbot, lambda: rig.rec.last.reported)
    assert len(server.calls("POST", "/v1/reports")) == 1

    rig.worker.close_chat()
    wait(qtbot, lambda: rig.rec.last.match_id is None)
    rig.worker.report_peer("other", None, ())
    settle(qtbot)

    assert len(server.calls("POST", "/v1/reports")) == 1


@pytest.mark.parametrize(
    ("status", "code"), [(409, "already_reported"), (409, "report_window_closed")]
)
def test_report_errors_surface_as_error_codes(
    qtbot: QtBot,
    make_rig: Callable[..., Rig],
    server: ChatServer,
    status: int,
    code: str,
) -> None:
    rig = make_rig()
    open_chat(qtbot, rig)
    server.report_error = (status, code)

    rig.worker.report_peer("hate", None, ())

    wait(qtbot, lambda: rig.rec.errors == [code])
    assert not rig.rec.last.reported


def test_a_note_that_is_too_long_is_rejected_locally(
    qtbot: QtBot, make_rig: Callable[..., Rig], server: ChatServer
) -> None:
    rig = make_rig()
    open_chat(qtbot, rig)

    rig.worker.report_peer("other", "x" * 501, ())

    wait(qtbot, lambda: rig.rec.errors == ["invalid_request"])
    assert server.calls("POST", "/v1/reports") == []


def test_close_chat_is_a_no_op_while_live_and_forgets_an_ended_chat(
    qtbot: QtBot, make_rig: Callable[..., Rig], server: ChatServer
) -> None:
    rig = make_rig()
    open_chat(qtbot, rig)
    count = len(rig.rec.states)

    rig.worker.close_chat()
    settle(qtbot)
    assert len(rig.rec.states) == count and rig.rec.last.match_id == MATCH_ID

    server.push(frame("match.ended", match_id=str(MATCH_ID), reason="ended"))
    wait(qtbot, lambda: rig.rec.last.ended == "ended")
    rig.worker.close_chat()

    wait(qtbot, lambda: rig.rec.last.match_id is None)
    assert rig.rec.last.messages == () and rig.rec.last.pending == ()


# --- sign-out, privacy ---


def test_sign_out_clears_the_chat(
    qtbot: QtBot, make_rig: Callable[..., Rig], server: ChatServer
) -> None:
    rig = make_rig()
    open_chat(qtbot, rig)
    server.push(frame("chat.message", match_id=str(MATCH_ID), message=message_json(1)))
    wait(qtbot, lambda: seqs(rig.rec.last) == [1])

    rig.worker.sign_out()

    wait(qtbot, lambda: rig.rec.last.match_id is None)
    assert rig.rec.last.messages == ()
    assert rig.worker.chat_state.match_id is None


def test_no_message_body_or_nickname_reaches_the_logs(
    qtbot: QtBot,
    make_rig: Callable[..., Rig],
    server: ChatServer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="beebreak_companion")
    rig = make_rig()
    open_chat(qtbot, rig)
    server.fail_sends = 1
    rig.worker.send_message(BODY_CANARY)  # a 502 with the socket up: failed
    wait(qtbot, lambda: [p.status for p in rig.rec.last.pending] == ["failed"])
    rig.worker.retry_message(rig.rec.last.pending[0].client_message_id)
    wait(qtbot, lambda: seqs(rig.rec.last) == [1])
    server.messages = []
    server.report_error = (409, "already_reported")
    rig.worker.report_peer("other", BODY_CANARY, ())
    wait(qtbot, lambda: "already_reported" in rig.rec.errors)

    assert BODY_CANARY not in caplog.text
    assert PEER not in caplog.text

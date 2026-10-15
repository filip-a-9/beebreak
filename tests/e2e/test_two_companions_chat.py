"""Two real RemoteWorkers chatting through the live server (M5-08 item 4).

The server runs its real matchmaker loop (sweep interval 0.05 s) with the short
chat timings of the spec: `chat_disconnect_s` 1, `match_ttl_s` 3, `chat_grace_s`
1, `chat_extension_s` 2 (the extension cases use `match_ttl_s` 8 and an
extension of 4 or 6 s so that request and accept fit well inside the TTL).
Nothing calls `run_once()`. Every wait polls a condition
with a deadline.

Timing rules (rule 15): a loaded machine can only make things LATE, never
early. So "not before" bounds are tight (the server never acts before a
deadline), while "ends at" bounds are an order of magnitude wider than the
0.05 s sweep, and every wait deadline is 15 s.

Deviations from the spec timings, per case, are named where the config is built:
the 40-message and report cases use a long `match_ttl_s` (the chat must not
expire while messages are being exchanged), the restart and short-drop cases a
longer `chat_disconnect_s` of 5 (a restart plus two reconnects, or an outage plus
three POSTs and a reconnect, must fit in the hold even on a slow machine),
and the message rate limit is raised everywhere
(20 messages each way arrive in a burst at the default limit).

A "dropped socket" is a loopback TCP proxy between one worker and the server:
`drop()` aborts every open connection (WebSocket and HTTP) and refuses new
ones until `restore()`. The server sees a real connection loss; the worker sees
a real network failure and reconnects with its normal backoff.
"""

import asyncio
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from PySide6.QtCore import QObject, Slot
from pytestqt.qtbot import QtBot
from sqlalchemy import select

from breakroom_companion.break_state import BreakState
from breakroom_companion.chat_state import ChatState
from breakroom_companion.credentials import FileCredentialStore
from breakroom_companion.remote import RemoteWorker
from breakroom_companion.remote_config import RemoteConfig
from breakroom_server.models import Block, Match, Report, ReportMessage, User
from breakroom_shared.api import Consent
from breakroom_shared.presence import OutboundPresence
from tests.e2e_support.server import LiveServer

pytest_plugins = ["tests.e2e_support.server"]  # live_server fixtures

WAIT_MS = 15_000
ALL = Consent(
    version=1, share_task_category=True, share_stack_tags=True, share_time_bucket=True
)
# Spec timings; `match_ttl_s` and friends are overridden per case (see above).
BASE: dict[str, Any] = {
    "match_sweep_interval_s": 0.05,
    "chat_disconnect_s": 1,
    "match_ttl_s": 3,
    "chat_grace_s": 1,
    "chat_extension_s": 2,
    "rate_queue_user": 1000,
    "rate_message_user": 1000,  # bursts of 20+ messages must not hit the limit
}
LONG_TTL: dict[str, Any] = {"match_ttl_s": 120}
EXT_ACCEPT_S = 4
EXT_DECLINE_S = 6


class Recorder(QObject):
    def __init__(self, worker: RemoteWorker) -> None:
        super().__init__()
        self.states: list[str] = []
        self.breaks: list[BreakState] = []
        self.chats: list[ChatState] = []
        self.chat_times: list[float] = []  # time.time() of each chat state
        self.consent: Consent | None = None
        self.nickname: str | None = None
        worker.state_changed.connect(self.on_state)
        worker.break_changed.connect(self.on_break)
        worker.chat_changed.connect(self.on_chat)
        worker.me_changed.connect(self.on_me)

    @Slot(str)
    def on_state(self, state: str) -> None:
        self.states.append(state)

    @Slot(object)
    def on_break(self, state: object) -> None:
        assert isinstance(state, BreakState)
        self.breaks.append(state)

    @Slot(object)
    def on_chat(self, state: object) -> None:
        assert isinstance(state, ChatState)
        self.chats.append(state)
        self.chat_times.append(time.time())

    @Slot(object)
    def on_me(self, me: Any) -> None:
        if me is not None:
            self.nickname, self.consent = me.nickname, me.consent

    @property
    def phase(self) -> str:
        return self.breaks[-1].phase if self.breaks else "none"

    @property
    def chat(self) -> ChatState:
        return self.chats[-1] if self.chats else ChatState()

    def ended_at(self) -> float | None:
        """time.time() at which the chat first showed an end, if it did."""
        for state, when in zip(self.chats, self.chat_times, strict=True):
            if state.ended is not None:
                return when
        return None


class FlakyProxy:
    """A loopback TCP forwarder that can cut the line and refuse new calls."""

    def __init__(self, upstream_port: Callable[[], int]) -> None:
        self._upstream_port = upstream_port
        self._blocked = False
        self._writers: set[asyncio.StreamWriter] = set()
        self._loop = asyncio.new_event_loop()
        self._server: asyncio.Server | None = None
        self.port = 0
        ready = threading.Event()

        def run() -> None:
            asyncio.set_event_loop(self._loop)
            self._server = self._loop.run_until_complete(
                asyncio.start_server(self._handle, "127.0.0.1", 0)
            )
            self.port = self._server.sockets[0].getsockname()[1]
            ready.set()
            self._loop.run_forever()
            # stop() ended the loop: let every handler finish, then close the
            # loop so nothing outlives the test.
            pending = asyncio.all_tasks(self._loop)
            for task in pending:
                task.cancel()
            self._loop.run_until_complete(
                asyncio.gather(*pending, return_exceptions=True)
            )
            self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            self._loop.close()

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        assert ready.wait(5)

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        if self._blocked:
            writer.transport.abort()
            return
        try:
            up_reader, up_writer = await asyncio.open_connection(
                "127.0.0.1", self._upstream_port()
            )
        except OSError:
            writer.transport.abort()
            return
        self._writers |= {writer, up_writer}

        async def pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
            try:
                while data := await src.read(65536):
                    dst.write(data)
                    await dst.drain()
            except (OSError, asyncio.CancelledError):
                pass

        tasks = [
            asyncio.ensure_future(pipe(reader, up_writer)),
            asyncio.ensure_future(pipe(up_reader, writer)),
        ]
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in tasks:
            task.cancel()
        for w in (writer, up_writer):
            w.transport.abort()
            self._writers.discard(w)

    def _abort_all(self) -> None:
        for w in list(self._writers):
            w.transport.abort()

    def drop(self) -> None:
        self._blocked = True
        self._loop.call_soon_threadsafe(self._abort_all)

    def restore(self) -> None:
        self._blocked = False

    def stop(self) -> None:
        self._loop.call_soon_threadsafe(self._abort_all)
        if self._server is not None:
            self._loop.call_soon_threadsafe(self._server.close)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(10)
        assert not self._thread.is_alive()


@dataclass
class Peer:
    name: str
    worker: RemoteWorker
    rec: Recorder
    user_id: UUID
    proxy: FlakyProxy | None = None

    def presence(self) -> OutboundPresence:
        return OutboundPresence(
            schema_version=1,
            presence_id=uuid4(),
            task_category="backend",
            stack_tags=("python",),
            remaining_time_bucket="5_10m",
            status="available",
        )


Pair = tuple[LiveServer, Peer, Peer]
PairFactory = Callable[..., Pair]


@pytest.fixture
def make_pair(
    tmp_path: Path, qtbot: QtBot, server_factory: Callable[..., LiveServer]
) -> Iterator[PairFactory]:
    workers: list[RemoteWorker] = []
    proxies: list[FlakyProxy] = []

    def make_peer(server: LiveServer, name: str, *, proxied: bool) -> Peer:
        proxy: FlakyProxy | None = None
        url = server.base_url
        if proxied:
            proxy = FlakyProxy(lambda: server.port)
            proxies.append(proxy)
            url = f"http://127.0.0.1:{proxy.port}"
        config = RemoteConfig(
            server_url=url,
            http_timeout_s=2,
            ws_ping_interval_s=1.0,
            reconnect_min_s=0.05,
            reconnect_max_s=0.2,
        )
        store = FileCredentialStore(tmp_path / f"{name}.json", config.server_url)
        worker = RemoteWorker(config, store)
        workers.append(worker)
        rec = Recorder(worker)
        email = f"{name}@example.com"
        worker.start()
        qtbot.waitUntil(lambda: rec.states[-1:] == ["signed_out"], timeout=WAIT_MS)
        sent = len(server.mailer.sent)
        worker.begin_sign_in(email, server.invite(email))
        qtbot.waitUntil(lambda: len(server.mailer.sent) > sent, timeout=WAIT_MS)
        worker.verify(server.last_code())
        qtbot.waitUntil(lambda: rec.states[-1:] == ["connected"], timeout=WAIT_MS)
        worker.set_nickname(name)
        qtbot.waitUntil(lambda: rec.nickname == name, timeout=WAIT_MS)
        worker.set_consent(ALL)
        qtbot.waitUntil(lambda: rec.consent == ALL, timeout=WAIT_MS)
        with server.session_factory() as s:
            user_id = s.scalars(
                select(User.id).where(User.email_canonical == email)
            ).one()
        return Peer(name, worker, rec, user_id, proxy)

    def make(**overrides: Any) -> Pair:
        proxied = bool(overrides.pop("proxy_a", False))
        server = server_factory(**(BASE | overrides))
        ada = make_peer(server, "ada_l", proxied=proxied)
        bob = make_peer(server, "bob_k", proxied=False)
        ada.worker.join_queue(ada.presence())
        bob.worker.join_queue(bob.presence())
        qtbot.waitUntil(
            lambda: (
                ada.rec.chat.match_id is not None and bob.rec.chat.match_id is not None
            ),
            timeout=WAIT_MS,
        )
        assert ada.rec.chat.match_id == bob.rec.chat.match_id
        return server, ada, bob

    yield make
    for worker in workers:
        worker.stop()
    for proxy in proxies:
        proxy.stop()


def _bodies(chat: ChatState) -> list[tuple[int, str]]:
    return [(m.seq, m.body) for m in chat.messages]


def _await_counts(qtbot: QtBot, *want: tuple[Peer, int]) -> None:
    qtbot.waitUntil(
        lambda: all(len(p.rec.chat.messages) == n for p, n in want), timeout=WAIT_MS
    )


def _match(server: LiveServer, chat: ChatState) -> Match:
    with server.session_factory() as s:
        return s.scalars(select(Match).where(Match.id == chat.match_id)).one()


def test_forty_interleaved_messages_give_identical_transcripts(
    qtbot: QtBot, make_pair: PairFactory
) -> None:
    _server, ada, bob = make_pair(**LONG_TTL)
    for i in range(20):
        ada.worker.send_message(f"a-{i}")
        _await_counts(qtbot, (ada, 2 * i + 1), (bob, 2 * i + 1))
        bob.worker.send_message(f"b-{i}")
        _await_counts(qtbot, (ada, 2 * i + 2), (bob, 2 * i + 2))
    assert [m.seq for m in ada.rec.chat.messages] == list(range(1, 41))
    assert _bodies(ada.rec.chat) == _bodies(bob.rec.chat)
    # Strict alternation was enforced above, so order is fully determined.
    assert [b for _, b in _bodies(ada.rec.chat)] == [
        f"{who}-{i}" for i in range(20) for who in "ab"
    ]
    for a, b in zip(ada.rec.chat.messages, bob.rec.chat.messages, strict=True):
        assert a.mine != b.mine
    assert not ada.rec.chat.pending and not bob.rec.chat.pending
    assert ada.rec.chat.ended is None and bob.rec.chat.ended is None


def test_short_drop_of_a_keeps_the_chat_live_and_a_catches_up(
    qtbot: QtBot, make_pair: PairFactory
) -> None:
    # Hold of 5 s: the real outage is the 0.3 s sleep plus Bob's three POST
    # round trips plus Ada's reconnect (backoff <= 0.2 s), so a loaded machine
    # still has several seconds before the server could end the chat.
    server, ada, bob = make_pair(proxy_a=True, chat_disconnect_s=5, **LONG_TTL)
    assert ada.proxy is not None
    bob.worker.send_message("before")
    qtbot.waitUntil(lambda: len(ada.rec.chat.messages) == 1, timeout=WAIT_MS)
    marks = len(ada.rec.states)

    ada.proxy.drop()
    for i in range(3):
        bob.worker.send_message(f"during-{i}")
    qtbot.waitUntil(lambda: len(bob.rec.chat.messages) == 4, timeout=WAIT_MS)
    time.sleep(0.3)
    ada.proxy.restore()

    qtbot.waitUntil(
        lambda: (
            "offline" in ada.rec.states[marks:] and ada.rec.states[-1:] == ["connected"]
        ),
        timeout=WAIT_MS,
    )
    qtbot.waitUntil(lambda: len(ada.rec.chat.messages) == 4, timeout=WAIT_MS)
    assert _bodies(ada.rec.chat) == _bodies(bob.rec.chat)
    assert [m.seq for m in ada.rec.chat.messages] == [1, 2, 3, 4]  # no duplicates
    assert ada.rec.chat.ended is None and bob.rec.chat.ended is None
    assert _match(server, ada.rec.chat).status == "active"
    # And the chat still works both ways afterwards.
    ada.worker.send_message("back")
    qtbot.waitUntil(
        lambda: len(ada.rec.chat.messages) == 5 and len(bob.rec.chat.messages) == 5,
        timeout=WAIT_MS,
    )
    assert _bodies(ada.rec.chat) == _bodies(bob.rec.chat)


def _drop_a_past_the_hold(qtbot: QtBot, make_pair: PairFactory) -> Pair:
    server, ada, bob = make_pair(proxy_a=True, **LONG_TTL)
    assert ada.proxy is not None
    ada.worker.send_message("hello")
    qtbot.waitUntil(lambda: len(bob.rec.chat.messages) == 1, timeout=WAIT_MS)

    # Deterministic: Ada stays cut off until the server has ended the chat (Bob
    # was told `disconnected`), however slow the machine is; only then does she
    # come back and find the match gone.
    ada.proxy.drop()
    qtbot.waitUntil(lambda: bob.rec.chat.ended == "disconnected", timeout=WAIT_MS)
    ada.proxy.restore()

    qtbot.waitUntil(
        lambda: ada.rec.chat.ended is not None and bob.rec.chat.ended is not None,
        timeout=WAIT_MS,
    )
    return server, ada, bob


def test_drop_of_a_for_longer_than_the_hold_ends_the_chat_disconnected(
    qtbot: QtBot, make_pair: PairFactory
) -> None:
    server, ada, bob = _drop_a_past_the_hold(qtbot, make_pair)
    assert bob.rec.chat.ended == "disconnected"
    assert _bodies(ada.rec.chat) == _bodies(bob.rec.chat) == [(1, "hello")]
    row = _match(server, ada.rec.chat)
    assert (row.status, row.end_reason) == ("ended", "disconnected")


@pytest.mark.xfail(
    strict=True,
    reason=(
        "FINDING: A was offline when the server ended the chat, so it never got "
        "match.ended; its reconnect sync has no match and RemoteWorker reduces "
        "that to Ended(None, None) -> 'ended'. The wire protocol carries no end "
        "reason in a sync, so A cannot show 'The connection was lost.'"
    ),
)
def test_the_dropped_side_also_sees_disconnected(
    qtbot: QtBot, make_pair: PairFactory
) -> None:
    _server, ada, bob = _drop_a_past_the_hold(qtbot, make_pair)
    assert bob.rec.chat.ended == "disconnected"
    assert ada.rec.chat.ended == "disconnected"


def test_accepted_extension_moves_the_end_declined_one_does_not(
    qtbot: QtBot, make_pair: PairFactory
) -> None:
    # Accepted: the chat must outlive the original deadline and end after the
    # extended one. Server time never runs early, so the lower bounds are tight
    # (50 ms for clock rounding); the upper bounds are loose.
    # TTL 8 s from match creation leaves ample time for request and accept
    # (a few HTTP round trips); the extension adds 4 s.
    server, ada, bob = make_pair(match_ttl_s=8, chat_extension_s=EXT_ACCEPT_S)
    original = ada.rec.chat.status.ends_at if ada.rec.chat.status else None
    assert original is not None
    ada.worker.request_extension()
    qtbot.waitUntil(
        lambda: (
            bob.rec.chat.status is not None and bob.rec.chat.status.extension != "none"
        ),
        timeout=WAIT_MS,
    )
    bob.worker.answer_extension(True)
    qtbot.waitUntil(
        lambda: (
            ada.rec.chat.status is not None and ada.rec.chat.status.ends_at > original
        ),
        timeout=WAIT_MS,
    )
    assert ada.rec.chat.status is not None
    extended = ada.rec.chat.status.ends_at
    assert (extended - original).total_seconds() == pytest.approx(
        EXT_ACCEPT_S, abs=0.01
    )

    qtbot.waitUntil(
        lambda: ada.rec.chat.ended is not None and bob.rec.chat.ended is not None,
        timeout=WAIT_MS,
    )
    assert ada.rec.chat.ended == bob.rec.chat.ended == "expired"
    for peer in (ada, bob):
        end = peer.rec.ended_at()
        assert end is not None
        assert end >= extended.timestamp() - 0.05  # not before the extended deadline
        # in particular, not at the old one
        assert end >= original.timestamp() + EXT_ACCEPT_S - 0.1
    row = _match(server, ada.rec.chat)
    assert row.ended_at is not None and row.end_reason == "expired"
    assert (
        row.ended_at.replace(tzinfo=UTC) - extended
    ).total_seconds() < 5  # ended by the sweep, soon after the deadline


def test_declined_extension_ends_at_the_original_deadline(
    qtbot: QtBot, make_pair: PairFactory
) -> None:
    # TTL 8 s leaves ample time to request and decline; a 6 s extension makes
    # the "no extension applied" bound below wide (see there).
    server, ada, bob = make_pair(match_ttl_s=8, chat_extension_s=EXT_DECLINE_S)
    assert ada.rec.chat.status is not None
    original = ada.rec.chat.status.ends_at
    ada.worker.request_extension()
    qtbot.waitUntil(
        lambda: (
            bob.rec.chat.status is not None and bob.rec.chat.status.extension != "none"
        ),
        timeout=WAIT_MS,
    )
    bob.worker.answer_extension(False)
    qtbot.waitUntil(
        lambda: (
            ada.rec.chat.status is not None and ada.rec.chat.status.extension == "none"
        ),
        timeout=WAIT_MS,
    )
    assert ada.rec.chat.status.ends_at == original

    qtbot.waitUntil(
        lambda: ada.rec.chat.ended is not None and bob.rec.chat.ended is not None,
        timeout=WAIT_MS,
    )
    assert ada.rec.chat.ended == bob.rec.chat.ended == "expired"
    row = _match(server, ada.rec.chat)
    assert row.ended_at is not None and row.extensions_used == 0
    lateness = (row.ended_at.replace(tzinfo=UTC) - original).total_seconds()
    # Never early; an applied extension would add 6 s, so ending less than 5 s
    # after the deadline (100 sweeps of slack) shows none was applied.
    assert -0.05 <= lateness < EXT_DECLINE_S - 1


def test_server_restart_mid_chat_keeps_the_chat_and_transcript(
    qtbot: QtBot, make_pair: PairFactory
) -> None:
    # A longer hold here: restart plus two reconnects must fit in it on a slow
    # machine. The server restarts each offline clock on its first sweep.
    server, ada, bob = make_pair(chat_disconnect_s=5, **LONG_TTL)
    for i in range(3):
        ada.worker.send_message(f"a-{i}")
        bob.worker.send_message(f"b-{i}")
    qtbot.waitUntil(
        lambda: len(ada.rec.chat.messages) == 6 and len(bob.rec.chat.messages) == 6,
        timeout=WAIT_MS,
    )
    before = _bodies(ada.rec.chat)
    marks = {p.name: len(p.rec.states) for p in (ada, bob)}

    server.restart()

    for peer in (ada, bob):

        def reconnected(p: Peer = peer) -> bool:
            return "offline" in p.rec.states[marks[p.name] :] and (
                p.rec.states[-1:] == ["connected"]
            )

        qtbot.waitUntil(reconnected, timeout=WAIT_MS)
    # Messages still flow, which also proves the chat is live on the new server.
    ada.worker.send_message("after")
    qtbot.waitUntil(
        lambda: len(ada.rec.chat.messages) == 7 and len(bob.rec.chat.messages) == 7,
        timeout=WAIT_MS,
    )
    assert _bodies(ada.rec.chat)[:6] == before
    assert _bodies(ada.rec.chat) == _bodies(bob.rec.chat)
    assert [m.seq for m in ada.rec.chat.messages] == list(range(1, 8))
    assert ada.rec.chat.ended is None and bob.rec.chat.ended is None
    assert _match(server, ada.rec.chat).status == "active"


def test_report_with_one_ticked_message_ends_and_blocks(
    qtbot: QtBot, make_pair: PairFactory
) -> None:
    server, ada, bob = make_pair(**LONG_TTL)
    for i in range(3):
        ada.worker.send_message(f"a-{i}")
        bob.worker.send_message(f"b-{i}")
    qtbot.waitUntil(
        lambda: len(ada.rec.chat.messages) == 6 and len(bob.rec.chat.messages) == 6,
        timeout=WAIT_MS,
    )
    ticked = ada.rec.chat.messages[2]  # one message, whatever its author
    match_id = ada.rec.chat.match_id

    ada.worker.report_peer("harassment", "a short note", (ticked.seq,))

    qtbot.waitUntil(
        lambda: (
            ada.rec.chat.ended is not None
            and ada.rec.chat.reported
            and bob.rec.chat.ended is not None
        ),
        timeout=WAIT_MS,
    )
    assert bob.rec.chat.ended == "ended"  # a plain end: no hint of a report
    assert not bob.rec.chat.reported
    with server.session_factory() as s:
        match = s.scalars(select(Match).where(Match.id == match_id)).one()
        assert (match.status, match.end_reason) == ("ended", "reported")
        blocks = s.scalars(select(Block)).all()
        assert [(b.blocker_id, b.blocked_id) for b in blocks] == [
            (ada.user_id, bob.user_id)
        ]
        report = s.scalars(select(Report)).one()
        assert (report.reporter_id, report.reported_id) == (ada.user_id, bob.user_id)
        assert (report.match_id, report.reason, report.status) == (
            match_id,
            "harassment",
            "open",
        )
        evidence = s.scalars(select(ReportMessage)).all()
        assert [(e.seq, e.body) for e in evidence] == [(ticked.seq, ticked.body)]
        assert evidence[0].report_id == report.id

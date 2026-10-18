"""A message the server stored is never shown as "Not sent" (ended-chat ticket).

Two real RemoteWorkers against the real in-process server. Ada's connection runs
through a proxy that can HOLD the server's reply to her message POST (the WebSocket
is never held), so the server stores the message and ends the chat while Ada's
worker still waits for the reply. That is the closest the harness gets to "send
right as the chat ends". Every end path is tried: deadline, peer leaves, peer
blocks, and the server ending the chat while Ada is briefly offline (her reply
lost with the connection).

The assertion looks at EVERY state the worker emitted, not just the last one: the
bug was a transient `failed` entry for a stored message.
"""

import asyncio
from collections.abc import Callable

import pytest
from pytestqt.qtbot import QtBot
from sqlalchemy import select

from beebreak_server.models import Message
from tests.e2e import test_two_companions_chat as base
from tests.e2e.test_two_companions_chat import (
    LONG_TTL,
    WAIT_MS,
    FlakyProxy,
    PairFactory,
    Peer,
    make_pair,  # noqa: F401  (fixture)
)
from tests.e2e_support.server import LiveServer

pytest_plugins = ["tests.e2e_support.server"]


class HoldProxy(FlakyProxy):
    """FlakyProxy that can also hold the downstream bytes of POST connections."""

    holding = False

    def __init__(self, upstream_port: Callable[[], int]) -> None:
        self._release = None  # set on the proxy loop in _handle
        self._held: list[asyncio.Event] = []
        super().__init__(upstream_port)

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
        gate = asyncio.Event()
        gate.set()
        self._held.append(gate)

        async def up(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
            try:
                while data := await src.read(65536):
                    if self.holding and data.startswith(b"POST "):
                        gate.clear()
                    dst.write(data)
                    await dst.drain()
            except (OSError, asyncio.CancelledError):
                pass

        async def down(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
            try:
                while data := await src.read(65536):
                    await gate.wait()
                    dst.write(data)
                    await dst.drain()
            except (OSError, asyncio.CancelledError):
                pass

        tasks = [
            asyncio.ensure_future(up(reader, up_writer)),
            asyncio.ensure_future(down(up_reader, writer)),
        ]
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in tasks:
            task.cancel()
        for w in (writer, up_writer):
            w.transport.abort()
            self._writers.discard(w)

    def hold(self) -> None:
        self.holding = True

    def release(self) -> None:
        self.holding = False

        def open_all() -> None:
            for gate in self._held:
                gate.set()

        self._loop.call_soon_threadsafe(open_all)


@pytest.fixture(autouse=True)
def _hold_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(base, "FlakyProxy", HoldProxy)


def _stored(server: LiveServer, peer: Peer) -> list[int]:
    with server.session_factory() as s:
        return list(
            s.scalars(
                select(Message.seq).where(Message.author_id == peer.user_id)
            ).all()
        )


def _no_stored_message_ever_failed(server: LiveServer, ada: Peer) -> None:
    """No state of the ended chat shows `failed` for a message the server stored,
    and every seq appears once."""
    assert _stored(server, ada), "the server must have stored the message"
    # A POST that fails while the socket still looks up shows `failed` (Retry) at
    # once, even if the server stored it; that is before the end and by design.
    # What must not happen is "Not sent" once the chat has ended.
    for state in (c for c in ada.rec.chats if c.ended is not None):
        assert not [p for p in state.pending if p.status == "failed"], (
            "a stored message was shown as not sent"
        )
        seqs = [m.seq for m in state.messages]
        assert len(seqs) == len(set(seqs))


def _send_held(qtbot: QtBot, server: LiveServer, ada: Peer) -> HoldProxy:
    proxy = ada.proxy
    assert isinstance(proxy, HoldProxy)
    proxy.hold()
    ada.worker.send_message("last words")
    qtbot.waitUntil(lambda: bool(_stored(server, ada)), timeout=WAIT_MS)
    return proxy


def _finish(qtbot: QtBot, server: LiveServer, ada: Peer, proxy: HoldProxy) -> None:
    proxy.release()
    qtbot.waitUntil(lambda: not ada.rec.chat.pending, timeout=WAIT_MS)
    assert [m.seq for m in ada.rec.chat.messages] == [1]
    _no_stored_message_ever_failed(server, ada)


def test_peer_leaves_while_the_reply_is_held(
    qtbot: QtBot,
    make_pair: PairFactory,  # noqa: F811
) -> None:
    server, ada, bob = make_pair(proxy_a=True, **LONG_TTL)
    proxy = _send_held(qtbot, server, ada)
    bob.worker.end_match()
    qtbot.waitUntil(lambda: ada.rec.chat.ended is not None, timeout=WAIT_MS)
    _no_stored_message_ever_failed(server, ada)
    _finish(qtbot, server, ada, proxy)


def test_peer_blocks_while_the_reply_is_held(
    qtbot: QtBot,
    make_pair: PairFactory,  # noqa: F811
) -> None:
    server, ada, bob = make_pair(proxy_a=True, **LONG_TTL)
    proxy = _send_held(qtbot, server, ada)
    bob.worker.block_peer()
    qtbot.waitUntil(lambda: ada.rec.chat.ended is not None, timeout=WAIT_MS)
    _no_stored_message_ever_failed(server, ada)
    _finish(qtbot, server, ada, proxy)


def test_deadline_reached_while_the_reply_is_held(
    qtbot: QtBot,
    make_pair: PairFactory,  # noqa: F811
) -> None:
    server, ada, _bob = make_pair(proxy_a=True, match_ttl_s=3)
    proxy = _send_held(qtbot, server, ada)
    qtbot.waitUntil(lambda: ada.rec.chat.ended == "expired", timeout=WAIT_MS)
    _no_stored_message_ever_failed(server, ada)
    _finish(qtbot, server, ada, proxy)


def test_server_ends_the_chat_while_the_sender_is_offline(
    qtbot: QtBot,
    make_pair: PairFactory,  # noqa: F811
) -> None:
    server, ada, bob = make_pair(proxy_a=True, **LONG_TTL)
    proxy = _send_held(qtbot, server, ada)
    proxy.drop()  # the stored message's reply is lost with the connection
    qtbot.waitUntil(lambda: bob.rec.chat.ended == "disconnected", timeout=WAIT_MS)
    proxy.restore()
    qtbot.waitUntil(lambda: ada.rec.chat.ended is not None, timeout=WAIT_MS)
    qtbot.waitUntil(lambda: not ada.rec.chat.pending, timeout=WAIT_MS)
    assert [m.seq for m in ada.rec.chat.messages] == [1]
    _no_stored_message_ever_failed(server, ada)


def test_unstored_message_at_the_end_is_still_not_sent(
    qtbot: QtBot,
    make_pair: PairFactory,  # noqa: F811
) -> None:
    """The other side of the rule: a message the server never stored must still
    end up `failed` (the user may want to copy it)."""
    server, ada, bob = make_pair(proxy_a=True, **LONG_TTL)
    proxy = ada.proxy
    assert isinstance(proxy, HoldProxy)
    proxy.drop()
    qtbot.waitUntil(lambda: bob.rec.chat.ended == "disconnected", timeout=WAIT_MS)
    ada.worker.send_message("never arrives")  # offline: stays pending
    qtbot.waitUntil(lambda: bool(ada.rec.chat.pending), timeout=WAIT_MS)
    proxy.restore()
    qtbot.waitUntil(lambda: ada.rec.chat.ended is not None, timeout=WAIT_MS)
    qtbot.waitUntil(
        lambda: [p.status for p in ada.rec.chat.pending] == ["failed"],
        timeout=WAIT_MS,
    )
    assert not _stored(server, ada)
    assert not ada.rec.chat.messages

"""RemoteWorker chat flows against the real server and matchmaker (M5-05 part B).

Two workers sign in, match and chat through the real HTTP routes and
WebSocket fan-out. A device "loses its network" by aborting the TCP transport
of its realtime socket (no close frame, like a cable pull) and then waits
`reconnect_s` before it dials again. That is reliable: it needs no server
restart (which would drop both users and reset the matchmaker), and the hold
is exact because the worker's backoff is pinned to `reconnect_s`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

import pytest
from PySide6.QtCore import Slot
from pytestqt.qtbot import QtBot
from sqlalchemy import select

from breakroom_companion.chat_state import ChatState
from breakroom_companion.credentials import FileCredentialStore
from breakroom_companion.remote import RemoteWorker
from breakroom_companion.remote_config import RemoteConfig
from breakroom_server.models import Message
from tests.unit.test_remote_worker_live import (
    FAST,
    PRESENCE,
    Client,
    Rec,
    phase_is,
    sign_in,
    wait,
)

if TYPE_CHECKING:
    from tests.e2e_support.server import LiveServer

pytest_plugins = ["tests.e2e_support.server"]  # live_server fixtures

CHAT = {**FAST, "chat_disconnect_s": 1}


class ChatRec(Rec):
    def __init__(self, worker: RemoteWorker) -> None:
        super().__init__(worker)
        self.chats: list[ChatState] = []
        worker.chat_changed.connect(self.on_chat)

    @Slot(object)
    def on_chat(self, state: object) -> None:
        assert isinstance(state, ChatState)
        self.chats.append(state)

    @property
    def chat(self) -> ChatState:
        return self.chats[-1] if self.chats else ChatState()


class ChatClient(Client):
    rec: ChatRec

    def __init__(
        self, worker: RemoteWorker, rec: ChatRec, store: FileCredentialStore
    ) -> None:
        super().__init__(worker, rec, store)
        self.rec = rec


@pytest.fixture
def make_chat_client(tmp_path: Path) -> Iterator[Callable[..., ChatClient]]:
    made: list[RemoteWorker] = []

    def make(server: LiveServer, name: str, *, reconnect_s: float = 0.1) -> ChatClient:
        config = RemoteConfig(
            server_url=server.base_url,
            http_timeout_s=2,
            ws_ping_interval_s=1.0,
            reconnect_min_s=reconnect_s,
            reconnect_max_s=reconnect_s,
        )
        store = FileCredentialStore(tmp_path / f"{name}.json", server.base_url)
        worker = RemoteWorker(config, store)
        made.append(worker)
        return ChatClient(worker, ChatRec(worker), store)

    yield make
    for worker in made:
        worker.stop()


def start_chat(
    qtbot: QtBot,
    server_factory: Callable[..., LiveServer],
    make: Callable[..., ChatClient],
    *,
    a_reconnect_s: float = 0.1,
    **config: object,
) -> tuple[LiveServer, ChatClient, ChatClient]:
    server = server_factory(**(CHAT | config))
    ada = make(server, "a", reconnect_s=a_reconnect_s)
    bob = make(server, "b")
    sign_in(qtbot, server, ada, "ada@example.com", "ada_l")
    sign_in(qtbot, server, bob, "bob@example.com", "bobby")
    ada.worker.join_queue(PRESENCE)
    bob.worker.join_queue(PRESENCE)
    wait(qtbot, phase_is(ada, "matched"))
    wait(qtbot, phase_is(bob, "matched"))
    wait(qtbot, lambda: ada.rec.chat.match_id is not None)
    wait(qtbot, lambda: bob.rec.chat.match_id is not None)
    return server, ada, bob


def drop_socket(client: ChatClient) -> None:
    """Abort the TCP connection of the worker's realtime socket."""
    worker = client.worker
    loop, rt = worker._loop, worker._rt
    assert loop is not None and rt is not None and rt._ws is not None
    loop.call_soon_threadsafe(rt._ws.transport.abort)


def bodies(client: ChatClient) -> list[str]:
    return [m.body for m in client.rec.chat.messages]


def stored(server: LiveServer, match_id: UUID | None) -> list[Message]:
    with server.session_factory() as session:
        return list(
            session.scalars(select(Message).where(Message.match_id == match_id))
        )


def test_two_workers_exchange_messages_both_ways_with_the_right_mine(
    qtbot: QtBot,
    server_factory: Callable[..., LiveServer],
    make_chat_client: Callable[..., ChatClient],
) -> None:
    _, ada, bob = start_chat(qtbot, server_factory, make_chat_client)

    ada.worker.send_message("hello bob")
    wait(qtbot, lambda: bodies(bob) == ["hello bob"] and bodies(ada) == ["hello bob"])
    bob.worker.send_message("hi ada")
    wait(qtbot, lambda: bodies(ada) == ["hello bob", "hi ada"])
    wait(qtbot, lambda: bodies(bob) == ["hello bob", "hi ada"])
    ada.worker.send_message("how are you")
    wait(qtbot, lambda: len(bob.rec.chat.messages) == 3)
    wait(qtbot, lambda: len(ada.rec.chat.messages) == 3)

    assert [(m.seq, m.mine) for m in ada.rec.chat.messages] == [
        (1, True),
        (2, False),
        (3, True),
    ]
    assert [(m.seq, m.mine) for m in bob.rec.chat.messages] == [
        (1, False),
        (2, True),
        (3, False),
    ]
    assert ada.rec.chat.pending == () and bob.rec.chat.pending == ()
    assert ada.rec.chat.peer_nickname == "bobby"
    assert bob.rec.chat.peer_nickname == "ada_l"
    assert ada.rec.chat.ended is None and bob.rec.chat.ended is None


def test_a_short_drop_loses_nothing_and_keeps_the_chat_live(
    qtbot: QtBot,
    server_factory: Callable[..., LiveServer],
    make_chat_client: Callable[..., ChatClient],
) -> None:
    server, ada, bob = start_chat(
        qtbot, server_factory, make_chat_client, a_reconnect_s=0.3
    )
    drop_socket(ada)
    wait(qtbot, lambda: "offline" in ada.rec.connection)

    for text in ("one", "two", "three"):
        bob.worker.send_message(text)
        wait(qtbot, lambda text=text: text in bodies(bob))  # type: ignore[misc]
    wait(qtbot, lambda: bodies(ada) == ["one", "two", "three"])
    wait(qtbot, lambda: ada.rec.connection[-1:] == ["connected"])

    chat = ada.rec.chat
    assert [m.seq for m in chat.messages] == [1, 2, 3]
    assert all(not m.mine for m in chat.messages)
    assert chat.ended is None and bob.rec.chat.ended is None
    assert len(stored(server, chat.match_id)) == 3


def test_a_long_drop_ends_the_chat_for_both(
    qtbot: QtBot,
    server_factory: Callable[..., LiveServer],
    make_chat_client: Callable[..., ChatClient],
) -> None:
    _, ada, bob = start_chat(qtbot, server_factory, make_chat_client, a_reconnect_s=2.0)

    drop_socket(ada)

    # Bob is told why. Ada is offline for the whole hold, so she only learns
    # that the chat is gone from the `sync` after reconnecting, which carries
    # no reason: her state says "ended".
    wait(qtbot, lambda: bob.rec.chat.ended == "disconnected")
    wait(qtbot, lambda: ada.rec.chat.ended is not None)
    assert ada.rec.chat.ended in ("disconnected", "ended")
    assert bob.rec.chat.match_id == ada.rec.chat.match_id


def test_an_extension_requested_by_one_and_accepted_by_the_other(
    qtbot: QtBot,
    server_factory: Callable[..., LiveServer],
    make_chat_client: Callable[..., ChatClient],
) -> None:
    _, ada, bob = start_chat(qtbot, server_factory, make_chat_client)
    before = ada.rec.chat.status
    assert before is not None

    ada.worker.request_extension()
    wait(
        qtbot,
        lambda: (
            ada.rec.chat.status is not None
            and ada.rec.chat.status.extension == "requested_by_me"
        ),
    )
    wait(
        qtbot,
        lambda: (
            bob.rec.chat.status is not None
            and bob.rec.chat.status.extension == "requested_by_peer"
        ),
    )
    bob.worker.answer_extension(True)

    new_end = before.ends_at + timedelta(seconds=before.extension_s)
    for client in (ada, bob):
        wait(
            qtbot,
            lambda client=client: (  # type: ignore[misc]
                client.rec.chat.status is not None
                and client.rec.chat.status.ends_at == new_end
            ),
        )
    for client in (ada, bob):
        status = client.rec.chat.status
        assert status is not None
        assert status.extension == "none" and status.extensions_left == 1
        assert client.rec.chat.ended is None


def test_a_report_ends_the_chat_and_the_peer_only_sees_ended(
    qtbot: QtBot,
    server_factory: Callable[..., LiveServer],
    make_chat_client: Callable[..., ChatClient],
) -> None:
    _, ada, bob = start_chat(qtbot, server_factory, make_chat_client)
    ada.worker.send_message("rude words")
    wait(qtbot, lambda: len(bob.rec.chat.messages) == 1)

    ada.worker.report_peer("harassment", "he was rude", (1,))

    wait(qtbot, lambda: ada.rec.chat.reported and ada.rec.chat.ended is not None)
    wait(qtbot, lambda: bob.rec.chat.ended is not None)
    assert ada.rec.chat.ended == "ended"
    assert bob.rec.chat.ended == "ended"
    assert not bob.rec.chat.reported


def test_a_message_sent_offline_is_resent_with_the_same_id_exactly_once(
    qtbot: QtBot,
    server_factory: Callable[..., LiveServer],
    make_chat_client: Callable[..., ChatClient],
) -> None:
    server, ada, bob = start_chat(
        qtbot, server_factory, make_chat_client, a_reconnect_s=0.4
    )
    drop_socket(ada)
    wait(qtbot, lambda: "offline" in ada.rec.connection)

    ada.worker.send_message("sent in the dark")
    wait(qtbot, lambda: [p.status for p in ada.rec.chat.pending] == ["sending"])
    pending_id = ada.rec.chat.pending[0].client_message_id
    assert stored(server, ada.rec.chat.match_id) == []  # nothing left the device

    wait(qtbot, lambda: bodies(bob) == ["sent in the dark"])
    wait(qtbot, lambda: bodies(ada) == ["sent in the dark"])

    assert ada.rec.chat.pending == ()
    assert [m.mine for m in ada.rec.chat.messages] == [True]
    assert ada.rec.chat.messages[0].client_message_id == pending_id
    assert [m.mine for m in bob.rec.chat.messages] == [False]
    (row,) = stored(server, ada.rec.chat.match_id)
    assert row.client_message_id == pending_id
    assert ada.rec.chat.ended is None

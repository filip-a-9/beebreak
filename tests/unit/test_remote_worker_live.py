"""RemoteWorker break flows against the real server and matchmaker loop (M4-05 B)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

import pytest
from PySide6.QtCore import QObject, Slot
from pytestqt.qtbot import QtBot
from sqlalchemy import func, select

from breakroom_companion.api_client import ApiClient
from breakroom_companion.break_state import BreakPhase, BreakState
from breakroom_companion.credentials import FileCredentialStore
from breakroom_companion.remote import RemoteWorker
from breakroom_companion.remote_config import RemoteConfig
from breakroom_server.models import QueueEntry
from breakroom_shared.api import Consent, QueueJoinRequest
from breakroom_shared.presence import OutboundPresence

if TYPE_CHECKING:
    from tests.e2e_support.server import LiveServer

pytest_plugins = ["tests.e2e_support.server"]  # live_server fixtures

WAIT_MS = 15_000
FAST = {"match_sweep_interval_s": 0.05}
PRESENCE = OutboundPresence(
    schema_version=1, presence_id=UUID(int=5), status="available"
)


class Rec(QObject):
    def __init__(self, worker: RemoteWorker) -> None:
        super().__init__()
        self.states: list[BreakState] = []
        self.connection: list[str] = []
        self.nickname: str | None = None
        self.consented = False
        worker.break_changed.connect(self.on_break)
        worker.state_changed.connect(self.on_state)
        worker.me_changed.connect(self.on_me)

    @Slot(object)
    def on_break(self, state: object) -> None:
        assert isinstance(state, BreakState)
        self.states.append(state)

    @Slot(str)
    def on_state(self, state: str) -> None:
        self.connection.append(state)

    @Slot(object)
    def on_me(self, me: Any) -> None:
        if me is not None:
            self.nickname = me.nickname
            self.consented = me.consent is not None

    @property
    def last(self) -> BreakState:
        return self.states[-1] if self.states else BreakState()


class Client:
    def __init__(self, worker: RemoteWorker, rec: Rec, store: FileCredentialStore):
        self.worker, self.rec, self.store = worker, rec, store


@pytest.fixture
def make_client(tmp_path: Path) -> Iterator[Callable[..., Client]]:
    made: list[RemoteWorker] = []

    def make(
        server: LiveServer, name: str, store: FileCredentialStore | None = None
    ) -> Client:
        config = RemoteConfig(
            server_url=server.base_url,
            http_timeout_s=2,
            ws_ping_interval_s=1.0,
            reconnect_min_s=0.05,
            reconnect_max_s=0.2,
        )
        store = store or FileCredentialStore(tmp_path / f"{name}.json", server.base_url)
        worker = RemoteWorker(config, store)
        made.append(worker)
        return Client(worker, Rec(worker), store)

    yield make
    for worker in made:
        worker.stop()


def wait(qtbot: QtBot, condition: Callable[[], bool]) -> None:
    qtbot.waitUntil(condition, timeout=WAIT_MS)


def phase_is(client: Client, phase: BreakPhase) -> Callable[[], bool]:
    return lambda: client.rec.last.phase == phase


def sign_in(
    qtbot: QtBot, server: LiveServer, client: Client, email: str, nickname: str
) -> None:
    client.worker.start()
    wait(qtbot, lambda: client.rec.connection[-1:] == ["signed_out"])
    sent = len(server.mailer.sent)
    client.worker.begin_sign_in(email, server.invite(email))
    wait(qtbot, lambda: len(server.mailer.sent) > sent)
    client.worker.verify(server.last_code())
    wait(qtbot, lambda: client.rec.connection[-1:] == ["connected"])
    client.worker.set_nickname(nickname)
    client.worker.set_consent(Consent(version=1))
    wait(qtbot, lambda: client.rec.nickname == nickname and client.rec.consented)


def entry_rows(server: LiveServer, status: str | None = None) -> int:
    with server.session_factory() as session:
        stmt = select(func.count()).select_from(QueueEntry)
        if status is not None:
            stmt = stmt.where(QueueEntry.status == status)
        return session.scalar(stmt) or 0


def test_two_workers_match_each_other_and_ending_ends_both(
    qtbot: QtBot,
    server_factory: Callable[..., LiveServer],
    make_client: Callable[..., Client],
) -> None:
    server = server_factory(**FAST)
    ada, bob = make_client(server, "a"), make_client(server, "b")
    sign_in(qtbot, server, ada, "ada@example.com", "ada_l")
    sign_in(qtbot, server, bob, "bob@example.com", "bobby")

    ada.worker.join_queue(PRESENCE)
    bob.worker.join_queue(PRESENCE)
    wait(qtbot, phase_is(ada, "matched"))
    wait(qtbot, phase_is(bob, "matched"))

    assert ada.rec.last.match is not None and bob.rec.last.match is not None
    assert ada.rec.last.match.peer_nickname == "bobby"
    assert bob.rec.last.match.peer_nickname == "ada_l"
    assert ada.rec.last.match.match_id == bob.rec.last.match.match_id

    ada.worker.end_match()
    wait(qtbot, phase_is(ada, "ended"))
    wait(qtbot, phase_is(bob, "ended"))


def test_blocking_ends_the_match_for_both(
    qtbot: QtBot,
    server_factory: Callable[..., LiveServer],
    make_client: Callable[..., Client],
) -> None:
    server = server_factory(**FAST)
    ada, bob = make_client(server, "a"), make_client(server, "b")
    sign_in(qtbot, server, ada, "ada@example.com", "ada_l")
    sign_in(qtbot, server, bob, "bob@example.com", "bobby")
    ada.worker.join_queue(PRESENCE)
    bob.worker.join_queue(PRESENCE)
    wait(qtbot, phase_is(ada, "matched"))
    wait(qtbot, phase_is(bob, "matched"))

    bob.worker.block_peer()

    wait(qtbot, phase_is(bob, "ended"))
    wait(qtbot, phase_is(ada, "ended"))


def test_a_lone_worker_gets_nobody_available_after_the_ttl(
    qtbot: QtBot,
    server_factory: Callable[..., LiveServer],
    make_client: Callable[..., Client],
) -> None:
    server = server_factory(queue_ttl_s=1, **FAST)
    ada = make_client(server, "a")
    sign_in(qtbot, server, ada, "ada@example.com", "ada_l")

    ada.worker.join_queue(PRESENCE)
    wait(qtbot, phase_is(ada, "searching"))
    wait(qtbot, phase_is(ada, "nobody_available"))

    assert [s.phase for s in ada.rec.states] == [
        "joining",
        "searching",
        "nobody_available",
    ]
    ada.worker.dismiss_break()
    wait(qtbot, phase_is(ada, "none"))


def test_a_restarted_worker_resumes_the_same_search(
    qtbot: QtBot,
    server_factory: Callable[..., LiveServer],
    make_client: Callable[..., Client],
) -> None:
    server = server_factory(**FAST)
    first = make_client(server, "a")
    sign_in(qtbot, server, first, "ada@example.com", "ada_l")
    first.worker.join_queue(PRESENCE)
    wait(qtbot, phase_is(first, "searching"))
    entry = first.rec.last.entry
    assert entry is not None
    first.worker.stop()

    second = make_client(server, "a2", store=first.store)
    second.worker.start()
    wait(qtbot, phase_is(second, "searching"))

    assert second.rec.states[0].phase == "searching"  # the first sync
    assert second.rec.last.entry is not None
    assert second.rec.last.entry.entry_id == entry.entry_id
    assert entry_rows(server) == 1
    assert entry_rows(server, "queued") == 1


def test_a_401_during_join_refreshes_and_retries_with_the_same_key(
    qtbot: QtBot,
    server_factory: Callable[..., LiveServer],
    make_client: Callable[..., Client],
) -> None:
    server = server_factory(**FAST)
    ada = make_client(server, "a")
    sign_in(qtbot, server, ada, "ada@example.com", "ada_l")
    keys: list[UUID] = []
    api = ada.worker._api
    assert isinstance(api, ApiClient)
    real_join = api.join_queue

    async def recording_join(token: str, request: QueueJoinRequest) -> Any:
        keys.append(request.idempotency_key)
        return await real_join(token, request)

    api.join_queue = recording_join  # type: ignore[method-assign, assignment]
    ada.worker._access = "bra_stale"  # the server will answer 401

    ada.worker.join_queue(PRESENCE)
    wait(qtbot, phase_is(ada, "searching"))

    assert len(keys) == 2 and keys[0] == keys[1]
    assert entry_rows(server) == 1
    assert [s.phase for s in ada.rec.states] == ["joining", "searching"]

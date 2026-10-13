"""Two real RemoteWorkers against the live server (M4-08 item 5).

The server runs its real matchmaker loop (sweep interval 0.05 s); nothing calls
`run_once()`. Every wait is bounded. The database is checked after each round,
because the guarantee is "no user in two active matches", not "the UI looked
right".
"""

from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from PySide6.QtCore import QObject, Slot
from pytestqt.qtbot import QtBot
from sqlalchemy import delete, func, select

from breakroom_companion.break_state import BreakState
from breakroom_companion.credentials import FileCredentialStore
from breakroom_companion.remote import RemoteWorker
from breakroom_companion.remote_config import RemoteConfig
from breakroom_server.models import (
    Block,
    Match,
    MatchParticipant,
    QueueEntry,
    User,
)
from breakroom_shared.api import Consent
from breakroom_shared.presence import OutboundPresence
from tests.e2e_support.server import LiveServer

pytest_plugins = ["tests.e2e_support.server"]  # live_server fixtures

WAIT_MS = 15_000
ROUNDS = 20
ALL = Consent(
    version=1, share_task_category=True, share_stack_tags=True, share_time_bucket=True
)


class Recorder(QObject):
    def __init__(self, worker: RemoteWorker) -> None:
        super().__init__()
        self.states: list[str] = []
        self.breaks: list[BreakState] = []
        self.consent: Consent | None = None
        self.nickname: str | None = None
        worker.state_changed.connect(self.on_state)
        worker.break_changed.connect(self.on_break)
        worker.me_changed.connect(self.on_me)

    @Slot(str)
    def on_state(self, state: str) -> None:
        self.states.append(state)

    @Slot(object)
    def on_break(self, state: object) -> None:
        assert isinstance(state, BreakState)
        self.breaks.append(state)

    @Slot(object)
    def on_me(self, me: Any) -> None:
        if me is not None:
            self.nickname, self.consent = me.nickname, me.consent

    @property
    def phase(self) -> str:
        return self.breaks[-1].phase if self.breaks else "none"

    @property
    def last(self) -> BreakState:
        return self.breaks[-1] if self.breaks else BreakState()


@dataclass
class Peer:
    name: str
    worker: RemoteWorker
    rec: Recorder
    user_id: UUID

    def presence(self, *tags: Any) -> OutboundPresence:
        return OutboundPresence(
            schema_version=1,
            presence_id=uuid4(),
            task_category="backend",
            stack_tags=tags,
            remaining_time_bucket="5_10m",
            status="available",
        )


@pytest.fixture
def server(server_factory: Callable[..., LiveServer]) -> LiveServer:
    return server_factory(match_sweep_interval_s=0.05, rate_queue_user=1000)


@pytest.fixture
def make_peer(
    tmp_path: Path, qtbot: QtBot, server: LiveServer
) -> Iterator[Callable[[str], Peer]]:
    made: list[RemoteWorker] = []

    def make(name: str) -> Peer:
        config = RemoteConfig(
            server_url=server.base_url,
            http_timeout_s=2,
            ws_ping_interval_s=1.0,
            reconnect_min_s=0.05,
            reconnect_max_s=0.2,
        )
        store = FileCredentialStore(tmp_path / f"{name}.json", config.server_url)
        worker = RemoteWorker(config, store)
        made.append(worker)
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
        return Peer(name, worker, rec, user_id)

    yield make
    for worker in made:
        worker.stop()


@pytest.fixture
def pair(
    server: LiveServer, make_peer: Callable[[str], Peer]
) -> tuple[LiveServer, Peer, Peer]:
    return server, make_peer("ada_l"), make_peer("bob_k")


def _db_invariants(server: LiveServer, *, matches: int) -> None:
    with server.session_factory() as s:
        parts = s.scalars(select(MatchParticipant)).all()
        active = Counter(p.user_id for p in parts if p.active)
        assert all(n == 1 for n in active.values())
        per_match = Counter(p.match_id for p in parts)
        assert len(per_match) == matches
        assert set(per_match.values()) <= {2}
        assert s.scalar(select(func.count()).select_from(Match)) == matches
        entries = s.scalars(select(QueueEntry)).all()
        queued = Counter(e.user_id for e in entries if e.status == "queued")
        assert all(n == 1 for n in queued.values())
        matched = Counter(e.match_id for e in entries if e.status == "matched")
        assert set(matched.values()) <= {2}


def test_two_workers_match_once(
    qtbot: QtBot, pair: tuple[LiveServer, Peer, Peer]
) -> None:
    server, ada, bob = pair
    ada.worker.join_queue(ada.presence("python", "docker"))
    bob.worker.join_queue(bob.presence("python"))

    qtbot.waitUntil(
        lambda: ada.rec.phase == "matched" and bob.rec.phase == "matched",
        timeout=WAIT_MS,
    )
    a, b = ada.rec.last.match, bob.rec.last.match
    assert a is not None and b is not None
    assert a.match_id == b.match_id
    assert (a.peer_nickname, b.peer_nickname) == ("bob_k", "ada_l")
    assert a.shared_tags == b.shared_tags == ("python",)
    for peer in (ada, bob):  # each side was told exactly once
        assert [s.phase for s in peer.rec.breaks].count("matched") == 1
    _db_invariants(server, matches=1)
    with server.session_factory() as s:
        assert not s.scalars(
            select(QueueEntry).where(QueueEntry.status == "queued")
        ).all()


def test_twenty_simultaneous_rounds_make_exactly_one_match_each(
    qtbot: QtBot, pair: tuple[LiveServer, Peer, Peer]
) -> None:
    server, ada, bob = pair
    for round_no in range(1, ROUNDS + 1):
        ada.worker.join_queue(ada.presence("python"))
        bob.worker.join_queue(bob.presence("python"))
        qtbot.waitUntil(
            lambda: ada.rec.phase == "matched" and bob.rec.phase == "matched",
            timeout=WAIT_MS,
        )
        _db_invariants(server, matches=round_no)
        with server.session_factory() as s:
            active = s.scalars(
                select(MatchParticipant).where(MatchParticipant.active.is_(True))
            ).all()
            assert {p.user_id for p in active} == {ada.user_id, bob.user_id}
            assert len({p.match_id for p in active}) == 1

        ada.worker.end_match()
        qtbot.waitUntil(
            lambda: ada.rec.phase == "ended" and bob.rec.phase == "ended",
            timeout=WAIT_MS,
        )
        ada.worker.dismiss_break()
        bob.worker.dismiss_break()
        qtbot.waitUntil(
            lambda: ada.rec.phase == "none" and bob.rec.phase == "none",
            timeout=WAIT_MS,
        )
        with server.session_factory() as s:
            assert not s.scalars(
                select(MatchParticipant).where(MatchParticipant.active.is_(True))
            ).all()


def test_server_restart_mid_search_never_makes_two_matches(
    qtbot: QtBot, pair: tuple[LiveServer, Peer, Peer]
) -> None:
    server, ada, bob = pair
    # A block keeps the pair apart, so both are certainly still searching when
    # the server restarts.
    with server.session_factory() as s:
        s.add(
            Block(
                blocker_id=ada.user_id,
                blocked_id=bob.user_id,
                created_at=datetime.now(UTC),
            )
        )
        s.commit()
    ada.worker.join_queue(ada.presence())
    bob.worker.join_queue(bob.presence())
    qtbot.waitUntil(
        lambda: ada.rec.phase == "searching" and bob.rec.phase == "searching",
        timeout=WAIT_MS,
    )
    before = {p.name: p.rec.last.entry for p in (ada, bob)}
    marks = {p.name: len(p.rec.states) for p in (ada, bob)}

    server.restart()

    for peer in (ada, bob):

        def reconnected(p: Peer = peer) -> bool:
            return "offline" in p.rec.states[marks[p.name] :] and (
                p.rec.states[-1:] == ["connected"]
            )

        qtbot.waitUntil(reconnected, timeout=WAIT_MS)
    for peer in (ada, bob):
        # The sync after reconnect shows the same single search, no match.
        assert peer.rec.phase == "searching"
        assert peer.rec.last.entry == before[peer.name]
        assert peer.rec.last.match is None
    with server.session_factory() as s:
        queued = s.scalars(
            select(QueueEntry).where(QueueEntry.status == "queued")
        ).all()
        assert Counter(e.user_id for e in queued) == {ada.user_id: 1, bob.user_id: 1}
        assert s.scalar(select(func.count()).select_from(Match)) == 0

    # Lift the block: the restarted server's matchmaker pairs them, once.
    with server.session_factory() as s:
        s.execute(delete(Block))
        s.commit()
    qtbot.waitUntil(
        lambda: ada.rec.phase == "matched" and bob.rec.phase == "matched",
        timeout=WAIT_MS,
    )
    _db_invariants(server, matches=1)
    for peer in (ada, bob):
        assert [s.phase for s in peer.rec.breaks].count("matched") == 1

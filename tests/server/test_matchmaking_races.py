"""M4-03 races: the database, not the in-process lock, keeps matches unique.

The threaded tests swap the matchmaker's private pass lock for a no-op and run
passes at the same time (PostgreSQL; set BREAKROOM_TEST_DATABASE_URL). The
deterministic tests replay the same interleavings by hand and run on SQLite too.
"""

import threading
from collections import Counter
from contextlib import nullcontext
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select, update

from breakroom_server.config import ServerConfig
from breakroom_server.db import SessionFactory
from breakroom_server.matchmaking import Matchmaker, _Candidate
from breakroom_server.models import Match, MatchParticipant, QueueEntry
from tests.server.conftest import (
    START_TIME,
    FixedClock,
    SeededUser,
    add_ready_user,
    requires_postgres,
    seed_queued,
)
from tests.server.fakes import RecordingNotifier

NAMES = ["Ada", "Bob", "Cyd", "Dia", "Eve", "Fay", "Gus", "Hal", "Ivy", "Jon"]


def _matchmaker(
    factory: SessionFactory,
    config: ServerConfig,
    clock: FixedClock,
    notifier: RecordingNotifier,
    *,
    lock: bool,
) -> Matchmaker:
    mm = Matchmaker(factory, config, clock, notifier)
    if not lock:
        mm._pass_lock = nullcontext()
    return mm


def _users(
    factory: SessionFactory, notifier: RecordingNotifier, n: int
) -> list[SeededUser]:
    users = [add_ready_user(factory, NAMES[i]) for i in range(n)]
    notifier.connected.update(u.user_id for u in users)
    return users


def _assert_consistent(factory: SessionFactory) -> None:
    with factory() as s:
        participants = s.scalars(select(MatchParticipant)).all()
        active_per_user = Counter(p.user_id for p in participants if p.active)
        assert all(count == 1 for count in active_per_user.values())
        per_match: dict[object, list[object]] = {}
        for p in participants:
            per_match.setdefault(p.match_id, []).append(p.user_id)
        matches = s.scalars(select(Match)).all()
        assert {m.id for m in matches} == set(per_match)
        for users in per_match.values():
            assert len(users) == 2 and len(set(users)) == 2
        entries = s.scalars(select(QueueEntry)).all()
        matched = [e for e in entries if e.status == "matched"]
        for e in entries:
            assert (e.match_id is not None) == (e.status == "matched")
            if e.match_id is not None:
                assert e.user_id in per_match[e.match_id]
        # Each user has at most one matched entry per match.
        assert len({(e.user_id, e.match_id) for e in matched}) == len(matched)
        # Two entries per match, one per participant.
        for match_id, users in per_match.items():
            assert sorted(e.user_id for e in matched if e.match_id == match_id) == (
                sorted(users)  # type: ignore[type-var]
            )


def _run_concurrently(passes: list[Matchmaker]) -> None:
    barrier = threading.Barrier(len(passes))
    errors: list[BaseException] = []

    def work(mm: Matchmaker) -> None:
        try:
            barrier.wait(timeout=10)
            mm.run_once()
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(mm,)) for mm in passes]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors, errors


# --- threaded, lock bypassed (PostgreSQL) ---------------------------------------


@requires_postgres
@pytest.mark.parametrize("n_users", [2, 3, 5, 10])
def test_concurrent_passes_never_double_match(
    session_factory: SessionFactory,
    server_config: ServerConfig,
    clock: FixedClock,
    n_users: int,
) -> None:
    notifier = RecordingNotifier()
    users = _users(session_factory, notifier, n_users)
    for i, user in enumerate(users):
        seed_queued(session_factory, user, now=START_TIME + timedelta(seconds=i))
    clock.advance(30)
    passes = [
        _matchmaker(session_factory, server_config, clock, notifier, lock=False)
        for _ in range(2)
    ]

    _run_concurrently(passes)
    _assert_consistent(session_factory)

    # A later pass places whoever the first two skipped: n // 2 matches in all.
    passes[0].run_once()
    _assert_consistent(session_factory)
    with session_factory() as s:
        assert len(s.scalars(select(Match)).all()) == n_users // 2


@requires_postgres
def test_concurrent_passes_repeated(
    session_factory: SessionFactory,
    server_config: ServerConfig,
    clock: FixedClock,
) -> None:
    """Many short rounds, to give the interleavings a chance to differ."""
    notifier = RecordingNotifier()
    users = _users(session_factory, notifier, 10)
    for _ in range(5):
        for user in users:
            seed_queued(session_factory, user, now=clock.now, ttl_s=10_000)
        passes = [
            _matchmaker(session_factory, server_config, clock, notifier, lock=False)
            for _ in range(3)
        ]
        _run_concurrently(passes)
        _assert_consistent(session_factory)
        # End every match so the same users can queue again.
        clock.advance(server_config.match_ttl_s)
        passes[0].run_once()
        _assert_consistent(session_factory)


@requires_postgres
def test_cancel_racing_a_pass(
    session_factory: SessionFactory,
    server_config: ServerConfig,
    clock: FixedClock,
) -> None:
    notifier = RecordingNotifier()
    for round_no in range(8):
        a, b, c = (
            add_ready_user(session_factory, f"{name}{round_no}")
            for name in ("Aa", "Bb", "Cc")
        )
        notifier.connected.update({a.user_id, b.user_id, c.user_id})
        seed_queued(session_factory, a, now=clock.now, ttl_s=10_000)
        victim = seed_queued(session_factory, b, now=clock.now, ttl_s=10_000)
        seed_queued(session_factory, c, now=clock.now, ttl_s=10_000)
        mm = _matchmaker(session_factory, server_config, clock, notifier, lock=False)
        barrier = threading.Barrier(2)

        def cancel(
            entry_id: object = victim, barrier: threading.Barrier = barrier
        ) -> None:
            barrier.wait(timeout=10)
            with session_factory() as s:
                s.execute(
                    update(QueueEntry)
                    .where(QueueEntry.id == entry_id, QueueEntry.status == "queued")
                    .values(status="cancelled", ended_at=clock.now)
                )
                s.commit()

        thread = threading.Thread(target=cancel)
        thread.start()
        barrier.wait(timeout=10)
        mm.run_once()
        thread.join(timeout=30)
        with session_factory() as s:
            entry = s.get_one(QueueEntry, victim)
            assert entry.status in ("cancelled", "matched")
            assert (entry.status == "matched") == (entry.match_id is not None)
        _assert_consistent(session_factory)


# --- deterministic interleavings (SQLite and PostgreSQL) ------------------------


def _snapshot(mm: Matchmaker, now: datetime) -> list[_Candidate]:
    candidates, _blocked, _recent = mm._load_candidates(now)
    return candidates


def test_stale_snapshot_cannot_match_twice(
    session_factory: SessionFactory,
    server_config: ServerConfig,
    clock: FixedClock,
) -> None:
    """Two passes plan the same pair from the same snapshot: only one wins."""
    notifier = RecordingNotifier()
    ada, bob = _users(session_factory, notifier, 2)
    seed_queued(session_factory, ada, now=clock.now)
    seed_queued(session_factory, bob, now=clock.now)
    one = _matchmaker(session_factory, server_config, clock, notifier, lock=False)
    two = _matchmaker(session_factory, server_config, clock, notifier, lock=False)
    first, second = _snapshot(one, clock.now), _snapshot(two, clock.now)

    assert one._create_match(clock.now, first[0], first[1]) is not None
    assert two._create_match(clock.now, second[0], second[1]) is None

    _assert_consistent(session_factory)
    with session_factory() as s:
        assert len(s.scalars(select(Match)).all()) == 1


def test_overlapping_pairs_from_stale_snapshots(
    session_factory: SessionFactory,
    server_config: ServerConfig,
    clock: FixedClock,
) -> None:
    """(A,B) and (A,C) planned apart: A is in one match only, C stays queued."""
    notifier = RecordingNotifier()
    users = _users(session_factory, notifier, 3)
    for i, user in enumerate(users):
        seed_queued(session_factory, user, now=START_TIME + timedelta(seconds=i))
    clock.advance(10)
    mm = _matchmaker(session_factory, server_config, clock, notifier, lock=False)
    a, b, c = _snapshot(mm, clock.now)

    assert mm._create_match(clock.now, a, b) is not None
    assert mm._create_match(clock.now, a, c) is None
    assert mm._create_match(clock.now, c, b) is None

    _assert_consistent(session_factory)
    with session_factory() as s:
        statuses = {e.user_id: e.status for e in s.scalars(select(QueueEntry))}
    assert statuses[c.user_id] == "queued"


def test_user_with_active_match_is_rejected_by_the_index(
    session_factory: SessionFactory,
    server_config: ServerConfig,
    clock: FixedClock,
) -> None:
    """A fresh entry for a user whose other entry matched meanwhile: no second match."""
    notifier = RecordingNotifier()
    ada, bob, cyd = _users(session_factory, notifier, 3)
    seed_queued(session_factory, ada, now=clock.now)
    seed_queued(session_factory, bob, now=clock.now)
    mm = _matchmaker(session_factory, server_config, clock, notifier, lock=False)
    a, b = _snapshot(mm, clock.now)
    assert mm._create_match(clock.now, a, b) is not None
    # Ada queues again while still matched (the queue API forbids it; the
    # matchmaker must not rely on that) and Cyd joins.
    seed_queued(session_factory, ada, now=clock.now)
    seed_queued(session_factory, cyd, now=clock.now)
    again = _snapshot(mm, clock.now)
    ada_entry = next(c for c in again if c.user_id == ada.user_id)
    cyd_entry = next(c for c in again if c.user_id == cyd.user_id)

    assert mm._create_match(clock.now, ada_entry, cyd_entry) is None

    _assert_consistent(session_factory)
    with session_factory() as s:
        assert len(s.scalars(select(Match)).all()) == 1


def test_cancel_after_snapshot_is_never_matched(
    session_factory: SessionFactory,
    server_config: ServerConfig,
    clock: FixedClock,
) -> None:
    notifier = RecordingNotifier()
    ada, bob = _users(session_factory, notifier, 2)
    seed_queued(session_factory, ada, now=clock.now)
    victim = seed_queued(session_factory, bob, now=clock.now)
    mm = _matchmaker(session_factory, server_config, clock, notifier, lock=False)
    a, b = _snapshot(mm, clock.now)
    with session_factory() as s:  # the cancel lands between plan and claim
        s.execute(
            update(QueueEntry)
            .where(QueueEntry.id == victim)
            .values(status="cancelled", ended_at=clock.now)
        )
        s.commit()

    assert mm._create_match(clock.now, a, b) is None

    with session_factory() as s:
        assert s.get_one(QueueEntry, victim).status == "cancelled"
        assert s.get_one(QueueEntry, victim).match_id is None
        assert s.scalars(select(Match)).all() == []
        assert s.scalars(select(MatchParticipant)).all() == []
    _assert_consistent(session_factory)


def test_entry_expired_after_snapshot_is_never_matched(
    session_factory: SessionFactory,
    server_config: ServerConfig,
    clock: FixedClock,
) -> None:
    notifier = RecordingNotifier()
    ada, bob = _users(session_factory, notifier, 2)
    seed_queued(session_factory, ada, now=clock.now, ttl_s=90)
    seed_queued(session_factory, bob, now=clock.now, ttl_s=10)
    mm = _matchmaker(session_factory, server_config, clock, notifier, lock=False)
    a, b = _snapshot(mm, clock.now)
    clock.advance(10)
    assert mm._create_match(clock.now, a, b) is None
    with session_factory() as s:
        assert s.scalars(select(Match)).all() == []


def test_sequential_unlocked_passes_are_consistent(
    session_factory: SessionFactory,
    server_config: ServerConfig,
    clock: FixedClock,
) -> None:
    notifier = RecordingNotifier()
    users = _users(session_factory, notifier, 7)
    for user in users:
        seed_queued(session_factory, user, now=clock.now)
    passes = [
        _matchmaker(session_factory, server_config, clock, notifier, lock=False)
        for _ in range(3)
    ]
    for mm in passes:
        mm.run_once()
    _assert_consistent(session_factory)
    with session_factory() as s:
        assert len(s.scalars(select(Match)).all()) == 3

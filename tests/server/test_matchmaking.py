"""M4-03: the matchmaker, driven through run_once() with a fake clock (rule 15)."""

import dataclasses
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from beebreak_server.app import create_app
from beebreak_server.config import ServerConfig
from beebreak_server.db import SessionFactory
from beebreak_server.mailer import RecordingMailer
from beebreak_server.matchmaking import MatchCreated, Matchmaker, _Candidate
from beebreak_server.models import Block, Match, MatchParticipant, QueueEntry, User
from beebreak_shared.api import WsMatchCreated, WsMatchEnded, WsQueueEnded
from beebreak_shared.presence import RemainingTimeBucket, StackTag
from tests.server.conftest import (
    START_TIME,
    FixedClock,
    SeededUser,
    add_ready_user,
    seed_match,
    seed_queued,
)
from tests.server.fakes import RecordingNotifier


@dataclasses.dataclass
class Rig:
    factory: SessionFactory
    config: ServerConfig
    clock: FixedClock
    notifier: RecordingNotifier
    mm: Matchmaker

    def user(self, nickname: str, *, status: str = "active") -> SeededUser:
        user = add_ready_user(self.factory, nickname, status=status)
        self.notifier.connected.add(user.user_id)
        return user

    def queue(
        self,
        user: SeededUser,
        *,
        at: datetime | None = None,
        tags: tuple[StackTag, ...] = (),
        bucket: RemainingTimeBucket | None = None,
        category: str | None = None,
        ttl_s: int = 90,
    ) -> uuid.UUID:
        return seed_queued(
            self.factory,
            user,
            now=at or self.clock.now,
            ttl_s=ttl_s,
            stack_tags=tags,
            bucket=bucket,
            task_category=category,  # type: ignore[arg-type]
        )

    def entry(self, entry_id: uuid.UUID) -> QueueEntry:
        with self.factory() as s:
            return s.get_one(QueueEntry, entry_id)

    def block(self, blocker: SeededUser, blocked: SeededUser) -> None:
        with self.factory() as s:
            s.add(
                Block(
                    blocker_id=blocker.user_id,
                    blocked_id=blocked.user_id,
                    created_at=self.clock.now,
                )
            )
            s.commit()

    def partner_of(self, user: SeededUser) -> uuid.UUID | None:
        """The user in the same match as `user`, from the database."""
        with self.factory() as s:
            mids = s.scalars(
                select(MatchParticipant.match_id).where(
                    MatchParticipant.user_id == user.user_id
                )
            ).all()
            peers = s.scalars(
                select(MatchParticipant.user_id).where(
                    MatchParticipant.match_id.in_(mids),
                    MatchParticipant.user_id != user.user_id,
                )
            ).all()
            return peers[-1] if peers else None


@pytest.fixture
def rig(
    session_factory: SessionFactory, server_config: ServerConfig, clock: FixedClock
) -> Rig:
    notifier = RecordingNotifier()
    return Rig(
        session_factory,
        server_config,
        clock,
        notifier,
        Matchmaker(session_factory, server_config, clock, notifier),
    )


def test_two_connected_users_match_once(rig: Rig) -> None:
    ada, bob = rig.user("Ada"), rig.user("Bob")
    e_ada = rig.queue(ada, tags=("python",), bucket="5_10m", category="backend")
    e_bob = rig.queue(bob, tags=("python",), bucket="5_10m", category="backend")

    (created,) = rig.mm.run_once()

    assert isinstance(created, MatchCreated)
    assert set(created.users) == {ada.user_id, bob.user_id}
    for entry_id in (e_ada, e_bob):
        entry = rig.entry(entry_id)
        assert entry.status == "matched"
        assert entry.match_id == created.match_id
        assert entry.ended_at == rig.clock.now
        assert entry.presence_id is None
        assert entry.task_category is None
        assert entry.stack_tags is None
        assert entry.remaining_time_bucket is None
    for me, peer, entry_id in ((ada, bob, e_ada), (bob, ada, e_bob)):
        (frame,) = rig.notifier.frames_for(me.user_id)
        assert isinstance(frame, WsMatchCreated)
        assert frame.entry_id == entry_id
        assert frame.match.peer_nickname == peer.nickname
        assert frame.match.match_id == created.match_id
        assert frame.server_time == rig.clock.now

    assert rig.mm.run_once() == []
    assert len(rig.notifier.sent) == 2


def test_match_ends_at_ttl(rig: Rig) -> None:
    ada, bob = rig.user("Ada"), rig.user("Bob")
    rig.queue(ada)
    rig.queue(bob)
    (created,) = rig.mm.run_once()
    with rig.factory() as s:
        match = s.get_one(Match, created.match_id)
        assert match.status == "active"
        assert match.ends_at == START_TIME + timedelta(seconds=rig.config.match_ttl_s)


def test_three_users_one_match_one_waits(rig: Rig) -> None:
    users = [rig.user(n) for n in ("Ada", "Bob", "Cyd")]
    ids = [
        rig.queue(u, at=START_TIME + timedelta(seconds=i)) for i, u in enumerate(users)
    ]
    rig.clock.advance(5)
    assert len(rig.mm.run_once()) == 1
    statuses = [rig.entry(i).status for i in ids]
    assert sorted(statuses) == ["matched", "matched", "queued"]
    assert statuses[2] == "queued"  # the newest waits


def test_four_users_two_matches(rig: Rig) -> None:
    for n in ("Ada", "Bob", "Cyd", "Dia"):
        rig.queue(rig.user(n))
    created = rig.mm.run_once()
    assert len(created) == 2
    assert len({u for c in created for u in c.users}) == 4


def test_disconnected_user_skipped_then_expires(rig: Rig) -> None:
    ada, bob = rig.user("Ada"), rig.user("Bob")
    rig.queue(ada)
    e_bob = rig.queue(bob)
    rig.notifier.connected.discard(bob.user_id)

    assert rig.mm.run_once() == []
    assert rig.entry(e_bob).status == "queued"

    rig.clock.advance(rig.config.queue_ttl_s)
    assert rig.mm.run_once() == []
    assert rig.entry(e_bob).status == "expired"


def test_reconnecting_before_expiry_matches(rig: Rig) -> None:
    ada, bob = rig.user("Ada"), rig.user("Bob")
    rig.queue(ada)
    rig.queue(bob)
    rig.notifier.connected.discard(bob.user_id)
    assert rig.mm.run_once() == []
    rig.clock.advance(30)
    rig.notifier.connected.add(bob.user_id)
    assert len(rig.mm.run_once()) == 1


@pytest.mark.parametrize("direction", ["a_blocks_b", "b_blocks_a"])
def test_block_prevents_pair_in_either_direction(rig: Rig, direction: str) -> None:
    ada, bob = rig.user("Ada"), rig.user("Bob")
    ea, eb = rig.queue(ada), rig.queue(bob)
    if direction == "a_blocks_b":
        rig.block(ada, bob)
    else:
        rig.block(bob, ada)
    assert rig.mm.run_once() == []
    assert rig.entry(ea).status == rig.entry(eb).status == "queued"


def _plan_candidate(rig: Rig, user: SeededUser, entry_id: uuid.UUID) -> _Candidate:
    """The candidate as a plan read before the claim would have seen it."""
    entry = rig.entry(entry_id)
    return _Candidate(
        entry_id=entry_id,
        user_id=user.user_id,
        admitted_at=entry.admitted_at,
        category=None,
        tags=frozenset(),
        has_tags=False,
        bucket=None,
    )


def _match_count(rig: Rig) -> int:
    with rig.factory() as s:
        return s.scalar(select(func.count()).select_from(Match)) or 0


@pytest.mark.parametrize("direction", ["a_blocks_b", "b_blocks_a"])
def test_block_committed_after_the_plan_stops_the_claim(
    rig: Rig, direction: str
) -> None:
    ada, bob = rig.user("Ada"), rig.user("Bob")
    ea, eb = rig.queue(ada), rig.queue(bob)
    first, second = _plan_candidate(rig, ada, ea), _plan_candidate(rig, bob, eb)
    if direction == "a_blocks_b":
        rig.block(ada, bob)
    else:
        rig.block(bob, ada)

    assert rig.mm._create_match(rig.clock.now, first, second) is None

    assert _match_count(rig) == 0
    assert rig.entry(ea).status == rig.entry(eb).status == "queued"


def test_user_disabled_after_the_plan_stops_the_claim(rig: Rig) -> None:
    ada, bob = rig.user("Ada"), rig.user("Bob")
    ea, eb = rig.queue(ada), rig.queue(bob)
    first, second = _plan_candidate(rig, ada, ea), _plan_candidate(rig, bob, eb)
    with rig.factory() as s:
        s.get_one(User, bob.user_id).status = "disabled"
        s.commit()

    assert rig.mm._create_match(rig.clock.now, first, second) is None

    assert _match_count(rig) == 0
    assert rig.entry(ea).status == rig.entry(eb).status == "queued"


def test_blocked_users_each_match_the_third(rig: Rig) -> None:
    ada, bob, cy = rig.user("Ada"), rig.user("Bob"), rig.user("Cyd")
    rig.block(ada, bob)
    rig.queue(ada, at=START_TIME, ttl_s=5000)
    rig.queue(bob, at=START_TIME + timedelta(seconds=1), ttl_s=5000)
    rig.clock.advance(5)
    rig.queue(cy, ttl_s=5000)

    (first,) = rig.mm.run_once()
    assert set(first.users) == {ada.user_id, cy.user_id}  # oldest picks first
    # The first match ends; Cy searches again and Bob (still waiting) gets Cy.
    rig.clock.advance(rig.config.match_ttl_s)
    assert rig.mm.run_once() == []
    rig.queue(cy, ttl_s=5000)
    (second,) = rig.mm.run_once()
    assert set(second.users) == {bob.user_id, cy.user_id}


def test_rematch_within_penalty_loses_to_fresh_partner(rig: Rig) -> None:
    ada, bob, cy = rig.user("Ada"), rig.user("Bob"), rig.user("Cyd")
    seed_match(rig.factory, ada, bob, now=START_TIME - timedelta(hours=1))
    with rig.factory() as s:
        for m in s.scalars(select(MatchParticipant)):
            m.active = False
        s.commit()
    rig.queue(ada, at=START_TIME)
    rig.queue(bob, at=START_TIME + timedelta(seconds=1))
    rig.queue(cy, at=START_TIME + timedelta(seconds=2))
    rig.clock.advance(5)
    (created,) = rig.mm.run_once()
    assert set(created.users) == {ada.user_id, cy.user_id}


def test_rematch_wins_if_alone_and_ages_out(rig: Rig) -> None:
    ada, bob = rig.user("Ada"), rig.user("Bob")
    seed_match(rig.factory, ada, bob, now=START_TIME - timedelta(hours=1))
    with rig.factory() as s:
        for m in s.scalars(select(MatchParticipant)):
            m.active = False
        s.commit()
    rig.queue(ada)
    rig.queue(bob)
    assert len(rig.mm.run_once()) == 1


def test_old_match_beyond_penalty_does_not_rank_last(rig: Rig) -> None:
    ada, bob, cy = rig.user("Ada"), rig.user("Bob"), rig.user("Cyd")
    long_ago = START_TIME - timedelta(seconds=rig.config.rematch_penalty_s + 60)
    seed_match(rig.factory, ada, bob, now=long_ago)
    with rig.factory() as s:
        for m in s.scalars(select(MatchParticipant)):
            m.active = False
        s.commit()
    rig.queue(ada, at=START_TIME)
    rig.queue(bob, at=START_TIME + timedelta(seconds=1))
    rig.queue(cy, at=START_TIME + timedelta(seconds=2))
    rig.clock.advance(5)
    (created,) = rig.mm.run_once()
    assert set(created.users) == {ada.user_id, bob.user_id}


def test_closer_bucket_beats_farther(rig: Rig) -> None:
    ada, far, near = rig.user("Ada"), rig.user("Far"), rig.user("Near")
    rig.queue(ada, at=START_TIME, bucket="under_5m")
    rig.queue(far, at=START_TIME + timedelta(seconds=1), bucket="20m_plus")
    rig.queue(near, at=START_TIME + timedelta(seconds=2), bucket="5_10m")
    rig.clock.advance(5)
    (created,) = rig.mm.run_once()
    assert set(created.users) == {ada.user_id, near.user_id}


def test_unknown_or_missing_bucket_counts_as_one_step(rig: Rig) -> None:
    ada, unk, none, adjacent = (rig.user(n) for n in ("Ada", "Unk", "None", "Adj"))
    rig.queue(ada, at=START_TIME, bucket="5_10m")
    rig.queue(unk, at=START_TIME + timedelta(seconds=1), bucket="unknown")
    rig.queue(none, at=START_TIME + timedelta(seconds=2), bucket=None)
    rig.queue(adjacent, at=START_TIME + timedelta(seconds=3), bucket="10_20m")
    rig.clock.advance(5)
    created = rig.mm.run_once()
    # All three candidates are one step away: the oldest of them wins the tie.
    assert set(created[0].users) == {ada.user_id, unk.user_id}


def test_same_bucket_beats_unknown(rig: Rig) -> None:
    ada, unk, same = rig.user("Ada"), rig.user("Unk"), rig.user("Same")
    rig.queue(ada, at=START_TIME, bucket="5_10m")
    rig.queue(unk, at=START_TIME + timedelta(seconds=1), bucket="unknown")
    rig.queue(same, at=START_TIME + timedelta(seconds=2), bucket="5_10m")
    rig.clock.advance(5)
    (created,) = rig.mm.run_once()
    assert set(created.users) == {ada.user_id, same.user_id}


def test_older_partner_beats_newer_when_buckets_tie(rig: Rig) -> None:
    ada, older, newer = rig.user("Ada"), rig.user("Older"), rig.user("Newer")
    rig.queue(ada, at=START_TIME, bucket="5_10m")
    rig.queue(newer, at=START_TIME + timedelta(seconds=20), bucket="5_10m")
    rig.queue(older, at=START_TIME + timedelta(seconds=10), bucket="5_10m")
    rig.clock.advance(30)
    (created,) = rig.mm.run_once()
    assert set(created.users) == {ada.user_id, older.user_id}


def test_more_shared_tags_break_the_final_tie(rig: Rig) -> None:
    ada, one, two = rig.user("Ada"), rig.user("One"), rig.user("Two")
    t0 = START_TIME
    rig.queue(ada, at=t0, tags=("python", "react"), bucket="5_10m")
    # Same admitted_at and bucket: only the shared-tag count differs.
    rig.queue(one, at=t0 + timedelta(seconds=5), tags=("python",), bucket="5_10m")
    rig.queue(
        two, at=t0 + timedelta(seconds=5), tags=("python", "react"), bucket="5_10m"
    )
    rig.clock.advance(10)
    (created,) = rig.mm.run_once()
    assert set(created.users) == {ada.user_id, two.user_id}


def test_oldest_entry_is_placed_first(rig: Rig) -> None:
    # Ada is oldest. Bob and Cy are a perfect pair (same bucket), but Ada picks
    # first: her best partner is whoever is closest to her, and Bob/Cy are the
    # only options, so she takes the closest and leaves the other waiting.
    ada, bob, cy = rig.user("Ada"), rig.user("Bob"), rig.user("Cyd")
    rig.queue(ada, at=START_TIME, bucket="under_5m")
    rig.queue(bob, at=START_TIME + timedelta(seconds=1), bucket="20m_plus")
    rig.queue(cy, at=START_TIME + timedelta(seconds=2), bucket="20m_plus")
    rig.clock.advance(5)
    (created,) = rig.mm.run_once()
    assert ada.user_id in created.users


def test_shared_fields_intersection_and_category(rig: Rig) -> None:
    ada, bob = rig.user("Ada"), rig.user("Bob")
    rig.queue(ada, tags=("python", "react", "docker"), category="backend")
    rig.queue(bob, tags=("react", "python", "rust"), category="backend")
    (created,) = rig.mm.run_once()
    with rig.factory() as s:
        match = s.get_one(Match, created.match_id)
        assert set((match.shared_tags or "").split(",")) == {"python", "react"}
        assert match.shared_category == "backend"
    (frame,) = rig.notifier.frames_for(ada.user_id)
    assert isinstance(frame, WsMatchCreated)
    assert set(frame.match.shared_tags) == {"python", "react"}
    assert frame.match.shared_category == "backend"


def test_category_other_is_never_shared(rig: Rig) -> None:
    ada, bob = rig.user("Ada"), rig.user("Bob")
    rig.queue(ada, category="other")
    rig.queue(bob, category="other")
    (created,) = rig.mm.run_once()
    with rig.factory() as s:
        assert s.get_one(Match, created.match_id).shared_category is None


def test_different_categories_are_not_shared(rig: Rig) -> None:
    ada, bob = rig.user("Ada"), rig.user("Bob")
    rig.queue(ada, category="backend")
    rig.queue(bob, category="frontend")
    (created,) = rig.mm.run_once()
    with rig.factory() as s:
        assert s.get_one(Match, created.match_id).shared_category is None


def test_no_tags_when_one_side_sent_none(rig: Rig) -> None:
    ada, bob = rig.user("Ada"), rig.user("Bob")
    rig.queue(ada, tags=("python", "react"))
    rig.queue(bob)
    (created,) = rig.mm.run_once()
    with rig.factory() as s:
        assert s.get_one(Match, created.match_id).shared_tags is None


def test_no_shared_tags_in_common_is_null(rig: Rig) -> None:
    ada, bob = rig.user("Ada"), rig.user("Bob")
    rig.queue(ada, tags=("python",))
    rig.queue(bob, tags=("rust",))
    (created,) = rig.mm.run_once()
    with rig.factory() as s:
        assert s.get_one(Match, created.match_id).shared_tags is None


def test_user_in_active_match_is_not_paired_again(rig: Rig) -> None:
    ada, bob, cy = rig.user("Ada"), rig.user("Bob"), rig.user("Cyd")
    seed_match(rig.factory, ada, bob)
    e_ada, e_cy = rig.queue(ada), rig.queue(cy)
    assert rig.mm.run_once() == []
    assert rig.entry(e_ada).status == rig.entry(e_cy).status == "queued"


def test_stray_entry_of_a_matched_user_does_not_starve_the_waiters(rig: Rig) -> None:
    ada, bob, cy = rig.user("Ada"), rig.user("Bob"), rig.user("Cyd")
    seed_match(rig.factory, ada, rig.user("Zed"))
    # Ada's stray entry is the oldest, so the plan would pick it first.
    ea = rig.queue(ada, at=START_TIME)
    rig.queue(bob, at=START_TIME + timedelta(seconds=1))
    rig.queue(cy, at=START_TIME + timedelta(seconds=2))
    rig.clock.advance(5)

    created = rig.mm.run_once()

    assert [set(m.users) for m in created] == [{bob.user_id, cy.user_id}]
    assert rig.entry(ea).status == "queued"


def test_disabled_user_cancelled_and_match_ended(rig: Rig) -> None:
    ada, bob, cy = rig.user("Ada"), rig.user("Bob"), rig.user("Cyd")
    match_id = seed_match(rig.factory, ada, bob)
    e_cy = rig.queue(cy, tags=("python",), bucket="5_10m", category="backend")
    with rig.factory() as s:
        for who in (ada, cy):
            s.get_one(User, who.user_id).status = "disabled"
        s.commit()

    assert rig.mm.run_once() == []

    entry = rig.entry(e_cy)
    assert entry.status == "cancelled"
    assert entry.ended_at == rig.clock.now
    assert entry.stack_tags is None and entry.presence_id is None
    assert entry.remaining_time_bucket is None and entry.task_category is None
    with rig.factory() as s:
        match = s.get_one(Match, match_id)
        assert match.status == "ended" and match.end_reason == "disabled"
        assert match.ended_at == rig.clock.now
        assert not s.scalars(
            select(MatchParticipant).where(MatchParticipant.active.is_(True))
        ).all()
    (frame,) = rig.notifier.frames_for(bob.user_id)
    assert isinstance(frame, WsMatchEnded)
    assert frame.match_id == match_id and frame.reason == "ended"
    assert rig.notifier.frames_for(ada.user_id) == []
    assert rig.notifier.frames_for(cy.user_id) == []


def test_disabled_user_is_never_paired(rig: Rig) -> None:
    ada, bob = rig.user("Ada"), rig.user("Bob", status="disabled")
    ea, eb = rig.queue(ada), rig.queue(bob)
    assert rig.mm.run_once() == []
    assert rig.entry(ea).status == "queued"
    assert rig.entry(eb).status == "cancelled"


def test_entry_expires_exactly_at_expires_at(rig: Rig) -> None:
    ada = rig.user("Ada")
    entry_id = rig.queue(ada, tags=("python",), bucket="5_10m", ttl_s=90)
    rig.clock.advance(89.999)
    rig.mm.run_once()
    assert rig.entry(entry_id).status == "queued"
    assert rig.notifier.sent == []

    rig.clock.advance(0.001)
    rig.mm.run_once()
    entry = rig.entry(entry_id)
    assert entry.status == "expired"
    assert entry.ended_at == rig.clock.now
    assert entry.stack_tags is None and entry.remaining_time_bucket is None
    assert entry.presence_id is None
    (frame,) = rig.notifier.frames_for(ada.user_id)
    assert isinstance(frame, WsQueueEnded)
    assert frame.entry_id == entry_id and frame.reason == "expired"

    rig.mm.run_once()
    assert len(rig.notifier.sent) == 1


def test_expired_entry_is_not_matched(rig: Rig) -> None:
    ada, bob = rig.user("Ada"), rig.user("Bob")
    ea = rig.queue(ada, ttl_s=90)
    rig.queue(bob, at=START_TIME + timedelta(seconds=60), ttl_s=90)
    rig.clock.advance(90)
    assert rig.mm.run_once() == []
    assert rig.entry(ea).status == "expired"


def test_match_ends_exactly_at_ends_at(rig: Rig) -> None:
    ada, bob = rig.user("Ada"), rig.user("Bob")
    match_id = seed_match(
        rig.factory,
        ada,
        bob,
        ttl_s=900,
        shared_tags=("python",),
        shared_category="backend",
    )
    rig.clock.advance(899.999)
    rig.mm.run_once()
    assert rig.notifier.sent == []

    rig.clock.advance(0.001)
    rig.mm.run_once()
    with rig.factory() as s:
        match = s.get_one(Match, match_id)
        assert match.status == "ended" and match.end_reason == "expired"
        assert match.ended_at == rig.clock.now
        assert match.shared_tags is None and match.shared_category is None
        assert not s.scalars(
            select(MatchParticipant).where(MatchParticipant.active.is_(True))
        ).all()
    for user in (ada, bob):
        (frame,) = rig.notifier.frames_for(user.user_id)
        assert isinstance(frame, WsMatchEnded)
        assert frame.match_id == match_id and frame.reason == "expired"
    rig.mm.run_once()
    assert len(rig.notifier.sent) == 2


def test_users_can_rematch_after_match_ends(rig: Rig) -> None:
    ada, bob = rig.user("Ada"), rig.user("Bob")
    seed_match(rig.factory, ada, bob, ttl_s=900)
    rig.clock.advance(900)
    rig.mm.run_once()
    rig.queue(ada)
    rig.queue(bob)
    assert len(rig.mm.run_once()) == 1


def _counts(rig: Rig) -> tuple[int, int, int]:
    with rig.factory() as s:
        return (
            s.scalar(select(func.count()).select_from(QueueEntry)) or 0,
            s.scalar(select(func.count()).select_from(Match)) or 0,
            s.scalar(select(func.count()).select_from(MatchParticipant)) or 0,
        )


def test_purge_deletes_only_rows_older_than_retention(rig: Rig) -> None:
    retention = rig.config.retention_s
    ada, bob, di = rig.user("Ada"), rig.user("Bob"), rig.user("Dia")
    rig.queue(ada)
    rig.queue(bob)
    (created,) = rig.mm.run_once()  # entries end at START
    waiting = rig.queue(di, ttl_s=retention * 3)
    rig.notifier.connected.discard(di.user_id)  # never matched, never expires soon
    rig.clock.advance(rig.config.match_ttl_s)
    rig.mm.run_once()  # the match ends at START + match_ttl_s

    # Exactly `retention` after the entries ended: not older, nothing deleted.
    rig.clock.now = START_TIME + timedelta(seconds=retention)
    rig.mm.run_once()
    assert _counts(rig) == (3, 1, 2)

    # Entries are now older than retention; the match ended 900 s later.
    rig.clock.advance(61)
    rig.mm.run_once()
    assert _counts(rig) == (1, 1, 2)
    assert rig.entry(waiting).status == "queued"

    rig.clock.now = START_TIME + timedelta(seconds=rig.config.match_ttl_s + retention)
    rig.mm.run_once()  # exactly retention after the match ended
    assert _counts(rig) == (1, 1, 2)
    rig.clock.advance(61)
    rig.mm.run_once()
    assert _counts(rig) == (1, 0, 0)
    with rig.factory() as s:
        assert s.get(Match, created.match_id) is None


def test_purge_runs_at_most_once_a_minute(rig: Rig) -> None:
    config = dataclasses.replace(rig.config, retention_s=1, rematch_penalty_s=1)
    mm = Matchmaker(rig.factory, config, rig.clock, rig.notifier)
    ada, bob = rig.user("Ada"), rig.user("Bob")
    rig.queue(ada)
    rig.queue(bob)
    mm.run_once()  # matches; the first purge finds nothing old
    rig.clock.advance(10)
    mm.run_once()
    assert _counts(rig)[0] == 2  # throttled
    rig.clock.advance(60)
    mm.run_once()
    assert _counts(rig)[0] == 0


def test_integrity_error_skips_pair_and_continues(rig: Rig) -> None:
    ada, bob, cy, di = (rig.user(n) for n in ("Ada", "Bob", "Cyd", "Dia"))
    # Ada is already in an active match but also (inconsistently) queued.
    seed_match(rig.factory, ada, rig.user("Zed"))
    ea = rig.queue(ada, at=START_TIME)
    rig.queue(bob, at=START_TIME + timedelta(seconds=1))
    rig.queue(cy, at=START_TIME + timedelta(seconds=2))
    rig.queue(di, at=START_TIME + timedelta(seconds=3))
    rig.clock.advance(5)
    created = rig.mm.run_once()
    assert len(created) == 1  # Ada's pair failed; the other pair went through
    assert rig.entry(ea).status == "queued"
    assert ada.user_id not in created[0].users


# --- the real loop (rule 15: only lifespan/loop tests use it) -------------------


def _wait_until(predicate: Callable[[], bool], timeout_s: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _looping_app(
    session_factory: SessionFactory,
    server_config: ServerConfig,
    clock: FixedClock,
    mailer: RecordingMailer,
    interval_s: float,
) -> tuple[FastAPI, RecordingNotifier, Matchmaker]:
    config = dataclasses.replace(server_config, match_sweep_interval_s=interval_s)
    app = create_app(config, mailer=mailer, clock=clock, run_matchmaker=True)
    notifier = RecordingNotifier()
    app.state.notifier = notifier
    # create_app gave the matchmaker the hub; the lifespan reads this at startup.
    matchmaker = Matchmaker(session_factory, config, clock, notifier)
    app.state.matchmaker = matchmaker
    return app, notifier, matchmaker


def test_lifespan_loop_matches_without_run_once(
    session_factory: SessionFactory,
    server_config: ServerConfig,
    clock: FixedClock,
    mailer: RecordingMailer,
) -> None:
    ada = add_ready_user(session_factory, "Ada")
    bob = add_ready_user(session_factory, "Bob")
    entries = [seed_queued(session_factory, u) for u in (ada, bob)]
    app, notifier, mm = _looping_app(
        session_factory, server_config, clock, mailer, 0.05
    )
    notifier.connected.update({ada.user_id, bob.user_id})

    with TestClient(app):
        assert _wait_until(lambda: len(notifier.sent) == 2)
        with session_factory() as s:
            statuses = {s.get_one(QueueEntry, i).status for i in entries}
        assert statuses == {"matched"}
    assert mm._wake is None  # shutdown stopped the loop
    assert len(notifier.sent) == 2  # matched exactly once


def test_kick_wakes_the_loop_before_the_interval(
    session_factory: SessionFactory,
    server_config: ServerConfig,
    clock: FixedClock,
    mailer: RecordingMailer,
) -> None:
    ada = add_ready_user(session_factory, "Ada")
    bob = add_ready_user(session_factory, "Bob")
    app, notifier, mm = _looping_app(session_factory, server_config, clock, mailer, 60)
    notifier.connected.update({ada.user_id, bob.user_id})

    with TestClient(app):
        seed_queued(session_factory, ada)
        seed_queued(session_factory, bob)
        mm.kick()
        assert _wait_until(lambda: len(notifier.sent) == 2)


def test_kick_before_run_is_not_lost(
    session_factory: SessionFactory,
    server_config: ServerConfig,
    clock: FixedClock,
    mailer: RecordingMailer,
) -> None:
    ada = add_ready_user(session_factory, "Ada")
    bob = add_ready_user(session_factory, "Bob")
    seed_queued(session_factory, ada)
    seed_queued(session_factory, bob)
    app, notifier, mm = _looping_app(session_factory, server_config, clock, mailer, 60)
    notifier.connected.update({ada.user_id, bob.user_id})
    mm.kick()

    with TestClient(app):
        assert _wait_until(lambda: len(notifier.sent) == 2)


def test_a_failing_pass_does_not_stop_the_loop(
    session_factory: SessionFactory,
    server_config: ServerConfig,
    clock: FixedClock,
    mailer: RecordingMailer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    ada = add_ready_user(session_factory, "Ada")
    bob = add_ready_user(session_factory, "Bob")
    seed_queued(session_factory, ada)
    seed_queued(session_factory, bob)
    app, notifier, mm = _looping_app(
        session_factory, server_config, clock, mailer, 0.05
    )
    notifier.connected.update({ada.user_id, bob.user_id})
    real = mm._pair
    calls = {"n": 0}

    def flaky(now: datetime) -> list[MatchCreated]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return real(now)

    mm._pair = flaky  # type: ignore[method-assign]
    with TestClient(app):
        assert _wait_until(lambda: len(notifier.sent) == 2)
    assert calls["n"] >= 2
    assert "matchmaker pass failed" in caplog.text
    assert "boom" not in caplog.text

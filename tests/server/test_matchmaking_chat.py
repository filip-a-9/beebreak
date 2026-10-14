"""M5-04: chat lifecycle in the sweep (offline hold, status frames, retention).

Driven through run_once() with a fake clock and a settable connected set (rule 15).
"""

import dataclasses
import threading
import uuid
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import ColumnElement, func, select

from breakroom_server.config import ServerConfig
from breakroom_server.db import SessionFactory
from breakroom_server.matchmaking import Matchmaker
from breakroom_server.models import (
    Match,
    MatchParticipant,
    Message,
    Report,
    ReportMessage,
)
from breakroom_server.queue import end_match
from breakroom_shared.api import WsChatStatus, WsMatchEnded
from tests.server.conftest import (
    FixedClock,
    SeededUser,
    add_ready_user,
    requires_postgres,
    seed_match,
    seed_message,
    seed_report,
)
from tests.server.fakes import RecordingNotifier

HOLD = 60


@dataclasses.dataclass
class Rig:
    factory: SessionFactory
    config: ServerConfig
    clock: FixedClock
    notifier: RecordingNotifier
    mm: Matchmaker
    ada: SeededUser
    bob: SeededUser
    match_id: uuid.UUID

    def row(self, user: SeededUser) -> MatchParticipant:
        with self.factory() as s:
            return s.get_one(MatchParticipant, (self.match_id, user.user_id))

    def match(self) -> Match:
        with self.factory() as s:
            return s.get_one(Match, self.match_id)

    def frames(self, user: SeededUser) -> list[Any]:
        return list(self.notifier.frames_for(user.user_id))


def _rig(
    factory: SessionFactory,
    config: ServerConfig,
    clock: FixedClock,
    *,
    mm_class: type[Matchmaker] = Matchmaker,
    extensions_used: int = 0,
    suffix: str = "",
) -> Rig:
    notifier = RecordingNotifier()
    ada = add_ready_user(factory, "Ada" + suffix)
    bob = add_ready_user(factory, "Bob" + suffix)
    notifier.connected.update({ada.user_id, bob.user_id})
    match_id = seed_match(
        factory, ada, bob, now=clock.now, extensions_used=extensions_used
    )
    mm = mm_class(factory, config, clock, notifier)
    return Rig(factory, config, clock, notifier, mm, ada, bob, match_id)


@pytest.fixture
def rig(
    session_factory: SessionFactory, server_config: ServerConfig, clock: FixedClock
) -> Rig:
    return _rig(session_factory, server_config, clock)


def test_peer_offline_and_back_online_notify_once(rig: Rig) -> None:
    rig.mm.run_once()
    assert rig.notifier.sent == []

    rig.notifier.connected.discard(rig.bob.user_id)
    rig.clock.advance(1)
    rig.mm.run_once()
    (frame,) = rig.frames(rig.ada)
    assert isinstance(frame, WsChatStatus)
    assert frame.change == "peer_offline"
    assert frame.status.peer_online is False
    assert frame.status.match_id == rig.match_id
    assert rig.frames(rig.bob) == []
    assert rig.row(rig.bob).offline_since == rig.clock.now

    for _ in range(3):
        rig.clock.advance(1)
        rig.mm.run_once()
    assert len(rig.notifier.sent) == 1

    rig.notifier.connected.add(rig.bob.user_id)
    rig.clock.advance(1)
    rig.mm.run_once()
    _, back = rig.frames(rig.ada)
    assert isinstance(back, WsChatStatus)
    assert back.change == "peer_online"
    assert back.status.peer_online is True
    assert rig.row(rig.bob).offline_since is None
    rig.clock.advance(1)
    rig.mm.run_once()
    assert len(rig.notifier.sent) == 2
    assert rig.match().status == "active"


def test_status_frame_carries_the_recipients_view(
    session_factory: SessionFactory, server_config: ServerConfig, clock: FixedClock
) -> None:
    notifier = RecordingNotifier()
    ada = add_ready_user(session_factory, "Ada")
    bob = add_ready_user(session_factory, "Bob")
    notifier.connected.add(ada.user_id)
    seed_match(session_factory, ada, bob, now=clock.now, extension_requested_by=bob)
    Matchmaker(session_factory, server_config, clock, notifier).run_once()
    (frame,) = notifier.frames_for(ada.user_id)
    assert isinstance(frame, WsChatStatus)
    assert frame.status.extension == "requested_by_peer"


def test_offline_just_under_hold_stays_active_then_ends(rig: Rig) -> None:
    rig.notifier.connected.discard(rig.bob.user_id)
    rig.mm.run_once()
    rig.notifier.clear()

    rig.clock.advance(HOLD - 1)
    rig.mm.run_once()
    assert rig.match().status == "active"
    assert rig.frames(rig.ada) == [] and rig.frames(rig.bob) == []

    rig.clock.advance(1)
    rig.mm.run_once()
    match = rig.match()
    assert match.status == "ended"
    assert match.end_reason == "disconnected"
    assert match.ended_at == rig.clock.now
    for user in (rig.ada, rig.bob):
        (frame,) = rig.frames(user)
        assert isinstance(frame, WsMatchEnded)
        assert frame.reason == "disconnected"
        assert frame.match_id == rig.match_id
        row = rig.row(user)
        assert row.active is False
        assert row.offline_since is None

    rig.clock.advance(5)
    rig.mm.run_once()
    assert len(rig.notifier.sent) == 2


def test_both_offline_ends_after_hold_with_no_status_frames(rig: Rig) -> None:
    rig.notifier.connected.clear()
    rig.mm.run_once()
    assert rig.notifier.sent == []
    rig.clock.advance(HOLD)
    rig.mm.run_once()
    assert rig.match().end_reason == "disconnected"
    frames = [f for _, f in rig.notifier.sent]
    assert len(frames) == 2
    assert all(isinstance(f, WsMatchEnded) for f in frames)
    assert {uid for uid, _ in rig.notifier.sent} == {rig.ada.user_id, rig.bob.user_id}


def test_fresh_matchmaker_resets_old_offline_clock(
    session_factory: SessionFactory, server_config: ServerConfig, clock: FixedClock
) -> None:
    notifier = RecordingNotifier()
    ada = add_ready_user(session_factory, "Ada")
    bob = add_ready_user(session_factory, "Bob")
    notifier.connected.add(ada.user_id)
    match_id = seed_match(
        session_factory,
        ada,
        bob,
        now=clock.now - timedelta(seconds=300),
        offline_since={bob.user_id: clock.now - timedelta(seconds=120)},
    )
    mm = Matchmaker(session_factory, server_config, clock, notifier)
    mm.run_once()
    with session_factory() as s:
        assert s.get_one(Match, match_id).status == "active"
        row = s.get_one(MatchParticipant, (match_id, bob.user_id))
        assert row.offline_since == clock.now
    assert notifier.sent == []
    clock.advance(HOLD)
    mm.run_once()
    with session_factory() as s:
        assert s.get_one(Match, match_id).end_reason == "disconnected"


def test_extended_match_is_not_ended_at_the_original_deadline(
    session_factory: SessionFactory, server_config: ServerConfig, clock: FixedClock
) -> None:
    r = _rig(session_factory, server_config, clock, extensions_used=1)
    with session_factory() as s:
        m = s.get_one(Match, r.match_id)
        m.ends_at = m.ends_at + timedelta(seconds=server_config.chat_extension_s)
        s.commit()
    clock.advance(server_config.match_ttl_s + 1)
    r.mm.run_once()
    assert r.match().status == "active"
    clock.advance(server_config.chat_extension_s)
    r.mm.run_once()
    match = r.match()
    assert match.status == "ended" and match.end_reason == "expired"


def test_end_clears_pending_extension_request(
    session_factory: SessionFactory, server_config: ServerConfig, clock: FixedClock
) -> None:
    ada = add_ready_user(session_factory, "Ada")
    bob = add_ready_user(session_factory, "Bob")
    notifier = RecordingNotifier({ada.user_id, bob.user_id})
    match_id = seed_match(
        session_factory, ada, bob, now=clock.now, extension_requested_by=ada
    )
    clock.advance(server_config.match_ttl_s)
    Matchmaker(session_factory, server_config, clock, notifier).run_once()
    with session_factory() as s:
        match = s.get_one(Match, match_id)
        assert match.end_reason == "expired"
        assert match.extension_requested_by is None


def _hold_expired(rig: Rig) -> None:
    rig.notifier.connected.discard(rig.bob.user_id)
    rig.mm.run_once()
    rig.notifier.clear()
    rig.clock.advance(HOLD)


class _HttpEndsFirst(Matchmaker):
    """The HTTP end lands between the sweep's read and its conditional end."""

    http_user: uuid.UUID

    def _end_match_ids(
        self,
        now: datetime,
        match_ids: list[uuid.UUID],
        reason: str,
        *,
        notify_active_only: bool = False,
        only_if: tuple[ColumnElement[bool], ...] = (),
    ) -> list[tuple[uuid.UUID, list[uuid.UUID]]]:
        if reason == "disconnected":
            with self._factory() as s:
                for mid in match_ids:
                    end_match(s, now, self.http_user, mid, block=False)
                s.commit()
        return super()._end_match_ids(
            now,
            match_ids,
            reason,
            notify_active_only=notify_active_only,
            only_if=only_if,
        )


def test_http_end_before_disconnect_end_wins(
    session_factory: SessionFactory, server_config: ServerConfig, clock: FixedClock
) -> None:
    r = _rig(session_factory, server_config, clock, mm_class=_HttpEndsFirst)
    assert isinstance(r.mm, _HttpEndsFirst)
    r.mm.http_user = r.ada.user_id
    _hold_expired(r)
    r.mm.run_once()
    assert r.match().end_reason == "ended"
    assert r.notifier.sent == []  # the HTTP handler sends its own frames


def test_disconnect_end_before_http_end_wins(rig: Rig) -> None:
    _hold_expired(rig)
    rig.mm.run_once()
    assert len(rig.notifier.sent) == 2
    with rig.factory() as s:
        ended = end_match(s, rig.clock.now, rig.ada.user_id, rig.match_id, block=False)
        s.commit()
    assert ended is None
    assert rig.match().end_reason == "disconnected"


@requires_postgres
def test_threaded_http_end_racing_disconnect_end(
    session_factory: SessionFactory, server_config: ServerConfig, clock: FixedClock
) -> None:
    for i in range(10):
        r = _rig(session_factory, server_config, clock, suffix=f"{i}")
        _hold_expired(r)
        http_result: list[uuid.UUID | None] = []
        barrier = threading.Barrier(2)

        def http(
            r: Rig = r,
            out: list[uuid.UUID | None] = http_result,
            barrier: threading.Barrier = barrier,
        ) -> None:
            barrier.wait()
            with r.factory() as s:
                out.append(
                    end_match(s, r.clock.now, r.ada.user_id, r.match_id, block=False)
                )
                s.commit()

        def sweep(r: Rig = r, barrier: threading.Barrier = barrier) -> None:
            barrier.wait()
            r.mm.run_once()

        threads = [threading.Thread(target=http), threading.Thread(target=sweep)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        sweep_frames = [f for _, f in r.notifier.sent if isinstance(f, WsMatchEnded)]
        if http_result == [None]:  # the sweep won
            assert r.match().end_reason == "disconnected"
            assert len(sweep_frames) == 2
        else:  # HTTP won
            assert r.match().end_reason == "ended"
            assert sweep_frames == []
        with session_factory() as s:
            assert not s.scalars(
                select(MatchParticipant).where(
                    MatchParticipant.match_id == r.match_id,
                    MatchParticipant.active.is_(True),
                )
            ).all()


def _count_messages(factory: SessionFactory, match_id: uuid.UUID) -> int:
    with factory() as s:
        return s.execute(
            select(func.count())
            .select_from(Message)
            .where(Message.match_id == match_id)
        ).scalar_one()


def test_purge_messages_by_retention(
    session_factory: SessionFactory, server_config: ServerConfig, clock: FixedClock
) -> None:
    notifier = RecordingNotifier()
    ada = add_ready_user(session_factory, "Ada")
    bob = add_ready_user(session_factory, "Bob")
    ret = server_config.message_retention_s
    assert ret == 24 * 3600
    old = seed_match(
        session_factory,
        ada,
        bob,
        now=clock.now - timedelta(seconds=ret + 900),
        status="ended",
        ended_at=clock.now - timedelta(seconds=ret + 1),
        end_reason="ended",
    )
    recent = seed_match(
        session_factory,
        ada,
        bob,
        now=clock.now - timedelta(hours=24),
        status="ended",
        ended_at=clock.now - timedelta(hours=23),
        end_reason="ended",
    )
    live = seed_match(session_factory, ada, bob, now=clock.now)
    for mid in (old, recent, live):
        seed_message(session_factory, mid, ada, seq=1, body="hello")
        seed_message(session_factory, mid, bob, seq=2, body="hi")
    Matchmaker(session_factory, server_config, clock, notifier).run_once()
    assert _count_messages(session_factory, old) == 0
    assert _count_messages(session_factory, recent) == 2
    assert _count_messages(session_factory, live) == 2
    with session_factory() as s:
        # the metric survives on the participant row
        assert s.get_one(MatchParticipant, (old, ada.user_id)).messages_sent == 1
        assert s.get_one(Match, old).last_seq == 2


def test_purge_reports_by_resolution_age(
    session_factory: SessionFactory, server_config: ServerConfig, clock: FixedClock
) -> None:
    notifier = RecordingNotifier()
    ada = add_ready_user(session_factory, "Ada")
    bob = add_ready_user(session_factory, "Bob")
    ret = server_config.report_retention_s
    assert ret == 90 * 86_400
    ancient = clock.now - timedelta(days=400)
    evidence = [(1, "reported", "x"), (2, "reporter", "y")]

    def report(
        reporter: SeededUser,
        reported: SeededUser,
        status: str,
        resolved_at: datetime | None,
    ) -> uuid.UUID:
        mid = seed_match(
            session_factory,
            reporter,
            reported,
            now=clock.now,
            status="ended",
            ended_at=clock.now,
            end_reason="reported",
        )
        return seed_report(
            session_factory,
            reporter,
            reported,
            mid,
            now=ancient,
            status=status,
            resolved_at=resolved_at,
            evidence=evidence,
        )

    old = report(ada, bob, "dismissed", clock.now - timedelta(seconds=ret + 1))
    fresh = report(bob, ada, "actioned", clock.now - timedelta(days=89))
    open_ = report(ada, bob, "open", None)
    Matchmaker(session_factory, server_config, clock, notifier).run_once()
    with session_factory() as s:
        assert set(s.scalars(select(Report.id))) == {fresh, open_}
        left = set(s.scalars(select(ReportMessage.report_id)))
        assert left == {fresh, open_}
        assert old not in left


def test_logs_hold_no_bodies_notes_or_nicknames(
    session_factory: SessionFactory,
    server_config: ServerConfig,
    clock: FixedClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    canary_body, canary_note = "BODY-CANARY-7731", "NOTE-CANARY-7731"
    ada = add_ready_user(session_factory, "Zebulon")
    bob = add_ready_user(session_factory, "Quixote")
    notifier = RecordingNotifier({ada.user_id})
    old = seed_match(
        session_factory,
        ada,
        bob,
        now=clock.now - timedelta(days=3),
        status="ended",
        ended_at=clock.now - timedelta(days=2),
        end_reason="ended",
    )
    seed_message(session_factory, old, ada, seq=1, body=canary_body)
    seed_report(
        session_factory,
        ada,
        bob,
        old,
        note=canary_note,
        status="dismissed",
        resolved_at=clock.now - timedelta(days=200),
        evidence=[(1, "reporter", canary_body)],
    )
    live = seed_match(session_factory, ada, bob, now=clock.now)
    seed_message(session_factory, live, bob, seq=1, body=canary_body)
    mm = Matchmaker(session_factory, server_config, clock, notifier)
    mm.run_once()
    clock.advance(HOLD)
    mm.run_once()
    assert "purged" in caplog.text and "presence" in caplog.text
    for secret in (canary_body, canary_note, "Zebulon", "Quixote"):
        assert secret not in caplog.text

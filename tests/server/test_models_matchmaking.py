"""M4-01 schema: constraints that make matching safe, enforced by the database.

Runs on SQLite always and on PostgreSQL when BEEBREAK_TEST_DATABASE_URL is set.
"""

import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError, StatementError

from beebreak_server.db import SessionFactory
from beebreak_server.models import (
    Block,
    Match,
    MatchParticipant,
    QueueEntry,
    join_tags,
    split_tags,
)
from tests.server.conftest import START_TIME, SeededUser, add_ready_user


def _entry(user: SeededUser, **kw: object) -> QueueEntry:
    fields: dict[str, object] = {
        "user_id": user.user_id,
        "device_id": user.device_id,
        "idempotency_key": uuid.uuid4(),
        "status": "queued",
        "admitted_at": START_TIME,
        "expires_at": START_TIME + timedelta(seconds=90),
    }
    fields.update(kw)
    return QueueEntry(**fields)


def _match() -> Match:
    return Match(created_at=START_TIME, ends_at=START_TIME + timedelta(seconds=900))


def _block(blocker: SeededUser, blocked: SeededUser) -> Block:
    return Block(
        blocker_id=blocker.user_id, blocked_id=blocked.user_id, created_at=START_TIME
    )


def _commit_fails(session_factory: SessionFactory, *rows: object) -> None:
    with session_factory() as s:
        s.add_all(list(rows))
        with pytest.raises(IntegrityError):
            s.commit()


def test_tag_helpers_use_vocabulary_order_and_drop_unknowns() -> None:
    assert join_tags([]) is None
    assert join_tags(["nonsense"]) is None
    assert split_tags(None) == ()
    assert split_tags("") == ()
    joined = join_tags(["rust", "python", "nonsense", "python"])
    assert joined is not None
    assert joined == join_tags(["python", "rust"])
    assert split_tags(joined) == ("python", "rust")
    assert split_tags(f"{joined},bogus") == ("python", "rust")


def test_entry_match_and_participant_round_trip(
    session_factory: SessionFactory,
) -> None:
    a = add_ready_user(session_factory, "Ada")
    b = add_ready_user(session_factory, "Bob")
    pid = uuid.uuid4()
    with session_factory() as s:
        match = Match(
            created_at=START_TIME,
            ends_at=START_TIME + timedelta(seconds=900),
            shared_tags=join_tags(["python", "rust"]),
            shared_category="backend",
        )
        s.add(match)
        s.flush()
        s.add_all(
            [
                MatchParticipant(match_id=match.id, user_id=a.user_id),
                MatchParticipant(match_id=match.id, user_id=b.user_id),
                _entry(
                    a,
                    presence_id=pid,
                    task_category="backend",
                    stack_tags=join_tags(["rust"]),
                    remaining_time_bucket="5_10m",
                    match_id=match.id,
                    status="matched",
                    ended_at=START_TIME,
                ),
                _block(a, b),
            ]
        )
        s.commit()
        match_id = match.id
    with session_factory() as s:
        got = s.get(Match, match_id)
        assert got is not None
        assert (got.status, got.end_reason, got.ended_at) == ("active", None, None)
        assert split_tags(got.shared_tags) == ("python", "rust")
        assert got.ends_at == START_TIME + timedelta(seconds=900)
        part = s.get(MatchParticipant, (match_id, a.user_id))
        assert part is not None and part.active is True
        block = s.get(Block, (a.user_id, b.user_id))
        assert block is not None and block.created_at == START_TIME


def test_second_queued_entry_for_a_user_is_refused(
    session_factory: SessionFactory,
) -> None:
    user = add_ready_user(session_factory, "Ada")
    with session_factory() as s:
        s.add(_entry(user))
        s.commit()
    _commit_fails(session_factory, _entry(user))


def test_ended_entries_do_not_conflict_with_a_queued_one(
    session_factory: SessionFactory,
) -> None:
    user = add_ready_user(session_factory, "Ada")
    with session_factory() as s:
        s.add_all(
            [
                _entry(user, status="cancelled"),
                _entry(user, status="expired"),
                _entry(user, status="cancelled"),
                _entry(user, status="queued"),
            ]
        )
        s.commit()


def test_two_users_may_each_be_queued(session_factory: SessionFactory) -> None:
    a = add_ready_user(session_factory, "Ada")
    b = add_ready_user(session_factory, "Bob")
    with session_factory() as s:
        s.add_all([_entry(a), _entry(b)])
        s.commit()


def test_queue_status_check(session_factory: SessionFactory) -> None:
    user = add_ready_user(session_factory, "Ada")
    _commit_fails(session_factory, _entry(user, status="waiting"))


def test_idempotency_key_is_unique_per_user_only(
    session_factory: SessionFactory,
) -> None:
    a = add_ready_user(session_factory, "Ada")
    b = add_ready_user(session_factory, "Bob")
    key = uuid.uuid4()
    with session_factory() as s:
        s.add_all(
            [
                _entry(a, idempotency_key=key, status="cancelled"),
                _entry(b, idempotency_key=key),
            ]
        )
        s.commit()
    _commit_fails(session_factory, _entry(a, idempotency_key=key, status="expired"))


def test_second_active_participant_row_for_a_user_is_refused(
    session_factory: SessionFactory,
) -> None:
    a = add_ready_user(session_factory, "Ada")
    b = add_ready_user(session_factory, "Bob")
    with session_factory() as s:
        first, second = _match(), _match()
        s.add_all([first, second])
        s.flush()
        s.add_all(
            [
                MatchParticipant(match_id=first.id, user_id=a.user_id),
                MatchParticipant(match_id=first.id, user_id=b.user_id),
            ]
        )
        s.commit()
        second_id = second.id
    _commit_fails(
        session_factory, MatchParticipant(match_id=second_id, user_id=a.user_id)
    )


def test_inactive_participant_rows_do_not_conflict(
    session_factory: SessionFactory,
) -> None:
    a = add_ready_user(session_factory, "Ada")
    with session_factory() as s:
        matches = [_match(), _match(), _match()]
        s.add_all(matches)
        s.flush()
        s.add_all(
            MatchParticipant(match_id=m.id, user_id=a.user_id, active=active)
            for m, active in zip(matches, (False, False, True), strict=True)
        )
        s.commit()


def test_same_user_twice_in_one_match_is_refused(
    session_factory: SessionFactory,
) -> None:
    a = add_ready_user(session_factory, "Ada")
    with session_factory() as s:
        match = _match()
        s.add(match)
        s.flush()
        s.add_all(
            [
                MatchParticipant(match_id=match.id, user_id=a.user_id, active=False),
                MatchParticipant(match_id=match.id, user_id=a.user_id, active=False),
            ]
        )
        with pytest.raises(IntegrityError):
            s.commit()


def test_match_status_and_end_reason_checks(session_factory: SessionFactory) -> None:
    _commit_fails(
        session_factory,
        Match(status="paused", created_at=START_TIME, ends_at=START_TIME),
    )
    _commit_fails(
        session_factory,
        Match(end_reason="vanished", created_at=START_TIME, ends_at=START_TIME),
    )
    with session_factory() as s:
        s.add_all(
            Match(
                status="ended",
                end_reason=reason,
                created_at=START_TIME,
                ends_at=START_TIME,
                ended_at=START_TIME,
            )
            for reason in ("ended", "blocked", "expired", "disabled")
        )
        s.commit()


def test_self_block_is_refused(session_factory: SessionFactory) -> None:
    a = add_ready_user(session_factory, "Ada")
    _commit_fails(session_factory, _block(a, a))


def test_duplicate_block_is_refused_but_the_reverse_is_fine(
    session_factory: SessionFactory,
) -> None:
    a = add_ready_user(session_factory, "Ada")
    b = add_ready_user(session_factory, "Bob")
    with session_factory() as s:
        s.add_all([_block(a, b), _block(b, a)])
        s.commit()
    _commit_fails(session_factory, _block(a, b))


def test_naive_datetimes_are_refused(session_factory: SessionFactory) -> None:
    a = add_ready_user(session_factory, "Ada")
    b = add_ready_user(session_factory, "Bob")
    naive = datetime(2026, 10, 13, 12, 0, 0)
    block = _block(a, b)
    block.created_at = naive
    for row in (
        _entry(a, admitted_at=naive),
        Match(created_at=naive, ends_at=START_TIME),
        block,
    ):
        with session_factory() as s:
            s.add(row)
            with pytest.raises((ValueError, StatementError)):
                s.commit()

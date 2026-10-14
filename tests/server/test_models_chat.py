"""M5-01 schema: chat constraints enforced by the database.

Runs on SQLite always and on PostgreSQL when BREAKROOM_TEST_DATABASE_URL is set.
"""

import uuid

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from breakroom_server.db import SessionFactory
from breakroom_server.models import (
    Match,
    MatchParticipant,
    Message,
    Report,
    ReportMessage,
)
from tests.server.conftest import (
    START_TIME,
    SeededUser,
    add_ready_user,
    seed_match,
    seed_message,
    seed_report,
)


def _pair(session_factory: SessionFactory) -> tuple[SeededUser, SeededUser]:
    return (
        add_ready_user(session_factory, "Ada"),
        add_ready_user(session_factory, "Bob"),
    )


def _commit_fails(session_factory: SessionFactory, *rows: object) -> None:
    with session_factory() as s:
        s.add_all(list(rows))
        with pytest.raises(IntegrityError):
            s.commit()


def _report(a: SeededUser, b: SeededUser, match_id: uuid.UUID, **kw: object) -> Report:
    fields: dict[str, object] = {
        "reporter_id": a.user_id,
        "reported_id": b.user_id,
        "match_id": match_id,
        "reason": "spam",
        "status": "open",
        "created_at": START_TIME,
    }
    fields.update(kw)
    return Report(**fields)


def _message(match_id: uuid.UUID, author: SeededUser, **kw: object) -> Message:
    fields: dict[str, object] = {
        "match_id": match_id,
        "seq": 1,
        "author_id": author.user_id,
        "client_message_id": uuid.uuid4(),
        "body": "hi",
        "created_at": START_TIME,
    }
    fields.update(kw)
    return Message(**fields)


def _delete_match(session_factory: SessionFactory, match_id: uuid.UUID) -> None:
    """Delete with Core statements so the database, not the ORM, cascades."""
    with session_factory() as s:
        s.execute(delete(MatchParticipant).where(MatchParticipant.match_id == match_id))
        s.execute(delete(Match).where(Match.id == match_id))
        s.commit()


def test_chat_columns_default_to_zero(session_factory: SessionFactory) -> None:
    a, b = _pair(session_factory)
    match_id = seed_match(session_factory, a, b)
    with session_factory() as s:
        match = s.get(Match, match_id)
        assert match is not None
        assert (match.extensions_used, match.last_seq) == (0, 0)
        assert match.extension_requested_by is None
        participant = s.get(MatchParticipant, (match_id, a.user_id))
        assert participant is not None
        assert participant.messages_sent == 0 and participant.offline_since is None


@pytest.mark.parametrize("reason", ["disconnected", "reported"])
def test_match_end_reason_accepts_chat_reasons(
    session_factory: SessionFactory, reason: str
) -> None:
    a, b = _pair(session_factory)
    match_id = seed_match(
        session_factory, a, b, status="ended", ended_at=START_TIME, end_reason=reason
    )
    with session_factory() as s:
        match = s.get(Match, match_id)
        assert match is not None and match.end_reason == reason


def test_match_end_reason_still_rejects_unknown(
    session_factory: SessionFactory,
) -> None:
    _commit_fails(
        session_factory,
        Match(
            status="ended",
            created_at=START_TIME,
            ends_at=START_TIME,
            end_reason="bogus",
        ),
    )


def test_match_extensions_used_must_not_be_negative(
    session_factory: SessionFactory,
) -> None:
    _commit_fails(
        session_factory,
        Match(created_at=START_TIME, ends_at=START_TIME, extensions_used=-1),
    )


def test_message_seq_must_be_positive(session_factory: SessionFactory) -> None:
    a, b = _pair(session_factory)
    match_id = seed_match(session_factory, a, b)
    _commit_fails(session_factory, _message(match_id, a, seq=0))


def test_message_client_id_unique_per_author(session_factory: SessionFactory) -> None:
    a, b = _pair(session_factory)
    match_id = seed_match(session_factory, a, b)
    client_id = seed_message(session_factory, match_id, a, seq=1, body="one")
    _commit_fails(
        session_factory, _message(match_id, a, seq=2, client_message_id=client_id)
    )
    with session_factory() as s:  # another author may reuse the same client id
        s.add(_message(match_id, b, seq=2, client_message_id=client_id))
        s.commit()


def test_message_seq_unique_per_match(session_factory: SessionFactory) -> None:
    a, b = _pair(session_factory)
    match_id = seed_match(session_factory, a, b)
    seed_message(session_factory, match_id, a, seq=1, body="one")
    _commit_fails(session_factory, _message(match_id, b, seq=1))
    c, d = add_ready_user(session_factory, "Cy"), add_ready_user(session_factory, "Di")
    other = seed_match(session_factory, c, d)
    with session_factory() as s:  # the same seq in another match is fine
        s.add(_message(other, c, seq=1))
        s.commit()


@pytest.mark.parametrize(
    "bad", [{"status": "bogus"}, {"reason": "bogus"}], ids=["status", "reason"]
)
def test_report_status_and_reason_checks(
    session_factory: SessionFactory, bad: dict[str, object]
) -> None:
    a, b = _pair(session_factory)
    _commit_fails(session_factory, _report(a, b, uuid.uuid4(), **bad))


@pytest.mark.parametrize("status", ["open", "dismissed", "actioned"])
@pytest.mark.parametrize("reason", ["harassment", "spam", "sexual", "hate", "other"])
def test_report_accepts_every_status_and_reason(
    session_factory: SessionFactory, status: str, reason: str
) -> None:
    a, b = _pair(session_factory)
    with session_factory() as s:
        s.add(_report(a, b, uuid.uuid4(), status=status, reason=reason))
        s.commit()


def test_report_cannot_target_self(session_factory: SessionFactory) -> None:
    a, _ = _pair(session_factory)
    _commit_fails(session_factory, _report(a, a, uuid.uuid4()))


def test_report_unique_per_reporter_and_match(
    session_factory: SessionFactory,
) -> None:
    a, b = _pair(session_factory)
    match_id = seed_match(session_factory, a, b)
    seed_report(session_factory, a, b, match_id)
    _commit_fails(session_factory, _report(a, b, match_id, reason="hate"))
    with session_factory() as s:  # the other side may report the same match
        s.add(_report(b, a, match_id))
        s.commit()


def test_report_message_author_check(session_factory: SessionFactory) -> None:
    a, b = _pair(session_factory)
    report_id = seed_report(session_factory, a, b, uuid.uuid4())
    _commit_fails(
        session_factory,
        ReportMessage(
            report_id=report_id, seq=1, author="someone", body="x", sent_at=START_TIME
        ),
    )


def test_deleting_a_match_cascades_to_its_messages(
    session_factory: SessionFactory,
) -> None:
    a, b = _pair(session_factory)
    match_id = seed_match(session_factory, a, b)
    seed_message(session_factory, match_id, a, seq=1, body="one")
    seed_message(session_factory, match_id, b, seq=2, body="two")
    c, d = add_ready_user(session_factory, "Cy"), add_ready_user(session_factory, "Di")
    other = seed_match(session_factory, c, d)
    seed_message(session_factory, other, c, seq=1, body="keep")

    _delete_match(session_factory, match_id)
    with session_factory() as s:
        assert s.scalars(select(Message.body)).all() == ["keep"]


def test_deleting_a_report_cascades_to_its_evidence(
    session_factory: SessionFactory,
) -> None:
    a, b = _pair(session_factory)
    report_id = seed_report(
        session_factory,
        a,
        b,
        uuid.uuid4(),
        evidence=[(1, "reporter", "x"), (2, "reported", "y")],
    )
    keep = seed_report(
        session_factory, b, a, uuid.uuid4(), evidence=[(1, "reporter", "z")]
    )
    with session_factory() as s:
        s.execute(delete(Report).where(Report.id == report_id))
        s.commit()
    with session_factory() as s:
        rows = s.scalars(select(ReportMessage)).all()
        assert [(r.report_id, r.body) for r in rows] == [(keep, "z")]


def test_report_survives_deletion_of_its_match(
    session_factory: SessionFactory,
) -> None:
    a, b = _pair(session_factory)
    match_id = seed_match(session_factory, a, b)
    seed_message(session_factory, match_id, a, seq=1, body="one")
    report_id = seed_report(
        session_factory, a, b, match_id, evidence=[(1, "reporter", "one")]
    )
    _delete_match(session_factory, match_id)
    with session_factory() as s:
        assert s.get(Match, match_id) is None
        report = s.get(Report, report_id)
        assert report is not None and report.match_id == match_id
        assert s.scalar(select(func.count()).select_from(ReportMessage)) == 1

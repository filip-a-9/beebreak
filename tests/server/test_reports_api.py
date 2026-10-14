"""M5-03: POST /v1/reports."""

import threading
import uuid
from datetime import timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from breakroom_server.db import SessionFactory
from breakroom_server.mailer import RecordingMailer
from breakroom_server.models import (
    Block,
    Match,
    MatchParticipant,
    Message,
    Report,
    ReportMessage,
)
from breakroom_shared.api import WsMatchEnded
from tests.server.conftest import (
    START_TIME,
    ApiUser,
    add_ready_user,
    ready_user,
    requires_postgres,
    seed_match,
    seed_message,
)
from tests.server.fakes import RecordingNotifier

NOTE = "canary-note-xyz"
BODY_1 = "canary-body-one"
BODY_2 = "canary-body-two"
BODY_3 = "canary-body-three"


@pytest.fixture
def notifier(app: FastAPI) -> RecordingNotifier:
    fake = RecordingNotifier()
    app.state.notifier = fake
    return fake


@pytest.fixture
def ada(
    client: TestClient, session_factory: SessionFactory, mailer: RecordingMailer
) -> ApiUser:
    return ready_user(client, session_factory, mailer, "ada@example.com", "Ada")


@pytest.fixture
def bob(
    client: TestClient, session_factory: SessionFactory, mailer: RecordingMailer
) -> ApiUser:
    return ready_user(client, session_factory, mailer, "bob@example.com", "Bob")


def post(client: TestClient, user: ApiUser, match_id: uuid.UUID, **fields: Any) -> Any:
    body: dict[str, Any] = {"match_id": str(match_id), "reason": "harassment"}
    body.update(fields)
    return client.post("/v1/reports", json=body, headers=user.headers)


def chat(
    factory: SessionFactory, ada: ApiUser, bob: ApiUser, **kwargs: Any
) -> uuid.UUID:
    """An active match with three messages: 1 by Ada, 2 by Bob, 3 by Ada."""
    match_id = seed_match(factory, ada, bob, **kwargs)
    seed_message(factory, match_id, ada, seq=1, body=BODY_1)
    seed_message(factory, match_id, bob, seq=2, body=BODY_2)
    seed_message(factory, match_id, ada, seq=3, body=BODY_3)
    return match_id


def reports(factory: SessionFactory) -> list[Report]:
    with factory() as s:
        return list(s.scalars(select(Report)))


def count(factory: SessionFactory, model: Any) -> int:
    with factory() as s:
        return s.scalar(select(func.count()).select_from(model)) or 0


def test_report_during_a_chat(
    client: TestClient,
    ada: ApiUser,
    bob: ApiUser,
    session_factory: SessionFactory,
    notifier: RecordingNotifier,
) -> None:
    match_id = chat(session_factory, ada, bob)
    r = post(client, ada, match_id, reason="spam", note=NOTE, message_seqs=[2, 1])
    assert r.status_code == 204 and r.content == b""

    (report,) = reports(session_factory)
    assert report.status == "open" and report.reason == "spam" and report.note == NOTE
    assert report.reporter_id == ada.user_id and report.reported_id == bob.user_id
    assert report.match_id == match_id and report.created_at == START_TIME
    with session_factory() as s:
        evidence = [
            (e.seq, e.author, e.body)
            for e in s.scalars(select(ReportMessage).order_by(ReportMessage.seq))
        ]
        assert s.get(Block, (ada.user_id, bob.user_id)) is not None
        assert s.get(Block, (bob.user_id, ada.user_id)) is None
        match = s.get(Match, match_id)
        assert match is not None
        assert match.status == "ended" and match.end_reason == "reported"
        assert not any(
            p.active for p in s.scalars(select(MatchParticipant))
        )  # chat is over
    assert evidence == [(1, "reporter", BODY_1), (2, "reported", BODY_2)]

    for user in (ada, bob):
        (frame,) = notifier.frames_for(user.user_id)
        assert isinstance(frame, WsMatchEnded)
        assert frame.reason == "ended" and frame.match_id == match_id


def test_no_ticked_messages_copies_nothing(
    client: TestClient,
    ada: ApiUser,
    bob: ApiUser,
    session_factory: SessionFactory,
) -> None:
    match_id = chat(session_factory, ada, bob)
    assert post(client, ada, match_id, note="   ").status_code == 204
    (report,) = reports(session_factory)
    assert report.note is None
    assert count(session_factory, ReportMessage) == 0
    assert count(session_factory, Message) == 3  # the chat itself is untouched


def test_seqs_are_deduplicated_and_note_normalized(
    client: TestClient,
    ada: ApiUser,
    bob: ApiUser,
    session_factory: SessionFactory,
) -> None:
    match_id = chat(session_factory, ada, bob)
    r = post(
        client, ada, match_id, note="  hi\x00 there\n\n\n\nyou  ", message_seqs=[3, 3]
    )
    assert r.status_code == 204
    assert reports(session_factory)[0].note == "hi there\n\nyou"
    assert count(session_factory, ReportMessage) == 1


def test_report_inside_the_window_after_the_end(
    client: TestClient,
    ada: ApiUser,
    bob: ApiUser,
    session_factory: SessionFactory,
    notifier: RecordingNotifier,
) -> None:
    match_id = chat(
        session_factory,
        ada,
        bob,
        status="ended",
        ended_at=START_TIME - timedelta(hours=23, minutes=59),
        end_reason="ended",
    )
    assert post(client, ada, match_id, message_seqs=[2]).status_code == 204
    assert len(reports(session_factory)) == 1
    with session_factory() as s:
        assert s.get(Block, (ada.user_id, bob.user_id)) is not None
        match = s.get(Match, match_id)
        assert match is not None and match.end_reason == "ended"  # first end wins
    assert notifier.sent == []


def test_report_after_the_window_is_refused(
    client: TestClient,
    ada: ApiUser,
    bob: ApiUser,
    session_factory: SessionFactory,
    notifier: RecordingNotifier,
) -> None:
    match_id = chat(
        session_factory,
        ada,
        bob,
        status="ended",
        ended_at=START_TIME - timedelta(hours=24, seconds=1),
        end_reason="ended",
    )
    r = post(client, ada, match_id)
    assert r.status_code == 409 and r.json()["error"] == "report_window_closed"
    assert reports(session_factory) == []
    assert count(session_factory, Block) == 0
    assert notifier.sent == []


def test_second_report_conflicts_but_the_peer_may_report_back(
    client: TestClient,
    ada: ApiUser,
    bob: ApiUser,
    session_factory: SessionFactory,
) -> None:
    match_id = chat(session_factory, ada, bob)
    assert post(client, ada, match_id).status_code == 204
    r = post(client, ada, match_id, note=NOTE)
    assert r.status_code == 409 and r.json()["error"] == "already_reported"
    assert post(client, bob, match_id, message_seqs=[1]).status_code == 204
    assert len(reports(session_factory)) == 2
    with session_factory() as s:
        assert s.get(Block, (bob.user_id, ada.user_id)) is not None
        assert s.get(Block, (ada.user_id, bob.user_id)) is not None


def test_non_participant_and_unknown_match_get_the_same_404(
    client: TestClient,
    ada: ApiUser,
    bob: ApiUser,
    session_factory: SessionFactory,
    notifier: RecordingNotifier,
) -> None:
    cara = add_ready_user(session_factory, "Cara")
    dan = add_ready_user(session_factory, "Dan")
    other = seed_match(session_factory, cara, dan)
    unknown = post(client, ada, uuid.uuid4())
    foreign = post(client, ada, other)
    assert unknown.status_code == foreign.status_code == 404
    assert unknown.json() == foreign.json() == {"error": "not_found"}
    assert reports(session_factory) == [] and count(session_factory, Block) == 0
    with session_factory() as s:
        match = s.get(Match, other)
        assert match is not None and match.status == "active"
    assert notifier.sent == []


@pytest.mark.parametrize(
    "fields",
    [
        {"message_seqs": [1, 99]},  # unknown seq
        {"message_seqs": [0]},
        {"note": "x" * 501},
        {"note": chr(0x202E) + "\x00"},  # non-empty, normalizes to nothing
    ],
)
def test_invalid_input_is_400_and_changes_nothing(
    fields: dict[str, Any],
    client: TestClient,
    ada: ApiUser,
    bob: ApiUser,
    session_factory: SessionFactory,
    notifier: RecordingNotifier,
) -> None:
    match_id = chat(session_factory, ada, bob)
    r = post(client, ada, match_id, **fields)
    assert r.status_code == 400 and r.json()["error"] == "invalid_request"
    assert reports(session_factory) == [] and count(session_factory, Block) == 0
    with session_factory() as s:
        match = s.get(Match, match_id)
        assert match is not None and match.status == "active"
    assert notifier.sent == []


def test_purged_messages_count_as_missing(
    client: TestClient,
    ada: ApiUser,
    bob: ApiUser,
    session_factory: SessionFactory,
) -> None:
    match_id = chat(
        session_factory,
        ada,
        bob,
        status="ended",
        ended_at=START_TIME - timedelta(hours=1),
        end_reason="ended",
    )
    with session_factory() as s:
        for m in s.scalars(select(Message)):
            s.delete(m)
        s.commit()
    assert post(client, ada, match_id, message_seqs=[1]).status_code == 400
    assert post(client, ada, match_id).status_code == 204


def test_fifty_one_seqs_are_rejected_by_the_model(
    client: TestClient, ada: ApiUser, bob: ApiUser, session_factory: SessionFactory
) -> None:
    match_id = chat(session_factory, ada, bob)
    r = post(client, ada, match_id, message_seqs=list(range(1, 52)))
    assert r.status_code in (400, 422)
    assert reports(session_factory) == []


def test_sixth_report_in_the_window_is_rate_limited(
    client: TestClient,
    ada: ApiUser,
    bob: ApiUser,
    session_factory: SessionFactory,
) -> None:
    codes = []
    for _ in range(6):
        codes.append(
            post(client, ada, seed_match(session_factory, ada, bob)).status_code
        )
    assert codes == [204] * 5 + [429]
    assert len(reports(session_factory)) == 5


@requires_postgres
def test_concurrent_duplicate_reports_make_one_row(
    client: TestClient,
    ada: ApiUser,
    bob: ApiUser,
    session_factory: SessionFactory,
    notifier: RecordingNotifier,
) -> None:
    match_id = chat(session_factory, ada, bob)
    n = 4
    barrier = threading.Barrier(n)
    codes: list[int] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        status = post(client, ada, match_id, message_seqs=[1]).status_code
        with lock:
            codes.append(status)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(codes) == [204] + [409] * (n - 1)
    assert len(reports(session_factory)) == 1
    assert count(session_factory, ReportMessage) == 1
    assert len(notifier.frames_for(bob.user_id)) == 1


def test_logs_hold_no_note_or_body(
    client: TestClient,
    ada: ApiUser,
    bob: ApiUser,
    session_factory: SessionFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    match_id = chat(session_factory, ada, bob)
    post(client, ada, match_id, note=NOTE, message_seqs=[1, 2, 3])
    post(client, ada, match_id, note=NOTE, message_seqs=[1])  # 409
    post(client, bob, match_id, note=NOTE, message_seqs=[99])  # 400
    text = caplog.text
    for secret in (NOTE, BODY_1, BODY_2, BODY_3, "Ada", "Bob"):
        assert secret not in text
    assert "report filed" in text

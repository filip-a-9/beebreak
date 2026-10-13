"""M4-02: queue and match HTTP API (real handlers, recording fakes for the rest)."""

import threading
import uuid
from datetime import timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from breakroom_server import queue
from breakroom_server.auth import ApiError
from breakroom_server.config import ServerConfig
from breakroom_server.db import SessionFactory
from breakroom_server.mailer import RecordingMailer
from breakroom_server.matchmaking import Matchmaker
from breakroom_server.models import Block, Match, MatchParticipant, QueueEntry
from breakroom_server.queue import current_state
from breakroom_shared.api import (
    Consent,
    QueueJoinRequest,
    WsMatchEnded,
    WsQueueEnded,
    WsQueueJoined,
)
from breakroom_shared.presence import OutboundPresence
from tests.server.conftest import (
    ALL_CONSENT,
    START_TIME,
    ApiUser,
    FixedClock,
    add_ready_user,
    ready_user,
    requires_postgres,
    seed_match,
    seed_queued,
)
from tests.server.fakes import RecordingMatchmaker, RecordingNotifier

TAGS = ["python", "fastapi"]


@pytest.fixture
def notifier(app: FastAPI) -> RecordingNotifier:
    fake = RecordingNotifier()
    app.state.notifier = fake
    return fake


@pytest.fixture
def matchmaker(app: FastAPI) -> RecordingMatchmaker:
    fake = RecordingMatchmaker()
    app.state.matchmaker = fake
    return fake


def presence(**fields: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "schema_version": 1,
        "presence_id": str(uuid.uuid4()),
        "status": "available",
    }
    body.update(fields)
    return body


def full_presence() -> dict[str, Any]:
    return presence(
        task_category="other", stack_tags=TAGS, remaining_time_bucket="5_10m"
    )


def join(
    client: TestClient,
    user: ApiUser,
    body: dict[str, Any] | None = None,
    key: uuid.UUID | None = None,
) -> Any:
    return client.post(
        "/v1/queue",
        json={
            "idempotency_key": str(key or uuid.uuid4()),
            "presence": body if body is not None else full_presence(),
        },
        headers=user.headers,
    )


def entries(factory: SessionFactory, user: ApiUser) -> list[QueueEntry]:
    with factory() as s:
        return list(
            s.scalars(select(QueueEntry).where(QueueEntry.user_id == user.user_id))
        )


def block_rows(factory: SessionFactory) -> list[Block]:
    with factory() as s:
        return list(s.scalars(select(Block)))


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


# --- join ---------------------------------------------------------------------


def test_join_creates_entry_kicks_and_notifies(
    client: TestClient,
    ada: ApiUser,
    notifier: RecordingNotifier,
    matchmaker: RecordingMatchmaker,
    session_factory: SessionFactory,
) -> None:
    r = join(client, ada)
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "queued"
    assert body["expires_at"].startswith("2026-10-13T12:01:30")
    assert matchmaker.kicks == 1
    frames = notifier.frames_for(ada.user_id)
    assert len(frames) == 1 and isinstance(frames[0], WsQueueJoined)
    assert str(frames[0].entry.entry_id) == body["entry_id"]
    (row,) = entries(session_factory, ada)
    assert row.stack_tags == "python,fastapi" and row.device_id == ada.device_id
    assert row.remaining_time_bucket == "5_10m" and row.task_category == "other"


def test_join_replay_returns_same_entry_without_second_kick(
    client: TestClient,
    ada: ApiUser,
    notifier: RecordingNotifier,
    matchmaker: RecordingMatchmaker,
) -> None:
    key = uuid.uuid4()
    first = join(client, ada, key=key)
    again = join(client, ada, key=key)
    assert first.status_code == 201 and again.status_code == 200
    assert again.json()["entry_id"] == first.json()["entry_id"]
    assert matchmaker.kicks == 1
    assert len(notifier.frames_for(ada.user_id)) == 1


def test_replay_returns_entry_whatever_its_status(
    client: TestClient, ada: ApiUser, notifier: RecordingNotifier
) -> None:
    key = uuid.uuid4()
    first = join(client, ada, key=key)
    assert client.delete("/v1/queue/current", headers=ada.headers).status_code == 204
    again = join(client, ada, key=key)
    assert again.status_code == 200
    assert again.json()["entry_id"] == first.json()["entry_id"]
    assert again.json()["status"] == "cancelled"


def test_replay_keys_are_scoped_to_the_user(
    client: TestClient,
    ada: ApiUser,
    bob: ApiUser,
    notifier: RecordingNotifier,
    matchmaker: RecordingMatchmaker,
) -> None:
    key = uuid.uuid4()
    a = join(client, ada, key=key)
    b = join(client, bob, key=key)
    assert a.status_code == 201 and b.status_code == 201
    assert a.json()["entry_id"] != b.json()["entry_id"]


def test_setup_incomplete(
    client: TestClient,
    session_factory: SessionFactory,
    mailer: RecordingMailer,
    notifier: RecordingNotifier,
    matchmaker: RecordingMatchmaker,
) -> None:
    no_nick = ready_user(
        client, session_factory, mailer, "n@example.com", None, consent=ALL_CONSENT
    )
    no_consent = ready_user(
        client, session_factory, mailer, "c@example.com", "Cyrus", consent=None
    )
    for user in (no_nick, no_consent):
        r = join(client, user, presence())
        assert r.status_code == 409
        assert r.json()["error"] == "setup_incomplete"
        assert entries(session_factory, user) == []
    assert matchmaker.kicks == 0 and notifier.sent == []


@pytest.mark.parametrize(
    ("consent_off", "fields"),
    [
        ("share_task_category", {"task_category": "other"}),
        ("share_stack_tags", {"stack_tags": TAGS}),
        ("share_time_bucket", {"remaining_time_bucket": "5_10m"}),
    ],
)
def test_field_without_consent_is_rejected(
    client: TestClient,
    session_factory: SessionFactory,
    mailer: RecordingMailer,
    notifier: RecordingNotifier,
    matchmaker: RecordingMatchmaker,
    consent_off: str,
    fields: dict[str, Any],
) -> None:
    consent = ALL_CONSENT.model_copy(update={consent_off: False})
    user = ready_user(
        client, session_factory, mailer, "d@example.com", "Dee", consent=consent
    )
    r = join(client, user, presence(**fields))
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_request"
    assert entries(session_factory, user) == []
    assert matchmaker.kicks == 0


def test_no_consent_and_presence_only_stores_nulls(
    client: TestClient,
    session_factory: SessionFactory,
    mailer: RecordingMailer,
    notifier: RecordingNotifier,
    matchmaker: RecordingMatchmaker,
) -> None:
    none = Consent(
        version=1,
        share_task_category=False,
        share_stack_tags=False,
        share_time_bucket=False,
    )
    user = ready_user(
        client, session_factory, mailer, "e@example.com", "Eve", consent=none
    )
    assert join(client, user, presence()).status_code == 201
    (row,) = entries(session_factory, user)
    assert row.task_category is None
    assert row.stack_tags is None
    assert row.remaining_time_bucket is None


def test_join_while_queued_or_matched_is_409(
    client: TestClient,
    ada: ApiUser,
    bob: ApiUser,
    session_factory: SessionFactory,
    notifier: RecordingNotifier,
    matchmaker: RecordingMatchmaker,
) -> None:
    assert join(client, ada).status_code == 201
    r = join(client, ada)
    assert r.status_code == 409 and r.json()["error"] == "already_queued"
    seed_match(session_factory, bob, add_ready_user(session_factory, "Cy"))
    r = join(client, bob)
    assert r.status_code == 409 and r.json()["error"] == "already_matched"
    assert matchmaker.kicks == 1


def test_join_after_unswept_expiry_replaces_the_stale_entry(
    client: TestClient,
    ada: ApiUser,
    clock: FixedClock,
    session_factory: SessionFactory,
    notifier: RecordingNotifier,
    matchmaker: RecordingMatchmaker,
) -> None:
    first = join(client, ada)
    clock.advance(91)
    second = join(client, ada)
    assert second.status_code == 201
    assert second.json()["entry_id"] != first.json()["entry_id"]
    rows = {r.id: r for r in entries(session_factory, ada)}
    stale = rows[uuid.UUID(first.json()["entry_id"])]
    assert stale.status == "expired" and stale.presence_id is None


def test_join_is_allowed_again_after_a_match_ended(
    client: TestClient,
    ada: ApiUser,
    bob: ApiUser,
    session_factory: SessionFactory,
    notifier: RecordingNotifier,
    matchmaker: RecordingMatchmaker,
) -> None:
    match_id = seed_match(session_factory, ada, bob)
    path = f"/v1/matches/{match_id}/end"
    assert client.post(path, headers=ada.headers).status_code == 204
    assert join(client, ada).status_code == 201


def _race(client: TestClient, user: ApiUser, n: int) -> list[int]:
    barrier = threading.Barrier(n)
    codes: list[int] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        status = join(client, user).status_code
        with lock:
            codes.append(status)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return codes


def test_concurrent_joins_of_one_user_make_one_entry(
    client: TestClient,
    ada: ApiUser,
    session_factory: SessionFactory,
    notifier: RecordingNotifier,
    matchmaker: RecordingMatchmaker,
) -> None:
    codes = _race(client, ada, 2)
    assert codes.count(201) == 1
    assert set(codes) <= {201, 409}
    queued = [r for r in entries(session_factory, ada) if r.status == "queued"]
    assert len(queued) == 1
    assert matchmaker.kicks == 1


@requires_postgres
def test_many_concurrent_joins_on_postgres(
    client: TestClient,
    ada: ApiUser,
    session_factory: SessionFactory,
    notifier: RecordingNotifier,
    matchmaker: RecordingMatchmaker,
) -> None:
    codes = _race(client, ada, 8)
    assert codes.count(201) == 1
    assert set(codes) <= {201, 409}
    assert len([r for r in entries(session_factory, ada) if r.status == "queued"]) == 1


def test_rate_limit_on_the_21st_join(
    client: TestClient,
    ada: ApiUser,
    notifier: RecordingNotifier,
    matchmaker: RecordingMatchmaker,
) -> None:
    key = uuid.uuid4()
    codes = [join(client, ada, key=key).status_code for _ in range(20)]
    assert codes == [201] + [200] * 19
    r = join(client, ada, key=key)
    assert r.status_code == 429
    assert int(r.headers["Retry-After"]) >= 1
    assert r.json()["error"] == "rate_limited"


# --- leave --------------------------------------------------------------------


def test_cancel_nulls_presence_and_is_idempotent(
    client: TestClient,
    ada: ApiUser,
    clock: FixedClock,
    session_factory: SessionFactory,
    notifier: RecordingNotifier,
    matchmaker: RecordingMatchmaker,
) -> None:
    entry_id = join(client, ada).json()["entry_id"]
    notifier.clear()
    clock.advance(5)
    assert client.delete("/v1/queue/current", headers=ada.headers).status_code == 204
    (row,) = entries(session_factory, ada)
    assert row.status == "cancelled"
    assert row.ended_at == START_TIME + timedelta(seconds=5)
    assert row.presence_id is None and row.task_category is None
    assert row.stack_tags is None and row.remaining_time_bucket is None
    (frame,) = notifier.frames_for(ada.user_id)
    assert isinstance(frame, WsQueueEnded)
    assert frame.reason == "cancelled" and str(frame.entry_id) == entry_id

    notifier.clear()
    assert client.delete("/v1/queue/current", headers=ada.headers).status_code == 204
    assert notifier.sent == []
    assert [r.status for r in entries(session_factory, ada)] == ["cancelled"]


def test_cancel_with_nothing_queued_is_204(
    client: TestClient, ada: ApiUser, notifier: RecordingNotifier
) -> None:
    assert client.delete("/v1/queue/current", headers=ada.headers).status_code == 204
    assert notifier.sent == []


def test_cancel_leaves_other_users_alone(
    client: TestClient,
    ada: ApiUser,
    bob: ApiUser,
    session_factory: SessionFactory,
    notifier: RecordingNotifier,
) -> None:
    seed_queued(session_factory, bob)
    assert client.delete("/v1/queue/current", headers=ada.headers).status_code == 204
    assert [r.status for r in entries(session_factory, bob)] == ["queued"]


# --- end and block ------------------------------------------------------------


def test_end_by_non_participant_or_unknown_match_is_404(
    client: TestClient,
    ada: ApiUser,
    bob: ApiUser,
    session_factory: SessionFactory,
    notifier: RecordingNotifier,
) -> None:
    cy = add_ready_user(session_factory, "Cy")
    match_id = seed_match(session_factory, bob, cy)
    for action in ("end", "block"):
        r = client.post(f"/v1/matches/{match_id}/{action}", headers=ada.headers)
        unknown = client.post(
            f"/v1/matches/{uuid.uuid4()}/{action}", headers=ada.headers
        )
        assert r.status_code == 404 == unknown.status_code
        assert r.json() == unknown.json() == {"error": "not_found"}
    with session_factory() as s:
        match = s.get(Match, match_id)
        assert match is not None and match.status == "active"
    assert block_rows(session_factory) == [] and notifier.sent == []


def test_end_ends_for_both_without_a_block(
    client: TestClient,
    ada: ApiUser,
    bob: ApiUser,
    clock: FixedClock,
    session_factory: SessionFactory,
    notifier: RecordingNotifier,
) -> None:
    match_id = seed_match(
        session_factory, ada, bob, shared_tags=["python"], shared_category="backend"
    )
    clock.advance(30)
    r = client.post(f"/v1/matches/{match_id}/end", headers=bob.headers)
    assert r.status_code == 204
    with session_factory() as s:
        match = s.get(Match, match_id)
        assert match is not None
        assert match.status == "ended" and match.end_reason == "ended"
        assert match.ended_at == START_TIME + timedelta(seconds=30)
        assert (match.shared_tags, match.shared_category) == (None, None)
        assert [p.active for p in s.scalars(select(MatchParticipant))] == [False] * 2
    assert block_rows(session_factory) == []
    for user in (ada, bob):
        (frame,) = notifier.frames_for(user.user_id)
        assert isinstance(frame, WsMatchEnded)
        assert frame.reason == "ended" and frame.match_id == match_id

    notifier.clear()
    again = client.post(f"/v1/matches/{match_id}/end", headers=ada.headers)
    assert again.status_code == 204
    assert notifier.sent == []


def test_block_records_block_and_tells_both_users_ended(
    client: TestClient,
    ada: ApiUser,
    bob: ApiUser,
    session_factory: SessionFactory,
    notifier: RecordingNotifier,
) -> None:
    match_id = seed_match(
        session_factory, ada, bob, shared_tags=["python"], shared_category="backend"
    )
    r = client.post(f"/v1/matches/{match_id}/block", headers=ada.headers)
    assert r.status_code == 204
    (block,) = block_rows(session_factory)
    assert block.blocker_id == ada.user_id and block.blocked_id == bob.user_id
    with session_factory() as s:
        match = s.get(Match, match_id)
        assert match is not None and match.end_reason == "blocked"
        assert (match.shared_tags, match.shared_category) == (None, None)
        assert not any(p.active for p in s.scalars(select(MatchParticipant)))
    for user in (ada, bob):
        (frame,) = notifier.frames_for(user.user_id)
        assert isinstance(frame, WsMatchEnded) and frame.reason == "ended"

    # Blocking again is a no-op; the block row stays single.
    notifier.clear()
    again = client.post(f"/v1/matches/{match_id}/block", headers=ada.headers)
    assert again.status_code == 204
    assert len(block_rows(session_factory)) == 1 and notifier.sent == []


def test_block_after_end_still_records_the_block(
    client: TestClient,
    ada: ApiUser,
    bob: ApiUser,
    session_factory: SessionFactory,
    notifier: RecordingNotifier,
) -> None:
    match_id = seed_match(session_factory, ada, bob)
    ended = client.post(f"/v1/matches/{match_id}/end", headers=bob.headers)
    assert ended.status_code == 204
    notifier.clear()
    blocked = client.post(f"/v1/matches/{match_id}/block", headers=ada.headers)
    assert blocked.status_code == 204
    (block,) = block_rows(session_factory)
    assert block.blocker_id == ada.user_id and block.blocked_id == bob.user_id
    assert notifier.sent == []
    with session_factory() as s:
        match = s.get(Match, match_id)
        assert match is not None and match.end_reason == "ended"


def test_mutual_blocks_are_two_rows(
    client: TestClient,
    ada: ApiUser,
    bob: ApiUser,
    session_factory: SessionFactory,
    notifier: RecordingNotifier,
) -> None:
    match_id = seed_match(session_factory, ada, bob)
    client.post(f"/v1/matches/{match_id}/block", headers=ada.headers)
    client.post(f"/v1/matches/{match_id}/block", headers=bob.headers)
    pairs = {(b.blocker_id, b.blocked_id) for b in block_rows(session_factory)}
    assert pairs == {(ada.user_id, bob.user_id), (bob.user_id, ada.user_id)}


# --- state, auth, logs ----------------------------------------------------------


def test_state_follows_join_and_cancel(
    client: TestClient,
    ada: ApiUser,
    session_factory: SessionFactory,
    clock: FixedClock,
    notifier: RecordingNotifier,
    matchmaker: RecordingMatchmaker,
) -> None:
    join(client, ada)
    with session_factory() as s:
        entry, match = current_state(s, clock(), ada.user_id)
    assert entry is not None and entry.status == "queued" and match is None
    client.delete("/v1/queue/current", headers=ada.headers)
    with session_factory() as s:
        assert current_state(s, clock(), ada.user_id) == (None, None)


def test_every_route_requires_authentication(client: TestClient) -> None:
    mid = uuid.uuid4()
    calls = [
        client.post(
            "/v1/queue",
            json={"idempotency_key": str(uuid.uuid4()), "presence": presence()},
        ),
        client.delete("/v1/queue/current"),
        client.get("/v1/queue/current"),
        client.post(f"/v1/matches/{mid}/end"),
        client.post(f"/v1/matches/{mid}/block"),
    ]
    assert [c.status_code for c in calls] == [401] * 5
    bad = client.delete("/v1/queue/current", headers={"Authorization": "Bearer bra_x"})
    assert bad.status_code == 401


def test_logs_hold_no_presence_nickname_or_bucket(
    client: TestClient,
    ada: ApiUser,
    bob: ApiUser,
    session_factory: SessionFactory,
    notifier: RecordingNotifier,
    matchmaker: RecordingMatchmaker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    body = presence(
        task_category="backend",
        stack_tags=["rust", "django"],
        remaining_time_bucket="10_20m",
    )
    join(client, ada, body)
    match_id = seed_match(session_factory, ada, bob)
    client.delete("/v1/queue/current", headers=ada.headers)
    client.post(f"/v1/matches/{match_id}/block", headers=ada.headers)
    client.post(f"/v1/matches/{uuid.uuid4()}/end", headers=ada.headers)
    secrets = ("rust", "django", "backend", "10_20m", "Ada", "Bob")
    for secret in (*secrets, body["presence_id"]):
        assert secret not in caplog.text


# --- races with the matcher (review findings R1, R3, R4, N2) ----------------------


def _pass_before_first_update(session: Session, action: Any) -> None:
    """Run `action` once, right before the session's first UPDATE statement."""
    fired = {"done": False}

    @event.listens_for(session, "do_orm_execute")
    def _interleave(state: Any) -> None:
        if state.is_update and not fired["done"]:
            fired["done"] = True
            action()


def _two_queued(
    factory: SessionFactory, now: Any, ttl_s: int = 90
) -> tuple[Any, Any, uuid.UUID, uuid.UUID]:
    ada = add_ready_user(factory, "Ada")
    bob = add_ready_user(factory, "Bob")
    a = seed_queued(factory, ada, now=now, ttl_s=ttl_s)
    b = seed_queued(factory, bob, now=now, ttl_s=ttl_s)
    return ada, bob, a, b


def test_cancel_racing_a_matcher_pass_keeps_the_match(
    session_factory: SessionFactory, server_config: ServerConfig, clock: FixedClock
) -> None:
    ada, bob, _a, victim = _two_queued(session_factory, clock.now)
    notifier = RecordingNotifier({ada.user_id, bob.user_id})
    mm = Matchmaker(session_factory, server_config, clock, notifier)
    with session_factory() as s:
        _pass_before_first_update(s, mm.run_once)
        cancelled = queue.leave_queue(s, clock.now, bob.user_id)
    with session_factory() as s:
        entry = s.get_one(QueueEntry, victim)
        active = s.scalars(select(Match).where(Match.status == "active")).all()
    assert len(active) == 1
    assert cancelled is None
    assert entry.status == "matched" and entry.match_id == active[0].id


def test_join_inline_expiry_racing_a_matcher_pass_answers_already_matched(
    session_factory: SessionFactory, server_config: ServerConfig, clock: FixedClock
) -> None:
    ada, bob, old, _b = _two_queued(session_factory, clock.now)
    notifier = RecordingNotifier({ada.user_id, bob.user_id})
    pass_time = clock.now + timedelta(seconds=89)  # the pass began before the TTL
    mm = Matchmaker(session_factory, server_config, lambda: pass_time, notifier)
    body = QueueJoinRequest(
        idempotency_key=uuid.uuid4(),
        presence=OutboundPresence(
            schema_version=1, presence_id=uuid.uuid4(), status="available"
        ),
    )
    with session_factory() as s:
        _pass_before_first_update(s, mm.run_once)
        with pytest.raises(ApiError) as raised:
            queue.join_queue(
                s,
                server_config,
                clock.now + timedelta(seconds=90),
                ada.user_id,
                ada.device_id,
                body,
            )
    assert raised.value.code == "already_matched"
    with session_factory() as s:
        rows = list(
            s.scalars(select(QueueEntry).where(QueueEntry.user_id == ada.user_id))
        )
        in_match = s.scalars(
            select(MatchParticipant).where(
                MatchParticipant.user_id == ada.user_id,
                MatchParticipant.active.is_(True),
            )
        ).all()
    assert len(in_match) == 1
    assert [(r.id, r.status) for r in rows] == [(old, "matched")]


def test_end_racing_a_matcher_expiry_keeps_the_first_end(
    session_factory: SessionFactory, clock: FixedClock
) -> None:
    ada = add_ready_user(session_factory, "Ada")
    bob = add_ready_user(session_factory, "Bob")
    match_id = seed_match(session_factory, ada, bob, shared_tags=["python"])

    def other_end() -> None:
        with session_factory() as other:
            assert queue.end_match(
                other, clock.now, bob.user_id, match_id, block=False
            ) == (ada.user_id)

    with session_factory() as s:
        _pass_before_first_update(s, other_end)
        peer = queue.end_match(
            s, clock.now + timedelta(seconds=5), ada.user_id, match_id, block=True
        )
    assert peer is None  # the other end won: no second match.ended
    with session_factory() as s:
        match = s.get_one(Match, match_id)
        assert match.end_reason == "ended" and match.ended_at == clock.now
    # The block is still recorded for a participant of an ended match.
    assert [(b.blocker_id, b.blocked_id) for b in block_rows(session_factory)] == [
        (ada.user_id, bob.user_id)
    ]


# --- GET /v1/queue/current --------------------------------------------------------


def test_snapshot_is_empty_then_shows_the_entry_then_the_match(
    client: TestClient,
    ada: ApiUser,
    bob: ApiUser,
    session_factory: SessionFactory,
    notifier: RecordingNotifier,
    matchmaker: RecordingMatchmaker,
) -> None:
    empty = client.get("/v1/queue/current", headers=ada.headers)
    assert empty.status_code == 200
    assert empty.json() == {"queue": None, "match": None}

    entry_id = join(client, ada).json()["entry_id"]
    queued = client.get("/v1/queue/current", headers=ada.headers).json()
    assert queued["match"] is None
    assert queued["queue"]["entry_id"] == entry_id
    assert queued["queue"]["status"] == "queued"
    assert client.get("/v1/queue/current", headers=bob.headers).json() == {
        "queue": None,
        "match": None,
    }

    client.delete("/v1/queue/current", headers=ada.headers)
    match_id = seed_match(session_factory, ada, bob, shared_tags=["python", "fastapi"])
    for me, peer_name in ((ada, "Bob"), (bob, "Ada")):
        snap = client.get("/v1/queue/current", headers=me.headers).json()
        assert snap["queue"] is None
        assert snap["match"]["match_id"] == str(match_id)
        assert snap["match"]["peer_nickname"] == peer_name
        assert snap["match"]["shared_tags"] == ["python", "fastapi"]


def test_snapshot_never_shows_another_users_state(
    client: TestClient,
    ada: ApiUser,
    bob: ApiUser,
    session_factory: SessionFactory,
    notifier: RecordingNotifier,
) -> None:
    cy = add_ready_user(session_factory, "Cyrus")
    dee = add_ready_user(session_factory, "Dee")
    seed_queued(session_factory, bob)
    seed_match(session_factory, cy, dee)
    snap = client.get("/v1/queue/current", headers=ada.headers)
    assert snap.json() == {"queue": None, "match": None}


def test_join_racing_the_expiry_sweep_still_succeeds(
    session_factory: SessionFactory, server_config: ServerConfig, clock: FixedClock
) -> None:
    ada = add_ready_user(session_factory, "Ada")
    notifier = RecordingNotifier({ada.user_id})
    old = seed_queued(session_factory, ada, now=clock.now, ttl_s=90)
    join_time = clock.now + timedelta(seconds=90)
    mm = Matchmaker(session_factory, server_config, lambda: join_time, notifier)
    body = QueueJoinRequest(
        idempotency_key=uuid.uuid4(),
        presence=OutboundPresence(
            schema_version=1, presence_id=uuid.uuid4(), status="available"
        ),
    )
    with session_factory() as s:
        # The sweep expires the stale entry between our read and our UPDATE.
        _pass_before_first_update(s, mm.run_once)
        view, created = queue.join_queue(
            s, server_config, join_time, ada.user_id, ada.device_id, body
        )
    assert created and view.status == "queued"
    with session_factory() as s:
        rows = {
            r.id: r.status
            for r in s.scalars(
                select(QueueEntry).where(QueueEntry.user_id == ada.user_id)
            )
        }
    assert rows[old] == "expired"
    assert sorted(rows.values()) == ["expired", "queued"]

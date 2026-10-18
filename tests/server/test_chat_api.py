"""M5-02: chat HTTP API, messages and extensions (real handlers, recording fakes)."""

import threading
import uuid
from datetime import timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, event, select

from beebreak_server import chat
from beebreak_server.config import ServerConfig
from beebreak_server.db import SessionFactory
from beebreak_server.mailer import RecordingMailer
from beebreak_server.models import Match, MatchParticipant, Message
from beebreak_server.ratelimit import RateLimiter
from beebreak_shared.api import (
    MAX_SERVER_FRAME_BYTES,
    SendMessageRequest,
    WsChatMessage,
    WsChatStatus,
)
from tests.server.conftest import (
    START_TIME,
    TEST_SECRET,
    ApiUser,
    FixedClock,
    ready_user,
    requires_postgres,
    seed_match,
    seed_message,
)
from tests.server.fakes import RecordingNotifier

CANARY = "canary-body-6d1f"


@pytest.fixture(autouse=True)
def _no_body_in_logs(caplog: pytest.LogCaptureFixture) -> Any:
    """Rule 16: no message body in any log line, whatever the test did."""
    yield
    assert CANARY not in caplog.text


@pytest.fixture
def server_config(database_url: str) -> ServerConfig:
    """Access tokens outlive the clock jumps these tests make."""
    return ServerConfig(
        database_url=database_url, secret=TEST_SECRET, access_ttl_s=200_000
    )


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


@pytest.fixture
def carol(
    client: TestClient, session_factory: SessionFactory, mailer: RecordingMailer
) -> ApiUser:
    return ready_user(client, session_factory, mailer, "carol@example.com", "Carol")


@pytest.fixture
def match_id(session_factory: SessionFactory, ada: ApiUser, bob: ApiUser) -> uuid.UUID:
    return seed_match(session_factory, ada, bob)


def send(
    client: TestClient,
    user: ApiUser,
    match: uuid.UUID,
    body: str = f"hello {CANARY}",
    cid: uuid.UUID | None = None,
) -> Any:
    return client.post(
        f"/v1/matches/{match}/messages",
        json={"client_message_id": str(cid or uuid.uuid4()), "body": body},
        headers=user.headers,
    )


def listing(client: TestClient, user: ApiUser, match: uuid.UUID, **params: Any) -> Any:
    return client.get(
        f"/v1/matches/{match}/messages", params=params, headers=user.headers
    )


def ext(client: TestClient, user: ApiUser, match: uuid.UUID, action: str) -> Any:
    return client.post(
        f"/v1/matches/{match}/extension", json={"action": action}, headers=user.headers
    )


def match_row(factory: SessionFactory, match: uuid.UUID) -> Match:
    with factory() as s:
        row = s.get(Match, match)
        assert row is not None
        return row


def messages_sent(factory: SessionFactory, match: uuid.UUID, user: ApiUser) -> int:
    with factory() as s:
        row = s.get(MatchParticipant, (match, user.user_id))
        assert row is not None
        return row.messages_sent


def end_in_db(
    factory: SessionFactory, match: uuid.UUID, *, at: Any = START_TIME
) -> None:
    with factory() as s:
        row = s.get(Match, match)
        assert row is not None
        row.status = "ended"
        row.ended_at = at
        row.end_reason = "ended"
        s.commit()


def unlimited(app: FastAPI, clock: FixedClock) -> None:
    app.state.limiters.message_user = RateLimiter(10_000, 10, clock)


# --- send ---------------------------------------------------------------------


def test_send_happy_path_and_replay(
    client: TestClient,
    ada: ApiUser,
    bob: ApiUser,
    match_id: uuid.UUID,
    notifier: RecordingNotifier,
    session_factory: SessionFactory,
) -> None:
    cid = uuid.uuid4()
    r = send(client, ada, match_id, cid=cid)
    assert r.status_code == 201
    view = r.json()
    assert view["seq"] == 1 and view["mine"] is True
    assert view["body"] == f"hello {CANARY}"
    assert view["client_message_id"] == str(cid)

    (mine,) = notifier.frames_for(ada.user_id)
    (theirs,) = notifier.frames_for(bob.user_id)
    assert isinstance(mine, WsChatMessage) and isinstance(theirs, WsChatMessage)
    assert mine.message.mine is True and mine.message.client_message_id == cid
    assert theirs.message.mine is False and theirs.message.client_message_id is None
    assert mine.message.seq == theirs.message.seq == 1
    assert mine.match_id == match_id

    notifier.clear()
    again = send(client, ada, match_id, cid=cid)
    assert again.status_code == 200
    assert again.json() == view
    assert notifier.sent == []
    assert messages_sent(session_factory, match_id, ada) == 1
    assert messages_sent(session_factory, match_id, bob) == 0
    assert match_row(session_factory, match_id).last_seq == 1


def test_replay_ignores_the_body_and_the_match_state(
    client: TestClient,
    ada: ApiUser,
    match_id: uuid.UUID,
    notifier: RecordingNotifier,
    session_factory: SessionFactory,
) -> None:
    cid = uuid.uuid4()
    first = send(client, ada, match_id, cid=cid).json()
    end_in_db(session_factory, match_id)
    r = send(client, ada, match_id, body="something else", cid=cid)
    assert r.status_code == 200 and r.json() == first


def test_replay_for_a_different_match_is_404(
    client: TestClient,
    ada: ApiUser,
    bob: ApiUser,
    carol: ApiUser,
    match_id: uuid.UUID,
    notifier: RecordingNotifier,
    session_factory: SessionFactory,
) -> None:
    other = seed_match(
        session_factory,
        ada,
        carol,
        status="ended",
        ended_at=START_TIME,
        end_reason="ended",
    )
    cid = uuid.uuid4()
    assert send(client, ada, match_id, cid=cid).status_code == 201
    r = send(client, ada, other, cid=cid)
    assert r.status_code == 404 and r.json() == {"error": "not_found"}


def test_two_users_use_the_same_client_message_id_independently(
    client: TestClient,
    ada: ApiUser,
    bob: ApiUser,
    match_id: uuid.UUID,
    notifier: RecordingNotifier,
) -> None:
    cid = uuid.uuid4()
    assert send(client, ada, match_id, cid=cid).json()["seq"] == 1
    r = send(client, bob, match_id, cid=cid)
    assert r.status_code == 201 and r.json()["seq"] == 2


def test_sequence_follows_last_seq(
    client: TestClient,
    ada: ApiUser,
    bob: ApiUser,
    session_factory: SessionFactory,
    notifier: RecordingNotifier,
) -> None:
    match = seed_match(session_factory, ada, bob)
    seed_message(session_factory, match, bob, seq=1, body="x")
    seed_message(session_factory, match, bob, seq=2, body="y")
    assert send(client, ada, match).json()["seq"] == 3


# --- normalization --------------------------------------------------------------


def test_control_and_bidi_characters_are_removed(
    client: TestClient,
    ada: ApiUser,
    match_id: uuid.UUID,
    notifier: RecordingNotifier,
) -> None:
    raw = f"  a{chr(0)}b{chr(0x202E)}c{chr(7)}d\ne{chr(0x2066)}  "
    r = send(client, ada, match_id, body=raw)
    assert r.status_code == 201
    assert r.json()["body"] == "abcd\ne"


def test_length_limits(
    client: TestClient,
    ada: ApiUser,
    match_id: uuid.UUID,
    notifier: RecordingNotifier,
    session_factory: SessionFactory,
) -> None:
    assert send(client, ada, match_id, body="x" * 1000).status_code == 201
    r = send(client, ada, match_id, body="x" * 1001)
    assert r.status_code == 400 and r.json() == {"error": "invalid_request"}
    assert match_row(session_factory, match_id).last_seq == 1


def test_whitespace_only_is_400(
    client: TestClient,
    ada: ApiUser,
    match_id: uuid.UUID,
    notifier: RecordingNotifier,
    session_factory: SessionFactory,
) -> None:
    for body in ("", "   ", f"\n\t{chr(0x200F)} \n"):
        r = send(client, ada, match_id, body=body)
        assert r.status_code == 400 and r.json() == {"error": "invalid_request"}
    assert match_row(session_factory, match_id).last_seq == 0
    assert notifier.sent == []


def test_four_byte_message_frame_fits_the_server_limit(
    client: TestClient,
    ada: ApiUser,
    bob: ApiUser,
    match_id: uuid.UUID,
    notifier: RecordingNotifier,
) -> None:
    assert send(client, ada, match_id, body="\U0001f600" * 1000).status_code == 201
    for user in (ada, bob):
        (frame,) = notifier.frames_for(user.user_id)
        assert len(frame.model_dump_json().encode()) < MAX_SERVER_FRAME_BYTES


# --- authorization --------------------------------------------------------------


def test_non_participant_gets_the_same_404_as_an_unknown_match(
    client: TestClient,
    ada: ApiUser,
    carol: ApiUser,
    match_id: uuid.UUID,
    notifier: RecordingNotifier,
) -> None:
    send(client, ada, match_id)
    unknown = uuid.uuid4()
    for target in (match_id, unknown):
        sent = send(client, carol, target)
        listed = listing(client, carol, target)
        for r in (sent, listed, ext(client, carol, target, "request")):
            assert r.status_code == 404
            assert r.json() == {"error": "not_found"}
    assert [m for _, m in notifier.sent if isinstance(m, WsChatStatus)] == []
    # Nothing leaked to the participants' state either.
    assert listing(client, ada, match_id).json()["last_seq"] == 1


def test_every_route_needs_a_token(
    client: TestClient, match_id: uuid.UUID, notifier: RecordingNotifier
) -> None:
    cid = str(uuid.uuid4())
    responses = [
        client.post(
            f"/v1/matches/{match_id}/messages",
            json={"client_message_id": cid, "body": "hi"},
        ),
        client.get(f"/v1/matches/{match_id}/messages"),
        client.post(f"/v1/matches/{match_id}/extension", json={"action": "request"}),
    ]
    assert [r.status_code for r in responses] == [401, 401, 401]


# --- ended matches --------------------------------------------------------------


def test_ended_match_refuses_sends_but_lists(
    client: TestClient,
    ada: ApiUser,
    bob: ApiUser,
    clock: FixedClock,
    notifier: RecordingNotifier,
    session_factory: SessionFactory,
    server_config: ServerConfig,
) -> None:
    match = seed_match(session_factory, ada, bob)
    seed_message(session_factory, match, bob, seq=1, body=f"before {CANARY}")
    clock.advance(60)
    end_in_db(session_factory, match, at=clock.now)

    r = send(client, ada, match)
    assert r.status_code == 409 and r.json() == {"error": "chat_ended"}
    assert notifier.sent == []
    page = listing(client, ada, match).json()
    assert [m["seq"] for m in page["messages"]] == [1]

    clock.advance(server_config.message_retention_s)  # exactly at the limit
    assert listing(client, bob, match).status_code == 200
    clock.advance(1)
    for user in (ada, bob):
        r = listing(client, user, match)
        assert r.status_code == 404 and r.json() == {"error": "not_found"}


def test_send_at_or_after_ends_at_is_409(
    client: TestClient,
    ada: ApiUser,
    match_id: uuid.UUID,
    clock: FixedClock,
    notifier: RecordingNotifier,
    session_factory: SessionFactory,
) -> None:
    clock.advance(899)
    assert send(client, ada, match_id).status_code == 201
    clock.advance(1)  # the match is still 'active' in the database
    assert match_row(session_factory, match_id).status == "active"
    r = send(client, ada, match_id)
    assert r.status_code == 409 and r.json() == {"error": "chat_ended"}
    assert match_row(session_factory, match_id).last_seq == 1


# --- list -----------------------------------------------------------------------


def test_list_is_per_viewer(
    client: TestClient,
    ada: ApiUser,
    bob: ApiUser,
    match_id: uuid.UUID,
    notifier: RecordingNotifier,
) -> None:
    cid = uuid.uuid4()
    send(client, ada, match_id, cid=cid)
    send(client, bob, match_id)
    a = listing(client, ada, match_id).json()
    b = listing(client, bob, match_id).json()
    assert a["last_seq"] == b["last_seq"] == 2
    assert [m["mine"] for m in a["messages"]] == [True, False]
    assert [m["mine"] for m in b["messages"]] == [False, True]
    assert a["messages"][0]["client_message_id"] == str(cid)
    assert a["messages"][1]["client_message_id"] is None
    assert b["messages"][0]["client_message_id"] is None
    assert b["messages"][1]["client_message_id"] is not None
    assert listing(client, ada, match_id, after_seq=2).json() == {
        "messages": [],
        "last_seq": 2,
    }


def test_list_pages_of_100(
    client: TestClient,
    ada: ApiUser,
    bob: ApiUser,
    session_factory: SessionFactory,
) -> None:
    match = seed_match(session_factory, ada, bob)
    for seq in range(1, 151):
        seed_message(
            session_factory, match, ada if seq % 2 else bob, seq=seq, body=f"m{seq}"
        )
    first = listing(client, ada, match, after_seq=0).json()
    assert [m["seq"] for m in first["messages"]] == list(range(1, 101))
    assert first["last_seq"] == 150
    second = listing(client, ada, match, after_seq=100).json()
    assert [m["seq"] for m in second["messages"]] == list(range(101, 151))
    default = listing(client, ada, match).json()
    assert len(default["messages"]) == 100


def test_negative_after_seq_is_400(
    client: TestClient, ada: ApiUser, match_id: uuid.UUID
) -> None:
    r = listing(client, ada, match_id, after_seq=-1)
    assert r.status_code == 400 and r.json() == {"error": "invalid_request"}


# --- rate limit -----------------------------------------------------------------


def test_21st_send_in_10_seconds_is_429_before_the_database(
    client: TestClient,
    ada: ApiUser,
    match_id: uuid.UUID,
    engine: Engine,
    notifier: RecordingNotifier,
) -> None:
    for _ in range(20):
        assert send(client, ada, match_id).status_code == 201
    statements: list[str] = []

    def record(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        r = send(client, ada, match_id)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert r.status_code == 429
    assert int(r.headers["Retry-After"]) >= 1
    assert r.json() == {"error": "rate_limited"}
    # Only authentication ran: nothing touched matches or messages.
    assert not [s for s in statements if "matches" in s or "messages" in s]


def test_rate_limit_is_per_user_and_recovers(
    client: TestClient,
    ada: ApiUser,
    bob: ApiUser,
    match_id: uuid.UUID,
    clock: FixedClock,
    notifier: RecordingNotifier,
) -> None:
    for _ in range(20):
        send(client, ada, match_id)
    assert send(client, ada, match_id).status_code == 429
    assert send(client, bob, match_id).status_code == 201
    clock.advance(11)
    assert send(client, ada, match_id).status_code == 201


def test_extension_calls_past_the_limit_are_429_and_per_user(
    client: TestClient,
    ada: ApiUser,
    bob: ApiUser,
    match_id: uuid.UUID,
    server_config: ServerConfig,
    notifier: RecordingNotifier,
) -> None:
    for _ in range(server_config.rate_extension_user):
        assert ext(client, ada, match_id, "request").status_code != 429
    r = ext(client, ada, match_id, "decline")
    assert r.status_code == 429
    assert int(r.headers["Retry-After"]) >= 1
    assert ext(client, bob, match_id, "decline").status_code != 429


# --- concurrency ----------------------------------------------------------------


def _run(workers: list[Any]) -> None:
    barrier = threading.Barrier(len(workers))

    def wrap(fn: Any) -> Any:
        def go() -> None:
            barrier.wait()
            fn()

        return go

    threads = [threading.Thread(target=wrap(fn)) for fn in workers]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)


def test_50_concurrent_sends_get_seqs_1_to_50(
    app: FastAPI,
    client: TestClient,
    clock: FixedClock,
    ada: ApiUser,
    bob: ApiUser,
    match_id: uuid.UUID,
    notifier: RecordingNotifier,
    session_factory: SessionFactory,
) -> None:
    unlimited(app, clock)
    codes: list[int] = []
    lock = threading.Lock()

    def sender(user: ApiUser) -> Any:
        def go() -> None:
            code = send(client, user, match_id).status_code
            with lock:
                codes.append(code)

        return go

    _run([sender(ada if i % 2 else bob) for i in range(50)])
    assert codes == [201] * 50
    with session_factory() as s:
        seqs = sorted(
            s.scalars(select(Message.seq).where(Message.match_id == match_id))
        )
    assert seqs == list(range(1, 51))
    assert match_row(session_factory, match_id).last_seq == 50
    assert (
        messages_sent(session_factory, match_id, ada)
        + messages_sent(session_factory, match_id, bob)
        == 50
    )


def test_concurrent_duplicate_sends_store_one_message(
    app: FastAPI,
    client: TestClient,
    clock: FixedClock,
    ada: ApiUser,
    match_id: uuid.UUID,
    notifier: RecordingNotifier,
    session_factory: SessionFactory,
) -> None:
    unlimited(app, clock)
    cid = uuid.uuid4()
    codes: list[int] = []
    lock = threading.Lock()

    def go() -> None:
        code = send(client, ada, match_id, cid=cid).status_code
        with lock:
            codes.append(code)

    _run([go] * 6)
    assert sorted(codes) == [200] * 5 + [201]
    assert match_row(session_factory, match_id).last_seq == 1
    assert messages_sent(session_factory, match_id, ada) == 1
    assert len(notifier.frames_for(ada.user_id)) == 1


def test_losing_the_unique_key_returns_the_replay_not_a_500(
    ada: ApiUser,
    bob: ApiUser,
    match_id: uuid.UUID,
    server_config: ServerConfig,
    session_factory: SessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The replay lookup finds nothing (the other request has not committed yet),
    the insert then loses on the unique key: rollback, then the stored answer."""
    cid = seed_message(session_factory, match_id, ada, seq=1, body=CANARY)
    real = chat._replay
    calls: list[int] = []

    def blind_once(*args: Any) -> Any:
        calls.append(1)
        return None if len(calls) == 1 else real(*args)

    monkeypatch.setattr(chat, "_replay", blind_once)
    with session_factory() as s:
        view, created, peer = chat.send_message(
            s,
            server_config,
            START_TIME,
            ada.user_id,
            match_id,
            SendMessageRequest(client_message_id=cid, body="again"),
        )
    assert created is False and peer == bob.user_id
    assert view.seq == 1 and view.body == CANARY
    assert match_row(session_factory, match_id).last_seq == 1  # no gap, no bump
    assert messages_sent(session_factory, match_id, ada) == 1


# --- extensions -----------------------------------------------------------------


def statuses(notifier: RecordingNotifier, user: ApiUser) -> list[WsChatStatus]:
    return [f for f in notifier.frames_for(user.user_id) if isinstance(f, WsChatStatus)]


def test_request_then_accept_extends_by_600_seconds(
    client: TestClient,
    ada: ApiUser,
    bob: ApiUser,
    match_id: uuid.UUID,
    notifier: RecordingNotifier,
    session_factory: SessionFactory,
) -> None:
    base = START_TIME + timedelta(seconds=900)
    r = ext(client, ada, match_id, "request")
    assert r.status_code == 200 and r.json()["extension"] == "requested_by_me"
    (a_frame,) = statuses(notifier, ada)
    (b_frame,) = statuses(notifier, bob)
    assert a_frame.change == b_frame.change == "extension_requested"
    assert a_frame.status.extension == "requested_by_me"
    assert b_frame.status.extension == "requested_by_peer"
    assert match_row(session_factory, match_id).ends_at == base

    notifier.clear()
    r = ext(client, bob, match_id, "accept")
    body = r.json()
    assert r.status_code == 200
    assert body["extension"] == "none" and body["extensions_left"] == 1
    assert body["extension_s"] == 600
    assert body["ends_at"].startswith("2026-10-13T12:25:00")
    row = match_row(session_factory, match_id)
    assert row.ends_at == base + timedelta(seconds=600)
    assert row.extensions_used == 1 and row.extension_requested_by is None
    (a_frame,) = statuses(notifier, ada)
    (b_frame,) = statuses(notifier, bob)
    assert a_frame.change == b_frame.change == "extension_accepted"
    assert a_frame.status.extensions_left == b_frame.status.extensions_left == 1
    assert a_frame.status.ends_at == b_frame.status.ends_at == row.ends_at


def test_third_extension_is_refused(
    client: TestClient,
    ada: ApiUser,
    bob: ApiUser,
    notifier: RecordingNotifier,
    session_factory: SessionFactory,
) -> None:
    match = seed_match(session_factory, ada, bob)
    for asker, answerer in ((ada, bob), (bob, ada)):
        assert ext(client, asker, match, "request").status_code == 200
        assert ext(client, answerer, match, "accept").status_code == 200
    row = match_row(session_factory, match)
    assert row.extensions_used == 2
    assert row.ends_at == START_TIME + timedelta(seconds=900 + 1200)
    notifier.clear()
    r = ext(client, ada, match, "request")
    assert r.status_code == 409 and r.json() == {"error": "no_extensions_left"}
    assert notifier.sent == []


def test_accept_with_nothing_pending_is_409(
    client: TestClient,
    ada: ApiUser,
    bob: ApiUser,
    match_id: uuid.UUID,
    notifier: RecordingNotifier,
    session_factory: SessionFactory,
) -> None:
    r = ext(client, bob, match_id, "accept")
    assert r.status_code == 409 and r.json() == {"error": "no_extension_request"}
    # Accepting your own request is not allowed either.
    ext(client, ada, match_id, "request")
    r = ext(client, ada, match_id, "accept")
    assert r.status_code == 409 and r.json() == {"error": "no_extension_request"}
    assert match_row(session_factory, match_id).extensions_used == 0


def test_both_request_extends_once(
    client: TestClient,
    ada: ApiUser,
    bob: ApiUser,
    match_id: uuid.UUID,
    notifier: RecordingNotifier,
    session_factory: SessionFactory,
) -> None:
    ext(client, ada, match_id, "request")
    notifier.clear()
    r = ext(client, bob, match_id, "request")
    assert r.status_code == 200 and r.json()["extensions_left"] == 1
    row = match_row(session_factory, match_id)
    assert row.extensions_used == 1
    assert row.ends_at == START_TIME + timedelta(seconds=1500)
    assert [f.change for f in statuses(notifier, ada) + statuses(notifier, bob)] == [
        "extension_accepted"
    ] * 2


def test_repeated_request_and_idle_decline_are_no_ops(
    client: TestClient,
    ada: ApiUser,
    bob: ApiUser,
    match_id: uuid.UUID,
    notifier: RecordingNotifier,
) -> None:
    assert ext(client, bob, match_id, "decline").status_code == 200
    assert notifier.sent == []
    ext(client, ada, match_id, "request")
    notifier.clear()
    r = ext(client, ada, match_id, "request")
    assert r.status_code == 200 and r.json()["extension"] == "requested_by_me"
    assert notifier.sent == []


def test_decline_and_withdraw_are_both_declined(
    client: TestClient,
    ada: ApiUser,
    bob: ApiUser,
    match_id: uuid.UUID,
    notifier: RecordingNotifier,
    session_factory: SessionFactory,
) -> None:
    ext(client, ada, match_id, "request")
    notifier.clear()
    r = ext(client, bob, match_id, "decline")
    assert r.status_code == 200 and r.json()["extension"] == "none"
    assert [f.change for f in statuses(notifier, ada) + statuses(notifier, bob)] == [
        "extension_declined"
    ] * 2

    ext(client, ada, match_id, "request")
    notifier.clear()
    r = ext(client, ada, match_id, "decline")  # the requester withdraws
    assert r.status_code == 200 and r.json()["extension"] == "none"
    assert [f.change for f in statuses(notifier, ada) + statuses(notifier, bob)] == [
        "extension_declined"
    ] * 2
    row = match_row(session_factory, match_id)
    assert row.extensions_used == 0 and row.extension_requested_by is None


def test_extension_at_ends_at_is_409(
    client: TestClient,
    ada: ApiUser,
    bob: ApiUser,
    match_id: uuid.UUID,
    clock: FixedClock,
    notifier: RecordingNotifier,
    session_factory: SessionFactory,
) -> None:
    ext(client, ada, match_id, "request")
    clock.advance(900)  # exactly ends_at; the sweep has not run
    notifier.clear()
    r = ext(client, bob, match_id, "accept")
    assert r.status_code == 409 and r.json() == {"error": "chat_ended"}
    for action in ("request", "decline"):
        assert ext(client, ada, match_id, action).status_code == 409
    row = match_row(session_factory, match_id)
    assert row.extensions_used == 0
    assert row.ends_at == START_TIME + timedelta(seconds=900)
    assert notifier.sent == []


def test_extension_on_an_ended_match_is_409(
    client: TestClient,
    ada: ApiUser,
    match_id: uuid.UUID,
    notifier: RecordingNotifier,
    session_factory: SessionFactory,
) -> None:
    end_in_db(session_factory, match_id)
    r = ext(client, ada, match_id, "request")
    assert r.status_code == 409 and r.json() == {"error": "chat_ended"}


def test_two_concurrent_accepts_extend_once(
    client: TestClient,
    ada: ApiUser,
    bob: ApiUser,
    session_factory: SessionFactory,
    notifier: RecordingNotifier,
) -> None:
    match = seed_match(session_factory, ada, bob, extension_requested_by=ada)
    codes: list[int] = []
    lock = threading.Lock()

    def go() -> None:
        code = ext(client, bob, match, "accept").status_code
        with lock:
            codes.append(code)

    _run([go, go])
    assert sorted(codes) == [200, 409]
    row = match_row(session_factory, match)
    assert row.extensions_used == 1
    assert row.ends_at == START_TIME + timedelta(seconds=1500)
    assert len(statuses(notifier, ada)) == 1


@requires_postgres
def test_many_concurrent_accepts_and_requests_extend_once_on_postgres(
    client: TestClient,
    ada: ApiUser,
    bob: ApiUser,
    session_factory: SessionFactory,
    notifier: RecordingNotifier,
) -> None:
    match = seed_match(session_factory, ada, bob, extension_requested_by=ada)
    codes: list[int] = []
    lock = threading.Lock()

    def accept() -> None:
        code = ext(client, bob, match, "accept").status_code
        with lock:
            codes.append(code)

    def request() -> None:
        code = ext(client, bob, match, "request").status_code
        with lock:
            codes.append(code)

    _run([accept, request, accept, request, accept, request])
    # Whatever the interleaving, the one pending request was granted once.
    assert match_row(session_factory, match).extensions_used == 1
    assert codes.count(500) == 0

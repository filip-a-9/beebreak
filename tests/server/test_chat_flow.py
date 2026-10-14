"""M5 server chat flow: the real app over HTTP, the sweep on an injected clock.

Rule 15: no sleeps and no real sockets. `RecordingNotifier.connected` stands in for
live WebSockets; `matchmaker.run_once()` is called by hand; the clock jumps.
"""

import dataclasses
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from breakroom_server.config import ServerConfig
from breakroom_server.db import SessionFactory
from breakroom_server.mailer import RecordingMailer
from breakroom_server.matchmaking import Matchmaker
from breakroom_server.models import (
    Block,
    Match,
    MatchParticipant,
    Message,
    Report,
    ReportMessage,
)
from breakroom_shared.api import WsChatMessage, WsChatStatus, WsMatchEnded
from tests.server.conftest import (
    TEST_SECRET,
    ApiUser,
    FixedClock,
    ready_user,
)
from tests.server.fakes import RecordingNotifier

HOLD = 60
NICK_A, NICK_B = "Zebulon", "Quixote"
BODY = "CANARY-BODY"
NOTE = "CANARY-NOTE-4417"
DAY = 86_400


@pytest.fixture
def server_config(database_url: str) -> ServerConfig:
    # The clock jumps by months; tokens must outlive it. Rate limits are not
    # under test here.
    return ServerConfig(
        database_url=database_url,
        secret=TEST_SECRET,
        access_ttl_s=10 * 365 * DAY,
        rate_message_user=10_000,
        rate_queue_user=10_000,
    )


@dataclasses.dataclass
class Flow:
    app: FastAPI
    client: TestClient
    factory: SessionFactory
    config: ServerConfig
    clock: FixedClock
    notifier: RecordingNotifier
    a: ApiUser
    b: ApiUser
    match_id: uuid.UUID

    def run(self) -> None:
        self.app.state.matchmaker.run_once()

    def send(self, user: ApiUser, body: str) -> Any:
        return self.client.post(
            f"/v1/matches/{self.match_id}/messages",
            json={"client_message_id": str(uuid.uuid4()), "body": body},
            headers=user.headers,
        )

    def fetch(self, user: ApiUser, after_seq: int = 0) -> Any:
        return self.client.get(
            f"/v1/matches/{self.match_id}/messages",
            params={"after_seq": after_seq},
            headers=user.headers,
        )

    def extension(self, user: ApiUser, action: str) -> Any:
        return self.client.post(
            f"/v1/matches/{self.match_id}/extension",
            json={"action": action},
            headers=user.headers,
        )

    def match(self) -> Match:
        with self.factory() as s:
            return s.get_one(Match, self.match_id)

    def frames(self, user: ApiUser, kind: type) -> list[Any]:
        return [
            f for f in self.notifier.frames_for(user.user_id) if isinstance(f, kind)
        ]

    def ended_frames(self, user: ApiUser) -> list[WsMatchEnded]:
        return self.frames(user, WsMatchEnded)


@pytest.fixture
def flow(
    app: FastAPI,
    client: TestClient,
    session_factory: SessionFactory,
    server_config: ServerConfig,
    clock: FixedClock,
    mailer: RecordingMailer,
) -> Flow:
    notifier = RecordingNotifier()
    app.state.notifier = notifier
    app.state.matchmaker = Matchmaker(session_factory, server_config, clock, notifier)
    a = ready_user(client, session_factory, mailer, "a@example.com", NICK_A)
    b = ready_user(client, session_factory, mailer, "b@example.com", NICK_B)
    notifier.connected.update({a.user_id, b.user_id})
    for user in (a, b):
        r = client.post(
            "/v1/queue",
            json={
                "idempotency_key": str(uuid.uuid4()),
                "presence": {
                    "schema_version": 1,
                    "presence_id": str(uuid.uuid4()),
                    "status": "available",
                    "task_category": "other",
                    "stack_tags": ["python"],
                    "remaining_time_bucket": "5_10m",
                },
            },
            headers=user.headers,
        )
        assert r.status_code in (200, 201, 202), r.text
    app.state.matchmaker.run_once()
    with session_factory() as s:
        match_id = s.scalars(select(Match.id)).one()
    notifier.clear()
    return Flow(
        app, client, session_factory, server_config, clock, notifier, a, b, match_id
    )


@pytest.fixture(autouse=True)
def _no_content_in_logs(caplog: pytest.LogCaptureFixture) -> Iterator[None]:
    caplog.set_level("DEBUG")
    yield
    for secret in (BODY, NOTE, NICK_A, NICK_B):
        assert secret not in caplog.text, "chat content in a log line"


def test_forty_messages_interleaved(flow: Flow) -> None:
    for i in range(20):
        assert flow.send(flow.a, f"{BODY}-a{i}").status_code == 201
        assert flow.send(flow.b, f"{BODY}-b{i}").status_code == 201

    transcripts = {}
    for user in (flow.a, flow.b):
        first = flow.fetch(user, 0).json()
        assert first["last_seq"] == 40
        messages = list(first["messages"])
        if len(messages) == 100:  # not reached with 40; kept for page limits
            messages += flow.fetch(user, messages[-1]["seq"]).json()["messages"]
        transcripts[user.user_id] = messages
    ma, mb = transcripts[flow.a.user_id], transcripts[flow.b.user_id]
    assert [m["seq"] for m in ma] == list(range(1, 41))
    assert [(m["seq"], m["body"]) for m in ma] == [(m["seq"], m["body"]) for m in mb]
    assert [m["mine"] for m in ma] == [i % 2 == 0 for i in range(40)]
    assert [m["mine"] for m in mb] == [i % 2 == 1 for i in range(40)]
    for user in (flow.a, flow.b):
        frames = flow.frames(user, WsChatMessage)
        assert sorted(f.message.seq for f in frames) == list(range(1, 41))
    assert flow.match().last_seq == 40


def test_short_disconnect_keeps_chat_and_gap_is_fetchable(flow: Flow) -> None:
    assert flow.send(flow.a, f"{BODY}-first").status_code == 201
    flow.notifier.connected.discard(flow.a.user_id)
    flow.clock.advance(1)
    flow.run()
    (offline,) = flow.frames(flow.b, WsChatStatus)
    assert offline.change == "peer_offline"
    assert flow.frames(flow.a, WsChatStatus) == []

    for i in range(3):
        flow.clock.advance(1)
        assert flow.send(flow.b, f"{BODY}-gap{i}").status_code == 201
    flow.clock.advance(HOLD - 10)
    flow.notifier.connected.add(flow.a.user_id)
    flow.run()
    assert flow.match().status == "active"
    status = flow.frames(flow.b, WsChatStatus)
    assert [f.change for f in status] == ["peer_offline", "peer_online"]

    page = flow.fetch(flow.a, after_seq=1).json()
    assert [m["seq"] for m in page["messages"]] == [2, 3, 4]
    assert all(not m["mine"] for m in page["messages"])
    assert page["last_seq"] == 4


def test_long_disconnect_ends_the_chat_but_history_stays_readable(flow: Flow) -> None:
    assert flow.send(flow.b, f"{BODY}-x").status_code == 201
    flow.notifier.connected.discard(flow.a.user_id)
    flow.run()
    flow.clock.advance(HOLD - 1)
    flow.run()
    assert flow.match().status == "active"
    flow.clock.advance(1)
    flow.run()

    assert flow.match().end_reason == "disconnected"
    for user in (flow.a, flow.b):
        (ended,) = flow.ended_frames(user)
        assert ended.reason == "disconnected"
    for user in (flow.a, flow.b):
        r = flow.send(user, f"{BODY}-late")
        assert r.status_code == 409
        assert r.json()["error"] == "chat_ended"
        page = flow.fetch(user).json()
        assert [m["seq"] for m in page["messages"]] == [1]


def test_extension_moves_the_deadline(flow: Flow) -> None:
    ttl = flow.config.match_ttl_s
    ext = flow.config.chat_extension_s
    assert flow.extension(flow.a, "request").status_code == 200
    r = flow.extension(flow.b, "accept")
    assert r.status_code == 200, r.text
    assert r.json()["extensions_left"] == flow.config.chat_max_extensions - 1

    flow.clock.advance(ttl + 1)  # past the original deadline
    flow.run()
    assert flow.match().status == "active"
    assert flow.ended_frames(flow.a) == []

    flow.clock.advance(ext)  # past the new one
    flow.run()
    match = flow.match()
    assert match.status == "ended" and match.end_reason == "expired"
    for user in (flow.a, flow.b):
        (ended,) = flow.ended_frames(user)
        assert ended.reason == "expired"


def _counts(flow: Flow) -> tuple[int, int, int, int]:
    """(messages, matches, reports, report messages)."""
    with flow.factory() as s:
        counts = [
            s.scalar(select(func.count()).select_from(model)) or 0
            for model in (Message, Match, Report, ReportMessage)
        ]
    return counts[0], counts[1], counts[2], counts[3]


def test_report_ends_chat_and_evidence_outlives_messages_and_match(flow: Flow) -> None:
    flow.send(flow.a, f"{BODY}-one")
    flow.send(flow.b, f"{BODY}-two")
    flow.send(flow.a, f"{BODY}-three")

    flow.notifier.clear()
    r = flow.client.post(
        "/v1/reports",
        json={
            "match_id": str(flow.match_id),
            "reason": "harassment",
            "note": NOTE,
            "message_seqs": [2],
        },
        headers=flow.a.headers,
    )
    assert r.status_code == 204, r.text
    assert flow.match().end_reason == "reported"
    with flow.factory() as s:
        assert s.get(Block, (flow.a.user_id, flow.b.user_id)) is not None
        report_id = s.scalars(select(Report.id)).one()
        evidence = s.scalars(select(ReportMessage)).all()
        assert [(e.seq, e.author, e.body) for e in evidence] == [
            (2, "reported", f"{BODY}-two")
        ]
    for user in (flow.a, flow.b):
        (ended,) = flow.ended_frames(user)
        assert ended.reason == "ended"

    again = flow.client.post(
        "/v1/reports",
        json={"match_id": str(flow.match_id), "reason": "spam", "message_seqs": []},
        headers=flow.a.headers,
    )
    assert again.status_code == 409
    flow.run()
    assert flow.match().status == "ended"  # nobody re-paired
    assert _counts(flow) == (3, 1, 1, 1)

    flow.clock.advance(flow.config.message_retention_s + 1)
    flow.run()
    assert _counts(flow) == (0, 1, 1, 1)  # bodies gone, report evidence kept

    flow.clock.advance(flow.config.retention_s)
    flow.run()
    assert _counts(flow) == (0, 0, 1, 1)  # match purged, report survives

    with flow.factory() as s:
        row = s.get_one(Report, report_id)
        row.status = "dismissed"
        row.resolved_at = flow.clock.now
        s.commit()
    flow.clock.advance(flow.config.report_retention_s)
    flow.run()
    assert _counts(flow) == (0, 0, 1, 1)  # exactly 90 days: not yet
    flow.clock.advance(61)  # 90 days + 1 s has passed; the purge runs every 60 s
    flow.run()
    assert _counts(flow) == (0, 0, 0, 0)


def test_server_restart_mid_hold_restarts_the_clock(flow: Flow) -> None:
    flow.notifier.connected.discard(flow.a.user_id)
    flow.run()
    flow.clock.advance(50)
    flow.run()
    assert flow.match().status == "active"

    flow.app.state.matchmaker = Matchmaker(
        flow.factory, flow.config, flow.clock, flow.notifier
    )
    flow.clock.advance(5)  # 55 s offline by the old reckoning
    flow.run()  # first pass of the new instance: resets the clock to now
    reset_at = flow.clock.now
    with flow.factory() as s:
        row = s.get_one(MatchParticipant, (flow.match_id, flow.a.user_id))
        assert row.offline_since == reset_at
    flow.clock.advance(HOLD - 1)
    flow.run()
    assert flow.match().status == "active"
    flow.clock.advance(1)
    flow.run()
    assert flow.match().end_reason == "disconnected"
    for user in (flow.a, flow.b):
        assert [e.reason for e in flow.ended_frames(user)] == ["disconnected"]

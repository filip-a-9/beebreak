"""M6-08: `breakroom-server purge`, the pass restore.sh runs after a restore."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from breakroom_server.cli import main
from breakroom_server.db import SessionFactory
from breakroom_server.models import Match, Message
from tests.server.conftest import SeededUser, add_ready_user, seed_match, seed_message


def _count(factory: SessionFactory, model: type) -> int:
    with factory() as s:
        return s.scalar(select(func.count()).select_from(model)) or 0


@pytest.mark.usefixtures("server_env")
def test_purge_deletes_stale_rows_and_keeps_fresh_ones(
    session_factory: SessionFactory,
) -> None:
    now = datetime.now(UTC)
    ada = add_ready_user(session_factory, "Ada", now=now)
    bob = add_ready_user(session_factory, "Bob", now=now)
    cy = add_ready_user(session_factory, "Cy", now=now)
    dee = add_ready_user(session_factory, "Dee", now=now)

    def ended(a: SeededUser, b: SeededUser, days: int) -> uuid.UUID:
        when = now - timedelta(days=days)
        match_id = seed_match(
            session_factory,
            a,
            b,
            now=when - timedelta(minutes=15),
            status="ended",
            ended_at=when,
            end_reason="ended",
        )
        seed_message(session_factory, match_id, a, seq=1, body="hi", now=when)
        return match_id

    stale = ended(ada, bob, days=60)  # past the 30-day match retention
    fresh = ended(cy, dee, days=0)  # ended just now

    assert main(["purge"]) == 0

    with session_factory() as s:
        remaining = set(s.scalars(select(Match.id)))
    assert remaining == {fresh}
    assert stale not in remaining
    assert _count(session_factory, Message) == 1  # only the fresh match's


def test_purge_refuses_a_bad_configuration(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("BREAKROOM_DATABASE_URL", raising=False)
    assert main(["purge"]) == 2

"""Account tombstone: a deleted disabled account leaves a keyed hash, nothing more."""

import base64
import dataclasses
import hashlib
import logging
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from breakroom_server.account_data import delete_account
from breakroom_server.admin import main
from breakroom_server.config import ServerConfig
from breakroom_server.db import SessionFactory
from breakroom_server.matchmaking import Matchmaker
from breakroom_server.models import BetaInvite, DeletedAccountTombstone, User
from breakroom_server.tokens import tombstone_hmac
from tests.fixtures.privacy_corpus import load_corpus
from tests.fixtures.privacy_corpus.scan import find_corpus_leaks
from tests.server.conftest import START_TIME, TEST_SECRET, FixedClock, add_ready_user
from tests.server.fakes import RecordingNotifier

ADA = "ada@example.com"
BOB = "bob@example.com"

pytestmark = pytest.mark.usefixtures("server_env")


def run(argv: list[str], clock: FixedClock) -> int:
    return main(argv, clock=clock)


def _tombstones(factory: SessionFactory) -> list[DeletedAccountTombstone]:
    with factory() as s:
        return list(s.scalars(select(DeletedAccountTombstone)))


def _invites(factory: SessionFactory) -> int:
    with factory() as s:
        return s.scalar(select(func.count()).select_from(BetaInvite)) or 0


def _delete_disabled(factory: SessionFactory, clock: FixedClock) -> None:
    add_ready_user(factory, "Ada", status="disabled")
    argv = ["account", "delete", ADA, "--yes", "--disabled-ok"]
    assert run(argv, clock) == 0


def test_the_key_depends_on_secret_and_email_and_is_not_a_plain_hash() -> None:
    a = tombstone_hmac(TEST_SECRET, ADA)
    assert a == tombstone_hmac(TEST_SECRET, ADA)
    assert a != tombstone_hmac(TEST_SECRET, BOB)
    assert a != tombstone_hmac(b"another-secret-of-32-bytes-long!!", ADA)
    assert a != hashlib.sha256(ADA.encode()).digest()
    assert len(a) == 32


def test_deleting_a_disabled_user_leaves_one_keyed_row_and_no_email(
    session_factory: SessionFactory,
    clock: FixedClock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _delete_disabled(session_factory, clock)
    rows = _tombstones(session_factory)
    assert len(rows) == 1
    assert rows[0].email_hmac == tombstone_hmac(TEST_SECRET, ADA)
    out = capsys.readouterr()
    assert "tombstones=1" in out.out.split()
    assert ADA not in out.out + out.err
    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(User)) == 0


def test_deleting_an_active_user_leaves_no_tombstone(
    session_factory: SessionFactory,
    clock: FixedClock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    add_ready_user(session_factory, "Ada")
    assert run(["account", "delete", ADA, "--yes"], clock) == 0
    assert _tombstones(session_factory) == []
    assert "tombstones=0" in capsys.readouterr().out.split()


def test_the_tombstone_is_part_of_the_delete_result(
    session_factory: SessionFactory, clock: FixedClock
) -> None:
    add_ready_user(session_factory, "Ada", status="disabled")
    with session_factory() as s:
        counts = delete_account(
            s, ADA, clock(), secret=TEST_SECRET, allow_disabled=True
        )
    assert counts["tombstones"] == 1
    assert len(_tombstones(session_factory)) == 1


def test_invite_for_a_tombstoned_email_is_refused_without_echoing_it(
    session_factory: SessionFactory,
    clock: FixedClock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _delete_disabled(session_factory, clock)
    capsys.readouterr()
    # Case does not get around it: the key is over the canonical form.
    assert run(["invite", "create", "ADA@Example.com"], clock) == 1
    out = capsys.readouterr()
    assert out.out == ""
    assert out.err.strip() != ""
    assert "ada" not in (out.out + out.err).lower()
    assert _invites(session_factory) == 0
    assert len(_tombstones(session_factory)) == 1


def test_invite_for_a_different_email_is_unaffected(
    session_factory: SessionFactory, clock: FixedClock
) -> None:
    _delete_disabled(session_factory, clock)
    assert run(["invite", "create", BOB], clock) == 0
    assert _invites(session_factory) == 1


def test_override_creates_the_invite_and_removes_the_tombstone(
    session_factory: SessionFactory,
    clock: FixedClock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _delete_disabled(session_factory, clock)
    capsys.readouterr()
    assert run(["invite", "create", ADA, "--override-tombstone"], clock) == 0
    assert capsys.readouterr().out.strip() != ""
    assert _invites(session_factory) == 1
    assert _tombstones(session_factory) == []
    # One decision per person: the next invite needs no flag.
    assert run(["invite", "create", ADA], clock) == 0


def test_override_flag_on_an_email_without_a_tombstone_is_harmless(
    session_factory: SessionFactory, clock: FixedClock
) -> None:
    assert run(["invite", "create", BOB, "--override-tombstone"], clock) == 0
    assert _invites(session_factory) == 1


def test_a_changed_secret_makes_the_old_tombstone_not_match(
    session_factory: SessionFactory,
    clock: FixedClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _delete_disabled(session_factory, clock)
    monkeypatch.setenv(
        "BREAKROOM_SERVER_SECRET",
        base64.urlsafe_b64encode(b"a-different-secret-of-32-bytes!!").decode(),
    )
    assert run(["invite", "create", ADA], clock) == 0


# --- retention -------------------------------------------------------------------


def _sweeper(
    factory: SessionFactory, config: ServerConfig, clock: FixedClock
) -> Matchmaker:
    config = dataclasses.replace(config, tombstone_retention_s=1_000)
    return Matchmaker(factory, config, clock, RecordingNotifier())


@pytest.mark.parametrize(("offset", "kept"), [(-1, True), (1, False)])
def test_purge_removes_a_tombstone_after_its_retention(
    session_factory: SessionFactory,
    server_config: ServerConfig,
    clock: FixedClock,
    offset: int,
    kept: bool,
) -> None:
    _delete_disabled(session_factory, clock)
    created = clock.now
    mm = _sweeper(session_factory, server_config, clock)
    clock.now = created + timedelta(seconds=1_000 + offset)
    mm.run_once()
    assert (len(_tombstones(session_factory)) == 1) is kept


def test_purge_and_deletion_logs_carry_counts_only(
    session_factory: SessionFactory,
    server_config: ServerConfig,
    clock: FixedClock,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    caplog.set_level(logging.DEBUG)
    _delete_disabled(session_factory, clock)
    key = tombstone_hmac(TEST_SECRET, ADA)
    mm = _sweeper(session_factory, server_config, clock)
    clock.now = START_TIME + timedelta(days=1)
    mm.run_once()
    assert "tombstones=1" in caplog.text
    captured = capsys.readouterr()
    text = caplog.text + captured.out + captured.err
    assert ADA not in text and key.hex() not in text
    assert find_corpus_leaks({"logs": [text.encode()]}, load_corpus().bases) == []

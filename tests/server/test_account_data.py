"""M6-04: account export and delete, against a real session and a seeded history."""

import dataclasses
import json
import uuid
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import func, select, update

from beebreak_server.account_data import (
    AccountBusy,
    AccountDisabled,
    AccountNotFound,
    delete_account,
    export_account,
    finish_delete,
    prepare_delete,
)
from beebreak_server.db import SessionFactory
from beebreak_server.models import (
    AuthChallenge,
    Base,
    BetaInvite,
    Block,
    Device,
    Match,
    MatchParticipant,
    Message,
    QueueEntry,
    RefreshToken,
    Report,
    ReportMessage,
    User,
)
from tests.server.conftest import (
    START_TIME,
    TEST_SECRET,
    FixedClock,
    SeededUser,
    add_ready_user,
    seed_match,
    seed_message,
    seed_queued,
    seed_report,
)

ADA_EMAIL = "ada@example.com"
ADA_BODY = "ada-body-7f3a"
BOB_BODY = "bob-body-91c2"
ADA_EVIDENCE = "ada-evidence-3b9d"
NOTE = "ada-note-55d1"
BOB_NOTE = "bob-note-02e8"
ENDED = START_TIME + timedelta(hours=1)
HASH_COLUMN_NAMES = ("hash", "hmac")


@dataclasses.dataclass
class World:
    factory: SessionFactory
    ada: SeededUser
    bob: SeededUser
    cy: SeededUser
    ada_bob_match: uuid.UUID
    bob_cy_match: uuid.UUID


@pytest.fixture
def world(session_factory: SessionFactory) -> World:
    ada = add_ready_user(session_factory, "Ada")
    bob = add_ready_user(session_factory, "Bob")
    cy = add_ready_user(session_factory, "Cyd")
    ab = seed_match(
        session_factory,
        ada,
        bob,
        status="ended",
        ended_at=ENDED,
        end_reason="ended",
    )
    seed_message(session_factory, ab, ada, seq=1, body=ADA_BODY)
    seed_message(session_factory, ab, bob, seq=2, body=BOB_BODY)
    bc = seed_match(
        session_factory, bob, cy, status="ended", ended_at=ENDED, end_reason="ended"
    )
    seed_message(session_factory, bc, bob, seq=1, body="bob-to-cy")
    seed_report(
        session_factory,
        ada,
        bob,
        ab,
        note=NOTE,
        status="dismissed",
        resolved_at=ENDED,
        evidence=[(1, "reporter", ADA_EVIDENCE), (2, "reported", BOB_BODY)],
    )
    seed_report(
        session_factory,
        bob,
        ada,
        ab,
        note=BOB_NOTE,
        status="dismissed",
        resolved_at=ENDED,
        evidence=[(1, "reported", ADA_BODY)],
    )
    with session_factory() as s:
        s.add(
            Block(blocker_id=ada.user_id, blocked_id=cy.user_id, created_at=START_TIME)
        )
        s.add(
            Block(blocker_id=bob.user_id, blocked_id=ada.user_id, created_at=START_TIME)
        )
        invite = BetaInvite(
            email_canonical=ADA_EMAIL,
            code_hmac=b"i" * 32,
            expires_at=ENDED,
            redeemed_at=START_TIME,
            redeemed_by=ada.user_id,
            created_at=START_TIME,
        )
        s.add(invite)
        s.flush()
        s.add(
            AuthChallenge(
                email_canonical=ADA_EMAIL,
                code_hmac=b"c" * 32,
                invite_id=invite.id,
                expires_at=ENDED,
                consumed_at=START_TIME,
                created_at=START_TIME,
            )
        )
        s.add(
            RefreshToken(
                token_hash=b"t" * 32, device_id=ada.device_id, expires_at=ENDED
            )
        )
        s.commit()
    return World(session_factory, ada, bob, cy, ab, bc)


def _keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        found = set(value)
        for item in value.values():
            found |= _keys(item)
        return found
    if isinstance(value, list):
        found = set()
        for item in value:
            found |= _keys(item)
        return found
    return set()


def _export(world: World, email: str = ADA_EMAIL) -> dict[str, Any]:
    with world.factory() as s:
        data = export_account(s, email)
    return data


# --- export ----------------------------------------------------------------


def test_export_is_json_serialisable_and_versioned(world: World) -> None:
    data = _export(world)
    assert json.loads(json.dumps(data))["export_version"] == 1


def test_export_has_own_bodies_and_none_of_the_peers(world: World) -> None:
    text = json.dumps(_export(world))
    assert ADA_BODY in text
    assert BOB_BODY not in text
    assert "bob-to-cy" not in text


def test_export_has_own_report_note_but_not_the_reporters_note_against_them(
    world: World,
) -> None:
    text = json.dumps(_export(world))
    assert NOTE in text
    assert BOB_NOTE not in text


def test_export_has_no_hash_column_anywhere(world: World) -> None:
    keys = _keys(_export(world))
    assert keys
    for key in keys:
        assert not any(marker in key.lower() for marker in HASH_COLUMN_NAMES), key


def test_export_has_no_secret_bytes(world: World) -> None:
    text = json.dumps(_export(world))
    for raw in ("i" * 32, "c" * 32, "t" * 32):
        assert raw not in text


def test_report_against_the_user_has_only_date_and_outcome(world: World) -> None:
    data = _export(world)
    (against,) = data["reports_against"]
    assert set(against) == {"created_at", "status", "resolved_at"}


def test_export_includes_core_sections(world: World) -> None:
    data = _export(world)
    assert data["user"]["email"] == ADA_EMAIL
    assert data["user"]["nickname"] == "Ada"
    assert len(data["devices"]) == 1
    assert len(data["matches"]) == 1
    assert [b["blocked_nickname"] for b in data["blocks"]] == ["Cyd"]
    assert len(data["invites"]) == 1
    assert len(data["challenges"]) == 1
    assert data["preferences"] is not None


def test_export_evidence_has_own_copies_only(world: World) -> None:
    data = _export(world)
    text = json.dumps(data)
    assert ADA_EVIDENCE in text
    assert BOB_BODY not in text
    (filed,) = data["reports_filed"]
    assert [e["body"] for e in filed["own_evidence"]] == [ADA_EVIDENCE]


def test_export_has_participation_and_device_connection_fields(
    world: World,
) -> None:
    with world.factory() as s:
        s.execute(
            update(Device)
            .where(Device.id == world.ada.device_id)
            .values(connected_at=START_TIME)
        )
        s.execute(
            update(MatchParticipant)
            .where(MatchParticipant.user_id == world.ada.user_id)
            .values(offline_since=ENDED)
        )
        s.commit()
    data = _export(world)
    assert data["devices"][0]["connected_at"] == START_TIME.isoformat()
    (match,) = data["matches"]
    assert match["messages_sent"] == 1
    assert match["offline_since"] == ENDED.isoformat()


def test_export_normalises_the_email(world: World) -> None:
    assert _export(world, "  ADA@Example.COM ")["user"]["email"] == ADA_EMAIL


@pytest.mark.parametrize("email", ["nobody@example.com", "not-an-email"])
def test_export_unknown_or_invalid_email_is_not_found(world: World, email: str) -> None:
    with world.factory() as s, pytest.raises(AccountNotFound):
        export_account(s, email)


# --- delete ----------------------------------------------------------------


def _fk_targets() -> list[tuple[Any, Any]]:
    """Every (table, column) in the models that references users.id."""
    found = []
    for table in Base.metadata.sorted_tables:
        for column in table.columns:
            if any(fk.column.table.name == "users" for fk in column.foreign_keys):
                found.append((table, column))
    return found


def _references(world: World, user_id: uuid.UUID) -> int:
    total = 0
    with world.factory() as s:
        for table, column in _fk_targets():
            total += (
                s.scalar(
                    select(func.count()).select_from(table).where(column == user_id)
                )
                or 0
            )
        total += s.scalar(select(func.count()).where(User.id == user_id)) or 0
    return total


def test_delete_covers_every_foreign_key_to_users() -> None:
    # Guards the delete against a new table that references users.
    assert {t.name for t, _ in _fk_targets()} == {
        "beta_invites",
        "devices",
        "user_preferences",
        "queue_entries",
        "matches",
        "match_participants",
        "blocks",
        "messages",
        "reports",
    }


def test_delete_leaves_no_row_referencing_the_user(
    world: World, clock: FixedClock
) -> None:
    seed_queued(world.factory, world.ada, ttl_s=1)
    with world.factory() as s:
        # Ada's queue entry is not live any more.
        s.execute(
            update(QueueEntry)
            .where(QueueEntry.user_id == world.ada.user_id)
            .values(status="expired", ended_at=ENDED)
        )
        s.commit()
    assert _references(world, world.ada.user_id) > 0
    with world.factory() as s:
        counts = delete_account(s, ADA_EMAIL, clock(), secret=TEST_SECRET)
    assert _references(world, world.ada.user_id) == 0
    assert counts["users"] == 1
    assert counts["messages"] == 1
    with world.factory() as s:
        assert (
            s.scalars(select(Device).where(Device.id == world.ada.device_id)).first()
            is None
        )
        assert s.scalars(select(BetaInvite)).first() is None
        assert s.scalars(select(AuthChallenge)).first() is None
        assert s.get(RefreshToken, b"t" * 32) is None


def test_delete_keeps_the_peers_match_and_messages(
    world: World, clock: FixedClock
) -> None:
    with world.factory() as s:
        delete_account(s, ADA_EMAIL, clock(), secret=TEST_SECRET)
    with world.factory() as s:
        assert s.get(Match, world.ada_bob_match) is not None
        bodies = s.scalars(
            select(Message.body).where(Message.match_id == world.ada_bob_match)
        ).all()
        assert bodies == [BOB_BODY]
        left = s.scalars(
            select(MatchParticipant.user_id).where(
                MatchParticipant.match_id == world.ada_bob_match
            )
        ).all()
        assert left == [world.bob.user_id]
        assert s.get(User, world.bob.user_id) is not None
        assert s.get(User, world.cy.user_id) is not None


def test_delete_removes_reports_filed_and_reports_against_with_evidence(
    world: World, clock: FixedClock
) -> None:
    with world.factory() as s:
        counts = delete_account(s, ADA_EMAIL, clock(), secret=TEST_SECRET)
    with world.factory() as s:
        assert s.scalars(select(Report)).first() is None
        assert s.scalars(select(ReportMessage)).first() is None
        assert (
            s.scalars(
                select(Block).where(Block.blocker_id == world.bob.user_id)
            ).first()
            is None
        )
    assert counts["reports"] == 2


def test_delete_drops_a_match_when_no_participant_remains(
    world: World, clock: FixedClock
) -> None:
    with world.factory() as s:
        delete_account(s, "bob@example.com", clock(), secret=TEST_SECRET)
    with world.factory() as s:
        assert s.get(Match, world.bob_cy_match) is not None  # Cyd remains
        delete_account(s, "cyd@example.com", clock(), secret=TEST_SECRET)
    with world.factory() as s:
        assert s.get(Match, world.bob_cy_match) is None


def test_delete_clears_extension_request_on_a_kept_match(
    session_factory: SessionFactory, clock: FixedClock
) -> None:
    ada = add_ready_user(session_factory, "Ada")
    bob = add_ready_user(session_factory, "Bob")
    mid = seed_match(
        session_factory,
        ada,
        bob,
        status="ended",
        ended_at=ENDED,
        end_reason="ended",
        extension_requested_by=ada,
    )
    with session_factory() as s:
        delete_account(s, ADA_EMAIL, clock(), secret=TEST_SECRET)
    with session_factory() as s:
        match = s.get(Match, mid)
        assert match is not None
        assert match.extension_requested_by is None


def test_delete_unknown_account_is_not_found(world: World, clock: FixedClock) -> None:
    with world.factory() as s, pytest.raises(AccountNotFound):
        delete_account(s, "nobody@example.com", clock(), secret=TEST_SECRET)


def _snapshot(world: World) -> dict[str, int]:
    with world.factory() as s:
        return {
            table.name: s.scalar(select(func.count()).select_from(table)) or 0
            for table in Base.metadata.sorted_tables
        }


def test_busy_when_queued(world: World, clock: FixedClock) -> None:
    seed_queued(world.factory, world.ada)
    before = _snapshot(world)
    with world.factory() as s, pytest.raises(AccountBusy):
        delete_account(s, ADA_EMAIL, clock(), secret=TEST_SECRET)
    assert _snapshot(world) == before


def test_busy_in_a_live_match(
    session_factory: SessionFactory, clock: FixedClock
) -> None:
    ada = add_ready_user(session_factory, "Ada")
    bob = add_ready_user(session_factory, "Bob")
    seed_match(session_factory, ada, bob)
    with session_factory() as s, pytest.raises(AccountBusy):
        delete_account(s, ADA_EMAIL, clock(), secret=TEST_SECRET)
    with session_factory() as s:
        assert s.get(User, ada.user_id) is not None
        assert s.scalar(select(func.count()).select_from(MatchParticipant)) == 2


def test_busy_when_the_subject_of_an_open_report(
    session_factory: SessionFactory, clock: FixedClock
) -> None:
    ada = add_ready_user(session_factory, "Ada")
    bob = add_ready_user(session_factory, "Bob")
    mid = seed_match(
        session_factory, ada, bob, status="ended", ended_at=ENDED, end_reason="ended"
    )
    seed_report(session_factory, bob, ada, mid, status="open")
    with session_factory() as s, pytest.raises(AccountBusy):
        delete_account(s, ADA_EMAIL, clock(), secret=TEST_SECRET)
    with session_factory() as s:
        assert s.get(User, ada.user_id) is not None
        assert s.scalar(select(func.count()).select_from(Report)) == 1


def test_busy_when_the_user_filed_an_open_report(
    session_factory: SessionFactory, clock: FixedClock
) -> None:
    ada = add_ready_user(session_factory, "Ada")
    bob = add_ready_user(session_factory, "Bob")
    mid = seed_match(
        session_factory, ada, bob, status="ended", ended_at=ENDED, end_reason="ended"
    )
    seed_report(session_factory, ada, bob, mid, status="open")
    with session_factory() as s, pytest.raises(AccountBusy):
        delete_account(s, ADA_EMAIL, clock(), secret=TEST_SECRET)
    with session_factory() as s:
        assert s.get(User, ada.user_id) is not None
        assert s.scalar(select(func.count()).select_from(Report)) == 1


# --- disabled accounts (D3) ----------------------------------------------------


def test_disabled_account_is_refused_without_the_flag(
    session_factory: SessionFactory, clock: FixedClock
) -> None:
    ada = add_ready_user(session_factory, "Ada", status="disabled")
    with session_factory() as s, pytest.raises(AccountDisabled):
        delete_account(s, ADA_EMAIL, clock(), secret=TEST_SECRET)
    with session_factory() as s:
        assert s.get(User, ada.user_id) is not None
        device = s.get(Device, ada.device_id)
        assert device is not None and device.revoked_at is None


def test_disabled_account_is_deleted_with_the_flag(
    session_factory: SessionFactory, clock: FixedClock
) -> None:
    ada = add_ready_user(session_factory, "Ada", status="disabled")
    with session_factory() as s:
        delete_account(s, ADA_EMAIL, clock(), secret=TEST_SECRET, allow_disabled=True)
    with session_factory() as s:
        assert s.get(User, ada.user_id) is None


# --- two-phase delete (F4) -------------------------------------------------------


def test_prepare_revokes_devices_and_tokens_and_deletes_nothing_else(
    world: World, clock: FixedClock
) -> None:
    before = _snapshot(world)
    with world.factory() as s:
        prepare_delete(s, ADA_EMAIL, clock())
    with world.factory() as s:
        device = s.get(Device, world.ada.device_id)
        assert device is not None
        assert device.revoked_at == clock()
        assert device.access_hash is None
        assert s.get(RefreshToken, b"t" * 32) is None
        assert s.get(User, world.ada.user_id) is not None
    after = _snapshot(world)
    assert after.pop("refresh_tokens") == before.pop("refresh_tokens") - 1
    assert after == before


@pytest.mark.parametrize("cause", ["queued", "live_match", "open_report"])
def test_becoming_busy_between_the_phases_refuses_the_delete(
    world: World, clock: FixedClock, cause: str
) -> None:
    with world.factory() as s:
        user_id = prepare_delete(s, ADA_EMAIL, clock())
    # The gap: something commits after the first phase.
    if cause == "queued":
        seed_queued(world.factory, world.ada)
    elif cause == "live_match":
        seed_match(world.factory, world.ada, world.cy)
    else:
        seed_report(
            world.factory,
            world.cy,
            world.ada,
            world.ada_bob_match,
            now=START_TIME + timedelta(seconds=1),
            status="open",
        )
    with world.factory() as s, pytest.raises(AccountBusy):
        finish_delete(s, user_id, clock(), TEST_SECRET)
    with world.factory() as s:
        assert s.get(User, user_id) is not None
        assert s.get(MatchParticipant, (world.ada_bob_match, user_id)) is not None
        device = s.get(Device, world.ada.device_id)
        assert device is not None and device.revoked_at is not None  # stays revoked


def test_a_busy_user_is_refused_before_anything_is_revoked(
    world: World, clock: FixedClock
) -> None:
    seed_queued(world.factory, world.ada)
    before = _snapshot(world)
    with world.factory() as s, pytest.raises(AccountBusy):
        prepare_delete(s, ADA_EMAIL, clock())
    assert _snapshot(world) == before
    with world.factory() as s:
        device = s.get(Device, world.ada.device_id)
        assert device is not None and device.revoked_at is None


# --- other-address rows (F5) ---------------------------------------------------


def test_invite_for_another_address_redeemed_by_the_user_survives_unlinked(
    world: World, clock: FixedClock
) -> None:
    with world.factory() as s:
        other = BetaInvite(
            email_canonical="other@example.com",
            code_hmac=b"o" * 32,
            expires_at=ENDED,
            redeemed_at=START_TIME,
            redeemed_by=world.ada.user_id,
            created_at=START_TIME,
        )
        s.add(other)
        s.commit()
        other_id = other.id
    with world.factory() as s:
        delete_account(s, ADA_EMAIL, clock(), secret=TEST_SECRET)
    with world.factory() as s:
        row = s.get(BetaInvite, other_id)
        assert row is not None
        assert row.redeemed_by is None


def test_foreign_challenge_referencing_the_users_invite_is_deleted_cleanly(
    world: World, clock: FixedClock
) -> None:
    with world.factory() as s:
        invite_id = s.scalars(select(BetaInvite.id)).one()
        stranger = AuthChallenge(
            email_canonical="stranger@example.com",
            code_hmac=b"s" * 32,
            invite_id=invite_id,
            expires_at=ENDED,
            created_at=START_TIME,
        )
        s.add(stranger)
        s.commit()
        stranger_id = stranger.id
    with world.factory() as s:
        delete_account(s, ADA_EMAIL, clock(), secret=TEST_SECRET)
    with world.factory() as s:
        assert s.get(AuthChallenge, stranger_id) is None
        assert s.scalars(select(BetaInvite)).first() is None

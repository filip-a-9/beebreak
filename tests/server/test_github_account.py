"""Export and delete for a GitHub account: found by GitHub id, not by email."""

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from breakroom_server.account_data import (
    AccountDisabled,
    AccountNotFound,
    delete_account,
    export_account,
)
from breakroom_server.db import SessionFactory
from breakroom_server.models import (
    BetaInvite,
    DeletedAccountTombstone,
    Device,
    User,
    UserPreference,
)
from breakroom_server.tokens import github_tombstone_hmac, tombstone_hmac
from tests.server.conftest import START_TIME, TEST_SECRET, add_ready_user

GID = 31_337
OTHER_GID = 31_338


def add_github_user(
    factory: SessionFactory, github_id: int, *, status: str = "active"
) -> uuid.UUID:
    with factory() as s:
        user = User(
            github_id=github_id,
            nickname=f"gh{github_id}",
            nickname_skeleton=f"gh{github_id}",
            status=status,
            created_at=START_TIME,
        )
        s.add(user)
        s.flush()
        s.add(Device(user_id=user.id, name="laptop", created_at=START_TIME))
        s.add(
            UserPreference(
                user_id=user.id,
                consent_version=1,
                share_task_category=True,
                share_stack_tags=False,
                share_time_bucket=False,
                consented_at=START_TIME,
            )
        )
        s.commit()
        return user.id


def add_invite(
    factory: SessionFactory, github_id: int, *, redeemed_by: uuid.UUID | None = None
) -> None:
    with factory() as s:
        s.add(
            BetaInvite(
                github_id=github_id,
                expires_at=START_TIME + timedelta(days=7),
                created_at=START_TIME,
                redeemed_at=START_TIME if redeemed_by else None,
                redeemed_by=redeemed_by,
            )
        )
        s.commit()


def test_export_finds_the_account_by_github_id(session_factory: SessionFactory) -> None:
    uid = add_github_user(session_factory, GID)
    add_invite(session_factory, GID, redeemed_by=uid)
    add_invite(session_factory, OTHER_GID)
    with session_factory() as s:
        data = export_account(s, None, github_id=GID)
    assert data["user"]["github_id"] == GID
    assert data["user"]["email"] is None
    assert data["user"]["nickname"] == f"gh{GID}"
    assert len(data["invites"]) == 1  # this id's invite, not the other's


def test_export_of_an_email_account_has_no_github_id(
    session_factory: SessionFactory,
) -> None:
    add_ready_user(session_factory, "Ada")
    with session_factory() as s:
        data = export_account(s, "ada@example.com")
    assert data["user"]["github_id"] is None


def test_the_export_is_looked_up_by_exactly_one_identity(
    session_factory: SessionFactory,
) -> None:
    add_github_user(session_factory, GID)
    with session_factory() as s:
        with pytest.raises(AccountNotFound):
            export_account(s, None)
        with pytest.raises(AccountNotFound):
            export_account(s, None, github_id=OTHER_GID)
        with pytest.raises(AccountNotFound):
            export_account(s, "ada@example.com", github_id=GID)  # ambiguous


def test_delete_removes_the_account_and_only_its_own_invites(
    session_factory: SessionFactory,
) -> None:
    uid = add_github_user(session_factory, GID)
    other = add_github_user(session_factory, OTHER_GID)
    add_invite(session_factory, GID, redeemed_by=uid)
    add_invite(session_factory, OTHER_GID, redeemed_by=other)
    with session_factory() as s:
        counts = delete_account(s, None, START_TIME, secret=TEST_SECRET, github_id=GID)
    assert counts["users"] == 1 and counts["devices"] == 1
    assert counts["beta_invites"] == 1 and counts["tombstones"] == 0
    with session_factory() as s:
        assert [u.github_id for u in s.scalars(select(User))] == [OTHER_GID]
        assert [i.github_id for i in s.scalars(select(BetaInvite))] == [OTHER_GID]


def test_deleting_an_email_account_leaves_github_invites_alone(
    session_factory: SessionFactory,
) -> None:
    add_ready_user(session_factory, "Ada")
    add_invite(session_factory, GID)
    with session_factory() as s:
        delete_account(s, "ada@example.com", START_TIME, secret=TEST_SECRET)
    with session_factory() as s:
        assert len(s.scalars(select(BetaInvite)).all()) == 1


def test_a_disabled_github_account_needs_the_flag_and_leaves_a_keyed_tombstone(
    session_factory: SessionFactory,
) -> None:
    add_github_user(session_factory, GID, status="disabled")
    with session_factory() as s, pytest.raises(AccountDisabled):
        delete_account(s, None, START_TIME, secret=TEST_SECRET, github_id=GID)
    with session_factory() as s:
        counts = delete_account(
            s, None, START_TIME, secret=TEST_SECRET, github_id=GID, allow_disabled=True
        )
    assert counts["tombstones"] == 1
    with session_factory() as s:
        (row,) = s.scalars(select(DeletedAccountTombstone)).all()
    assert row.email_hmac == github_tombstone_hmac(TEST_SECRET, GID)
    assert row.email_hmac != tombstone_hmac(TEST_SECRET, str(GID))
    assert row.email_hmac != github_tombstone_hmac(TEST_SECRET, OTHER_GID)


def test_the_delete_of_an_unknown_github_id_is_not_found(
    session_factory: SessionFactory,
) -> None:
    with session_factory() as s, pytest.raises(AccountNotFound):
        delete_account(s, None, START_TIME, secret=TEST_SECRET, github_id=GID)

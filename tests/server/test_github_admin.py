"""beebreak-admin with GitHub: invites by username, export and delete by id."""

import json
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from beebreak_server.admin import main
from beebreak_server.db import SessionFactory
from beebreak_server.mailer import RecordingMailer
from beebreak_server.models import BetaInvite, DeletedAccountTombstone, User
from beebreak_server.tokens import github_tombstone_hmac
from tests.server.conftest import TEST_SECRET, FixedClock
from tests.server.github_fake import FakeGitHub, fake_github
from tests.server.test_github_account import add_github_user

__all__ = ["fake_github"]

GID = 583_231
USERNAME = "octocat-lover"


@pytest.fixture(autouse=True)
def _env(
    monkeypatch: pytest.MonkeyPatch, server_env: None, fake_github: FakeGitHub
) -> None:
    monkeypatch.setenv("BEEBREAK_GITHUB_CLIENT_ID", fake_github.client_id)
    monkeypatch.setenv("BEEBREAK_GITHUB_OAUTH_URL", fake_github.url)
    monkeypatch.setenv("BEEBREAK_GITHUB_API_URL", fake_github.url)
    monkeypatch.setenv("BEEBREAK_GITHUB_TIMEOUT_S", "3")
    fake_github.users[USERNAME] = GID


def run(argv: list[str], clock: FixedClock, **kwargs: object) -> int:
    return main(argv, clock=clock, **kwargs)  # type: ignore[arg-type]


def test_invite_by_username_stores_only_the_numeric_id(
    session_factory: SessionFactory,
    clock: FixedClock,
    fake_github: FakeGitHub,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run(["invite", "create", "--github", USERNAME], clock) == 0
    out = capsys.readouterr()
    uuid.UUID(out.out.strip())  # prints the invite id; there is no code to pass on
    assert USERNAME not in out.out + out.err
    with session_factory() as s:
        invite = s.scalars(select(BetaInvite)).one()
    assert (invite.github_id, invite.email_canonical, invite.code_hmac) == (
        GID,
        None,
        None,
    )
    assert invite.expires_at == clock() + timedelta(seconds=604_800)
    (call,) = fake_github.calls(f"/users/{USERNAME}")
    assert "authorization" not in call.headers


def test_days_sets_the_expiry(
    session_factory: SessionFactory, clock: FixedClock
) -> None:
    assert run(["invite", "create", "--github", USERNAME, "--days", "2"], clock) == 0
    with session_factory() as s:
        invite = s.scalars(select(BetaInvite)).one()
    assert invite.expires_at == clock() + timedelta(days=2)


def test_an_unknown_username_creates_nothing_and_is_not_echoed(
    session_factory: SessionFactory,
    clock: FixedClock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run(["invite", "create", "--github", "no-such-person"], clock) == 1
    out = capsys.readouterr()
    assert "no-such-person" not in out.out + out.err
    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(BetaInvite)) == 0


def test_github_being_down_is_a_clean_failure(
    session_factory: SessionFactory,
    clock: FixedClock,
    fake_github: FakeGitHub,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake_github.stop()
    assert run(["invite", "create", "--github", USERNAME], clock) == 1
    out = capsys.readouterr()
    assert USERNAME not in out.out + out.err and "127.0.0.1" not in out.out + out.err
    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(BetaInvite)) == 0


@pytest.mark.parametrize("name", ["bad name", "-x", "a/b", "x" * 40])
def test_a_malformed_username_is_bad_input_and_sends_nothing(
    clock: FixedClock, fake_github: FakeGitHub, name: str
) -> None:
    assert run(["invite", "create", "--github", name], clock) == 2
    assert fake_github.requests == []


def test_exactly_one_of_email_and_username(clock: FixedClock) -> None:
    assert (
        run(["invite", "create", "ada@example.com", "--github", USERNAME], clock) == 2
    )
    assert run(["invite", "create"], clock) == 2


def test_an_invite_by_username_cannot_be_emailed(clock: FixedClock) -> None:
    assert run(["invite", "create", "--github", USERNAME, "--send"], clock) == 2


def test_github_invites_need_github_sign_in_to_be_on(
    monkeypatch: pytest.MonkeyPatch, clock: FixedClock, fake_github: FakeGitHub
) -> None:
    monkeypatch.delenv("BEEBREAK_GITHUB_CLIENT_ID")
    assert run(["invite", "create", "--github", USERNAME], clock) == 1
    assert fake_github.requests == []


def test_email_invites_need_email_sign_in_to_be_on(
    monkeypatch: pytest.MonkeyPatch, clock: FixedClock
) -> None:
    monkeypatch.setenv("BEEBREAK_SMTP_HOST", "")
    assert run(["invite", "create", "ada@example.com"], clock) == 1


def test_an_email_invite_with_send_goes_through_the_mailer(
    clock: FixedClock, capsys: pytest.CaptureFixture[str]
) -> None:
    mailer = RecordingMailer()
    assert (
        run(["invite", "create", "ada@example.com", "--send"], clock, mailer=mailer)
        == 0
    )
    assert [kind for kind, _, _ in mailer.sent] == ["invite"]


def test_the_list_marks_github_invites_without_any_identity(
    clock: FixedClock, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(["invite", "create", "--github", USERNAME], clock) == 0
    capsys.readouterr()
    assert run(["invite", "list"], clock) == 0
    line = capsys.readouterr().out
    assert "github" in line and str(GID) not in line and USERNAME not in line


def test_a_github_invite_can_be_revoked(
    session_factory: SessionFactory,
    clock: FixedClock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run(["invite", "create", "--github", USERNAME], clock)
    invite_id = capsys.readouterr().out.strip()
    assert run(["invite", "revoke", invite_id], clock) == 0
    with session_factory() as s:
        assert s.scalars(select(BetaInvite)).one().revoked_at is not None


# --- tombstone ---------------------------------------------------------------------


def _tombstone(session_factory: SessionFactory) -> None:
    with session_factory() as s:
        s.add(
            DeletedAccountTombstone(
                email_hmac=github_tombstone_hmac(TEST_SECRET, GID),
                created_at=FixedClock()(),
            )
        )
        s.commit()


def test_a_tombstoned_github_id_is_refused_at_invite_time(
    session_factory: SessionFactory,
    clock: FixedClock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _tombstone(session_factory)
    assert run(["invite", "create", "--github", USERNAME], clock) == 1
    err = capsys.readouterr().err
    assert "--override-tombstone" in err and USERNAME not in err
    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(BetaInvite)) == 0


def test_override_invites_and_removes_the_tombstone(
    session_factory: SessionFactory, clock: FixedClock
) -> None:
    _tombstone(session_factory)
    args = ["invite", "create", "--github", USERNAME, "--override-tombstone"]
    assert run(args, clock) == 0
    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(BetaInvite)) == 1
        assert s.scalar(select(func.count()).select_from(DeletedAccountTombstone)) == 0


def test_deleting_a_disabled_github_account_by_id_leaves_the_tombstone(
    session_factory: SessionFactory,
    clock: FixedClock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    add_github_user(session_factory, GID, status="disabled")
    argv = ["account", "delete", "--github-id", str(GID), "--yes", "--disabled-ok"]
    assert run(argv, clock) == 0
    out = capsys.readouterr()
    assert "tombstones=1" in out.out.split() and str(GID) not in out.out + out.err
    with session_factory() as s:
        (row,) = s.scalars(select(DeletedAccountTombstone)).all()
        assert s.scalar(select(func.count()).select_from(User)) == 0
    assert row.email_hmac == github_tombstone_hmac(TEST_SECRET, GID)
    # And inviting that person again is now a decision for the operator.
    assert run(["invite", "create", "--github", USERNAME], clock) == 1


def test_export_by_github_id(
    session_factory: SessionFactory,
    clock: FixedClock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    add_github_user(session_factory, GID)
    assert run(["account", "export", "--github-id", str(GID)], clock) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["user"]["github_id"] == GID and data["user"]["email"] is None


def test_an_account_needs_exactly_one_identity_on_the_command_line(
    clock: FixedClock,
) -> None:
    assert run(["account", "export"], clock) == 2
    assert run(["account", "export", "a@example.com", "--github-id", "5"], clock) == 2
    assert run(["account", "export", "--github-id", "not-a-number"], clock) == 2
    assert run(["account", "delete", "--yes"], clock) == 2
    assert run(["account", "export", "--github-id", "99"], clock) == 1  # not found

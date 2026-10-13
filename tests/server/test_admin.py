import re
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from breakroom_server.admin import main
from breakroom_server.db import SessionFactory
from breakroom_server.mailer import RecordingMailer
from breakroom_server.models import (
    BetaInvite,
    Block,
    Device,
    Match,
    MatchParticipant,
    QueueEntry,
    User,
)
from breakroom_server.tokens import hmac_code, normalize_code
from tests.server.conftest import (
    START_TIME,
    TEST_SECRET,
    FixedClock,
    SeededUser,
    add_ready_user,
    seed_match,
    seed_queued,
)

EMAIL = "Jane.Doe@Example.org"
LOCAL = "jane.doe"

pytestmark = pytest.mark.usefixtures("server_env")


def run(argv: list[str], clock: FixedClock) -> int:
    return main(argv, clock=clock)


def create_invite(
    clock: FixedClock,
    capsys: pytest.CaptureFixture[str],
    *extra: str,
    email: str = EMAIL,
) -> str:
    assert run(["invite", "create", email, *extra], clock) == 0
    return capsys.readouterr().out.strip()


def test_create_prints_a_code_and_stores_its_hmac(
    session_factory: SessionFactory,
    clock: FixedClock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    printed = create_invite(clock, capsys)
    code = normalize_code(printed)
    assert code is not None
    with session_factory() as s:
        invite = s.scalars(select(BetaInvite)).one()
    assert invite.code_hmac == hmac_code(TEST_SECRET, code)
    assert invite.email_canonical == "jane.doe@example.org"
    assert invite.created_at == START_TIME
    assert invite.expires_at == START_TIME + timedelta(days=7)
    assert invite.redeemed_at is None and invite.revoked_at is None


def test_create_days_overrides_default_ttl(
    session_factory: SessionFactory,
    clock: FixedClock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    create_invite(clock, capsys, "--days", "2")
    with session_factory() as s:
        assert s.scalars(select(BetaInvite)).one().expires_at == START_TIME + timedelta(
            days=2
        )


def test_codes_differ_between_invites(
    clock: FixedClock, capsys: pytest.CaptureFixture[str]
) -> None:
    assert create_invite(clock, capsys) != create_invite(clock, capsys)


@pytest.mark.parametrize(
    "argv",
    [
        ["invite", "create", "not-an-email"],
        ["invite", "create", "a@b@example.com"],
        ["invite", "create", "a@example.com", "--days", "0"],
        ["invite", "create", "a@example.com", "--days", "abc"],
        ["invite", "revoke", "not-a-uuid"],
        ["user", "disable", "x"],  # too short to be a nickname
        ["frobnicate"],
        [],
    ],
)
def test_bad_input_exits_2_without_side_effects(
    argv: list[str],
    session_factory: SessionFactory,
    clock: FixedClock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run(argv, clock) == 2
    assert capsys.readouterr().out == ""
    with session_factory() as s:
        assert s.scalars(select(BetaInvite)).first() is None


def test_bad_email_message_does_not_echo_it(
    clock: FixedClock, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(["invite", "create", "secret.person@@example.com"], clock) == 2
    captured = capsys.readouterr()
    assert "secret.person" not in captured.out + captured.err


def test_send_emails_the_printed_code_through_the_mailer(
    session_factory: SessionFactory,
    clock: FixedClock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mailer = RecordingMailer()
    assert main(["invite", "create", EMAIL, "--send"], clock=clock, mailer=mailer) == 0
    printed = capsys.readouterr().out.strip()
    assert mailer.sent == [("invite", "jane.doe@example.org", normalize_code(printed))]
    with session_factory() as s:
        assert s.scalars(select(BetaInvite)).one().redeemed_at is None


def test_send_without_a_mailer_uses_the_configured_one(
    monkeypatch: pytest.MonkeyPatch,
    clock: FixedClock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("BREAKROOM_MAILER", "record")
    assert run(["invite", "create", EMAIL, "--send"], clock) == 0
    assert normalize_code(capsys.readouterr().out.strip()) is not None


def test_no_mail_without_send(clock: FixedClock) -> None:
    mailer = RecordingMailer()
    assert main(["invite", "create", EMAIL], clock=clock, mailer=mailer) == 0
    assert mailer.sent == []


def test_missing_config_exits_2(
    monkeypatch: pytest.MonkeyPatch,
    clock: FixedClock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("BREAKROOM_SERVER_SECRET")
    assert run(["invite", "list"], clock) == 2
    assert capsys.readouterr().out == ""


def test_database_without_schema_exits_1_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    clock: FixedClock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("BREAKROOM_DATABASE_URL", "sqlite://")
    assert run(["invite", "list"], clock) == 1
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err and captured.out == ""


def test_list_shows_domain_only_and_states(
    session_factory: SessionFactory,
    clock: FixedClock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    create_invite(clock, capsys, email="open@example.org")
    create_invite(clock, capsys, "--days", "1", email="old@example.net")
    create_invite(clock, capsys, email="gone@example.org")
    create_invite(clock, capsys, email="used@example.org")
    with session_factory() as s:
        by_local = {
            i.email_canonical.split("@")[0]: i for i in s.scalars(select(BetaInvite))
        }
        gone_id = by_local["gone"].id
        by_local["used"].redeemed_at = START_TIME
        s.commit()
    assert run(["invite", "revoke", str(gone_id)], clock) == 0
    clock.advance(2 * 86400)  # "old" (1 day) expires; the 7-day ones do not

    assert run(["invite", "list"], clock) == 0
    out = capsys.readouterr().out
    assert not re.search(r"\b(open|old|gone|used)@", out)
    rows = {tuple(line.split("\t")[1:3]) for line in out.strip().splitlines()}
    assert rows == {
        ("*@example.org", "open"),
        ("*@example.net", "expired"),
        ("*@example.org", "revoked"),
        ("*@example.org", "redeemed"),
    }
    assert str(gone_id) in out
    assert all(len(line.split("\t")) == 4 for line in out.strip().splitlines())


def test_revoke_sets_revoked_at_and_is_repeatable(
    session_factory: SessionFactory,
    clock: FixedClock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    create_invite(clock, capsys)
    with session_factory() as s:
        invite_id = s.scalars(select(BetaInvite)).one().id
    clock.advance(60)
    assert run(["invite", "revoke", str(invite_id)], clock) == 0
    assert run(["invite", "revoke", str(invite_id)], clock) == 0
    with session_factory() as s:
        assert s.get(BetaInvite, invite_id).revoked_at == START_TIME + timedelta(  # type: ignore[union-attr]
            seconds=60
        )


def test_revoke_unknown_or_redeemed_invite_exits_1(
    session_factory: SessionFactory,
    clock: FixedClock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run(["invite", "revoke", str(uuid4())], clock) == 1
    create_invite(clock, capsys)
    with session_factory() as s:
        invite = s.scalars(select(BetaInvite)).one()
        invite.redeemed_at = START_TIME
        s.commit()
        invite_id = invite.id
    assert run(["invite", "revoke", str(invite_id)], clock) == 1
    with session_factory() as s:
        assert s.get(BetaInvite, invite_id).revoked_at is None  # type: ignore[union-attr]


def _seed_user_with_devices(session_factory: SessionFactory) -> None:
    with session_factory() as s:
        user = User(
            email_canonical="ada@example.com",
            nickname="Ada",
            nickname_skeleton="ada",
            created_at=START_TIME,
        )
        s.add(user)
        s.flush()
        s.add_all(
            [
                Device(
                    user_id=user.id,
                    name="laptop",
                    access_hash=b"\x01" * 32,
                    created_at=START_TIME,
                    last_seen_at=START_TIME,
                    connected_at=START_TIME,
                ),
                Device(user_id=user.id, name="desktop", created_at=START_TIME),
                Device(
                    user_id=user.id,
                    name="old-phone",
                    created_at=START_TIME,
                    revoked_at=START_TIME,
                ),
            ]
        )
        s.commit()


def test_disable_revokes_all_devices_and_enable_restores_status(
    session_factory: SessionFactory,
    clock: FixedClock,
) -> None:
    _seed_user_with_devices(session_factory)
    clock.advance(30)
    assert run(["user", "disable", "Ada"], clock) == 0
    with session_factory() as s:
        assert s.scalars(select(User)).one().status == "disabled"
        devices = {d.name: d for d in s.scalars(select(Device))}
    assert devices["laptop"].revoked_at == START_TIME + timedelta(seconds=30)
    assert devices["desktop"].revoked_at == START_TIME + timedelta(seconds=30)
    assert devices["old-phone"].revoked_at == START_TIME  # unchanged
    assert devices["laptop"].access_hash is None

    assert run(["user", "enable", "Ada"], clock) == 0
    with session_factory() as s:
        assert s.scalars(select(User)).one().status == "active"
        # Enabling does not resurrect revoked devices.
        assert all(d.revoked_at is not None for d in s.scalars(select(Device)))


def test_disable_unknown_user_exits_1(clock: FixedClock) -> None:
    assert run(["user", "disable", "Nobody"], clock) == 1


def test_devices_lists_active_devices_only(
    session_factory: SessionFactory,
    clock: FixedClock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_user_with_devices(session_factory)
    assert run(["devices"], clock) == 0
    out = capsys.readouterr().out
    rows = {tuple(line.split("\t")[:3]) for line in out.strip().splitlines()}
    assert rows == {
        ("Ada", "laptop", "connected=yes"),
        ("Ada", "desktop", "connected=no"),
    }
    assert "old-phone" not in out
    assert "example.com" not in out
    assert START_TIME.isoformat() in out


def test_devices_with_none_prints_nothing(
    clock: FixedClock, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(["devices"], clock) == 0
    assert capsys.readouterr().out == ""


def test_no_output_contains_a_full_email_address(
    clock: FixedClock, capsys: pytest.CaptureFixture[str]
) -> None:
    _ = run(["invite", "create", EMAIL], clock)
    _ = run(["invite", "create", "bad@@x"], clock)
    _ = main(
        ["invite", "create", EMAIL, "--send"], clock=clock, mailer=RecordingMailer()
    )
    _ = run(["invite", "list"], clock)
    _ = run(["devices"], clock)
    captured = capsys.readouterr()
    assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", captured.out + captured.err)


# --- M4-01: queue, matches, blocks -------------------------------------------


def _seed_pair(
    session_factory: SessionFactory,
) -> tuple[SeededUser, SeededUser]:
    return (
        add_ready_user(session_factory, "Ada"),
        add_ready_user(session_factory, "Bob"),
    )


def test_queue_list_prints_field_names_never_values(
    session_factory: SessionFactory,
    clock: FixedClock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ada, bob = _seed_pair(session_factory)
    cara = add_ready_user(session_factory, "Cara")
    seed_queued(
        session_factory,
        ada,
        task_category="backend",
        stack_tags=["python", "rust"],
        bucket="5_10m",
    )
    seed_queued(session_factory, bob, now=START_TIME + timedelta(seconds=5))
    ended = seed_queued(session_factory, cara)
    with session_factory() as s:
        entry = s.get(QueueEntry, ended)
        assert entry is not None
        entry.status = "cancelled"
        s.commit()

    assert run(["queue", "list"], clock) == 0
    lines = [ln.split("\t") for ln in capsys.readouterr().out.strip().splitlines()]
    assert [ln[0] for ln in lines] == ["Ada", "Bob"]
    assert lines[0][1:4] == [
        "queued",
        START_TIME.isoformat(),
        (START_TIME + timedelta(seconds=90)).isoformat(),
    ]
    assert lines[0][4] == (
        "fields=presence_id,task_category,stack_tags,remaining_time_bucket"
    )
    assert lines[1][4] == "fields=presence_id"
    out = "\t".join("\t".join(ln) for ln in lines)
    for value in ("python", "rust", "backend", "5_10m"):
        assert value not in out


def test_queue_list_with_none_prints_nothing(
    clock: FixedClock, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(["queue", "list"], clock) == 0
    assert capsys.readouterr().out == ""


def test_queue_list_shows_dash_when_presence_was_cleared(
    session_factory: SessionFactory,
    clock: FixedClock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ada = add_ready_user(session_factory, "Ada")
    entry_id = seed_queued(session_factory, ada, stack_tags=["python"])
    with session_factory() as s:
        entry = s.get(QueueEntry, entry_id)
        assert entry is not None
        entry.presence_id = entry.stack_tags = None
        s.commit()
    assert run(["queue", "list"], clock) == 0
    assert capsys.readouterr().out.strip().endswith("fields=-")


def test_matches_lists_active_matches_only(
    session_factory: SessionFactory,
    clock: FixedClock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ada, bob = _seed_pair(session_factory)
    cara = add_ready_user(session_factory, "Cara")
    dan = add_ready_user(session_factory, "Dan")
    seed_match(session_factory, bob, ada, shared_tags=["python"])
    ended = seed_match(session_factory, cara, dan)
    with session_factory() as s:
        match = s.get(Match, ended)
        assert match is not None
        match.status = "ended"
        for part in s.scalars(select(MatchParticipant)):
            if part.match_id == ended:
                part.active = False
        s.commit()

    assert run(["matches"], clock) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert [ln.split("\t") for ln in lines] == [
        [
            "Ada",
            "Bob",
            START_TIME.isoformat(),
            (START_TIME + timedelta(seconds=900)).isoformat(),
        ]
    ]
    assert "python" not in lines[0]


def test_block_list_and_remove(
    session_factory: SessionFactory,
    clock: FixedClock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ada, bob = _seed_pair(session_factory)
    cara = add_ready_user(session_factory, "Cara")
    with session_factory() as s:
        s.add_all(
            [
                Block(
                    blocker_id=ada.user_id,
                    blocked_id=cara.user_id,
                    created_at=START_TIME,
                ),
                Block(
                    blocker_id=ada.user_id,
                    blocked_id=bob.user_id,
                    created_at=START_TIME,
                ),
                Block(
                    blocker_id=bob.user_id,
                    blocked_id=ada.user_id,
                    created_at=START_TIME,
                ),
            ]
        )
        s.commit()

    assert run(["block", "list", "Ada"], clock) == 0
    assert capsys.readouterr().out.splitlines() == ["Bob", "Cara"]
    assert run(["block", "list", "Cara"], clock) == 0
    assert capsys.readouterr().out == ""

    assert run(["block", "remove", "Ada", "Bob"], clock) == 0
    assert capsys.readouterr().out == ""
    with session_factory() as s:
        pairs = {(b.blocker_id, b.blocked_id) for b in s.scalars(select(Block))}
    assert pairs == {
        (ada.user_id, cara.user_id),
        (bob.user_id, ada.user_id),  # the reverse block is untouched
    }

    assert run(["block", "remove", "Ada", "Bob"], clock) == 1  # already gone
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err.strip() != ""


@pytest.mark.parametrize(
    ("argv", "code"),
    [
        (["block", "list", "Nobody"], 1),
        (["block", "remove", "Ada", "Nobody"], 1),
        (["block", "remove", "Nobody", "Ada"], 1),
        (["block", "list", "x"], 2),
        (["block", "remove", "Ada"], 2),
        (["block"], 2),
        (["queue"], 2),
    ],
)
def test_block_and_queue_bad_input(
    argv: list[str],
    code: int,
    session_factory: SessionFactory,
    clock: FixedClock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    add_ready_user(session_factory, "Ada")
    assert run(argv, clock) == code
    assert capsys.readouterr().out == ""


def test_new_subcommands_print_no_email_address(
    session_factory: SessionFactory,
    clock: FixedClock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ada, bob = _seed_pair(session_factory)
    seed_queued(session_factory, ada, stack_tags=["python"])
    seed_match(session_factory, ada, bob)
    with session_factory() as s:
        s.add(
            Block(blocker_id=ada.user_id, blocked_id=bob.user_id, created_at=START_TIME)
        )
        s.commit()
    for argv in (["queue", "list"], ["matches"], ["block", "list", "Ada"]):
        assert run(argv, clock) == 0
    captured = capsys.readouterr()
    assert "@" not in captured.out + captured.err
    assert "example.com" not in captured.out

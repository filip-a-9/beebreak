"""`beebreak-admin`: invites, users, devices, queue, matches, blocks, accounts.

Owned by M3-01, M4-01 and M6-04.


Exit codes: 0 success, 2 bad input, 1 anything else. Expected errors print one
fixed line to stderr, never a traceback, and never an email address. The only
place an invite code is ever printed is `invite create`, once, on stdout.
"""

import argparse
import contextlib
import json
import os
import re
import sys
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, aliased

from beebreak_server.account_data import (
    AccountBusy,
    AccountDisabled,
    AccountNotFound,
    delete_account,
    export_account,
)
from beebreak_server.config import ServerConfig
from beebreak_server.db import make_engine
from beebreak_server.github import USERNAME_RE, GitHubClient, GitHubUnavailable
from beebreak_server.mailer import Mailer, RecordingMailer, SmtpMailer
from beebreak_server.models import (
    BetaInvite,
    Block,
    DeletedAccountTombstone,
    Device,
    Match,
    MatchParticipant,
    QueueEntry,
    Report,
    ReportMessage,
    User,
    email_canonical,
)
from beebreak_server.tokens import (
    github_tombstone_hmac,
    hmac_code,
    new_code,
    tombstone_hmac,
)

Clock = Callable[[], datetime]

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_BAD_INPUT = 2

_NICKNAME_RE = re.compile(r"^[A-Za-z0-9_-]{3,24}$")


class AdminError(Exception):
    """An expected failure: `message` is fixed text, `code` the exit status."""

    def __init__(self, message: str, code: int = EXIT_FAIL) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _positive_int(raw: str) -> int:
    value = int(raw)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="beebreak-admin")
    sub = parser.add_subparsers(dest="group", required=True)

    invite = sub.add_parser("invite", help="beta invites").add_subparsers(
        dest="action", required=True
    )
    create = invite.add_parser(
        "create", help="create an invite for an email or a GitHub username"
    )
    create.add_argument("email", nargs="?", default=None)
    create.add_argument(
        "--github",
        metavar="USERNAME",
        default=None,
        help="invite a GitHub user instead (the numeric id is looked up once "
        "and is all that is stored)",
    )
    create.add_argument("--days", type=_positive_int, default=None)
    create.add_argument("--send", action="store_true", help="email the code")
    create.add_argument(
        "--override-tombstone",
        action="store_true",
        help="invite an identity whose deleted account was disabled "
        "(removes the record)",
    )
    invite.add_parser("list", help="list invites")
    revoke = invite.add_parser("revoke", help="revoke an invite")
    revoke.add_argument("invite_id")

    user = sub.add_parser("user", help="accounts").add_subparsers(
        dest="action", required=True
    )
    for name in ("disable", "enable"):
        user.add_parser(name).add_argument("nickname")

    sub.add_parser("devices", help="active devices")

    queue = sub.add_parser("queue", help="matchmaking queue").add_subparsers(
        dest="action", required=True
    )
    queue.add_parser("list", help="queued entries (presence field names only)")
    sub.add_parser("matches", help="active matches")

    report = sub.add_parser("report", help="user reports").add_subparsers(
        dest="action", required=True
    )
    rlist = report.add_parser("list", help="reports, oldest first (no user content)")
    rlist.add_argument("--all", action="store_true", help="include resolved reports")
    report.add_parser(
        "show", help="one report with its note and evidence"
    ).add_argument("report_id")
    resolve = report.add_parser("resolve", help="resolve an open report")
    resolve.add_argument("report_id")
    resolve.add_argument("outcome", choices=("dismiss", "disable"))

    block = sub.add_parser("block", help="blocks between users").add_subparsers(
        dest="action", required=True
    )
    block.add_parser("list", help="nicknames a user blocked").add_argument("nickname")
    remove = block.add_parser("remove", help="remove a block")
    remove.add_argument("blocker")
    remove.add_argument("blocked")

    account = sub.add_parser(
        "account", help="data requests for one account"
    ).add_subparsers(dest="action", required=True)
    export = account.add_parser(
        "export", help="the account's data as JSON (contains user content)"
    )
    _add_identity(export)
    export.add_argument("--out", default=None, help="write to PATH with mode 0600")
    delete_ = account.add_parser("delete", help="hard-delete an account")
    _add_identity(delete_)
    delete_.add_argument("--yes", action="store_true", help="confirm the deletion")
    delete_.add_argument(
        "--disabled-ok",
        action="store_true",
        help="allow deleting a disabled account (frees its email and nickname)",
    )
    return parser


def _add_identity(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("email", nargs="?", default=None)
    parser.add_argument(
        "--github-id", type=int, default=None, help="a GitHub account's numeric id"
    )


def _identity(args: argparse.Namespace) -> tuple[str | None, int | None]:
    """The (email, github id) the command line named: exactly one."""
    if (args.email is None) == (args.github_id is None):
        raise AdminError("give either an email or --github-id", EXIT_BAD_INPUT)
    return args.email, args.github_id


def _invite_state(invite: BetaInvite, now: datetime) -> str:
    if invite.revoked_at is not None:
        return "revoked"
    if invite.redeemed_at is not None:
        return "redeemed"
    if invite.expires_at <= now:
        return "expired"
    return "open"


def _format_code(code: str) -> str:
    return f"{code[:4]}-{code[4:]}"


def _mailer_from(config: ServerConfig) -> Mailer:
    if config.mailer == "record":
        return RecordingMailer()
    return SmtpMailer(
        config.smtp_host,
        config.smtp_port,
        config.mail_from,
        tls=config.smtp_tls,
        username=config.smtp_username,
        password=config.smtp_password,
        timeout_s=config.smtp_timeout_s,
        code_ttl_s=config.code_ttl_s,
    )


def _invite_create(
    session: Session,
    config: ServerConfig,
    args: argparse.Namespace,
    now: datetime,
    mailer: Mailer | None,
) -> None:
    if (args.email is None) == (args.github is None):
        raise AdminError("give either an email or --github", EXIT_BAD_INPUT)
    if args.github is not None and args.send:
        raise AdminError("a GitHub invite has no code to send", EXIT_BAD_INPUT)
    email: str | None = None
    github_id: int | None = None
    if args.github is not None:
        if USERNAME_RE.fullmatch(args.github) is None:
            raise AdminError("invalid GitHub username", EXIT_BAD_INPUT)
        if not config.github_enabled:
            raise AdminError("GitHub sign-in is off on this server")
        github_id = _resolve_github(config, args.github)
        key = github_tombstone_hmac(config.secret, github_id)
    else:
        email = email_canonical(args.email)
        if email is None:
            raise AdminError("invalid email address", EXIT_BAD_INPUT)
        if not config.email_enabled:
            raise AdminError("email sign-in is off on this server")
        key = tombstone_hmac(config.secret, email)
    ttl = (
        timedelta(days=args.days)
        if args.days is not None
        else timedelta(seconds=config.invite_ttl_s)
    )
    tombstone = session.scalar(
        select(DeletedAccountTombstone).where(DeletedAccountTombstone.email_hmac == key)
    )
    if tombstone is not None:
        if not args.override_tombstone:
            raise AdminError(
                "this identity belonged to a deleted disabled account; "
                "pass --override-tombstone to invite it anyway"
            )
        # The operator decided: one decision per person.
        session.delete(tombstone)
    if github_id is not None:
        invite = BetaInvite(github_id=github_id, expires_at=now + ttl, created_at=now)
        session.add(invite)
        session.commit()
        print(invite.id)  # there is no code: the person just signs in with GitHub
        return
    assert email is not None
    code = new_code()
    session.add(
        BetaInvite(
            email_canonical=email,
            code_hmac=hmac_code(config.secret, code),
            expires_at=now + ttl,
            created_at=now,
        )
    )
    session.commit()
    print(_format_code(code))
    if args.send:
        if mailer is None:
            mailer = _mailer_from(config)
        # Delivery is best effort and logged without addresses by the mailer;
        # the code above is printed either way so it can be passed on by hand.
        mailer.send_invite(email, code)
        mailer.close()


def _resolve_github(config: ServerConfig, username: str) -> int:
    """The numeric id behind a username: one unauthenticated call, then forgotten."""
    try:
        github_id = GitHubClient(config).lookup_user_id(username)
    except GitHubUnavailable:
        raise AdminError("could not reach GitHub") from None
    if github_id is None:
        raise AdminError("no such GitHub user")
    return github_id


def _invite_list(session: Session, now: datetime) -> None:
    invites = session.scalars(select(BetaInvite).order_by(BetaInvite.created_at))
    for invite in invites:
        who = (
            f"*@{invite.email_canonical.rpartition('@')[2]}"
            if invite.email_canonical is not None
            else "github"  # never the id or the username
        )
        print(
            f"{invite.id}\t{who}\t{_invite_state(invite, now)}\t"
            f"{invite.expires_at.isoformat()}"
        )


def _parse_uuid(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except ValueError:
        raise AdminError("invalid id", EXIT_BAD_INPUT) from None


def _invite_revoke(session: Session, raw_id: str, now: datetime) -> None:
    invite = session.get(BetaInvite, _parse_uuid(raw_id))
    if invite is None:
        raise AdminError("no such invite")
    if invite.redeemed_at is not None:
        raise AdminError("invite already redeemed")
    if invite.revoked_at is None:
        invite.revoked_at = now
        session.commit()


def _find_user(session: Session, nickname: str) -> User:
    if not _NICKNAME_RE.match(nickname):
        raise AdminError("invalid nickname", EXIT_BAD_INPUT)
    user = session.scalars(select(User).where(User.nickname == nickname)).first()
    if user is None:
        raise AdminError("no such user")
    return user


def _user_set_status(
    session: Session, nickname: str, status: str, now: datetime
) -> None:
    user = _find_user(session, nickname)
    _apply_status(session, user, status, now)
    session.commit()


def _apply_status(session: Session, user: User, status: str, now: datetime) -> None:
    user.status = status
    if status == "disabled":
        session.execute(
            update(Device)
            .where(Device.user_id == user.id, Device.revoked_at.is_(None))
            .values(revoked_at=now, access_hash=None, connected_at=None)
        )


def _devices(session: Session) -> None:
    rows = session.execute(
        select(User.nickname, Device)
        .join(Device, Device.user_id == User.id)
        .where(Device.revoked_at.is_(None))
        .order_by(User.nickname, Device.created_at)
    )
    for nickname, device in rows:
        connected = "yes" if device.connected_at is not None else "no"
        last_seen = device.last_seen_at.isoformat() if device.last_seen_at else "-"
        print(f"{nickname or '-'}\t{device.name}\tconnected={connected}\t{last_seen}")


# Presence columns of a queue entry. Only these NAMES are ever printed.
_PRESENCE_FIELDS = (
    "presence_id",
    "task_category",
    "stack_tags",
    "remaining_time_bucket",
)


def _queue_list(session: Session) -> None:
    rows = session.execute(
        select(User.nickname, QueueEntry)
        .join(QueueEntry, QueueEntry.user_id == User.id)
        .where(QueueEntry.status == "queued")
        .order_by(QueueEntry.admitted_at, QueueEntry.id)
    )
    for nickname, entry in rows:
        present = [f for f in _PRESENCE_FIELDS if getattr(entry, f) is not None]
        print(
            f"{nickname or '-'}\t{entry.status}\t{entry.admitted_at.isoformat()}\t"
            f"{entry.expires_at.isoformat()}\tfields={','.join(present) or '-'}"
        )


def _matches(session: Session) -> None:
    for match in session.scalars(
        select(Match)
        .where(Match.status == "active")
        .order_by(Match.created_at, Match.id)
    ):
        nicknames = sorted(
            nickname or "-"
            for nickname in session.scalars(
                select(User.nickname)
                .join(MatchParticipant, MatchParticipant.user_id == User.id)
                .where(MatchParticipant.match_id == match.id)
            )
        )
        messages = session.scalar(
            select(func.coalesce(func.sum(MatchParticipant.messages_sent), 0)).where(
                MatchParticipant.match_id == match.id
            )
        )
        print(
            "\t".join(
                [
                    *nicknames,
                    match.created_at.isoformat(),
                    match.ends_at.isoformat(),
                    f"extensions_used={match.extensions_used}",
                    f"messages={messages}",
                ]
            )
        )


def _find_report(session: Session, raw_id: str) -> Report:
    report = session.get(Report, _parse_uuid(raw_id))
    if report is None:
        raise AdminError("no such report")
    return report


def _report_list(session: Session, include_all: bool) -> None:
    reporter = aliased(User)
    reported = aliased(User)
    evidence = (
        select(func.count())
        .where(ReportMessage.report_id == Report.id)
        .scalar_subquery()
    )
    query = (
        select(Report, reporter.nickname, reported.nickname, evidence)
        .join(reporter, reporter.id == Report.reporter_id)
        .join(reported, reported.id == Report.reported_id)
        .order_by(Report.created_at, Report.id)
    )
    if not include_all:
        query = query.where(Report.status == "open")
    # Never the note or any body here; `report show` prints them.
    for row in session.execute(query):
        report: Report = row[0]
        reporter_name: str | None = row[1]
        reported_name: str | None = row[2]
        count: int = row[3]
        print(
            f"{report.id}\t{report.created_at.isoformat()}\t{report.reason}\t"
            f"{reporter_name or '-'}\t{reported_name or '-'}\t{count}\t{report.status}"
        )


def _report_show(session: Session, raw_id: str) -> None:
    report = _find_report(session, raw_id)
    names = {
        u.id: u.nickname or "-"
        for u in session.scalars(
            select(User).where(User.id.in_([report.reporter_id, report.reported_id]))
        )
    }
    print("WARNING: the output below contains user-written content.")
    print(f"id: {report.id}")
    print(f"match: {report.match_id}")
    print(f"created: {report.created_at.isoformat()}")
    print(f"reason: {report.reason}")
    print(f"reporter: {names.get(report.reporter_id, '-')}")
    print(f"reported: {names.get(report.reported_id, '-')}")
    print(f"status: {report.status}")
    resolved = report.resolved_at.isoformat() if report.resolved_at else "-"
    print(f"resolved: {resolved}")
    print(f"note: {report.note if report.note is not None else '-'}")
    for msg in session.scalars(
        select(ReportMessage)
        .where(ReportMessage.report_id == report.id)
        .order_by(ReportMessage.seq)
    ):
        print(f"[{msg.seq}] {msg.author} {msg.sent_at.isoformat()}: {msg.body}")


def _report_resolve(session: Session, raw_id: str, outcome: str, now: datetime) -> None:
    report = _find_report(session, raw_id)
    if report.status != "open":
        raise AdminError("report already resolved")
    report.status = "dismissed" if outcome == "dismiss" else "actioned"
    report.resolved_at = now
    if outcome == "disable":
        user = session.get(User, report.reported_id)
        if user is not None:
            _apply_status(session, user, "disabled", now)
    session.commit()


def _block_list(session: Session, nickname: str) -> None:
    user = _find_user(session, nickname)
    names = session.scalars(
        select(User.nickname)
        .join(Block, Block.blocked_id == User.id)
        .where(Block.blocker_id == user.id)
        .order_by(User.nickname)
    )
    for name in names:
        print(name or "-")


def _block_remove(session: Session, blocker: str, blocked: str) -> None:
    blocker_user = _find_user(session, blocker)
    blocked_user = _find_user(session, blocked)
    result = session.execute(
        delete(Block).where(
            Block.blocker_id == blocker_user.id, Block.blocked_id == blocked_user.id
        )
    )
    if result.rowcount == 0:  # type: ignore[attr-defined]
        raise AdminError("no such block")
    session.commit()


def _account_export(
    session: Session, email: str | None, github_id: int | None, out: str | None
) -> None:
    try:
        data = export_account(session, email, github_id=github_id)
    except AccountNotFound:
        raise AdminError("no such account") from None
    text = json.dumps(data, indent=2, sort_keys=True)
    if out is None:
        print(text)
        return
    _write_private(out, text + "\n")


def _write_private(path: str, text: str) -> None:
    """Write `path` atomically with mode 0600.

    A fresh temp file in the same directory (O_EXCL, never following a link),
    fsynced and renamed over the target. Any failure removes the temp file and
    leaves an existing target untouched.
    """
    directory = os.path.dirname(os.path.abspath(path))
    temp = os.path.join(directory, f".export-{uuid.uuid4().hex}.tmp")
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temp)
            raise
    except OSError:
        raise AdminError("cannot write the output file") from None


def _account_delete(
    session: Session,
    config: ServerConfig,
    email: str | None,
    github_id: int | None,
    now: datetime,
    allow_disabled: bool,
) -> None:
    try:
        counts = delete_account(
            session,
            email,
            now,
            secret=config.secret,
            github_id=github_id,
            allow_disabled=allow_disabled,
        )
    except AccountNotFound:
        raise AdminError("no such account") from None
    except AccountDisabled:
        raise AdminError(
            "account is disabled; pass --disabled-ok to delete it anyway"
        ) from None
    except AccountBusy:
        raise AdminError(
            "account is queued, in a live match or has an open report; "
            "resolve first, and if devices were already revoked retry after "
            "the next matchmaker sweep"
        ) from None
    # Table names and counts only.
    print(" ".join(f"{table}={count}" for table, count in counts.items()))


def _run(args: argparse.Namespace, clock: Clock, mailer: Mailer | None) -> None:
    try:
        config = ServerConfig.from_env()
    except ValueError as err:
        raise AdminError(str(err), EXIT_BAD_INPUT) from None
    if (args.group, getattr(args, "action", None)) == ("account", "delete") and (
        not args.yes
    ):
        raise AdminError("refusing to delete without --yes", EXIT_BAD_INPUT)
    now = clock()
    engine = make_engine(config.database_url)
    try:
        with Session(engine, expire_on_commit=False) as session:
            match (args.group, getattr(args, "action", None)):
                case ("invite", "create"):
                    _invite_create(session, config, args, now, mailer)
                case ("invite", "list"):
                    _invite_list(session, now)
                case ("invite", "revoke"):
                    _invite_revoke(session, args.invite_id, now)
                case ("user", "disable"):
                    _user_set_status(session, args.nickname, "disabled", now)
                case ("user", "enable"):
                    _user_set_status(session, args.nickname, "active", now)
                case ("devices", _):
                    _devices(session)
                case ("queue", "list"):
                    _queue_list(session)
                case ("matches", _):
                    _matches(session)
                case ("report", "list"):
                    _report_list(session, args.all)
                case ("report", "show"):
                    _report_show(session, args.report_id)
                case ("report", "resolve"):
                    _report_resolve(session, args.report_id, args.outcome, now)
                case ("block", "list"):
                    _block_list(session, args.nickname)
                case ("block", "remove"):
                    _block_remove(session, args.blocker, args.blocked)
                case ("account", "export"):
                    email, github_id = _identity(args)
                    _account_export(session, email, github_id, args.out)
                case ("account", "delete"):
                    email, github_id = _identity(args)
                    _account_delete(
                        session, config, email, github_id, now, args.disabled_ok
                    )
    finally:
        engine.dispose()


def main(
    argv: list[str] | None = None,
    *,
    clock: Clock = _utc_now,
    mailer: Mailer | None = None,
) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exit_:
        return exit_.code if isinstance(exit_.code, int) else EXIT_BAD_INPUT
    try:
        _run(args, clock, mailer)
    except AdminError as err:
        print(err.message, file=sys.stderr)
        return err.code
    except SQLAlchemyError:
        # Driver messages can carry the database URL or row values.
        print("database error", file=sys.stderr)
        return EXIT_FAIL
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

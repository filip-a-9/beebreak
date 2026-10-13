"""`breakroom-admin`: invites, users, devices. Owned by M3-01.

Exit codes: 0 success, 2 bad input, 1 anything else. Expected errors print one
fixed line to stderr, never a traceback, and never an email address. The only
place an invite code is ever printed is `invite create`, once, on stdout.
"""

import argparse
import re
import sys
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from breakroom_server.config import ServerConfig
from breakroom_server.db import make_engine
from breakroom_server.mailer import Mailer, RecordingMailer, SmtpMailer
from breakroom_server.models import BetaInvite, Device, User, email_canonical
from breakroom_server.tokens import hmac_code, new_code

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
    parser = argparse.ArgumentParser(prog="breakroom-admin")
    sub = parser.add_subparsers(dest="group", required=True)

    invite = sub.add_parser("invite", help="beta invites").add_subparsers(
        dest="action", required=True
    )
    create = invite.add_parser("create", help="create an invite for an email")
    create.add_argument("email")
    create.add_argument("--days", type=_positive_int, default=None)
    create.add_argument("--send", action="store_true", help="email the code")
    invite.add_parser("list", help="list invites")
    revoke = invite.add_parser("revoke", help="revoke an invite")
    revoke.add_argument("invite_id")

    user = sub.add_parser("user", help="accounts").add_subparsers(
        dest="action", required=True
    )
    for name in ("disable", "enable"):
        user.add_parser(name).add_argument("nickname")

    sub.add_parser("devices", help="active devices")
    return parser


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
        starttls=config.smtp_starttls,
    )


def _invite_create(
    session: Session,
    config: ServerConfig,
    args: argparse.Namespace,
    now: datetime,
    mailer: Mailer | None,
) -> None:
    email = email_canonical(args.email)
    if email is None:
        raise AdminError("invalid email address", EXIT_BAD_INPUT)
    ttl = (
        timedelta(days=args.days)
        if args.days is not None
        else timedelta(seconds=config.invite_ttl_s)
    )
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


def _invite_list(session: Session, now: datetime) -> None:
    invites = session.scalars(select(BetaInvite).order_by(BetaInvite.created_at))
    for invite in invites:
        domain = invite.email_canonical.rpartition("@")[2]
        print(
            f"{invite.id}\t*@{domain}\t{_invite_state(invite, now)}\t"
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
    user.status = status
    if status == "disabled":
        session.execute(
            update(Device)
            .where(Device.user_id == user.id, Device.revoked_at.is_(None))
            .values(revoked_at=now, access_hash=None, connected_at=None)
        )
    session.commit()


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


def _run(args: argparse.Namespace, clock: Clock, mailer: Mailer | None) -> None:
    try:
        config = ServerConfig.from_env()
    except ValueError as err:
        raise AdminError(str(err), EXIT_BAD_INPUT) from None
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

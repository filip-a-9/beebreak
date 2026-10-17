"""Account data requests: export and hard delete (M6-04).

Both take an open SQLAlchemy session and never read the wall clock or log:
the admin CLI decides what is printed (the export JSON is the only output that
may carry user content).
"""

import uuid
from datetime import datetime
from typing import Any, cast

from sqlalchemy import CursorResult, delete, exists, or_, select, update
from sqlalchemy.orm import Session

from breakroom_server.auth import revoke_device
from breakroom_server.models import (
    AuthChallenge,
    BetaInvite,
    Block,
    DeletedAccountTombstone,
    Device,
    Match,
    MatchParticipant,
    Message,
    QueueEntry,
    RefreshToken,
    Report,
    ReportMessage,
    User,
    UserPreference,
    email_canonical,
)
from breakroom_server.tokens import github_tombstone_hmac, tombstone_hmac

EXPORT_VERSION = 1


class AccountNotFound(LookupError):
    """No account has this (canonicalised) email address."""


class AccountBusy(RuntimeError):
    """Queued, in a live match, or reporter or subject of an open report."""


class AccountDisabled(RuntimeError):
    """The account is disabled (banned) and the caller did not allow deleting it.

    Interim ban-evasion guard: deleting a disabled account frees its email and
    nickname for a fresh sign-up.
    """


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _find(session: Session, email: str | None, github_id: int | None) -> User:
    """The account with this email, or with this GitHub id: exactly one given."""
    if (email is None) == (github_id is None):
        raise AccountNotFound
    if github_id is not None:
        user = session.scalars(select(User).where(User.github_id == github_id)).first()
    else:
        assert email is not None
        canonical = email_canonical(email)
        if canonical is None:
            raise AccountNotFound
        user = session.scalars(
            select(User).where(User.email_canonical == canonical)
        ).first()
    if user is None:
        raise AccountNotFound
    return user


def _invite_clause(user: User) -> Any:
    """Invites for this account's identity (never `col IS NULL` by accident)."""
    if user.github_id is not None:
        return BetaInvite.github_id == user.github_id
    return BetaInvite.email_canonical == user.email_canonical


def export_account(
    session: Session, email: str | None, *, github_id: int | None = None
) -> dict[str, Any]:
    """Everything stored about the account, as JSON-serialisable data.

    Never includes a hash or HMAC column, a peer's message body, or the
    identity or note of anyone who reported this user.
    """
    user = _find(session, email, github_id)
    uid = user.id

    prefs = session.get(UserPreference, uid)
    devices = session.scalars(
        select(Device).where(Device.user_id == uid).order_by(Device.created_at)
    )
    entries = session.scalars(
        select(QueueEntry)
        .where(QueueEntry.user_id == uid)
        .order_by(QueueEntry.admitted_at)
    )
    matches = session.execute(
        select(Match, MatchParticipant)
        .join(MatchParticipant, MatchParticipant.match_id == Match.id)
        .where(MatchParticipant.user_id == uid)
        .order_by(Match.created_at)
    )
    messages = session.scalars(
        select(Message)
        .where(Message.author_id == uid)
        .order_by(Message.match_id, Message.seq)
    )
    filed = session.scalars(
        select(Report).where(Report.reporter_id == uid).order_by(Report.created_at)
    )
    against = session.scalars(
        select(Report).where(Report.reported_id == uid).order_by(Report.created_at)
    )
    blocks = session.execute(
        select(User.nickname, Block.created_at)
        .join(Block, Block.blocked_id == User.id)
        .where(Block.blocker_id == uid)
        .order_by(Block.created_at)
    )
    invites = session.scalars(
        select(BetaInvite).where(_invite_clause(user)).order_by(BetaInvite.created_at)
    )
    # GitHub challenges hold no identity (only a device code), so none is theirs.
    challenges = (
        session.scalars(
            select(AuthChallenge)
            .where(AuthChallenge.email_canonical == user.email_canonical)
            .order_by(AuthChallenge.created_at)
        )
        if user.email_canonical is not None
        else ()
    )

    return {
        "export_version": EXPORT_VERSION,
        "user": {
            "email": user.email_canonical,
            "github_id": user.github_id,
            "nickname": user.nickname,
            "status": user.status,
            "created_at": _iso(user.created_at),
        },
        "preferences": (
            None
            if prefs is None
            else {
                "consent_version": prefs.consent_version,
                "share_task_category": prefs.share_task_category,
                "share_stack_tags": prefs.share_stack_tags,
                "share_time_bucket": prefs.share_time_bucket,
                "consented_at": _iso(prefs.consented_at),
            }
        ),
        "devices": [
            {
                "name": d.name,
                "created_at": _iso(d.created_at),
                "last_seen_at": _iso(d.last_seen_at),
                "connected_at": _iso(d.connected_at),
                "revoked_at": _iso(d.revoked_at),
            }
            for d in devices
        ],
        "queue_entries": [
            {
                "status": e.status,
                "task_category": e.task_category,
                "stack_tags": e.stack_tags,
                "remaining_time_bucket": e.remaining_time_bucket,
                "admitted_at": _iso(e.admitted_at),
                "expires_at": _iso(e.expires_at),
                "ended_at": _iso(e.ended_at),
            }
            for e in entries
        ],
        "matches": [
            {
                "id": str(m.id),
                "created_at": _iso(m.created_at),
                "ended_at": _iso(m.ended_at),
                "end_reason": m.end_reason,
                "shared_tags": m.shared_tags,
                "shared_category": m.shared_category,
                "messages_sent": p.messages_sent,
                "offline_since": _iso(p.offline_since),
            }
            for m, p in matches
        ],
        "messages": [
            {
                "match_id": str(m.match_id),
                "seq": m.seq,
                "sent_at": _iso(m.created_at),
                "body": m.body,
            }
            for m in messages
        ],
        "reports_filed": [
            {
                "reason": r.reason,
                "note": r.note,
                "status": r.status,
                "created_at": _iso(r.created_at),
                "resolved_at": _iso(r.resolved_at),
                # Copies of the user's own messages only, never the peer's.
                "own_evidence": [
                    {"seq": e.seq, "sent_at": _iso(e.sent_at), "body": e.body}
                    for e in session.scalars(
                        select(ReportMessage)
                        .where(
                            ReportMessage.report_id == r.id,
                            ReportMessage.author == "reporter",
                        )
                        .order_by(ReportMessage.seq)
                    )
                ],
            }
            for r in filed
        ],
        # Third-party data: dates and outcome only.
        "reports_against": [
            {
                "created_at": _iso(r.created_at),
                "status": r.status,
                "resolved_at": _iso(r.resolved_at),
            }
            for r in against
        ],
        "blocks": [
            {"blocked_nickname": nickname, "created_at": _iso(created_at)}
            for nickname, created_at in blocks
        ],
        "invites": [
            {
                "created_at": _iso(i.created_at),
                "expires_at": _iso(i.expires_at),
                "redeemed_at": _iso(i.redeemed_at),
                "revoked_at": _iso(i.revoked_at),
            }
            for i in invites
        ],
        "challenges": [
            {
                "created_at": _iso(c.created_at),
                "expires_at": _iso(c.expires_at),
                "consumed_at": _iso(c.consumed_at),
            }
            for c in challenges
        ],
    }


def _count(session: Session, statement: Any) -> int:
    return cast("CursorResult[Any]", session.execute(statement)).rowcount


def _is_busy(session: Session, uid: uuid.UUID) -> bool:
    queued = exists().where(QueueEntry.user_id == uid, QueueEntry.status == "queued")
    live = exists().where(
        MatchParticipant.user_id == uid,
        MatchParticipant.match_id == Match.id,
        Match.status != "ended",
    )
    open_report = exists().where(
        or_(Report.reporter_id == uid, Report.reported_id == uid),
        Report.status == "open",
    )
    return bool(
        session.scalar(select(queued))
        or session.scalar(select(live))
        or session.scalar(select(open_report))
    )


def prepare_delete(
    session: Session,
    email: str | None,
    now: datetime,
    *,
    github_id: int | None = None,
    allow_disabled: bool = False,
) -> uuid.UUID:
    """Phase one of a delete; returns the user id for `finish_delete`.

    Checks not-found, disabled and busy first and refuses with no change.
    Then, in its own committed transaction, revokes every device of the user
    and deletes their refresh tokens, so the user can no longer act between
    the busy check and the deletes (READ COMMITTED, no lock).
    """
    user = _find(session, email, github_id)
    if user.status == "disabled" and not allow_disabled:
        raise AccountDisabled
    if _is_busy(session, user.id):
        raise AccountBusy
    devices = session.scalars(
        select(Device).where(Device.user_id == user.id, Device.revoked_at.is_(None))
    ).all()
    for device in devices:
        revoke_device(session, device, now)
    session.commit()
    return user.id


def finish_delete(
    session: Session, user_id: uuid.UUID, now: datetime, secret: bytes
) -> dict[str, int]:
    """Phase two: re-check busy, then delete everything in one transaction.

    Raises `AccountBusy` if the user became busy after `prepare_delete`; the
    devices stay revoked and nothing is deleted. A disabled user leaves one
    tombstone (keyed hash of the email or GitHub id), written in the same transaction.
    Returns per-table counts.
    """
    user = session.get(User, user_id)
    if user is None:
        raise AccountNotFound
    if _is_busy(session, user_id):
        session.rollback()
        raise AccountBusy
    counts: dict[str, int] = {}
    try:
        _delete_rows(session, user, counts, now, secret)
        session.commit()
    except BaseException:
        session.rollback()
        raise
    return counts


def delete_account(
    session: Session,
    email: str | None,
    now: datetime,
    *,
    secret: bytes,
    github_id: int | None = None,
    allow_disabled: bool = False,
) -> dict[str, int]:
    """Hard-delete the account and every row that references it.

    Refuses (`AccountNotFound`, `AccountDisabled`, `AccountBusy`) with no
    change; see `prepare_delete` and `finish_delete` for the two phases. Busy
    means queued, in a live match, or reporter or subject of an open report.
    """
    user_id = prepare_delete(
        session, email, now, github_id=github_id, allow_disabled=allow_disabled
    )
    return finish_delete(session, user_id, now, secret)


def _delete_rows(
    session: Session,
    user: User,
    counts: dict[str, int],
    now: datetime,
    secret: bytes,
) -> None:
    uid = user.id
    mine = select(MatchParticipant.match_id).where(MatchParticipant.user_id == uid)
    # Matches in which nobody else took part: they go away with the user.
    solo = [
        match_id
        for match_id in session.scalars(mine)
        if not session.scalars(
            select(MatchParticipant.user_id).where(
                MatchParticipant.match_id == match_id,
                MatchParticipant.user_id != uid,
            )
        ).first()
    ]

    reports = select(Report.id).where(
        or_(Report.reporter_id == uid, Report.reported_id == uid)
    )
    counts["report_messages"] = _count(
        session, delete(ReportMessage).where(ReportMessage.report_id.in_(reports))
    )
    counts["reports"] = _count(
        session,
        delete(Report).where(or_(Report.reporter_id == uid, Report.reported_id == uid)),
    )

    counts["messages"] = _count(
        session,
        delete(Message).where(
            or_(Message.author_id == uid, Message.match_id.in_(solo))
        ),
    )
    counts["queue_entries"] = _count(
        session,
        delete(QueueEntry).where(
            or_(QueueEntry.user_id == uid, QueueEntry.match_id.in_(solo))
        ),
    )
    session.execute(
        update(Match)
        .where(Match.extension_requested_by == uid)
        .values(extension_requested_by=None)
    )
    counts["match_participants"] = _count(
        session, delete(MatchParticipant).where(MatchParticipant.user_id == uid)
    )
    counts["matches"] = _count(session, delete(Match).where(Match.id.in_(solo)))
    counts["blocks"] = _count(
        session,
        delete(Block).where(or_(Block.blocker_id == uid, Block.blocked_id == uid)),
    )

    devices = select(Device.id).where(Device.user_id == uid)
    counts["refresh_tokens"] = _count(
        session, delete(RefreshToken).where(RefreshToken.device_id.in_(devices))
    )
    counts["devices"] = _count(session, delete(Device).where(Device.user_id == uid))
    counts["user_preferences"] = _count(
        session, delete(UserPreference).where(UserPreference.user_id == uid)
    )

    invites = select(BetaInvite.id).where(_invite_clause(user))
    if user.email_canonical is not None:
        counts["auth_challenges"] = _count(
            session,
            delete(AuthChallenge).where(
                or_(
                    AuthChallenge.email_canonical == user.email_canonical,
                    AuthChallenge.invite_id.in_(invites),
                )
            ),
        )
    else:
        counts["auth_challenges"] = _count(
            session, delete(AuthChallenge).where(AuthChallenge.invite_id.in_(invites))
        )
    # An invite for another identity that this user redeemed is not theirs to
    # delete; it only loses the pointer.
    session.execute(
        update(BetaInvite)
        .where(BetaInvite.redeemed_by == uid, ~_invite_clause(user))
        .values(redeemed_by=None)
    )
    counts["beta_invites"] = _count(
        session, delete(BetaInvite).where(_invite_clause(user))
    )
    counts["users"] = _count(session, delete(User).where(User.id == uid))
    # Only a disabled account leaves one, so a banned person who asks for
    # deletion cannot come back as a new user unnoticed.
    counts["tombstones"] = 0
    if user.status == "disabled":
        key = (
            github_tombstone_hmac(secret, user.github_id)
            if user.github_id is not None
            else tombstone_hmac(secret, user.email_canonical or "")
        )
        if not session.scalar(
            select(DeletedAccountTombstone.id).where(
                DeletedAccountTombstone.email_hmac == key
            )
        ):
            session.add(DeletedAccountTombstone(email_hmac=key, created_at=now))
        counts["tombstones"] = 1

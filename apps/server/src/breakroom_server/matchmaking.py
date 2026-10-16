"""Matchmaker: expiry, match ends, pairing and retention purge. Owned by M4-03.

`run_once()` does one pass and notifies users through the `Notifier`; `run()`
repeats it on every `kick()` and every `match_sweep_interval_s`. Single server
instance (spec §9.3); uniqueness is enforced by the database (partial unique
indexes plus conditional updates), not by the in-process lock.

Logs carry counts only: never presence fields, tags, buckets or nicknames.
"""

import asyncio
import contextlib
import logging
import threading
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import (
    ColumnElement,
    CursorResult,
    delete,
    exists,
    func,
    or_,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from breakroom_server.config import ServerConfig
from breakroom_server.db import SessionFactory
from breakroom_server.models import (
    AuthChallenge,
    BetaInvite,
    Block,
    Match,
    MatchParticipant,
    Message,
    QueueEntry,
    RefreshToken,
    Report,
    ReportMessage,
    User,
    join_tags,
    split_tags,
)
from breakroom_server.notify import Notifier, make_frame
from breakroom_server.queue import chat_status, match_view
from breakroom_shared.api import (
    ChatChange,
    WsChatStatus,
    WsMatchCreated,
    WsMatchEnded,
    WsQueueEnded,
)

_log = logging.getLogger(__name__)

# The purge runs at most this often (seconds of injected-clock time).
_PURGE_INTERVAL_S = 60
_BUCKET_ORDER = ("under_5m", "5_10m", "10_20m", "20m_plus")
_UNKNOWN_BUCKET_DISTANCE = 1


@dataclass(frozen=True)
class MatchCreated:
    match_id: UUID
    users: tuple[UUID, UUID]


@dataclass(frozen=True)
class _Candidate:
    """A queued entry as the pairing pass sees it (immutable while queued)."""

    entry_id: UUID
    user_id: UUID
    admitted_at: datetime
    category: str | None
    tags: frozenset[str]
    has_tags: bool
    bucket: str | None


def _bucket_distance(a: str | None, b: str | None) -> int:
    if a not in _BUCKET_ORDER or b not in _BUCKET_ORDER:
        return _UNKNOWN_BUCKET_DISTANCE
    return abs(_BUCKET_ORDER.index(a) - _BUCKET_ORDER.index(b))


def _presence_cleared() -> dict[str, None]:
    return {
        "presence_id": None,
        "task_category": None,
        "stack_tags": None,
        "remaining_time_bucket": None,
    }


class Matchmaker:
    def __init__(
        self,
        factory: SessionFactory,
        config: ServerConfig,
        clock: Callable[[], datetime],
        notifier: Notifier,
    ) -> None:
        self._factory = factory
        self._config = config
        self._clock = clock
        self._notifier = notifier
        # Serializes passes in this process. Private: the race tests swap it
        # for `nullcontext()` to prove the database alone keeps matches unique.
        self._pass_lock: AbstractContextManager[Any] = threading.Lock()
        self._last_purge: datetime | None = None
        # False until this instance's first presence pass has run.
        self._presence_primed = False
        self._kick_lock = threading.Lock()
        self._kicked = False
        self._wake: tuple[asyncio.AbstractEventLoop, asyncio.Event] | None = None

    # --- loop ----------------------------------------------------------------

    def kick(self) -> None:
        """Wake the loop for a pass soon. Thread-safe."""
        with self._kick_lock:
            self._kicked = True
            wake = self._wake
        if wake is not None:
            loop, event = wake
            with contextlib.suppress(RuntimeError):  # loop closed: shutting down
                loop.call_soon_threadsafe(event.set)

    async def run(self, stop: asyncio.Event) -> None:
        """Loop until `stop` is set: wait for a kick or the sweep interval."""
        loop = asyncio.get_running_loop()
        event = asyncio.Event()
        with self._kick_lock:
            self._wake = (loop, event)
            if self._kicked:
                event.set()
        stop_task = asyncio.ensure_future(stop.wait())
        try:
            while not stop.is_set():
                kick_task = asyncio.ensure_future(event.wait())
                await asyncio.wait(
                    {stop_task, kick_task},
                    timeout=self._config.match_sweep_interval_s,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                kick_task.cancel()
                if stop.is_set():
                    break
                with self._kick_lock:
                    self._kicked = False
                event.clear()
                try:
                    await run_in_threadpool(self.run_once)
                except Exception:
                    _log.error("matchmaker pass failed")
        finally:
            with self._kick_lock:
                self._wake = None
            stop_task.cancel()

    # --- one pass --------------------------------------------------------------

    def run_once(self) -> list[MatchCreated]:
        """Expire, end, observe presence, pair, purge; notify. Returns the matches
        created."""
        with self._pass_lock:
            now = self._clock()
            self._expire_entries(now)
            self._end_matches(now)
            self._observe_presence(now)
            created = self._pair(now)
            self._purge(now)
        return created

    def _send(self, user_id: UUID, frame: BaseModel) -> None:
        try:
            self._notifier.send_to_user(user_id, frame)
        except Exception:
            _log.error("notify failed")

    # 1. expire ------------------------------------------------------------------

    def _expire_entries(self, now: datetime) -> None:
        with self._factory() as session:
            rows = session.execute(
                update(QueueEntry)
                .where(QueueEntry.status == "queued", QueueEntry.expires_at <= now)
                .values(status="expired", ended_at=now, **_presence_cleared())
                .returning(QueueEntry.id, QueueEntry.user_id)
                .execution_options(synchronize_session=False)
            ).all()
            session.commit()
        for entry_id, user_id in rows:
            self._send(
                user_id,
                make_frame(WsQueueEnded, now, entry_id=entry_id, reason="expired"),
            )
        if rows:
            _log.info("expired entries=%d", len(rows))

    # 2. end ---------------------------------------------------------------------

    def _end_matches(self, now: datetime) -> None:
        with self._factory() as session:
            due = list(
                session.scalars(
                    select(Match.id).where(
                        Match.status == "active", Match.ends_at <= now
                    )
                )
            )
        # Re-checked in the UPDATE: an extension accepted after this read moved
        # `ends_at`, and that chat must stay open.
        expired_matches = self._end_match_ids(
            now, due, "expired", only_if=(Match.ends_at <= now,)
        )
        for match_id, users in expired_matches:
            for user_id in users:
                self._send(
                    user_id,
                    make_frame(WsMatchEnded, now, match_id=match_id, reason="expired"),
                )

        # Disabled users (admin CLI writes only users.status).
        with self._factory() as session:
            cancelled = session.execute(
                update(QueueEntry)
                .where(
                    QueueEntry.status == "queued",
                    QueueEntry.user_id.in_(
                        select(User.id).where(User.status == "disabled")
                    ),
                )
                .values(status="cancelled", ended_at=now, **_presence_cleared())
                .execution_options(synchronize_session=False)
            )
            session.commit()
            n_cancelled = cast("CursorResult[Any]", cancelled).rowcount
            ids = list(
                session.scalars(
                    select(Match.id)
                    .join(MatchParticipant, MatchParticipant.match_id == Match.id)
                    .join(User, User.id == MatchParticipant.user_id)
                    .where(
                        Match.status == "active",
                        MatchParticipant.active.is_(True),
                        User.status == "disabled",
                    )
                    .distinct()
                )
            )
        ended = self._end_match_ids(now, ids, "disabled", notify_active_only=True)
        for match_id, users in ended:
            for user_id in users:
                self._send(
                    user_id,
                    make_frame(WsMatchEnded, now, match_id=match_id, reason="ended"),
                )
        if n_cancelled or ended or expired_matches:
            _log.info(
                "ended matches=%d disabled=%d cancelled entries=%d",
                len(expired_matches),
                len(ended),
                n_cancelled,
            )

    def _end_match_ids(
        self,
        now: datetime,
        match_ids: list[UUID],
        reason: str,
        *,
        notify_active_only: bool = False,
        only_if: tuple[ColumnElement[bool], ...] = (),
    ) -> list[tuple[UUID, list[UUID]]]:
        """End the still-active matches among `match_ids` that also meet
        `only_if`; (match, users to tell)."""
        if not match_ids:
            return []
        with self._factory() as session:
            won = list(
                session.scalars(
                    update(Match)
                    .where(Match.id.in_(match_ids), Match.status == "active", *only_if)
                    .values(
                        status="ended",
                        ended_at=now,
                        end_reason=reason,
                        shared_tags=None,
                        shared_category=None,
                        extension_requested_by=None,
                    )
                    .returning(Match.id)
                    .execution_options(synchronize_session=False)
                )
            )
            result: list[tuple[UUID, list[UUID]]] = []
            if won:
                people = session.execute(
                    select(
                        MatchParticipant.match_id,
                        MatchParticipant.user_id,
                        User.status,
                    )
                    .join(User, User.id == MatchParticipant.user_id)
                    .where(MatchParticipant.match_id.in_(won))
                ).all()
                session.execute(
                    update(MatchParticipant)
                    .where(MatchParticipant.match_id.in_(won))
                    .values(active=False, offline_since=None)
                    .execution_options(synchronize_session=False)
                )
                for match_id in won:
                    result.append(
                        (
                            match_id,
                            [
                                uid
                                for mid, uid, status in people
                                if mid == match_id
                                and (not notify_active_only or status == "active")
                            ],
                        )
                    )
            session.commit()
        return result

    # 2b. presence ---------------------------------------------------------------

    def _observe_presence(self, now: datetime) -> None:
        """Hold chats through short disconnects; end them after `chat_disconnect_s`.

        The first pass of this instance restarts every offline clock, so a server
        restart never ends a chat by itself.
        """
        active = (
            Match.status == "active",
            MatchParticipant.active.is_(True),
        )
        if not self._presence_primed:
            with self._factory() as session:
                session.execute(
                    update(MatchParticipant)
                    .where(
                        MatchParticipant.offline_since.is_not(None),
                        MatchParticipant.match_id.in_(
                            select(Match.id).where(Match.status == "active")
                        ),
                    )
                    .values(offline_since=now)
                    .execution_options(synchronize_session=False)
                )
                session.commit()
            self._presence_primed = True

        with self._factory() as session:
            rows = session.execute(
                select(
                    MatchParticipant.match_id,
                    MatchParticipant.user_id,
                    MatchParticipant.offline_since,
                )
                .join(Match, Match.id == MatchParticipant.match_id)
                .where(*active)
            ).all()
        peers: dict[UUID, dict[UUID, datetime | None]] = {}
        for match_id, user_id, offline_since in rows:
            peers.setdefault(match_id, {})[user_id] = offline_since

        to_end: list[UUID] = []
        n_offline = n_online = 0
        for match_id, members in peers.items():
            for user_id, offline_since in members.items():
                connected = self._notifier.is_user_connected(user_id)
                if connected and offline_since is not None:
                    if self._flip(now, match_id, user_id, going_offline=False):
                        n_online += 1
                elif not connected and offline_since is None:
                    if self._flip(now, match_id, user_id, going_offline=True):
                        n_offline += 1
                elif not connected and offline_since is not None:
                    held = (now - offline_since).total_seconds()
                    if held >= self._config.chat_disconnect_s:
                        to_end.append(match_id)
        ended = self._end_match_ids(now, list(dict.fromkeys(to_end)), "disconnected")
        for match_id, users in ended:
            for user_id in users:
                self._send(
                    user_id,
                    make_frame(
                        WsMatchEnded, now, match_id=match_id, reason="disconnected"
                    ),
                )
        if n_offline or n_online or ended:
            _log.info(
                "presence offline=%d online=%d disconnected matches=%d",
                n_offline,
                n_online,
                len(ended),
            )

    def _flip(
        self, now: datetime, match_id: UUID, user_id: UUID, *, going_offline: bool
    ) -> bool:
        """Set or clear one participant's `offline_since`; tell the peer if it
        changed here (the conditional update is what stops a double send)."""
        with self._factory() as session:
            column = MatchParticipant.offline_since
            flipped = cast(
                "CursorResult[Any]",
                session.execute(
                    update(MatchParticipant)
                    .where(
                        MatchParticipant.match_id == match_id,
                        MatchParticipant.user_id == user_id,
                        MatchParticipant.active.is_(True),
                        column.is_(None) if going_offline else column.is_not(None),
                    )
                    .values(offline_since=now if going_offline else None)
                    .execution_options(synchronize_session=False)
                ),
            ).rowcount
            frame: BaseModel | None = None
            peer_id: UUID | None = None
            match = session.get(Match, match_id)
            if flipped and match is not None and match.status == "active":
                peer_id = session.scalars(
                    select(MatchParticipant.user_id).where(
                        MatchParticipant.match_id == match_id,
                        MatchParticipant.user_id != user_id,
                        MatchParticipant.active.is_(True),
                    )
                ).first()
                if peer_id is not None:
                    change: ChatChange = (
                        "peer_offline" if going_offline else "peer_online"
                    )
                    frame = make_frame(
                        WsChatStatus,
                        now,
                        status=chat_status(session, self._config, match, peer_id),
                        change=change,
                    )
            session.commit()
        # An offline peer cannot be told; it fetches the state on reconnect.
        if (
            frame is not None
            and peer_id is not None
            and self._notifier.is_user_connected(peer_id)
        ):
            self._send(peer_id, frame)
        return bool(flipped)

    # 3. pair --------------------------------------------------------------------

    def _pair(self, now: datetime) -> list[MatchCreated]:
        candidates, blocked, recent = self._load_candidates(now)
        pairs = self._plan(candidates, blocked, recent)
        created: list[MatchCreated] = []
        for first, second in pairs:
            outcome = self._create_match(now, first, second)
            if outcome is None:
                continue
            match, frames = outcome
            created.append(match)
            for user_id, frame in frames:
                self._send(user_id, frame)
        if created:
            _log.info("matched pairs=%d", len(created))
        return created

    def _load_candidates(
        self, now: datetime
    ) -> tuple[list[_Candidate], set[frozenset[UUID]], set[frozenset[UUID]]]:
        # A plain read: it is only a plan. The locked re-check and conditional
        # claim in `_create_match` are what keep a pair from forming twice.
        with self._factory() as session:
            entries = session.scalars(
                select(QueueEntry)
                .join(User, User.id == QueueEntry.user_id)
                .where(
                    QueueEntry.status == "queued",
                    QueueEntry.expires_at > now,
                    User.status == "active",
                    # A stray entry of a matched user can never be claimed (the
                    # active-participant index); planning it would starve others.
                    ~exists().where(
                        MatchParticipant.user_id == QueueEntry.user_id,
                        MatchParticipant.active.is_(True),
                    ),
                )
                .order_by(QueueEntry.admitted_at, QueueEntry.id)
            ).all()
            candidates = [
                _Candidate(
                    entry_id=e.id,
                    user_id=e.user_id,
                    admitted_at=e.admitted_at,
                    category=e.task_category,
                    tags=frozenset(split_tags(e.stack_tags)),
                    has_tags=bool(e.stack_tags),
                    bucket=e.remaining_time_bucket,
                )
                for e in entries
                if self._notifier.is_user_connected(e.user_id)
            ]
            user_ids = [c.user_id for c in candidates]
            blocked: set[frozenset[UUID]] = set()
            recent: set[frozenset[UUID]] = set()
            if len(user_ids) >= 2:
                for blocker, blocked_id in session.execute(
                    select(Block.blocker_id, Block.blocked_id).where(
                        Block.blocker_id.in_(user_ids), Block.blocked_id.in_(user_ids)
                    )
                ):
                    blocked.add(frozenset((blocker, blocked_id)))
                cutoff = now - timedelta(seconds=self._config.rematch_penalty_s)
                members: dict[UUID, set[UUID]] = {}
                for match_id, user_id in session.execute(
                    select(MatchParticipant.match_id, MatchParticipant.user_id)
                    .join(Match, Match.id == MatchParticipant.match_id)
                    .where(
                        Match.created_at > cutoff,
                        MatchParticipant.user_id.in_(user_ids),
                    )
                ):
                    members.setdefault(match_id, set()).add(user_id)
                for users in members.values():
                    if len(users) == 2:
                        recent.add(frozenset(users))
            session.rollback()
        return candidates, blocked, recent

    @staticmethod
    def _plan(
        candidates: list[_Candidate],
        blocked: set[frozenset[UUID]],
        recent: set[frozenset[UUID]],
    ) -> list[tuple[_Candidate, _Candidate]]:
        """Oldest first; each picks the best partner among the unplaced."""
        placed: set[UUID] = set()
        pairs: list[tuple[_Candidate, _Candidate]] = []
        for entry in candidates:
            if entry.entry_id in placed:
                continue
            best: tuple[tuple[Any, ...], _Candidate] | None = None
            for other in candidates:
                if other.entry_id in placed or other.entry_id == entry.entry_id:
                    continue
                users = frozenset((entry.user_id, other.user_id))
                if len(users) != 2 or users in blocked:
                    continue
                key = (
                    1 if users in recent else 0,
                    _bucket_distance(entry.bucket, other.bucket),
                    other.admitted_at,
                    -len(entry.tags & other.tags),
                    other.entry_id,
                )
                if best is None or key < best[0]:
                    best = (key, other)
            if best is None:
                continue
            placed.add(entry.entry_id)
            placed.add(best[1].entry_id)
            pairs.append((entry, best[1]))
        return pairs

    def _create_match(
        self, now: datetime, a: _Candidate, b: _Candidate
    ) -> tuple[MatchCreated, list[tuple[UUID, BaseModel]]] | None:
        """One transaction: the match, two participants, both entries claimed.

        None when a row was claimed or ended meanwhile, the pair got blocked or a user
        disabled since the plan, or a user already has an active match (the
        partial unique indexes say no).
        """
        ids = [a.entry_id, b.entry_id]
        with self._factory() as session:
            try:
                live = session.scalars(
                    select(QueueEntry.id)
                    .where(
                        QueueEntry.id.in_(ids),
                        QueueEntry.status == "queued",
                        QueueEntry.expires_at > now,
                    )
                    .with_for_update(skip_locked=True)
                ).all()
                if len(live) != 2:
                    session.rollback()
                    return None
                # The plan is a plain read: a block or an admin disable may have
                # committed since. Re-check both inside the claim.
                users = [a.user_id, b.user_id]
                blocked = session.scalar(
                    select(
                        exists().where(
                            Block.blocker_id.in_(users), Block.blocked_id.in_(users)
                        )
                    )
                )
                active = session.scalars(
                    select(User.id).where(User.id.in_(users), User.status == "active")
                ).all()
                if blocked or len(active) != 2:
                    session.rollback()
                    return None
                shared = (
                    join_tags(a.tags & b.tags) if a.has_tags and b.has_tags else None
                )
                category = (
                    a.category
                    if a.category is not None
                    and a.category == b.category
                    and a.category != "other"
                    else None
                )
                match = Match(
                    status="active",
                    created_at=now,
                    ends_at=now + timedelta(seconds=self._config.match_ttl_s),
                    shared_tags=shared,
                    shared_category=category,
                )
                session.add(match)
                session.flush()
                session.add_all(
                    [
                        MatchParticipant(
                            match_id=match.id, user_id=a.user_id, active=True
                        ),
                        MatchParticipant(
                            match_id=match.id, user_id=b.user_id, active=True
                        ),
                    ]
                )
                session.flush()
                claimed = cast(
                    "CursorResult[Any]",
                    session.execute(
                        update(QueueEntry)
                        .where(
                            QueueEntry.id.in_(ids),
                            QueueEntry.status == "queued",
                            QueueEntry.expires_at > now,
                        )
                        .values(
                            status="matched",
                            match_id=match.id,
                            ended_at=now,
                            **_presence_cleared(),
                        )
                        .execution_options(synchronize_session=False)
                    ),
                )
                if claimed.rowcount != 2:
                    session.rollback()
                    return None
                frames: list[tuple[UUID, BaseModel]] = [
                    (
                        mine.user_id,
                        make_frame(
                            WsMatchCreated,
                            now,
                            entry_id=mine.entry_id,
                            match=match_view(
                                session, self._config, match, mine.user_id
                            ),
                        ),
                    )
                    for mine in (a, b)
                ]
                match_id = match.id
                session.commit()
            except IntegrityError:
                session.rollback()
                return None
        return MatchCreated(match_id, (a.user_id, b.user_id)), frames

    # 4. purge -------------------------------------------------------------------

    def _purge(self, now: datetime) -> None:
        last = self._last_purge
        if last is not None and now - last < timedelta(seconds=_PURGE_INTERVAL_S):
            return
        self._last_purge = now
        cutoff = now - timedelta(seconds=self._config.retention_s)
        message_cutoff = now - timedelta(seconds=self._config.message_retention_s)
        report_cutoff = now - timedelta(seconds=self._config.report_retention_s)
        with self._factory() as session:
            msgs = cast(
                "CursorResult[Any]",
                session.execute(
                    delete(Message).where(
                        Message.match_id.in_(
                            select(Match.id).where(
                                Match.status == "ended", Match.ended_at < message_cutoff
                            )
                        )
                    )
                ),
            ).rowcount
            resolved = select(Report.id).where(
                Report.resolved_at.is_not(None), Report.resolved_at < report_cutoff
            )
            session.execute(
                delete(ReportMessage).where(ReportMessage.report_id.in_(resolved))
            )
            reports = cast(
                "CursorResult[Any]",
                session.execute(delete(Report).where(Report.id.in_(resolved))),
            ).rowcount
            entries = cast(
                "CursorResult[Any]",
                session.execute(
                    delete(QueueEntry).where(
                        QueueEntry.status != "queued", QueueEntry.ended_at < cutoff
                    )
                ),
            ).rowcount
            old = select(Match.id).where(
                Match.status == "ended",
                Match.ended_at < cutoff,
                ~exists().where(QueueEntry.match_id == Match.id),
            )
            session.execute(delete(Message).where(Message.match_id.in_(old)))
            session.execute(
                delete(MatchParticipant).where(MatchParticipant.match_id.in_(old))
            )
            matches = cast(
                "CursorResult[Any]",
                session.execute(delete(Match).where(Match.id.in_(old))),
            ).rowcount
            challenges, tokens, invites = self._purge_auth(session, now)
            session.commit()
        if entries or matches or msgs or reports or challenges or tokens or invites:
            _log.info(
                "purged entries=%d matches=%d messages=%d reports=%d "
                "challenges=%d tokens=%d invites=%d",
                entries,
                matches,
                msgs,
                reports,
                challenges,
                tokens,
                invites,
            )

    def _purge_auth(self, session: Session, now: datetime) -> tuple[int, int, int]:
        """Delete stale sign-in rows; returns (challenges, tokens, invites)."""
        config = self._config
        challenge_cutoff = now - timedelta(seconds=config.challenge_retention_s)
        token_cutoff = now - timedelta(seconds=config.refresh_token_retention_s)
        invite_cutoff = now - timedelta(seconds=config.invite_retention_s)
        challenges = cast(
            "CursorResult[Any]",
            session.execute(
                delete(AuthChallenge).where(
                    func.coalesce(AuthChallenge.consumed_at, AuthChallenge.expires_at)
                    < challenge_cutoff
                )
            ),
        ).rowcount
        # Purged by expiry only, used or not. auth.refresh_tokens rejects an
        # expired token before reuse detection runs, and a used row is what
        # reuse detection (device revocation on replay) needs. Keeping every
        # row until expiry + retention keeps replay detection for the token's
        # whole lifetime; a used but unexpired row survives any age.
        tokens = cast(
            "CursorResult[Any]",
            session.execute(
                delete(RefreshToken).where(RefreshToken.expires_at < token_cutoff)
            ),
        ).rowcount
        # An invite is terminal once revoked, redeemed or (unredeemed) expired;
        # the earliest of those is below the cutoff iff any applicable one is.
        # An invite a retained challenge still references waits for a later
        # sweep: the foreign key must never break.
        invites = cast(
            "CursorResult[Any]",
            session.execute(
                delete(BetaInvite).where(
                    or_(
                        BetaInvite.revoked_at < invite_cutoff,
                        BetaInvite.redeemed_at < invite_cutoff,
                        (BetaInvite.redeemed_at.is_(None))
                        & (BetaInvite.expires_at < invite_cutoff),
                    ),
                    ~exists().where(AuthChallenge.invite_id == BetaInvite.id),
                )
            ),
        ).rowcount
        return challenges, tokens, invites

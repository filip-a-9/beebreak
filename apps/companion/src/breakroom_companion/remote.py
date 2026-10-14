"""Background worker for the server connection. Owned by M3-05a.

Runs its own asyncio loop in a daemon thread. Public methods are thread-safe
and only schedule work; results come back as Qt signals (queued to the thread
that owns the worker). The access token lives in memory only; the rotated
refresh token is persisted before the new access token is used.

Never touches the network until a stored login exists or the user starts a
sign-in. No log line or signal carries an email, code, token or nickname.
"""

import asyncio
import contextlib
import logging
import random
import re
import socket
import threading
from collections.abc import Awaitable, Callable, Coroutine
from datetime import UTC, datetime
from typing import Any, Literal, TypeVar
from uuid import UUID, uuid4

from PySide6.QtCore import QObject, Signal

from breakroom_companion.api_client import ApiClient, ApiError
from breakroom_companion.break_state import (
    BreakEvent,
    BreakState,
    Dismiss,
    Frame,
    Joined,
    JoinFailed,
    Joining,
    SignedOut,
    Sync,
    reduce,
)
from breakroom_companion.chat_state import (
    ChatEvent,
    ChatFrame,
    ChatState,
    Closed,
    Ended,
    Matched,
    Messages,
    Reported,
    SendFailed,
    Sending,
    ServerTime,
    StatusEvent,
)
from breakroom_companion.chat_state import SignedOut as ChatSignedOut
from breakroom_companion.chat_state import reduce as chat_reduce
from breakroom_companion.config import worker_start_timeout_s, worker_stop_timeout_s
from breakroom_companion.credentials import CredentialStore, StoredCredentials
from breakroom_companion.realtime_client import (
    RealtimeClient,
    RealtimeError,
    ServerFrame,
)
from breakroom_companion.remote_config import RemoteConfig
from breakroom_shared.api import (
    WS_CLOSE,
    Consent,
    ExtensionAction,
    MatchView,
    Me,
    MessagePage,
    MessageView,
    QueueJoinRequest,
    ReportReason,
    ReportRequest,
    SendMessageRequest,
    TokenPair,
    WsChatMessage,
    WsChatStatus,
    WsMatchCreated,
    WsMatchEnded,
    WsQueueEnded,
    WsQueueJoined,
    WsReady,
    WsSync,
)
from breakroom_shared.chat import MAX_NOTE_CHARS, normalize_text
from breakroom_shared.presence import OutboundPresence

ConnectionState = Literal[
    "signed_out", "signing_in", "connecting", "connected", "offline"
]

_log = logging.getLogger(__name__)
_T = TypeVar("_T")

REFRESH_LEAD_S = 60.0
# The server's default access lifetime; the fallback when our clock disagrees.
NOMINAL_ACCESS_TTL_S = 900.0
MIN_REFRESH_DELAY_S = 0.05
_BAD_DEVICE_CHARS = re.compile(r"[^A-Za-z0-9 ._-]")


def _device_name() -> str:
    try:
        name = _BAD_DEVICE_CHARS.sub("", socket.gethostname()).strip()[:40]
    except OSError:
        name = ""
    return name or "companion"


def refresh_delay_s(ttl_s: float) -> float:
    """Seconds after receiving a token to refresh it: 60 s before expiry, or at
    half the lifetime when the lifetime is short.

    `ttl_s` is the server's expiry minus our clock, so clock skew can make it
    negative or longer than any real lifetime; either would mean a refresh
    storm or a missed expiry, so it is replaced by the nominal lifetime.
    """
    if ttl_s <= 0 or ttl_s > NOMINAL_ACCESS_TTL_S:
        ttl_s = NOMINAL_ACCESS_TTL_S
    return max(MIN_REFRESH_DELAY_S, ttl_s - REFRESH_LEAD_S, ttl_s / 2)


class RemoteWorker(QObject):
    """Create on the Qt main thread; signals are delivered there (queued)."""

    state_changed = Signal(str)  # ConnectionState
    me_changed = Signal(object)  # Me | None
    error = Signal(str)  # ErrorCode, "network" or "replaced"
    break_changed = Signal(object)  # BreakState (M4-05)
    chat_changed = Signal(object)  # ChatState (M5-05)

    def __init__(self, config: RemoteConfig, store: CredentialStore) -> None:
        super().__init__()
        self.config = config
        self.store = store
        self.state: ConnectionState = "signed_out"
        self._thread_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()
        self._stopped = False
        # Everything below is touched only on the worker loop.
        self._stop_event: asyncio.Event | None = None
        self._api: ApiClient | None = None
        self._refresh_lock: asyncio.Lock | None = None
        self._command_lock: asyncio.Lock | None = None
        self._connection_task: asyncio.Task[None] | None = None
        self._rt: RealtimeClient | None = None
        self._signed_in = False
        self._access: str | None = None
        self._refresh_token: str | None = None
        self._refresh_after_s = MIN_REFRESH_DELAY_S
        self._challenge_id: UUID | None = None
        self._break = BreakState()
        self._join_in_flight = False
        self._frames_seen = 0  # bumped per applied queue/match/sync frame
        self._chat = ChatState()
        self._gap_tasks: dict[UUID, asyncio.Task[None]] = {}
        self._gap_target: dict[UUID, int] = {}
        self._resend_task: asyncio.Task[None] | None = None
        self._inflight: set[UUID] = set()  # client message ids being POSTed

    # --- public, thread-safe ---

    def start(self) -> None:
        """Connect if credentials exist; otherwise stay signed out, no network."""
        self._submit(self._start())

    def begin_sign_in(self, email: str, invite_code: str | None) -> None:
        self._submit(self._begin_sign_in(email, invite_code))

    def verify(self, code: str) -> None:
        self._submit(self._verify(code))

    def set_nickname(self, nickname: str) -> None:
        self._submit(self._update_me(lambda api, tok: api.set_nickname(tok, nickname)))

    def set_consent(self, consent: Consent) -> None:
        self._submit(self._update_me(lambda api, tok: api.set_consent(tok, consent)))

    def sign_out(self) -> None:
        self._submit(self._sign_out())

    # --- break (M4-05) ---

    def join_queue(self, presence: OutboundPresence) -> None:
        """Start a search with `presence` (the only path presence leaves by)."""
        self._submit(self._join_queue(presence))

    def leave_queue(self) -> None:
        """Cancel the current search; no-op when not searching."""
        self._submit(self._leave_queue())

    def end_match(self) -> None:
        """End the current match; no-op when not matched."""
        self._submit(self._end_match(block=False))

    def block_peer(self) -> None:
        """End the current match and block the peer; no-op when not matched."""
        self._submit(self._end_match(block=True))

    def dismiss_break(self) -> None:
        """Clear a finished result ("nobody available", "ended")."""
        self._submit(self._dismiss_break())

    # --- chat (M5-05) ---

    @property
    def chat_state(self) -> ChatState:
        """The last ChatState emitted on `chat_changed`."""
        return self._chat

    def send_message(self, text: str) -> None:
        """Normalize and send `text` to the current chat; no-op if it
        normalizes to nothing or no chat is live."""
        self._submit(self._send_message(text))

    def retry_message(self, client_message_id: UUID) -> None:
        """Resend a `failed` pending message with the same id."""
        self._submit(self._retry_message(client_message_id))

    def request_extension(self) -> None:
        """Ask for (or, if the peer asked, accept) one more extension."""
        self._submit(self._extension("request"))

    def answer_extension(self, accept: bool) -> None:
        """Accept or decline the pending request (declining also cancels
        one's own)."""
        self._submit(self._extension("accept" if accept else "decline"))

    def report_peer(
        self, reason: ReportReason, note: str | None, message_seqs: tuple[int, ...]
    ) -> None:
        """Report the peer of the current or just-ended chat."""
        self._submit(self._report_peer(reason, note, message_seqs))

    def close_chat(self) -> None:
        """Forget an ended chat (its window was closed); no-op while live."""
        self._submit(self._close_chat())

    def stop(self) -> None:
        """Close the socket and client, join the thread (bounded wait)."""
        with self._thread_lock:
            thread, loop = self._thread, self._loop
            self._stopped = True
            self._thread = None
        if thread is None or loop is None:
            return
        timeout = worker_stop_timeout_s()
        if not loop.is_closed():
            with contextlib.suppress(Exception):
                asyncio.run_coroutine_threadsafe(self._shutdown(), loop).result(timeout)
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(self._set_stop_event)
        thread.join(timeout)

    # --- plumbing ---

    def _ensure_thread(self) -> asyncio.AbstractEventLoop | None:
        with self._thread_lock:
            if self._stopped:
                return None
            if self._thread is None:
                self._thread = threading.Thread(
                    target=lambda: asyncio.run(self._main()),
                    name="breakroom-remote",
                    daemon=True,
                )
                self._thread.start()
        if not self._ready.wait(worker_start_timeout_s()):
            return None
        return self._loop

    def _submit(self, coro: Coroutine[Any, Any, None]) -> None:
        loop = self._ensure_thread()
        if loop is None:
            coro.close()
            return
        try:
            asyncio.run_coroutine_threadsafe(self._guard(coro), loop)
        except RuntimeError:  # loop already closed
            coro.close()

    def _set_stop_event(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()

    async def _main(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        self._refresh_lock = asyncio.Lock()
        self._command_lock = asyncio.Lock()
        self._api = ApiClient(self.config.server_url, self.config.http_timeout_s)
        self._ready.set()
        await self._stop_event.wait()

    async def _guard(self, coro: Coroutine[Any, Any, None]) -> None:
        assert self._command_lock is not None
        try:
            # Commands run one at a time, in the order they were submitted
            # (asyncio.Lock is FIFO), so verify() never overtakes begin_sign_in().
            async with self._command_lock:
                await coro
        except ApiError as exc:
            self._emit_error(exc.code)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning("remote worker command failed")
            self._emit_error("network")

    async def _shutdown(self) -> None:
        await self._cancel_chat_tasks()
        await self._cancel_connection()
        if self._api is not None:
            await self._api.aclose()

    # --- signals (loop thread) ---

    def _set_state(self, state: ConnectionState, *, force: bool = False) -> None:
        if force or state != self.state:
            self.state = state
            with contextlib.suppress(RuntimeError):  # Qt object already deleted
                self.state_changed.emit(state)

    def _emit_error(self, code: str) -> None:
        with contextlib.suppress(RuntimeError):
            self.error.emit(code)

    def _emit_me(self, me: Me | None) -> None:
        with contextlib.suppress(RuntimeError):
            self.me_changed.emit(me)

    # --- break (loop thread) ---

    def _apply(self, event: BreakEvent) -> None:
        new = reduce(self._break, event, datetime.now(UTC))
        if new != self._break:
            self._break = new
            with contextlib.suppress(RuntimeError):  # Qt object already deleted
                self.break_changed.emit(new)

    def _apply_sync(
        self, queue: Any, match: Any, in_flight: bool, *, from_frame: bool
    ) -> None:
        self._apply(Sync(queue, match, in_flight))
        if match is not None:
            self._on_matched(match)
        elif from_frame and self._chat.match_id is not None:
            # A sync without our match: it ended while this device was offline.
            self._apply_chat(Ended(None, None))

    def _on_frame(self, frame: ServerFrame) -> None:
        self._note_server_time(frame.server_time)
        if isinstance(
            frame, WsSync | WsQueueJoined | WsQueueEnded | WsMatchCreated | WsMatchEnded
        ):
            self._frames_seen += 1
        if isinstance(frame, WsSync):
            self._apply_sync(
                frame.queue, frame.match, self._join_in_flight, from_frame=True
            )
        elif isinstance(frame, WsMatchCreated):
            self._apply(Frame(frame))
            self._on_matched(frame.match)
        elif isinstance(frame, WsMatchEnded):
            self._apply(Frame(frame))
            self._apply_chat(Ended(frame.match_id, frame.reason))
        elif isinstance(frame, WsQueueJoined | WsQueueEnded):
            self._apply(Frame(frame))
        elif isinstance(frame, WsChatMessage):
            self._apply_chat(ChatFrame(frame))
            if frame.match_id == self._chat.match_id:
                self._check_gap(frame.message.seq)
        elif isinstance(frame, WsChatStatus):
            self._apply_chat(StatusEvent(frame.status))

    # --- chat (loop thread) ---

    def _apply_chat(self, event: ChatEvent) -> None:
        new = chat_reduce(self._chat, event, datetime.now(UTC))
        if new != self._chat:
            self._chat = new
            with contextlib.suppress(RuntimeError):  # Qt object already deleted
                self.chat_changed.emit(new)

    def _note_server_time(self, server_time: datetime) -> None:
        """Refresh the clock offset from a frame. Not emitted on its own: every
        emitted state carries the newest offset, and a countdown needs no push
        for a few milliseconds of jitter."""
        now = datetime.now(UTC)
        self._chat = chat_reduce(self._chat, ServerTime(server_time, now), now)

    def _on_matched(self, match: MatchView) -> None:
        self._apply_chat(Matched(match))
        self._check_gap(match.last_seq)

    def _contiguous(self) -> int:
        """The highest seq n such that messages 1..n are all known."""
        known = 0
        for message in self._chat.messages:
            if message.seq == known + 1:
                known += 1
            elif message.seq > known + 1:
                break
        return known

    def _check_gap(self, target: int) -> None:
        """Start (or extend) the one fetch per match that fills a gap up to
        `target`."""
        match_id = self._chat.match_id
        if match_id is None or self._chat.ended is not None or not self._signed_in:
            return
        if target <= self._contiguous():
            return
        self._gap_target[match_id] = max(self._gap_target.get(match_id, 0), target)
        if match_id not in self._gap_tasks:
            self._gap_tasks[match_id] = asyncio.create_task(self._fill_gap(match_id))

    async def _fill_gap(self, match_id: UUID) -> None:
        api = self._api
        assert api is not None
        try:
            while self._chat.match_id == match_id and self._chat.ended is None:
                known = self._contiguous()
                if known >= self._gap_target.get(match_id, 0):
                    return
                page = await self._fetch_page(api, match_id, known)
                self._apply_chat(Messages(match_id, page))
                self._gap_target[match_id] = max(
                    self._gap_target.get(match_id, 0), page.last_seq
                )
                if self._contiguous() <= known:
                    return  # no progress; the next frame or sync tries again
        except ApiError:
            _log.warning("chat gap fill failed")
        finally:
            self._gap_tasks.pop(match_id, None)
            self._gap_target.pop(match_id, None)

    async def _fetch_page(
        self, api: ApiClient, match_id: UUID, after_seq: int
    ) -> MessagePage:
        return await self._authed(
            lambda token: api.list_messages(token, match_id, after_seq)
        )

    def _live_chat(self) -> UUID | None:
        if not self._signed_in or self._chat.ended is not None:
            return None
        return self._chat.match_id

    async def _send_message(self, text: str) -> None:
        body = normalize_text(text)
        match_id = self._live_chat()
        if body is None or match_id is None:
            return
        cid = uuid4()
        self._apply_chat(Sending(cid, body))
        await self._post_message(match_id, cid, body, only_if_connected=True)

    async def _retry_message(self, cid: UUID) -> None:
        match_id = self._live_chat()
        if match_id is None or cid in self._inflight:
            return
        for pending in self._chat.pending:
            if pending.client_message_id == cid:
                self._apply_chat(Sending(cid, pending.body))
                await self._post_message(
                    match_id, cid, pending.body, only_if_connected=False
                )
                return

    async def _post_message(
        self, match_id: UUID, cid: UUID, body: str, *, only_if_connected: bool
    ) -> None:
        """POST one pending message. A network failure (or being offline) leaves
        it `sending`: it is resent with the same id after the next
        `session.ready`. Any HTTP error marks it `failed`."""
        api = self._api
        assert api is not None
        if only_if_connected and self.state != "connected":
            return
        request = SendMessageRequest(client_message_id=cid, body=body)
        self._inflight.add(cid)
        try:
            view = await self._authed(
                lambda token: api.send_message(token, match_id, request)
            )
        except ApiError as exc:
            if exc.code != "network" and self._signed_in:
                self._apply_chat(SendFailed(cid))
            return
        finally:
            self._inflight.discard(cid)
        self._confirm(match_id, view)

    def _confirm(self, match_id: UUID, view: MessageView) -> None:
        page = MessagePage(messages=(view,), last_seq=view.seq)
        self._apply_chat(Messages(match_id, page))
        self._check_gap(view.seq)

    def _on_ready(self, frame: WsReady) -> None:
        """A session is up: resend what the last one could not deliver."""
        self._note_server_time(frame.server_time)
        if self._resend_task is None or self._resend_task.done():
            self._resend_task = asyncio.create_task(self._resend_pending())

    async def _resend_pending(self) -> None:
        for pending in self._chat.pending:
            match_id = self._live_chat()
            if match_id is None:
                return
            if (
                pending.status == "sending"
                and pending.client_message_id not in self._inflight
            ):
                await self._post_message(
                    match_id,
                    pending.client_message_id,
                    pending.body,
                    only_if_connected=False,
                )

    async def _extension(self, action: ExtensionAction) -> None:
        api = self._api
        match_id = self._live_chat()
        if api is None or match_id is None:
            return
        status = await self._authed(
            lambda token: api.extension(token, match_id, action)
        )
        self._apply_chat(StatusEvent(status))

    async def _report_peer(
        self, reason: ReportReason, note: str | None, message_seqs: tuple[int, ...]
    ) -> None:
        api = self._api
        match_id = self._chat.match_id
        if api is None or not self._signed_in or match_id is None:
            return
        text: str | None = None
        if note is not None and note.strip():
            text = normalize_text(note, max_chars=MAX_NOTE_CHARS)
            if text is None:
                self._emit_error("invalid_request")
                return
        try:
            request = ReportRequest(
                match_id=match_id, reason=reason, note=text, message_seqs=message_seqs
            )
        except ValueError:
            self._emit_error("invalid_request")
            return
        await self._authed(lambda token: api.report(token, request))
        self._apply_chat(Reported(match_id))

    async def _close_chat(self) -> None:
        self._apply_chat(Closed())

    async def _cancel_chat_tasks(self) -> None:
        current = asyncio.current_task()
        tasks = [*self._gap_tasks.values()]
        if self._resend_task is not None:
            tasks.append(self._resend_task)
        self._gap_tasks.clear()
        self._gap_target.clear()
        self._resend_task = None
        tasks = [t for t in tasks if t is not current]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def _synthetic(self) -> dict[str, Any]:
        return {"version": 1, "event_id": uuid4(), "server_time": datetime.now(UTC)}

    async def _join_queue(self, presence: OutboundPresence) -> None:
        api = self._api
        assert api is not None
        if not self._signed_in or self.state != "connected":
            return
        if self._break.phase not in ("none", "nobody_available", "ended"):
            return
        # One key per call: the retry after a token refresh reuses this request.
        request = QueueJoinRequest(idempotency_key=uuid4(), presence=presence)
        self._apply(Joining())
        self._join_in_flight = True
        try:
            entry = await self._authed(lambda token: api.join_queue(token, request))
        except ApiError as exc:
            self._join_in_flight = False
            self._apply(JoinFailed(exc.code))
            if exc.code in ("already_queued", "already_matched"):
                await self._show_existing(api)
                return
            raise
        except BaseException:
            self._join_in_flight = False
            raise
        self._join_in_flight = False
        self._apply(Joined(entry))

    async def _show_existing(self, api: ApiClient) -> None:
        """After a 409: show the search or match another device started. If the
        fetch fails the state stays idle and the next frame or sync shows it."""
        seen = self._frames_seen
        try:
            snapshot = await self._authed(lambda token: api.current_queue(token))
        except ApiError:
            return
        if self._frames_seen != seen:
            return  # a newer frame arrived while the reply was in flight
        self._apply_sync(snapshot.queue, snapshot.match, False, from_frame=False)

    async def _leave_queue(self) -> None:
        api = self._api
        entry = self._break.entry
        if api is None or not self._signed_in or self._break.phase != "searching":
            return
        assert entry is not None
        await self._authed(lambda token: api.leave_queue(token))
        self._apply(
            Frame(
                WsQueueEnded(
                    type="queue.ended",
                    entry_id=entry.entry_id,
                    reason="cancelled",
                    **self._synthetic(),
                )
            )
        )

    async def _end_match(self, *, block: bool) -> None:
        api = self._api
        match = self._break.match
        phase = self._break.phase
        # Block is also allowed after the match ended (the peer ended it while the
        # confirmation was open); the server records it and the state stays ended.
        if api is None or not self._signed_in or phase not in ("matched", "ended"):
            return
        if phase == "ended" and not block:
            return
        assert match is not None
        if block:
            await self._authed(lambda token: api.block_match(token, match.match_id))
            if phase == "ended":
                return
        else:
            await self._authed(lambda token: api.end_match(token, match.match_id))
        self._apply(
            Frame(
                WsMatchEnded(
                    type="match.ended",
                    match_id=match.match_id,
                    reason="ended",
                    **self._synthetic(),
                )
            )
        )
        self._apply_chat(Ended(match.match_id, "ended"))

    async def _dismiss_break(self) -> None:
        self._apply(Dismiss())

    # --- sign-in ---

    async def _start(self) -> None:
        if self._signed_in or self.state == "signing_in":
            return
        creds = await asyncio.to_thread(self.store.load)
        if creds is None:
            self._set_state("signed_out", force=True)  # tell the UI where we are
            return
        self._refresh_token = creds.refresh_token
        self._begin_connection(fetch_me=True)

    async def _begin_sign_in(self, email: str, invite_code: str | None) -> None:
        if self._signed_in:
            return
        assert self._api is not None
        self._challenge_id = None
        self._set_state("signing_in")
        try:
            reply = await self._api.start_auth(email, invite_code)
        except ApiError:
            self._set_state("signed_out")
            raise
        self._challenge_id = reply.challenge_id

    async def _verify(self, code: str) -> None:
        assert self._api is not None
        if self._signed_in or self._challenge_id is None:
            self._emit_error("invalid_request")
            return
        try:
            session = await self._api.verify(self._challenge_id, code, _device_name())
        except ApiError as exc:
            if exc.code in ("expired", "unauthorized"):
                self._challenge_id = None
                self._set_state("signed_out")
            raise
        self._challenge_id = None
        self._adopt(session.tokens)
        await self._persist(session.tokens)  # persist first, then tell the UI
        self._signed_in = True
        self._emit_me(session.me)
        self._begin_connection(fetch_me=False)

    # --- tokens ---

    def _adopt(self, tokens: TokenPair) -> None:
        self._access = tokens.access_token
        self._refresh_token = tokens.refresh_token
        ttl = (tokens.access_expires_at - datetime.now(UTC)).total_seconds()
        self._refresh_after_s = refresh_delay_s(ttl)

    async def _persist(self, tokens: TokenPair) -> None:
        creds = StoredCredentials(
            refresh_token=tokens.refresh_token,
            refresh_expires_at=tokens.refresh_expires_at.astimezone(UTC),
        )
        # False: the login lasts for this run only; nothing safe to tell the user.
        await asyncio.to_thread(self.store.save, creds)

    async def _refresh(self, stale: str | None) -> None:
        """Rotate the refresh token. `stale` is the access token the caller saw;
        if another task already replaced it, do nothing."""
        assert self._api is not None and self._refresh_lock is not None
        async with self._refresh_lock:
            if self._access is not None and self._access != stale:
                return
            if self._refresh_token is None:
                raise ApiError("unauthorized")
            tokens = await self._api.refresh(self._refresh_token)
            self._adopt(tokens)
            await self._persist(tokens)

    async def _authed(self, call: Callable[[str], Awaitable[_T]]) -> _T:
        """Call with the access token; on 401 refresh once and retry."""
        token = self._access
        if token is None:
            raise ApiError("unauthorized")
        try:
            return await call(token)
        except ApiError as exc:
            if exc.code != "unauthorized":
                raise
        try:
            await self._refresh(stale=token)
            fresh = self._access
            assert fresh is not None
            return await call(fresh)
        except ApiError as exc:
            if exc.code == "unauthorized":
                await self._drop_session()
            raise

    async def _update_me(self, call: Callable[[ApiClient, str], Awaitable[Me]]) -> None:
        api = self._api
        assert api is not None
        if not self._signed_in:
            self._emit_error("unauthorized")
            return
        me = await self._authed(lambda token: call(api, token))
        self._emit_me(me)

    # --- sign-out ---

    async def _cancel_connection(self) -> None:
        task, self._connection_task = self._connection_task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        rt, self._rt = self._rt, None
        if rt is not None:
            await rt.close()

    async def _drop_session(self) -> None:
        """Forget the login locally: store, memory, UI state."""
        if not self._signed_in:
            return
        self._signed_in = False
        await self._cancel_connection()
        self._apply(SignedOut())
        await self._cancel_chat_tasks()
        self._apply_chat(ChatSignedOut())
        self._access = None
        self._refresh_token = None
        await asyncio.to_thread(self.store.clear)
        self._set_state("signed_out")
        self._emit_me(None)

    async def _sign_out(self) -> None:
        if not self._signed_in:
            # Signing in but not finished: just abandon it.
            self._challenge_id = None
            self._set_state("signed_out")
            return
        api = self._api
        await self._cancel_connection()
        if api is not None:
            with contextlib.suppress(ApiError):  # best effort, bounded by timeout
                if self._access is None:
                    await self._refresh(stale=None)
                # A 401 refreshes once, so an expired access token still revokes.
                await self._authed(lambda token: api.logout(token))
        await self._drop_session()

    # --- connection ---

    def _begin_connection(self, *, fetch_me: bool) -> None:
        self._signed_in = True
        self._set_state("connecting")
        self._connection_task = asyncio.create_task(self._connection_loop(fetch_me))

    def _backoff_s(self, attempt: int) -> float:
        low, high = self.config.reconnect_min_s, self.config.reconnect_max_s
        return random.uniform(low, min(high, low * 2 ** min(attempt, 20)))

    def _go_offline(self, code: str) -> None:
        if self.state != "offline":
            self._set_state("offline")
            self._emit_error(code)

    async def _connection_loop(self, fetch_me: bool) -> None:
        assert self._api is not None
        api = self._api
        attempt = 0
        force_refresh = self._access is None
        after_4401 = False
        try:
            while True:
                try:
                    if force_refresh:
                        await self._refresh(stale=self._access)
                        force_refresh = False
                    if fetch_me:
                        me = await self._authed(lambda tok: api.me(tok))
                        fetch_me = False
                        self._emit_me(me)
                    if self.state != "offline":
                        self._set_state("connecting")
                    rt = RealtimeClient(
                        self.config.server_url,
                        self.config.http_timeout_s,
                        self.config.ws_ping_interval_s,
                        self._on_frame,
                    )
                    self._rt = rt
                    assert self._access is not None
                    ready = await rt.connect(self._access)
                    attempt = 0  # a good connection ends the backoff
                    self._on_ready(ready)
                    code = await self._hold(rt)
                    after_4401 = False
                except RealtimeError as exc:
                    code = exc.code
                    if code == WS_CLOSE["unauthorized"] and after_4401:
                        # A fresh token was refused too: the login is dead.
                        await self._drop_session()
                        self._emit_error("unauthorized")
                        return
                except ApiError as exc:
                    if exc.code == "unauthorized":
                        await self._drop_session()
                        self._emit_error("unauthorized")
                        return
                    self._go_offline(exc.code)
                    await self._pause(attempt)
                    attempt += 1
                    continue
                except Exception:
                    # A bug must not leave the UI stuck in "connecting".
                    _log.warning("connection loop error")
                    self._go_offline("network")
                    await self._pause(attempt)
                    attempt += 1
                    continue
                finally:
                    leftover, self._rt = self._rt, None
                    if leftover is not None:
                        await leftover.close()

                if code == WS_CLOSE["replaced"]:
                    # Another instance owns this device; do not fight for it.
                    self._set_state("offline")
                    self._emit_error("replaced")
                    return
                if code == WS_CLOSE["unauthorized"]:
                    force_refresh = True
                    after_4401 = True
                    continue
                self._go_offline("network")
                await self._pause(attempt)
                attempt += 1
        except asyncio.CancelledError:
            raise

    async def _pause(self, attempt: int) -> None:
        await asyncio.sleep(self._backoff_s(attempt))

    async def _hold(self, rt: RealtimeClient) -> int | None:
        """Stay connected, refreshing before expiry. Returns the close code."""
        self._set_state("connected")
        closed = asyncio.ensure_future(rt.wait_closed())
        try:
            while True:
                done, _ = await asyncio.wait({closed}, timeout=self._refresh_after_s)
                if closed in done:
                    return closed.result()
                await self._refresh(stale=self._access)
                assert self._access is not None
                try:
                    await rt.reauth(self._access)
                except RealtimeError:
                    return await closed
        finally:
            closed.cancel()

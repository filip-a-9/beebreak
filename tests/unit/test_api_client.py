from __future__ import annotations

import asyncio
import http.server
import json
import threading
from collections.abc import Callable, Coroutine, Iterator
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, TypeVar
from uuid import UUID

import httpx
import pytest

from breakroom_companion.api_client import ApiClient, ApiError
from breakroom_shared.api import (
    Consent,
    QueueEntryView,
    QueueJoinRequest,
    ReportRequest,
    SendMessageRequest,
    SessionResponse,
)
from breakroom_shared.presence import OutboundPresence

if TYPE_CHECKING:
    from tests.e2e_support.server import LiveServer

pytest_plugins = ["tests.e2e_support.server"]  # live_server fixtures

T = TypeVar("T")
TIMEOUT_S = 2.0


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


async def sign_in(
    api: ApiClient, server: LiveServer, email: str = "ada@example.com"
) -> SessionResponse:
    invite = server.invite(email)
    challenge = await api.start_auth(email, invite)
    return await api.verify(challenge.challenge_id, server.last_code(), "test box")


def test_every_route_round_trips(live_server: LiveServer) -> None:
    async def scenario() -> None:
        api = ApiClient(live_server.base_url, TIMEOUT_S)
        try:
            session = await sign_in(api, live_server)
            access = session.tokens.access_token
            assert session.me.nickname is None and session.me.consent is None

            me = await api.set_nickname(access, "Ada_L")
            assert me.nickname == "Ada_L"
            consent = Consent(version=1, share_stack_tags=True)
            me = await api.set_consent(access, consent)
            assert me.consent == consent
            assert (await api.me(access)).consent == consent

            rotated = await api.refresh(session.tokens.refresh_token)
            assert rotated.refresh_token != session.tokens.refresh_token
            await api.logout(rotated.access_token)
            with pytest.raises(ApiError) as info:
                await api.me(rotated.access_token)
            assert info.value.code == "unauthorized"
        finally:
            await api.aclose()

    run(scenario())


def test_error_codes_map(live_server: LiveServer) -> None:
    async def scenario() -> None:
        api = ApiClient(live_server.base_url, TIMEOUT_S)
        try:
            session = await sign_in(api, live_server)
            access = session.tokens.access_token
            other = await sign_in(api, live_server, "bob@example.com")
            await api.set_nickname(other.tokens.access_token, "bobby")

            async def code_of(coro: Coroutine[Any, Any, object]) -> str:
                with pytest.raises(ApiError) as info:
                    await coro
                return info.value.code

            assert await code_of(api.set_nickname(access, "bobby")) == "nickname_taken"
            assert (
                await code_of(api.set_nickname(access, "admin")) == "nickname_reserved"
            )
            assert await code_of(api.set_nickname(access, "x")) == "invalid_request"
            assert await code_of(api.me("bra_nope")) == "unauthorized"
            assert await code_of(api.refresh("brr_nope")) == "unauthorized"
            challenge = await api.start_auth("ada@example.com", None)
            assert (
                await code_of(api.verify(challenge.challenge_id, "ZZZZZZZZ", "box"))
                == "invalid_code"
            )
            assert (
                await code_of(api.verify(challenge.challenge_id, "short", "box"))
                == "invalid_request"
            )
        finally:
            await api.aclose()

    run(scenario())


def test_tokens_never_appear_in_exceptions(live_server: LiveServer) -> None:
    async def scenario() -> None:
        api = ApiClient(live_server.base_url, TIMEOUT_S)
        try:
            session = await sign_in(api, live_server)
            tokens = session.tokens
            await api.logout(tokens.access_token)
            failures: list[ApiError] = []
            for call in (
                api.me(tokens.access_token),
                api.refresh(tokens.refresh_token),
                api.set_nickname(tokens.access_token, "adaaa"),
            ):
                try:
                    await call
                except ApiError as exc:
                    failures.append(exc)
            assert len(failures) == 3
            for failure in failures:
                text = f"{failure!s} {failure!r} {failure.args}"
                assert tokens.access_token not in text
                assert tokens.refresh_token not in text
        finally:
            await api.aclose()

    run(scenario())


def test_connection_refused_is_network() -> None:
    async def scenario() -> None:
        api = ApiClient("http://127.0.0.1:1", TIMEOUT_S)
        try:
            with pytest.raises(ApiError) as info:
                await api.me("bra_token")
            assert info.value.code == "network"
            assert "bra_token" not in str(info.value)
        finally:
            await api.aclose()

    run(scenario())


class Canned:
    def __init__(self, status: int, headers: dict[str, str], body: bytes) -> None:
        self.status, self.headers, self.body = status, headers, body
        self.hits: list[str] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _reply(self) -> None:
                outer.hits.append(self.path)
                self.send_response(outer.status)
                for k, v in outer.headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(outer.body)))
                self.end_headers()
                self.wfile.write(outer.body)

            do_GET = do_POST = do_PUT = do_PATCH = _reply

            def log_message(self, format: str, *args: Any) -> None:
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(5)


@pytest.fixture
def canned() -> Iterator[Callable[[int, dict[str, str], bytes], Canned]]:
    made: list[Canned] = []

    def make(status: int, headers: dict[str, str], body: bytes) -> Canned:
        server = Canned(status, headers, body)
        made.append(server)
        return server

    yield make
    for server in made:
        server.close()


def _code_for(server: Canned) -> str:
    async def scenario() -> str:
        api = ApiClient(server.url, TIMEOUT_S)
        try:
            await api.me("bra_token")
        except ApiError as exc:
            return exc.code
        finally:
            await api.aclose()
        return "no error"

    return run(scenario())


def test_redirect_is_not_followed(
    canned: Callable[[int, dict[str, str], bytes], Canned],
) -> None:
    target = canned(
        200, {}, b'{"nickname": null, "consent": null, "consented_at": null}'
    )
    redirecting = canned(307, {"Location": f"{target.url}/v1/me"}, b"")

    assert _code_for(redirecting) == "network"
    assert target.hits == []  # nothing was sent to the redirect target


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (500, b"<html>oops</html>"),
        (401, b'{"error": "something_else"}'),
        (200, b"not json"),
        (200, b'{"nickname": "ok", "extra": 1, "consent": null, "consented_at": null}'),
        (200, b"x" * 70_000),
    ],
)
def test_unusable_responses_are_network(
    canned: Callable[[int, dict[str, str], bytes], Canned], status: int, body: bytes
) -> None:
    assert _code_for(canned(status, {}, body)) == "network"


def test_error_body_maps_to_its_code(
    canned: Callable[[int, dict[str, str], bytes], Canned],
) -> None:
    assert _code_for(canned(429, {}, b'{"error": "rate_limited"}')) == "rate_limited"


def test_cookies_are_not_kept(
    canned: Callable[[int, dict[str, str], bytes], Canned],
) -> None:
    server = canned(
        200,
        {"Set-Cookie": "sid=abc"},
        b'{"nickname": null, "consent": null, "consented_at": null}',
    )

    async def scenario() -> int:
        api = ApiClient(server.url, TIMEOUT_S)
        try:
            await api.me("bra_token")
            return len(api._client.cookies.jar)
        finally:
            await api.aclose()

    assert run(scenario()) == 0


# --- queue and matches (M4-05), against httpx.MockTransport ---

ENTRY_ID = UUID(int=7)
MATCH_ID = UUID(int=9)
KEY = UUID(int=3)
PRESENCE = OutboundPresence(
    schema_version=1,
    presence_id=UUID(int=4),
    task_category="backend",
    stack_tags=("python", "docker"),
    remaining_time_bucket="5_10m",
    status="available",
)
JOIN = QueueJoinRequest(idempotency_key=KEY, presence=PRESENCE)
ENTRY_JSON = {
    "entry_id": str(ENTRY_ID),
    "status": "queued",
    "expires_at": datetime(2026, 10, 13, 12, 0, tzinfo=UTC).isoformat(),
}


def mock_api(
    reply: Callable[[httpx.Request], httpx.Response],
) -> tuple[ApiClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return reply(request)

    api = ApiClient(
        "http://server.test", TIMEOUT_S, transport=httpx.MockTransport(handler)
    )
    return api, seen


def error_reply(status: int, code: str) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _r: httpx.Response(status, json={"error": code})


def test_join_queue_posts_exactly_the_model_with_a_bearer_token() -> None:
    async def scenario() -> tuple[QueueEntryView, list[httpx.Request]]:
        api, seen = mock_api(lambda _r: httpx.Response(201, json=ENTRY_JSON))
        try:
            return await api.join_queue("bra_tok", JOIN), seen
        finally:
            await api.aclose()

    entry, seen = run(scenario())

    assert entry.entry_id == ENTRY_ID and entry.status == "queued"
    (request,) = seen
    assert (request.method, request.url.path) == ("POST", "/v1/queue")
    assert request.headers["authorization"] == "Bearer bra_tok"
    assert request.headers["content-type"] == "application/json"
    assert json.loads(request.content) == json.loads(JOIN.model_dump_json())


def test_join_queue_accepts_200_for_an_idempotent_replay() -> None:
    async def scenario() -> QueueEntryView:
        api, _ = mock_api(lambda _r: httpx.Response(200, json=ENTRY_JSON))
        try:
            return await api.join_queue("bra_tok", JOIN)
        finally:
            await api.aclose()

    assert run(scenario()).entry_id == ENTRY_ID


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (400, "invalid_request"),
        (401, "unauthorized"),
        (409, "setup_incomplete"),
        (409, "already_queued"),
        (409, "already_matched"),
        (429, "rate_limited"),
    ],
)
def test_join_queue_error_codes_map(status: int, code: str) -> None:
    async def scenario() -> str:
        api, _ = mock_api(error_reply(status, code))
        try:
            await api.join_queue("bra_tok", JOIN)
        except ApiError as exc:
            return exc.code
        finally:
            await api.aclose()
        return "no error"

    assert run(scenario()) == code


def test_join_queue_with_a_bad_body_is_network() -> None:
    async def scenario() -> str:
        api, _ = mock_api(lambda _r: httpx.Response(201, json={"entry_id": "nope"}))
        try:
            await api.join_queue("bra_tok", JOIN)
        except ApiError as exc:
            return exc.code
        finally:
            await api.aclose()
        return "no error"

    assert run(scenario()) == "network"


def test_leave_queue_deletes_current_without_a_body() -> None:
    async def scenario() -> list[httpx.Request]:
        api, seen = mock_api(lambda _r: httpx.Response(204))
        try:
            await api.leave_queue("bra_tok")
            return seen
        finally:
            await api.aclose()

    (request,) = run(scenario())

    assert (request.method, request.url.path) == ("DELETE", "/v1/queue/current")
    assert request.headers["authorization"] == "Bearer bra_tok"
    assert request.content == b""


@pytest.mark.parametrize("name", ["end_match", "block_match"])
def test_match_actions_post_to_their_route(name: str) -> None:
    async def scenario() -> list[httpx.Request]:
        api, seen = mock_api(lambda _r: httpx.Response(204))
        try:
            assert await getattr(api, name)("bra_tok", MATCH_ID) is None
            return seen
        finally:
            await api.aclose()

    (request,) = run(scenario())

    suffix = "end" if name == "end_match" else "block"
    assert (request.method, request.url.path) == (
        "POST",
        f"/v1/matches/{MATCH_ID}/{suffix}",
    )
    assert request.headers["authorization"] == "Bearer bra_tok"
    assert request.content == b""


@pytest.mark.parametrize("name", ["end_match", "block_match"])
@pytest.mark.parametrize(
    ("status", "code"),
    [(404, "not_found"), (401, "unauthorized"), (429, "rate_limited")],
)
def test_match_action_error_codes_map(name: str, status: int, code: str) -> None:
    async def scenario() -> str:
        api, _ = mock_api(error_reply(status, code))
        try:
            await getattr(api, name)("bra_tok", MATCH_ID)
        except ApiError as exc:
            return exc.code
        finally:
            await api.aclose()
        return "no error"

    assert run(scenario()) == code


def test_queue_calls_never_leak_the_token_or_presence_into_errors() -> None:
    async def scenario() -> str:
        api, _ = mock_api(error_reply(500, "not a code"))
        try:
            await api.join_queue("bra_secret", JOIN)
        except ApiError as exc:
            return f"{exc!s} {exc!r} {exc.args}"
        finally:
            await api.aclose()
        return ""

    text = run(scenario())

    assert "bra_secret" not in text and "python" not in text


def test_current_queue_gets_the_snapshot_with_a_bearer_token() -> None:
    body = {"queue": ENTRY_JSON, "match": None}

    async def scenario() -> tuple[Any, list[httpx.Request]]:
        api, seen = mock_api(lambda _r: httpx.Response(200, json=body))
        try:
            return await api.current_queue("bra_tok"), seen
        finally:
            await api.aclose()

    snapshot, seen = run(scenario())

    assert snapshot.queue is not None and snapshot.queue.entry_id == ENTRY_ID
    assert snapshot.match is None
    (request,) = seen
    assert (request.method, request.url.path) == ("GET", "/v1/queue/current")
    assert request.headers["authorization"] == "Bearer bra_tok"
    assert request.content == b""


@pytest.mark.parametrize(
    ("status", "code"), [(401, "unauthorized"), (429, "rate_limited")]
)
def test_current_queue_error_codes_map(status: int, code: str) -> None:
    async def scenario() -> str:
        api, _ = mock_api(error_reply(status, code))
        try:
            await api.current_queue("bra_tok")
        except ApiError as exc:
            return exc.code
        finally:
            await api.aclose()
        return "no error"

    assert run(scenario()) == code


def test_current_queue_with_a_bad_body_is_network() -> None:
    async def scenario() -> str:
        api, _ = mock_api(lambda _r: httpx.Response(200, json={"queue": 1}))
        try:
            await api.current_queue("bra_tok")
        except ApiError as exc:
            return exc.code
        finally:
            await api.aclose()
        return "no error"

    assert run(scenario()) == "network"


# --- chat and reports (M5-05) ---

CHAT_MATCH = UUID(int=700)
CHAT_CLIENT_ID = UUID(int=701)
_SENT_AT = datetime(2026, 10, 14, 12, 0, 5, tzinfo=UTC)


def _message_json(seq: int, *, mine: bool = True) -> dict[str, Any]:
    return {
        "seq": seq,
        "mine": mine,
        "body": f"hello {seq}",
        "sent_at": _SENT_AT.isoformat(),
        "client_message_id": str(CHAT_CLIENT_ID) if mine else None,
    }


def _status_json(extension: str = "none") -> dict[str, Any]:
    return {
        "match_id": str(CHAT_MATCH),
        "ends_at": "2026-10-14T12:15:00+00:00",
        "grace_at": "2026-10-14T12:13:00+00:00",
        "extensions_left": 2,
        "extension_s": 600,
        "extension": extension,
        "peer_online": True,
    }


async def _capture[R](
    reply: Callable[[httpx.Request], httpx.Response],
    call: Callable[[ApiClient], Coroutine[Any, Any, R]],
) -> tuple[R, list[httpx.Request]]:
    api, seen = mock_api(reply)
    try:
        return await call(api), seen
    finally:
        await api.aclose()


async def _code_of(
    reply: Callable[[httpx.Request], httpx.Response],
    call: Callable[[ApiClient], Coroutine[Any, Any, object]],
) -> str:
    api, _ = mock_api(reply)
    try:
        await call(api)
    except ApiError as exc:
        return exc.code
    finally:
        await api.aclose()
    return "no error"


@pytest.mark.parametrize("status", [200, 201])
def test_send_message_posts_exactly_the_request(status: int) -> None:
    body = SendMessageRequest(client_message_id=CHAT_CLIENT_ID, body="hello 7")

    view, seen = run(
        _capture(
            lambda _r: httpx.Response(status, json=_message_json(7)),
            lambda api: api.send_message("bra_tok", CHAT_MATCH, body),
        )
    )

    assert view.seq == 7 and view.mine and view.client_message_id == CHAT_CLIENT_ID
    (request,) = seen
    assert (request.method, request.url.path) == (
        "POST",
        f"/v1/matches/{CHAT_MATCH}/messages",
    )
    assert request.headers["authorization"] == "Bearer bra_tok"
    assert json.loads(request.content) == {
        "client_message_id": str(CHAT_CLIENT_ID),
        "body": "hello 7",
    }


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (400, "invalid_request"),
        (401, "unauthorized"),
        (404, "not_found"),
        (409, "chat_ended"),
        (429, "rate_limited"),
    ],
)
def test_send_message_error_codes_map(status: int, code: str) -> None:
    body = SendMessageRequest(client_message_id=CHAT_CLIENT_ID, body="x")

    assert (
        run(
            _code_of(
                error_reply(status, code),
                lambda api: api.send_message("bra_tok", CHAT_MATCH, body),
            )
        )
        == code
    )


def test_list_messages_gets_after_seq_with_a_bearer_token() -> None:
    page = {"messages": [_message_json(4), _message_json(5, mine=False)], "last_seq": 5}

    result, seen = run(
        _capture(
            lambda _r: httpx.Response(200, json=page),
            lambda api: api.list_messages("bra_tok", CHAT_MATCH, 3),
        )
    )

    assert [m.seq for m in result.messages] == [4, 5] and result.last_seq == 5
    (request,) = seen
    assert (request.method, request.url.path) == (
        "GET",
        f"/v1/matches/{CHAT_MATCH}/messages",
    )
    assert request.url.params["after_seq"] == "3"
    assert request.headers["authorization"] == "Bearer bra_tok"
    assert request.content == b""


def test_list_messages_accepts_a_full_page_of_large_messages() -> None:
    big = {**_message_json(1), "body": "\U0001f600" * 1000}
    messages = [{**big, "seq": seq} for seq in range(1, 101)]

    result, _ = run(
        _capture(
            lambda _r: httpx.Response(
                200, json={"messages": messages, "last_seq": 100}
            ),
            lambda api: api.list_messages("bra_tok", CHAT_MATCH, 0),
        )
    )

    assert len(result.messages) == 100


@pytest.mark.parametrize(
    ("status", "code"),
    [(400, "invalid_request"), (401, "unauthorized"), (404, "not_found")],
)
def test_list_messages_error_codes_map(status: int, code: str) -> None:
    assert (
        run(
            _code_of(
                error_reply(status, code),
                lambda api: api.list_messages("bra_tok", CHAT_MATCH, 0),
            )
        )
        == code
    )


def test_list_messages_with_a_bad_body_is_network() -> None:
    assert (
        run(
            _code_of(
                lambda _r: httpx.Response(200, json={"messages": "no"}),
                lambda api: api.list_messages("bra_tok", CHAT_MATCH, 0),
            )
        )
        == "network"
    )


@pytest.mark.parametrize("action", ["request", "accept", "decline"])
def test_extension_posts_the_action_and_returns_the_status(action: Any) -> None:
    status, seen = run(
        _capture(
            lambda _r: httpx.Response(200, json=_status_json("requested_by_me")),
            lambda api: api.extension("bra_tok", CHAT_MATCH, action),
        )
    )

    assert status.extension == "requested_by_me" and status.match_id == CHAT_MATCH
    (request,) = seen
    assert (request.method, request.url.path) == (
        "POST",
        f"/v1/matches/{CHAT_MATCH}/extension",
    )
    assert request.headers["authorization"] == "Bearer bra_tok"
    assert json.loads(request.content) == {"action": action}


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (404, "not_found"),
        (409, "chat_ended"),
        (409, "no_extensions_left"),
        (409, "no_extension_request"),
    ],
)
def test_extension_error_codes_map(status: int, code: str) -> None:
    assert (
        run(
            _code_of(
                error_reply(status, code),
                lambda api: api.extension("bra_tok", CHAT_MATCH, "request"),
            )
        )
        == code
    )


def test_report_posts_exactly_the_request_and_accepts_204() -> None:
    model = ReportRequest(
        match_id=CHAT_MATCH, reason="spam", note="again", message_seqs=(2, 5)
    )

    result, seen = run(
        _capture(
            lambda _r: httpx.Response(204),
            lambda api: api.report("bra_tok", model),
        )
    )

    assert result is None
    (request,) = seen
    assert (request.method, request.url.path) == ("POST", "/v1/reports")
    assert request.headers["authorization"] == "Bearer bra_tok"
    assert json.loads(request.content) == {
        "match_id": str(CHAT_MATCH),
        "reason": "spam",
        "note": "again",
        "message_seqs": [2, 5],
    }


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (400, "invalid_request"),
        (404, "not_found"),
        (409, "already_reported"),
        (409, "report_window_closed"),
        (429, "rate_limited"),
    ],
)
def test_report_error_codes_map(status: int, code: str) -> None:
    model = ReportRequest(match_id=CHAT_MATCH, reason="other")

    assert (
        run(
            _code_of(
                error_reply(status, code),
                lambda api: api.report("bra_tok", model),
            )
        )
        == code
    )


def test_chat_calls_never_leak_the_token_or_text_into_errors() -> None:
    body = SendMessageRequest(client_message_id=CHAT_CLIENT_ID, body="secret words")

    async def scenario() -> str:
        api, _ = mock_api(lambda _r: httpx.Response(500, text="oops secret words"))
        try:
            await api.send_message("bra_secret", CHAT_MATCH, body)
        except ApiError as exc:
            return f"{exc!r} {exc}"
        finally:
            await api.aclose()
        return ""

    text = run(scenario())

    assert text and "bra_secret" not in text and "secret words" not in text

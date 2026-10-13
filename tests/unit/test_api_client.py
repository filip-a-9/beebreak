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

from __future__ import annotations

import asyncio
import http.server
import threading
from collections.abc import Callable, Coroutine, Iterator
from typing import TYPE_CHECKING, Any, TypeVar

import pytest

from breakroom_companion.api_client import ApiClient, ApiError
from breakroom_shared.api import Consent, SessionResponse

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

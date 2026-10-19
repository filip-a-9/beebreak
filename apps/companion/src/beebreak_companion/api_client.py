"""HTTP client for the Beebreak server. Owned by M3-05a.

One route, one method. Only the frozen `beebreak_shared.api` models are parsed.
A failure is `ApiError(code)` where code is an ErrorResponse code, or "network"
for anything else (timeout, connection, redirect, bad body). Tokens travel only
in the Authorization header, and never appear in an exception message.
"""

import ssl
from typing import TypeVar
from uuid import UUID

import httpx
from pydantic import BaseModel, ValidationError

from beebreak_shared.api import (
    AuthMethods,
    AuthStartRequest,
    AuthStartResponse,
    AuthVerifyRequest,
    ChatStatus,
    Consent,
    DeviceNameUpdate,
    ErrorResponse,
    ExtensionAction,
    ExtensionRequest,
    GitHubPollRequest,
    GitHubPollResponse,
    GitHubStartResponse,
    Me,
    MessagePage,
    MessageView,
    NicknameUpdate,
    QueueEntryView,
    QueueJoinRequest,
    QueueSnapshot,
    RefreshRequest,
    ReportRequest,
    SendMessageRequest,
    SessionResponse,
    TokenPair,
)

MAX_RESPONSE_BYTES = 64 * 1024
# A page of 100 messages of 1000 four-byte characters is ~400 KB.
MAX_PAGE_BYTES = 1024 * 1024

_M = TypeVar("_M", bound=BaseModel)


class ApiError(Exception):
    """`code` is an ErrorResponse code or "network". The message is the code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ApiClient:
    def __init__(
        self,
        origin: str,
        timeout_s: float,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=origin,
            transport=transport,
            verify=ssl_context if ssl_context is not None else True,
            timeout=timeout_s,
            follow_redirects=False,
            trust_env=False,  # never pick up proxies from the environment
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _call(
        self,
        method: str,
        path: str,
        *,
        model: type[_M] | None,
        body: BaseModel | None = None,
        access_token: str | None = None,
        max_bytes: int = MAX_RESPONSE_BYTES,
    ) -> _M | None:
        headers = {"Authorization": f"Bearer {access_token}"} if access_token else {}
        try:
            response = await self._client.request(
                method,
                path,
                content=body.model_dump_json() if body is not None else None,
                headers=(
                    headers | {"Content-Type": "application/json"}
                    if body is not None
                    else headers
                ),
            )
            self._client.cookies.clear()  # no cookie state, ever
            content = response.content
        except Exception:  # httpx errors can quote the URL; keep none of it
            raise ApiError("network") from None
        if len(content) > max_bytes:
            raise ApiError("network")
        if 200 <= response.status_code < 300:
            if model is None:
                return None
            try:
                return model.model_validate_json(content)
            except ValidationError:
                raise ApiError("network") from None
        if 300 <= response.status_code < 400:
            raise ApiError("network")  # redirects are never followed
        try:
            raise ApiError(ErrorResponse.model_validate_json(content).error)
        except ValidationError:
            raise ApiError("network") from None

    async def auth_methods(self) -> AuthMethods:
        reply = await self._call("GET", "/v1/auth/methods", model=AuthMethods)
        assert reply is not None
        return reply

    async def github_start(self) -> GitHubStartResponse:
        reply = await self._call(
            "POST", "/v1/auth/github/start", model=GitHubStartResponse
        )
        assert reply is not None
        return reply

    async def github_poll(
        self, challenge_id: UUID, device_name: str
    ) -> GitHubPollResponse:
        try:
            request = GitHubPollRequest(
                challenge_id=challenge_id, device_name=device_name
            )
        except ValidationError:
            raise ApiError("invalid_request") from None
        reply = await self._call(
            "POST",
            "/v1/auth/github/poll",
            model=GitHubPollResponse,
            body=request,
            max_bytes=MAX_RESPONSE_BYTES,
        )
        assert reply is not None
        return reply

    async def start_auth(
        self, email: str, invite_code: str | None
    ) -> AuthStartResponse:
        reply = await self._call(
            "POST",
            "/v1/auth/start",
            model=AuthStartResponse,
            body=AuthStartRequest(email=email, invite_code=invite_code),
        )
        assert reply is not None
        return reply

    async def verify(
        self, challenge_id: UUID, code: str, device_name: str
    ) -> SessionResponse:
        try:
            request = AuthVerifyRequest(
                challenge_id=challenge_id, code=code, device_name=device_name
            )
        except ValidationError:
            raise ApiError("invalid_request") from None
        reply = await self._call(
            "POST", "/v1/auth/verify", model=SessionResponse, body=request
        )
        assert reply is not None
        return reply

    async def refresh(self, refresh_token: str) -> TokenPair:
        reply = await self._call(
            "POST",
            "/v1/auth/refresh",
            model=TokenPair,
            body=RefreshRequest(refresh_token=refresh_token),
        )
        assert reply is not None
        return reply

    async def logout(self, access_token: str) -> None:
        await self._call(
            "POST", "/v1/auth/logout", model=None, access_token=access_token
        )

    async def me(self, access_token: str) -> Me:
        reply = await self._call("GET", "/v1/me", model=Me, access_token=access_token)
        assert reply is not None
        return reply

    async def set_nickname(self, access_token: str, nickname: str) -> Me:
        try:
            request = NicknameUpdate(nickname=nickname)
        except ValidationError:
            raise ApiError("invalid_request") from None
        reply = await self._call(
            "PATCH", "/v1/me", model=Me, body=request, access_token=access_token
        )
        assert reply is not None
        return reply

    async def set_device_name(self, access_token: str, device_name: str) -> None:
        try:
            request = DeviceNameUpdate(device_name=device_name)
        except ValidationError:
            raise ApiError("invalid_request") from None
        await self._call(
            "PATCH",
            "/v1/me/device",
            model=None,
            body=request,
            access_token=access_token,
        )

    async def set_consent(self, access_token: str, consent: Consent) -> Me:
        reply = await self._call(
            "PUT", "/v1/me/consent", model=Me, body=consent, access_token=access_token
        )
        assert reply is not None
        return reply

    # --- queue and matches (M4) ---

    async def join_queue(
        self, access_token: str, request: QueueJoinRequest
    ) -> QueueEntryView:
        reply = await self._call(
            "POST",
            "/v1/queue",
            model=QueueEntryView,
            body=request,
            access_token=access_token,
        )
        assert reply is not None
        return reply

    async def leave_queue(self, access_token: str) -> None:
        await self._call(
            "DELETE", "/v1/queue/current", model=None, access_token=access_token
        )

    async def end_match(self, access_token: str, match_id: UUID) -> None:
        await self._call(
            "POST",
            f"/v1/matches/{match_id}/end",
            model=None,
            access_token=access_token,
        )

    async def block_match(self, access_token: str, match_id: UUID) -> None:
        await self._call(
            "POST",
            f"/v1/matches/{match_id}/block",
            model=None,
            access_token=access_token,
        )

    async def current_queue(self, access_token: str) -> QueueSnapshot:
        reply = await self._call(
            "GET", "/v1/queue/current", model=QueueSnapshot, access_token=access_token
        )
        assert reply is not None
        return reply

    # --- chat and reports (M5-05) ---

    async def send_message(
        self, access_token: str, match_id: UUID, request: SendMessageRequest
    ) -> MessageView:
        """POST /v1/matches/{id}/messages (201 new, 200 replay)."""
        reply = await self._call(
            "POST",
            f"/v1/matches/{match_id}/messages",
            model=MessageView,
            body=request,
            access_token=access_token,
        )
        assert reply is not None
        return reply

    async def list_messages(
        self, access_token: str, match_id: UUID, after_seq: int
    ) -> MessagePage:
        """GET /v1/matches/{id}/messages?after_seq=N, oldest first."""
        reply = await self._call(
            "GET",
            f"/v1/matches/{match_id}/messages?after_seq={int(after_seq)}",
            model=MessagePage,
            access_token=access_token,
            max_bytes=MAX_PAGE_BYTES,
        )
        assert reply is not None
        return reply

    async def extension(
        self, access_token: str, match_id: UUID, action: ExtensionAction
    ) -> ChatStatus:
        """POST /v1/matches/{id}/extension."""
        reply = await self._call(
            "POST",
            f"/v1/matches/{match_id}/extension",
            model=ChatStatus,
            body=ExtensionRequest(action=action),
            access_token=access_token,
        )
        assert reply is not None
        return reply

    async def report(self, access_token: str, request: ReportRequest) -> None:
        """POST /v1/reports (204)."""
        await self._call(
            "POST",
            "/v1/reports",
            model=None,
            body=request,
            access_token=access_token,
        )

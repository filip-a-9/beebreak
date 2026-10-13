"""HTTP client for the Breakroom server. Owned by M3-05a.

One route, one method. Only the frozen `breakroom_shared.api` models are parsed.
A failure is `ApiError(code)` where code is an ErrorResponse code, or "network"
for anything else (timeout, connection, redirect, bad body). Tokens travel only
in the Authorization header, and never appear in an exception message.
"""

from typing import TypeVar
from uuid import UUID

import httpx
from pydantic import BaseModel, ValidationError

from breakroom_shared.api import (
    AuthStartRequest,
    AuthStartResponse,
    AuthVerifyRequest,
    Consent,
    ErrorResponse,
    Me,
    NicknameUpdate,
    RefreshRequest,
    SessionResponse,
    TokenPair,
)

MAX_RESPONSE_BYTES = 64 * 1024

_M = TypeVar("_M", bound=BaseModel)


class ApiError(Exception):
    """`code` is an ErrorResponse code or "network". The message is the code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ApiClient:
    def __init__(self, origin: str, timeout_s: float) -> None:
        self._client = httpx.AsyncClient(
            base_url=origin,
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
        if len(content) > MAX_RESPONSE_BYTES:
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

    async def set_consent(self, access_token: str, consent: Consent) -> Me:
        reply = await self._call(
            "PUT", "/v1/me/consent", model=Me, body=consent, access_token=access_token
        )
        assert reply is not None
        return reply

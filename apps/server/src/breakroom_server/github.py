"""The only module that talks to GitHub: OAuth device flow and two lookups.

Base URLs and the timeout come from `ServerConfig`. Standard library HTTP, no
redirects, no proxy from the environment, answers capped in size. Calls:

- `start_device_flow`   POST {oauth}/login/device/code     (client id, no scope)
- `poll`                POST {oauth}/login/oauth/access_token
- `user_id`             GET  {api}/user                    (the one place the
                        access token is used; only the numeric id is returned)
- `lookup_user_id`      GET  {api}/users/<name>            (no credentials)

Every failure is `GitHubUnavailable` with a fixed message: an exception from
the HTTP stack can quote the URL (which holds a username) and must not escape.
The access token lives in a local variable and an `Authorized` object whose
repr hides it; nothing here logs.
"""

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

from breakroom_server.config import ServerConfig

MAX_RESPONSE_BYTES = 64 * 1024
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
USER_AGENT = "breakroom-server"
MAX_INTERVAL_S = 300
# GitHub logins: alphanumerics and single hyphens, not at either end, 39 at most.
USERNAME_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}")


class GitHubUnavailable(Exception):
    """GitHub did not give a usable answer. The message never names a URL."""

    def __init__(self) -> None:
        super().__init__("github unavailable")


@dataclass(frozen=True)
class DeviceCode:
    device_code: str = field(repr=False)
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int


@dataclass(frozen=True)
class Pending:
    pass


@dataclass(frozen=True)
class SlowDown:
    interval: int | None  # the new interval when GitHub sent one


@dataclass(frozen=True)
class Expired:
    pass


@dataclass(frozen=True)
class Denied:
    pass


@dataclass(frozen=True)
class Authorized:
    access_token: str = field(repr=False)


PollResult = Pending | SlowDown | Expired | Denied | Authorized


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None  # the 3xx surfaces as an HTTPError


def _positive_int(value: object, limit: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= limit:
        raise GitHubUnavailable
    return value


def _text(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 200:
        raise GitHubUnavailable
    return value


class GitHubClient:
    def __init__(self, config: ServerConfig) -> None:
        self._client_id = config.github_client_id
        self._oauth = config.github_oauth_url
        self._api = config.github_api_url
        self._timeout_s = config.github_timeout_s
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect
        )

    # --- device flow ---

    def start_device_flow(self) -> DeviceCode:
        answer = self._json(
            self._request(
                "POST",
                f"{self._oauth}/login/device/code",
                form={"client_id": self._client_id},
                accept="application/json",
            )
        )
        if "error" in answer:
            raise GitHubUnavailable
        uri = _text(answer.get("verification_uri"))
        if not uri.startswith(self._oauth + "/"):
            raise GitHubUnavailable
        return DeviceCode(
            device_code=_text(answer.get("device_code")),
            user_code=_text(answer.get("user_code")),
            verification_uri=uri,
            expires_in=_positive_int(answer.get("expires_in"), 3600),
            interval=_positive_int(answer.get("interval"), MAX_INTERVAL_S),
        )

    def poll(self, device_code: str) -> PollResult:
        answer = self._json(
            self._request(
                "POST",
                f"{self._oauth}/login/oauth/access_token",
                form={
                    "client_id": self._client_id,
                    "device_code": device_code,
                    "grant_type": DEVICE_GRANT,
                },
                accept="application/json",
            )
        )
        error = answer.get("error")
        if error is None:
            return Authorized(access_token=_text(answer.get("access_token")))
        if error == "authorization_pending":
            return Pending()
        if error == "slow_down":
            raw = answer.get("interval")
            return SlowDown(
                interval=_positive_int(raw, MAX_INTERVAL_S) if raw is not None else None
            )
        if error in ("expired_token", "token_expired"):  # docs use both spellings
            return Expired()
        if error == "access_denied":
            return Denied()
        # device_flow_disabled, incorrect_client_credentials, incorrect_device_code,
        # unsupported_grant_type, anything new: a setup problem, not the user's.
        raise GitHubUnavailable

    # --- lookups ---

    def user_id(self, access_token: str) -> int:
        """The numeric id of the token's user. Nothing else is read or kept."""
        answer = self._json(
            self._request(
                "GET",
                f"{self._api}/user",
                accept="application/vnd.github+json",
                bearer=access_token,
            )
        )
        return _positive_int(answer.get("id"), 2**62)

    def lookup_user_id(self, username: str) -> int | None:
        """The id behind a username, or None when there is no such user."""
        if USERNAME_RE.fullmatch(username) is None:
            return None
        try:
            answer = self._json(
                self._request(
                    "GET",
                    f"{self._api}/users/{username}",
                    accept="application/vnd.github+json",
                )
            )
        except _NotFound:
            return None
        return _positive_int(answer.get("id"), 2**62)

    # --- plumbing ---

    def _request(
        self,
        method: str,
        url: str,
        *,
        accept: str,
        form: dict[str, str] | None = None,
        bearer: str | None = None,
    ) -> urllib.request.Request:
        data = None
        headers = {"Accept": accept, "User-Agent": USER_AGENT}
        if form is not None:
            data = urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        if bearer is not None:
            headers["Authorization"] = f"Bearer {bearer}"
            headers["X-GitHub-Api-Version"] = "2022-11-28"
        return urllib.request.Request(url, data=data, headers=headers, method=method)

    def _json(self, request: urllib.request.Request) -> dict[str, Any]:
        try:
            with self._opener.open(request, timeout=self._timeout_s) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as err:
            if err.code == 404 and request.get_method() == "GET":
                raise _NotFound from None
            raise GitHubUnavailable from None
        except Exception:  # URLError, timeouts, TLS: all carry the URL somewhere
            raise GitHubUnavailable from None
        if len(body) > MAX_RESPONSE_BYTES:
            raise GitHubUnavailable
        try:
            parsed = json.loads(body)
        except ValueError:
            raise GitHubUnavailable from None
        if not isinstance(parsed, dict):
            raise GitHubUnavailable
        return parsed


class _NotFound(GitHubUnavailable):
    """A 404 on a GET; `lookup_user_id` turns it into None, anyone else sees
    it as unavailable."""

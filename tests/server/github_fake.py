"""A fake GitHub on a loopback port, for the GitHub sign-in tests.

Real HTTP, not a mock of our client. It serves the documented response shapes of
the device flow (POST /login/device/code, POST /login/oauth/access_token) and of
the API (GET /user, GET /users/<name>), one server for both base URLs:

    ServerConfig(github_oauth_url=fake.url, github_api_url=fake.url, ...)

Polls are scripted: `fake.script_poll(...)` queues the next answers of the token
endpoint; once the queue is empty the answer is `authorization_pending`.
"""

import json
import threading
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

DEVICE_CODE = "3584d83530557fdd1f46af8289938c8ef79f9dc5"  # 40 chars, as documented
USER_CODE = "WDJB-MJHT"
ACCESS_TOKEN = "gho_16C7e42F292c6912E7710c838347Ae178B4a"  # the documented example


@dataclass
class Recorded:
    method: str
    path: str
    headers: dict[str, str]
    form: dict[str, str]


@dataclass
class FakeGitHub:
    client_id: str = "Iv1.testclientid0001"
    expires_in: int = 900
    interval: int = 5
    token: str = ACCESS_TOKEN
    device_code: str = DEVICE_CODE  # what the device-code request hands out
    users: dict[str, int] = field(default_factory=dict)  # login (lower case) -> id
    token_user_id: int = 4242  # whom GET /user answers for `token`
    device_code_status: int = 200  # force a failure of the device-code request
    user_status: int | None = None  # force a status for GET /user
    verification_uri: str | None = None  # default: <url>/login/device
    redirect_lookup: bool = False  # answer GET /users/<name> with a redirect
    requests: list[Recorded] = field(default_factory=list)
    _script: deque[dict[str, Any]] = field(default_factory=deque)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _server: ThreadingHTTPServer | None = None
    _thread: threading.Thread | None = None
    port: int = 0

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def script_poll(self, *answers: dict[str, Any]) -> None:
        with self._lock:
            self._script.extend(answers)

    def approve(self) -> None:
        """The user entered the code: the next poll returns the access token."""
        self.script_poll(
            {"access_token": self.token, "token_type": "bearer", "scope": ""}
        )

    def calls(self, path: str) -> list[Recorded]:
        with self._lock:
            return [r for r in self.requests if r.path == path]

    def _next_answer(self) -> dict[str, Any]:
        with self._lock:
            if self._script:
                return self._script.popleft()
        return {
            "error": "authorization_pending",
            "error_description": "The authorization request is still pending.",
            "error_uri": "https://docs.github.com/developers/apps",
        }

    def start(self) -> None:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                return

            def _reply(self, status: int, body: dict[str, Any]) -> None:
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _record(self, form: dict[str, str]) -> None:
                headers = {k.lower(): v for k, v in self.headers.items()}
                with fake._lock:
                    fake.requests.append(
                        Recorded(self.command, urlparse(self.path).path, headers, form)
                    )

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length).decode()
                form = {k: v[0] for k, v in parse_qs(raw).items()}
                path = urlparse(self.path).path
                self._record(form)
                if path == "/login/device/code":
                    if fake.device_code_status != 200:
                        self._reply(fake.device_code_status, {"message": "nope"})
                        return
                    if form.get("client_id") != fake.client_id:
                        self._reply(
                            200,
                            {
                                "error": "incorrect_client_credentials",
                                "error_description": "bad client id",
                            },
                        )
                        return
                    self._reply(
                        200,
                        {
                            "device_code": fake.device_code,
                            "user_code": USER_CODE,
                            "verification_uri": fake.verification_uri
                            or f"{fake.url}/login/device",
                            "expires_in": fake.expires_in,
                            "interval": fake.interval,
                        },
                    )
                elif path == "/login/oauth/access_token":
                    if (
                        form.get("client_id") != fake.client_id
                        or form.get("device_code") != fake.device_code
                        or form.get("grant_type")
                        != "urn:ietf:params:oauth:grant-type:device_code"
                    ):
                        self._reply(200, {"error": "incorrect_device_code"})
                        return
                    self._reply(200, fake._next_answer())
                else:
                    self._reply(404, {"message": "Not Found"})

            def do_GET(self) -> None:
                path = urlparse(self.path).path
                self._record({})
                auth = self.headers.get("Authorization", "")
                if path == "/user":
                    if fake.user_status is not None:
                        self._reply(fake.user_status, {"message": "forced"})
                    elif auth in (f"Bearer {fake.token}", f"token {fake.token}"):
                        self._reply(
                            200,
                            {
                                "login": "octocat",
                                "id": fake.token_user_id,
                                "node_id": "MDQ6VXNlcjE=",
                                "type": "User",
                                "name": "monalisa octocat",
                            },
                        )
                    else:
                        self._reply(401, {"message": "Bad credentials"})
                elif path.startswith("/users/") and fake.redirect_lookup:
                    self.send_response(302)
                    self.send_header("Location", f"{fake.url}/user")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                elif path.startswith("/users/"):
                    name = path.removeprefix("/users/").lower()
                    if name in fake.users:
                        self._reply(
                            200,
                            {"login": name, "id": fake.users[name], "type": "User"},
                        )
                    else:
                        self._reply(404, {"message": "Not Found"})
                else:
                    self._reply(404, {"message": "Not Found"})

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(5)


@pytest.fixture
def fake_github() -> Iterator[FakeGitHub]:
    """Import into a test module to use it."""
    fake = FakeGitHub()
    fake.start()
    try:
        yield fake
    finally:
        fake.stop()

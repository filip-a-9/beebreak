from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import pytestqt.exceptions
from PySide6.QtCore import QObject, Slot
from pytestqt.qtbot import QtBot
from sqlalchemy import select, update

from breakroom_companion.api_client import ApiClient, ApiError
from breakroom_companion.credentials import FileCredentialStore
from breakroom_companion.remote import RemoteWorker, refresh_delay_s
from breakroom_companion.remote_config import RemoteConfig
from breakroom_server.models import Device, User
from breakroom_shared.api import Consent, Me

if TYPE_CHECKING:
    from tests.e2e_support.server import LiveServer

pytest_plugins = ["tests.e2e_support.server"]  # live_server fixtures

WAIT_MS = 15_000


class Recorder(QObject):
    """Receives the worker's signals on the Qt thread, in order."""

    def __init__(self, worker: RemoteWorker) -> None:
        super().__init__()
        self.events: list[tuple[str, Any]] = []
        worker.state_changed.connect(self.on_state)
        worker.me_changed.connect(self.on_me)
        worker.error.connect(self.on_error)

    @Slot(str)
    def on_state(self, state: str) -> None:
        self.events.append(("state", state))

    @Slot(object)
    def on_me(self, me: object) -> None:
        self.events.append(("me", me))

    @Slot(str)
    def on_error(self, code: str) -> None:
        self.events.append(("error", code))

    @property
    def states(self) -> list[str]:
        return [v for k, v in self.events if k == "state"]

    @property
    def errors(self) -> list[str]:
        return [v for k, v in self.events if k == "error"]

    @property
    def last_me(self) -> Me | None:
        mes = [v for k, v in self.events if k == "me"]
        return mes[-1] if mes else None

    def has_me(self) -> bool:
        return any(k == "me" for k, _ in self.events)


class Client:
    def __init__(
        self, worker: RemoteWorker, rec: Recorder, store: FileCredentialStore
    ) -> None:
        self.worker, self.rec, self.store = worker, rec, store


@pytest.fixture
def make_client(tmp_path: Path, qtbot: QtBot) -> Iterator[Callable[..., Client]]:
    created: list[Client] = []

    def make(
        server: LiveServer, name: str = "a", store: FileCredentialStore | None = None
    ) -> Client:
        config = RemoteConfig(
            server_url=server.base_url,
            http_timeout_s=2,
            ws_ping_interval_s=1.0,  # the server floods-limits above 2 frames/s
            reconnect_min_s=0.05,
            reconnect_max_s=0.2,
        )
        store = store or FileCredentialStore(tmp_path / f"{name}.json", server.base_url)
        worker = RemoteWorker(config, store)
        client = Client(worker, Recorder(worker), store)
        created.append(client)
        return client

    yield make
    for client in created:
        client.worker.stop()


def wait_for(qtbot: QtBot, condition: Callable[[], bool]) -> None:
    qtbot.waitUntil(condition, timeout=WAIT_MS)


def wait_state(qtbot: QtBot, client: Client, state: str) -> None:
    try:
        wait_for(qtbot, lambda: client.rec.states[-1:] == [state])
    except (TimeoutError, pytestqt.exceptions.TimeoutError):
        seen = [(k, v) for k, v in client.rec.events if k != "me"]
        raise AssertionError(f"never reached {state}; saw {seen}") from None


def sign_in(
    qtbot: QtBot,
    server: LiveServer,
    client: Client,
    email: str,
    *,
    nickname: str | None = None,
) -> None:
    client.worker.start()
    wait_state(qtbot, client, "signed_out")
    sent_before = len(server.mailer.sent)
    client.worker.begin_sign_in(email, server.invite(email))
    wait_for(qtbot, lambda: len(server.mailer.sent) > sent_before)
    client.worker.verify(server.last_code())
    wait_state(qtbot, client, "connected")
    if nickname is not None:
        client.worker.set_nickname(nickname)
        wait_for(
            qtbot,
            lambda: (
                client.rec.last_me is not None
                and client.rec.last_me.nickname == nickname
            ),
        )


# --- sign-in and resume ---


def test_full_flow_then_a_new_worker_resumes_without_sign_in(
    qtbot: QtBot,
    live_server: LiveServer,
    make_client: Callable[..., Client],
) -> None:
    first = make_client(live_server, "a")
    sign_in(qtbot, live_server, first, "ada@example.com", nickname="ada_l")
    consent = Consent(version=1, share_task_category=True)
    first.worker.set_consent(consent)
    wait_for(
        qtbot,
        lambda: first.rec.last_me is not None and first.rec.last_me.consent == consent,
    )

    # Persisted first, then announced, then connected.
    events = first.rec.events
    me_at = next(i for i, e in enumerate(events) if e[0] == "me")
    connecting_at = events.index(("state", "connecting"))
    assert events.index(("state", "signing_in")) < me_at < connecting_at
    assert first.store.load() is not None
    assert first.rec.errors == []

    first.worker.stop()
    mails = len(live_server.mailer.sent)
    second = make_client(live_server, "b", store=first.store)
    second.worker.start()

    wait_state(qtbot, second, "connected")
    assert second.rec.last_me is not None
    assert second.rec.last_me.nickname == "ada_l"
    assert second.rec.last_me.consent == consent
    assert "signing_in" not in second.rec.states
    assert len(live_server.mailer.sent) == mails


def test_without_credentials_the_server_sees_no_request(
    qtbot: QtBot, live_server: LiveServer, make_client: Callable[..., Client]
) -> None:
    client = make_client(live_server)

    client.worker.start()

    wait_state(qtbot, client, "signed_out")
    assert live_server.requests == []
    assert client.rec.errors == []


def test_wrong_code_keeps_signing_in_and_a_retry_succeeds(
    qtbot: QtBot, live_server: LiveServer, make_client: Callable[..., Client]
) -> None:
    client = make_client(live_server)
    client.worker.start()
    wait_state(qtbot, client, "signed_out")
    client.worker.begin_sign_in(
        "ada@example.com", live_server.invite("ada@example.com")
    )
    wait_for(qtbot, lambda: len(live_server.mailer.sent) > 0)

    client.worker.verify("ZZZZZZZZ")
    wait_for(qtbot, lambda: client.rec.errors == ["invalid_code"])
    assert client.rec.states[-1] == "signing_in"

    client.worker.verify(live_server.last_code())
    wait_state(qtbot, client, "connected")


def test_verify_without_a_challenge_is_an_error(
    qtbot: QtBot, live_server: LiveServer, make_client: Callable[..., Client]
) -> None:
    client = make_client(live_server)
    client.worker.start()
    wait_state(qtbot, client, "signed_out")

    client.worker.verify("ABCD2345")

    wait_for(qtbot, lambda: client.rec.errors == ["invalid_request"])
    assert live_server.requests == []


def test_api_errors_are_reported_as_codes(
    qtbot: QtBot, live_server: LiveServer, make_client: Callable[..., Client]
) -> None:
    client = make_client(live_server)
    sign_in(qtbot, live_server, client, "ada@example.com")

    client.worker.set_nickname("admin")

    wait_for(qtbot, lambda: client.rec.errors == ["nickname_reserved"])
    assert client.rec.states[-1] == "connected"


def test_garbage_refresh_token_in_store_ends_signed_out(
    qtbot: QtBot,
    live_server: LiveServer,
    make_client: Callable[..., Client],
    tmp_path: Path,
) -> None:
    from datetime import timedelta

    from breakroom_companion.credentials import StoredCredentials

    store = FileCredentialStore(tmp_path / "bad.json", live_server.base_url)
    store.save(StoredCredentials("brr_garbage", datetime.now(UTC) + timedelta(days=1)))
    client = make_client(live_server, store=store)

    client.worker.start()

    wait_state(qtbot, client, "signed_out")
    wait_for(qtbot, lambda: client.rec.errors == ["unauthorized"])
    wait_for(qtbot, lambda: client.rec.has_me())
    assert store.load() is None
    assert client.rec.last_me is None


# --- keeping the session alive ---


def test_short_access_ttl_refreshes_and_reauths_without_going_offline(
    qtbot: QtBot,
    server_factory: Callable[..., LiveServer],
    make_client: Callable[..., Client],
) -> None:
    server = server_factory(access_ttl_s=2)
    client = make_client(server)
    sign_in(qtbot, server, client, "ada@example.com")
    seen: list[str] = []

    def rotated_twice() -> bool:
        creds = client.store.load()
        if creds is not None and creds.refresh_token not in seen:
            seen.append(creds.refresh_token)
        return len(seen) >= 3  # the sign-in token plus two rotations

    wait_for(qtbot, rotated_twice)

    connected_at = client.rec.states.index("connected")
    assert client.rec.states[connected_at:] == ["connected"]
    assert client.rec.errors == []


def test_refresh_delay_is_sixty_seconds_early_or_half_for_short_lifetimes() -> None:
    assert refresh_delay_s(900) == 840
    assert refresh_delay_s(100) == 50
    assert refresh_delay_s(2) == 1


@pytest.mark.parametrize("skew_s", [-3600, -901, 0, 600, 1800, 86_400])
def test_refresh_delay_survives_clock_skew(skew_s: float) -> None:
    # A real 15 minute token seen through a clock that is off by skew_s.
    delay = refresh_delay_s(900 + skew_s)
    assert 30 <= delay <= 840


# --- losing and regaining the server ---


def test_server_stopped_goes_offline_and_restart_reconnects(
    qtbot: QtBot, live_server: LiveServer, make_client: Callable[..., Client]
) -> None:
    client = make_client(live_server)
    sign_in(qtbot, live_server, client, "ada@example.com")

    live_server.stop()
    wait_state(qtbot, client, "offline")
    assert client.rec.errors == ["network"]
    live_server.restart()

    wait_state(qtbot, client, "connected")
    assert client.store.load() is not None


def test_backoff_starts_over_after_a_successful_reconnect(
    qtbot: QtBot, live_server: LiveServer, make_client: Callable[..., Client]
) -> None:
    client = make_client(live_server)
    attempts: list[int] = []
    backoff = client.worker._backoff_s

    def recording(attempt: int) -> float:
        attempts.append(attempt)
        return backoff(attempt)

    client.worker._backoff_s = recording  # type: ignore[method-assign]
    sign_in(qtbot, live_server, client, "ada@example.com")
    live_server.stop()
    wait_for(qtbot, lambda: len(attempts) >= 3)
    live_server.restart()
    wait_state(qtbot, client, "connected")
    before = len(attempts)

    live_server.stop()
    wait_for(qtbot, lambda: len(attempts) > before)
    assert attempts[before] == 0


def test_start_with_credentials_while_server_is_down_is_offline_not_signed_out(
    qtbot: QtBot, live_server: LiveServer, make_client: Callable[..., Client]
) -> None:
    first = make_client(live_server, "a")
    sign_in(qtbot, live_server, first, "ada@example.com")
    first.worker.stop()
    live_server.stop()
    second = make_client(live_server, "b", store=first.store)

    second.worker.start()

    wait_state(qtbot, second, "offline")
    assert first.store.load() is not None
    live_server.restart()
    wait_state(qtbot, second, "connected")
    assert second.rec.last_me is not None


# --- being removed server-side ---


def _revoke_device(server: LiveServer) -> None:
    with server.session_factory() as s:
        s.execute(update(Device).values(revoked_at=datetime.now(UTC)))
        s.commit()


def _disable_user(server: LiveServer) -> None:
    with server.session_factory() as s:
        s.execute(update(User).values(status="disabled"))
        s.commit()


@pytest.mark.parametrize("remove", [_revoke_device, _disable_user])
def test_removed_server_side_ends_signed_out_and_clears_the_store(
    qtbot: QtBot,
    live_server: LiveServer,
    make_client: Callable[..., Client],
    remove: Callable[[LiveServer], None],
) -> None:
    client = make_client(live_server)
    sign_in(qtbot, live_server, client, "ada@example.com")

    remove(live_server)

    wait_state(qtbot, client, "signed_out")
    wait_for(qtbot, lambda: "unauthorized" in client.rec.errors)
    wait_for(qtbot, lambda: ("me", None) in client.rec.events)
    assert client.store.load() is None


def test_sign_out_revokes_the_login_and_clears_everything(
    qtbot: QtBot, live_server: LiveServer, make_client: Callable[..., Client]
) -> None:
    client = make_client(live_server)
    sign_in(qtbot, live_server, client, "ada@example.com", nickname="ada_l")
    creds = client.store.load()
    assert creds is not None

    client.worker.sign_out()

    wait_state(qtbot, client, "signed_out")
    wait_for(qtbot, lambda: client.rec.events[-1] == ("me", None))
    assert client.store.load() is None

    async def spent() -> str:
        api = ApiClient(live_server.base_url, 2)
        try:
            await api.refresh(creds.refresh_token)
        except ApiError as exc:
            return exc.code
        finally:
            await api.aclose()
        return "accepted"

    assert asyncio.run(spent()) == "unauthorized"


def test_sign_out_with_an_expired_access_token_still_revokes_the_device(
    qtbot: QtBot, live_server: LiveServer, make_client: Callable[..., Client]
) -> None:
    client = make_client(live_server)
    sign_in(qtbot, live_server, client, "ada@example.com")
    with live_server.session_factory() as s:
        s.execute(
            update(Device).values(access_expires_at=datetime(2020, 1, 1, tzinfo=UTC))
        )
        s.commit()

    client.worker.sign_out()

    wait_state(qtbot, client, "signed_out")
    wait_for(qtbot, lambda: client.rec.events[-1] == ("me", None))
    assert client.store.load() is None
    with live_server.session_factory() as s:
        assert [d.revoked_at is not None for d in s.scalars(select(Device))] == [True]


# --- several instances ---


def test_second_worker_on_the_same_device_replaces_the_first(
    qtbot: QtBot, live_server: LiveServer, make_client: Callable[..., Client]
) -> None:
    first = make_client(live_server, "a")
    sign_in(qtbot, live_server, first, "ada@example.com")
    second = make_client(live_server, "b", store=first.store)

    second.worker.start()

    wait_for(qtbot, lambda: "replaced" in first.rec.errors)
    wait_state(qtbot, second, "connected")
    assert first.rec.states[-1] == "offline"
    task = first.worker._connection_task
    assert task is not None
    wait_for(qtbot, task.done)  # it gave up instead of reconnecting
    assert "offline" not in second.rec.states


def test_two_users_are_both_connected(
    qtbot: QtBot, live_server: LiveServer, make_client: Callable[..., Client]
) -> None:
    ada = make_client(live_server, "ada")
    bob = make_client(live_server, "bob")

    sign_in(qtbot, live_server, ada, "ada@example.com", nickname="ada_l")
    sign_in(qtbot, live_server, bob, "bob@example.com", nickname="bobby")

    assert ada.rec.states[-1] == bob.rec.states[-1] == "connected"
    assert "offline" not in ada.rec.states + bob.rec.states

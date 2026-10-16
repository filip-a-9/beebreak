"""M6-06: the device name is generic, editable and never the machine hostname."""

from __future__ import annotations

import socket
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from pytestqt.qtbot import QtBot
from sqlalchemy import select

from breakroom_companion.credentials import FileCredentialStore
from breakroom_companion.onboarding import PAGE_CODE, PAGE_NICKNAME, OnboardingDialog
from breakroom_companion.privacy_preview import PrivacyPreviewDialog
from breakroom_companion.remote import DEFAULT_DEVICE_NAME, RemoteWorker
from breakroom_companion.remote_config import RemoteConfig
from breakroom_server.models import Device
from breakroom_shared.api import Consent, Me
from tests.fakes.remote_worker import FakeRemoteWorker

if TYPE_CHECKING:
    from tests.e2e_support.server import LiveServer

pytest_plugins = ["tests.e2e_support.server"]  # live_server fixtures

WAIT_MS = 15_000
EMAIL = "ada@example.com"
HOSTNAME_CANARY = "CANARY-HOST-7f3a9"
NO_CONSENT = Consent(
    version=1,
    share_task_category=False,
    share_stack_tags=False,
    share_time_bucket=False,
)


@pytest.fixture
def sent_requests(monkeypatch: pytest.MonkeyPatch) -> list[bytes]:
    """Method, URL, headers and body of every request the companion sends."""
    seen: list[bytes] = []
    real_send = httpx.AsyncClient.send

    async def send(
        self: httpx.AsyncClient, request: httpx.Request, **kwargs: Any
    ) -> httpx.Response:
        head = f"{request.method} {request.url}\n{dict(request.headers)}\n"
        seen.append(head.encode() + request.content)
        return await real_send(self, request, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    return seen


@pytest.fixture
def hostname_canary(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(socket, "gethostname", lambda: HOSTNAME_CANARY)
    return HOSTNAME_CANARY


@pytest.fixture
def worker(live_server: LiveServer, tmp_path: Path) -> Iterator[RemoteWorker]:
    config = RemoteConfig(
        server_url=live_server.base_url,
        http_timeout_s=2,
        ws_ping_interval_s=1.0,
        reconnect_min_s=0.05,
        reconnect_max_s=0.2,
    )
    w = RemoteWorker(
        config, FileCredentialStore(tmp_path / "creds.json", config.server_url)
    )
    yield w
    w.stop()


def sign_in_through_dialog(
    qtbot: QtBot,
    server: LiveServer,
    worker: RemoteWorker,
    device_name: str | None,
) -> None:
    """Drive the real dialog to the nickname page; type a name unless None."""
    dialog = OnboardingDialog(worker)
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.email_edit.setText(EMAIL)
    dialog.invite_edit.setText(server.invite(EMAIL))
    sent = len(server.mailer.sent)
    dialog.next_button.click()
    qtbot.waitUntil(lambda: len(server.mailer.sent) > sent, timeout=WAIT_MS)
    dialog.code_edit.setText(server.last_code())
    if device_name is not None:
        dialog.device_name_edit.setText(device_name)
    dialog.next_button.click()
    qtbot.waitUntil(lambda: dialog.page == PAGE_NICKNAME, timeout=WAIT_MS)


def stored_device_names(server: LiveServer) -> list[str]:
    with server.session_factory() as session:
        return list(session.scalars(select(Device.name)))


def test_default_name_is_sent_and_hostname_never_leaves(
    qtbot: QtBot,
    live_server: LiveServer,
    worker: RemoteWorker,
    sent_requests: list[bytes],
    hostname_canary: str,
) -> None:
    worker.start()
    sign_in_through_dialog(qtbot, live_server, worker, None)

    assert stored_device_names(live_server) == [DEFAULT_DEVICE_NAME]
    assert len(sent_requests) >= 2
    assert not any(hostname_canary.encode() in blob for blob in sent_requests)


def test_typed_name_is_sent_unchanged(
    qtbot: QtBot,
    live_server: LiveServer,
    worker: RemoteWorker,
    sent_requests: list[bytes],
    hostname_canary: str,
) -> None:
    worker.start()
    sign_in_through_dialog(qtbot, live_server, worker, "Work laptop_2")

    assert stored_device_names(live_server) == ["Work laptop_2"]
    assert not any(hostname_canary.encode() in blob for blob in sent_requests)


def code_page(qtbot: QtBot) -> OnboardingDialog:
    dialog = OnboardingDialog(FakeRemoteWorker())
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.email_edit.setText(EMAIL)
    dialog.next_button.click()
    assert dialog.page == PAGE_CODE
    dialog.code_edit.setText("ABCD2345")
    return dialog


def test_field_is_prefilled_with_the_default(qtbot: QtBot) -> None:
    dialog = code_page(qtbot)
    assert dialog.device_name_edit.text() == DEFAULT_DEVICE_NAME
    assert dialog.next_button.isEnabled()


@pytest.mark.parametrize("bad", ["", "x" * 41, "a/b", "é"])
def test_invalid_name_disables_next(qtbot: QtBot, bad: str) -> None:
    dialog = code_page(qtbot)
    dialog.device_name_edit.setMaxLength(100)  # let the 41-char case be typed
    dialog.device_name_edit.setText(bad)
    assert not dialog.next_button.isEnabled()
    dialog.device_name_edit.setText("ok name")
    assert dialog.next_button.isEnabled()


def test_fake_worker_records_the_typed_name(qtbot: QtBot) -> None:
    dialog = code_page(qtbot)
    dialog.device_name_edit.setText("Desk")
    dialog.next_button.click()
    fake = dialog.worker
    assert isinstance(fake, FakeRemoteWorker)
    assert fake.calls.codes == ["ABCD2345"]
    assert fake.calls.device_names == ["Desk"]


def test_preview_lists_the_given_device_name(qtbot: QtBot) -> None:
    dialog = PrivacyPreviewDialog(NO_CONSENT, device_name="Work laptop")
    qtbot.addWidget(dialog)
    assert "Work laptop" in dialog.signin_list_label.text()


def test_preview_without_a_name_shows_the_default(qtbot: QtBot) -> None:
    dialog = PrivacyPreviewDialog(NO_CONSENT)
    qtbot.addWidget(dialog)
    assert DEFAULT_DEVICE_NAME in dialog.signin_list_label.text()


def test_onboarding_preview_follows_the_field(qtbot: QtBot) -> None:
    dialog = code_page(qtbot)
    dialog.device_name_edit.setText("Desk")
    dialog.show_preview()
    assert dialog.preview is not None
    assert "Desk" in dialog.preview.signin_list_label.text()
    dialog.device_name_edit.setText("Desk 2")
    assert "Desk 2" in dialog.preview.signin_list_label.text()


def test_all_space_name_disables_next(qtbot: QtBot) -> None:
    dialog = code_page(qtbot)
    dialog.device_name_edit.setText("   ")
    assert not dialog.next_button.isEnabled()


def test_surrounding_whitespace_is_stripped_before_sending(qtbot: QtBot) -> None:
    dialog = code_page(qtbot)
    dialog.device_name_edit.setText("  Desk  ")
    assert dialog.next_button.isEnabled()
    dialog.next_button.click()
    fake = dialog.worker
    assert isinstance(fake, FakeRemoteWorker)
    assert fake.calls.device_names == ["Desk"]


def test_code_page_can_preview_what_is_sent_before_sending(qtbot: QtBot) -> None:
    dialog = code_page(qtbot)
    dialog.device_name_edit.setText("Desk")
    dialog.show_sent_button.click()
    assert dialog.preview is not None
    assert "Desk" in dialog.preview.signin_list_label.text()
    fake = dialog.worker
    assert isinstance(fake, FakeRemoteWorker)
    assert fake.calls.codes == []  # previewing sends nothing


def test_reopened_dialog_does_not_claim_a_specific_name(qtbot: QtBot) -> None:
    me = Me(nickname="ada_l", consent=NO_CONSENT, consented_at=datetime.now(UTC))
    dialog = OnboardingDialog(FakeRemoteWorker(), me)
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.show_preview()
    assert dialog.preview is not None
    shown = dialog.preview.signin_list_label.text()
    assert DEFAULT_DEVICE_NAME not in shown
    assert "Desk" not in shown

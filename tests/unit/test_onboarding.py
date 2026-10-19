"""Onboarding dialog against a real loopback server (M3 rule 11)."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, get_args

import pytest
from PySide6.QtCore import QObject, Qt, Slot
from PySide6.QtWidgets import QLabel
from pytestqt.qtbot import QtBot
from sqlalchemy import update

from beebreak_companion.credentials import FileCredentialStore
from beebreak_companion.onboarding import (
    ERROR_TEXT,
    PAGE_CODE,
    PAGE_DONE,
    PAGE_EMAIL,
    PAGE_NICKNAME,
    PAGE_PRIVACY,
    OnboardingDialog,
    valid_code,
    valid_email,
    valid_nickname,
)
from beebreak_companion.remote import RemoteWorker
from beebreak_companion.remote_config import RemoteConfig
from beebreak_server.models import AuthChallenge
from beebreak_shared.api import Consent, Me
from beebreak_shared.presence import StackTag
from tests.fakes.remote_worker import FakeRemoteWorker

if TYPE_CHECKING:
    from tests.e2e_support.server import LiveServer

pytest_plugins = ["tests.e2e_support.server"]  # live_server fixtures

WAIT_MS = 15_000
EMAIL = "ada@example.com"


class MeRecorder(QObject):
    def __init__(self, worker: RemoteWorker) -> None:
        super().__init__()
        self.mes: list[Me] = []
        self.states: list[str] = []
        worker.me_changed.connect(self.on_me)
        worker.state_changed.connect(self.on_state)

    @Slot(object)
    def on_me(self, me: object) -> None:
        if isinstance(me, Me):
            self.mes.append(me)

    @Slot(str)
    def on_state(self, state: str) -> None:
        self.states.append(state)

    @property
    def last(self) -> Me | None:
        return self.mes[-1] if self.mes else None


@dataclass
class Rig:
    server: LiveServer
    worker: RemoteWorker
    store: FileCredentialStore
    rec: MeRecorder


@pytest.fixture
def make_rig(tmp_path: Path) -> Iterator[Callable[[LiveServer], Rig]]:
    made: list[Rig] = []

    def make(server: LiveServer, name: str = "a") -> Rig:
        config = RemoteConfig(
            server_url=server.base_url,
            http_timeout_s=2,
            ws_ping_interval_s=1.0,
            reconnect_min_s=0.05,
            reconnect_max_s=0.2,
        )
        store = FileCredentialStore(tmp_path / f"{name}.json", server.base_url)
        worker = RemoteWorker(config, store)
        rig = Rig(server, worker, store, MeRecorder(worker))
        made.append(rig)
        return rig

    yield make
    for rig in made:
        rig.worker.stop()


@pytest.fixture
def rig(live_server: LiveServer, make_rig: Callable[..., Rig]) -> Rig:
    return make_rig(live_server)


def new_dialog(qtbot: QtBot, rig: Rig, me: Me | None = None) -> OnboardingDialog:
    dialog = OnboardingDialog(rig.worker, me)
    qtbot.addWidget(dialog)
    dialog.show()
    return dialog


def wait_page(qtbot: QtBot, dialog: OnboardingDialog, page: int) -> None:
    qtbot.waitUntil(lambda: dialog.page == page, timeout=WAIT_MS)


def request_code(
    qtbot: QtBot,
    rig: Rig,
    dialog: OnboardingDialog,
    email: str = EMAIL,
    *,
    invite: bool = True,
) -> None:
    dialog.email_edit.setText(email)
    if invite:
        dialog.invite_edit.setText(rig.server.invite(email))
    sent = len(rig.server.mailer.sent)
    assert dialog.next_button.isEnabled()
    dialog.next_button.click()
    assert dialog.page == PAGE_CODE
    qtbot.waitUntil(lambda: len(rig.server.mailer.sent) > sent, timeout=WAIT_MS)


def verify_code(qtbot: QtBot, rig: Rig, dialog: OnboardingDialog, page: int) -> None:
    dialog.code_edit.setText(rig.server.last_code())
    dialog.next_button.click()
    wait_page(qtbot, dialog, page)


def sign_in(
    qtbot: QtBot, rig: Rig, dialog: OnboardingDialog, email: str = EMAIL
) -> None:
    request_code(qtbot, rig, dialog, email)
    verify_code(qtbot, rig, dialog, PAGE_NICKNAME)


def error_text(dialog: OnboardingDialog, page: int) -> str | None:
    label = dialog._errors[page]
    return label.text() if label.isVisibleTo(dialog) else None


# --- full flow ---


def test_full_sign_in_through_the_dialog(qtbot: QtBot, rig: Rig) -> None:
    dialog = new_dialog(qtbot, rig)
    assert dialog.page == PAGE_EMAIL

    sign_in(qtbot, rig, dialog)
    dialog.nickname_edit.setText("ada_l")
    dialog.next_button.click()
    wait_page(qtbot, dialog, PAGE_PRIVACY)
    dialog.category_box.setChecked(True)
    dialog.next_button.click()  # Save
    wait_page(qtbot, dialog, PAGE_DONE)

    assert rig.rec.last is not None
    assert rig.rec.last.nickname == "ada_l"
    assert rig.rec.last.consent == Consent(version=1, share_task_category=True)
    assert rig.store.load() is not None  # still signed in
    dialog.next_button.click()  # Close
    assert not dialog.isVisible()
    assert dialog.result() == dialog.DialogCode.Accepted


def test_privacy_boxes_start_unchecked_and_save_with_all_off(
    qtbot: QtBot, rig: Rig
) -> None:
    dialog = new_dialog(qtbot, rig)
    sign_in(qtbot, rig, dialog)
    dialog.nickname_edit.setText("ada_l")
    dialog.next_button.click()
    wait_page(qtbot, dialog, PAGE_PRIVACY)
    boxes = (dialog.category_box, dialog.stack_box, dialog.time_box)
    assert [b.isChecked() for b in boxes] == [False, False, False]
    assert rig.rec.last is not None and rig.rec.last.consent is None

    dialog.next_button.click()  # Save with everything off
    wait_page(qtbot, dialog, PAGE_DONE)

    assert rig.rec.last is not None
    assert rig.rec.last.consent == Consent(version=1)


def test_next_waits_for_syntactically_valid_input(qtbot: QtBot, rig: Rig) -> None:
    dialog = new_dialog(qtbot, rig)
    assert not dialog.next_button.isEnabled()
    dialog.email_edit.setText("not-an-email")
    assert not dialog.next_button.isEnabled()
    dialog.email_edit.setText(EMAIL)
    assert dialog.next_button.isEnabled()  # invite code is optional
    dialog.invite_edit.setText("abc")
    assert not dialog.next_button.isEnabled()
    dialog.invite_edit.setText("ABCD-2345")
    assert dialog.next_button.isEnabled()


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("ABCD2345", True),
        ("abcd-2345", True),
        ("ABCD23456", False),
        ("ABC-D234-5", False),
        ("ABCD234", False),
        ("ABCD 234", False),
        ("ABCD234\N{LATIN SMALL LETTER E WITH ACUTE}", False),
    ],
)
def test_code_syntax(value: str, ok: bool) -> None:
    assert valid_code(value) is ok


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("ada_l", True),
        ("a-b", True),
        ("ab", False),
        ("a b c", False),
        ("x" * 25, False),
    ],
)
def test_nickname_syntax(value: str, ok: bool) -> None:
    assert valid_nickname(value) is ok


@pytest.mark.parametrize(
    ("value", "ok"),
    [(EMAIL, True), ("a@b", False), ("a b@c.d", False), ("", False), ("a@b.c\n", True)],
)
def test_email_syntax(value: str, ok: bool) -> None:
    assert valid_email(value) is ok


def test_code_and_nickname_pages_gate_next(qtbot: QtBot, rig: Rig) -> None:
    dialog = new_dialog(qtbot, rig)
    request_code(qtbot, rig, dialog)
    assert not dialog.next_button.isEnabled()
    dialog.code_edit.setText("123")
    assert not dialog.next_button.isEnabled()
    verify_code(qtbot, rig, dialog, PAGE_NICKNAME)
    assert not dialog.next_button.isEnabled()
    dialog.nickname_edit.setText("ab")
    assert not dialog.next_button.isEnabled()
    dialog.nickname_edit.setText("a b c")
    assert not dialog.next_button.isEnabled()
    dialog.nickname_edit.setText("ada_l")
    assert dialog.next_button.isEnabled()


def test_nickname_page_skipped_when_a_nickname_exists(
    qtbot: QtBot, live_server: LiveServer, make_rig: Callable[..., Rig]
) -> None:
    first = make_rig(live_server, "a")
    dialog = new_dialog(qtbot, first)
    sign_in(qtbot, first, dialog)
    dialog.nickname_edit.setText("ada_l")
    dialog.next_button.click()
    wait_page(qtbot, dialog, PAGE_PRIVACY)
    dialog.reject()
    first.worker.sign_out()
    qtbot.waitUntil(lambda: first.rec.states[-1:] == ["signed_out"], timeout=WAIT_MS)

    again = make_rig(live_server, "b")
    second = new_dialog(qtbot, again)
    request_code(qtbot, again, second, invite=False)
    verify_code(qtbot, again, second, PAGE_PRIVACY)  # nickname already set


# --- errors ---


def test_wrong_code_shows_fixed_text_and_a_retry_works(qtbot: QtBot, rig: Rig) -> None:
    dialog = new_dialog(qtbot, rig)
    request_code(qtbot, rig, dialog)
    dialog.code_edit.setText("ZZZZZZZZ")
    dialog.next_button.click()
    qtbot.waitUntil(lambda: error_text(dialog, PAGE_CODE) is not None, timeout=WAIT_MS)

    assert error_text(dialog, PAGE_CODE) == ERROR_TEXT["invalid_code"]
    assert dialog.page == PAGE_CODE
    verify_code(qtbot, rig, dialog, PAGE_NICKNAME)
    assert error_text(dialog, PAGE_NICKNAME) is None


def test_nickname_errors(
    qtbot: QtBot, live_server: LiveServer, make_rig: Callable[..., Rig]
) -> None:
    taken = make_rig(live_server, "t")
    d1 = new_dialog(qtbot, taken)
    sign_in(qtbot, taken, d1, "taken@example.com")
    d1.nickname_edit.setText("ada_l")
    d1.next_button.click()
    wait_page(qtbot, d1, PAGE_PRIVACY)

    other = make_rig(live_server, "o")
    d2 = new_dialog(qtbot, other)
    sign_in(qtbot, other, d2, "other@example.com")
    for nickname, code in (("ada_l", "nickname_taken"), ("admin", "nickname_reserved")):
        d2.nickname_edit.setText(nickname)
        d2.next_button.click()
        want = ERROR_TEXT[code]

        def shown(want: str = want) -> bool:
            return error_text(d2, PAGE_NICKNAME) == want

        qtbot.waitUntil(shown, timeout=WAIT_MS)
        assert d2.page == PAGE_NICKNAME


def test_rate_limited_returns_to_the_email_page(
    qtbot: QtBot,
    server_factory: Callable[..., LiveServer],
    make_rig: Callable[..., Rig],
) -> None:
    server = server_factory(rate_start_email=1)
    rig = make_rig(server)
    dialog = new_dialog(qtbot, rig)
    request_code(qtbot, rig, dialog)
    dialog.back_button.click()
    assert dialog.page == PAGE_EMAIL
    dialog.next_button.click()  # second start for the same email
    qtbot.waitUntil(lambda: error_text(dialog, PAGE_EMAIL) is not None, timeout=WAIT_MS)

    assert error_text(dialog, PAGE_EMAIL) == ERROR_TEXT["rate_limited"]
    assert dialog.page == PAGE_EMAIL


def test_expired_code_returns_to_the_email_page(qtbot: QtBot, rig: Rig) -> None:
    dialog = new_dialog(qtbot, rig)
    request_code(qtbot, rig, dialog)
    with rig.server.session_factory() as session:
        session.execute(
            update(AuthChallenge).values(
                expires_at=datetime.now(UTC) - timedelta(minutes=1)
            )
        )
        session.commit()
    dialog.code_edit.setText(rig.server.last_code())
    dialog.next_button.click()
    qtbot.waitUntil(lambda: error_text(dialog, PAGE_EMAIL) is not None, timeout=WAIT_MS)

    assert error_text(dialog, PAGE_EMAIL) == ERROR_TEXT["expired"]
    assert dialog.page == PAGE_EMAIL


def test_server_down_shows_network_text(qtbot: QtBot, rig: Rig) -> None:
    dialog = new_dialog(qtbot, rig)
    dialog.email_edit.setText(EMAIL)
    rig.server.stop()
    dialog.next_button.click()
    qtbot.waitUntil(lambda: error_text(dialog, PAGE_EMAIL) is not None, timeout=WAIT_MS)

    assert error_text(dialog, PAGE_EMAIL) == ERROR_TEXT["network"]


def test_every_mapped_error_text_is_distinct_and_fixed() -> None:
    for code in (
        "invalid_code",
        "expired",
        "nickname_taken",
        "nickname_reserved",
        "rate_limited",
        "network",
        "credentials_not_saved",
        "credentials_unreadable",
    ):
        assert code in ERROR_TEXT
    assert len(set(ERROR_TEXT.values())) == len(ERROR_TEXT)


def test_unknown_error_code_is_never_echoed(qtbot: QtBot, rig: Rig) -> None:
    dialog = new_dialog(qtbot, rig)
    dialog.email_edit.setText(EMAIL)
    dialog._busy = True
    dialog._on_error("<b>surprise</b>")
    shown = error_text(dialog, PAGE_EMAIL)
    assert shown is not None and "surprise" not in shown
    assert dialog.email_error.textFormat() == Qt.TextFormat.PlainText


def test_background_errors_are_not_shown(qtbot: QtBot, rig: Rig) -> None:
    dialog = new_dialog(qtbot, rig)
    dialog._on_error("network")  # nothing was submitted
    assert error_text(dialog, PAGE_EMAIL) is None


# --- cancel / back ---


def test_cancel_on_each_page_before_verify_abandons_sign_in(
    qtbot: QtBot, rig: Rig
) -> None:
    first = new_dialog(qtbot, rig)
    first.cancel_button.click()
    assert first.result() == first.DialogCode.Rejected
    assert rig.server.requests == []  # nothing was sent

    second = new_dialog(qtbot, rig)
    request_code(qtbot, rig, second)
    second.cancel_button.click()
    qtbot.waitUntil(lambda: rig.rec.states[-1:] == ["signed_out"], timeout=WAIT_MS)
    assert rig.store.load() is None
    assert not second.isVisible()


def test_back_from_code_page_returns_to_email(qtbot: QtBot, rig: Rig) -> None:
    dialog = new_dialog(qtbot, rig)
    assert not dialog.back_button.isEnabled()
    request_code(qtbot, rig, dialog)
    assert dialog.back_button.isEnabled()
    dialog.back_button.click()
    assert dialog.page == PAGE_EMAIL


def test_cancel_after_verify_stays_signed_in(qtbot: QtBot, rig: Rig) -> None:
    dialog = new_dialog(qtbot, rig)
    sign_in(qtbot, rig, dialog)
    dialog.cancel_button.click()
    assert not dialog.isVisible()
    qtbot.waitUntil(lambda: rig.rec.states[-1:] == ["connected"], timeout=WAIT_MS)
    assert rig.store.load() is not None
    assert rig.rec.last is not None and rig.rec.last.nickname is None


def test_cancel_on_privacy_page_keeps_nickname_without_consent(
    qtbot: QtBot, rig: Rig
) -> None:
    dialog = new_dialog(qtbot, rig)
    sign_in(qtbot, rig, dialog)
    dialog.nickname_edit.setText("ada_l")
    dialog.next_button.click()
    wait_page(qtbot, dialog, PAGE_PRIVACY)
    dialog.cancel_button.click()
    assert rig.rec.last is not None
    assert rig.rec.last.nickname == "ada_l" and rig.rec.last.consent is None
    assert rig.store.load() is not None


def test_escape_key_cancels(qtbot: QtBot, rig: Rig) -> None:
    dialog = new_dialog(qtbot, rig)
    qtbot.keyClick(dialog, Qt.Key.Key_Escape)  # type: ignore[no-untyped-call]
    assert not dialog.isVisible()


# --- settings ---


def test_settings_reopens_privacy_with_current_consent(qtbot: QtBot, rig: Rig) -> None:
    first = new_dialog(qtbot, rig)
    sign_in(qtbot, rig, first)
    first.nickname_edit.setText("ada_l")
    first.next_button.click()
    wait_page(qtbot, first, PAGE_PRIVACY)
    first.stack_box.setChecked(True)
    first.time_box.setChecked(True)
    first.next_button.click()
    wait_page(qtbot, first, PAGE_DONE)
    first.next_button.click()

    me = rig.rec.last
    assert me is not None
    settings = new_dialog(qtbot, rig, me)
    assert settings.page == PAGE_PRIVACY
    assert [
        settings.category_box.isChecked(),
        settings.stack_box.isChecked(),
        settings.time_box.isChecked(),
    ] == [False, True, True]

    settings.stack_box.setChecked(False)
    settings.category_box.setChecked(True)
    settings.next_button.click()
    wait_page(qtbot, settings, PAGE_DONE)

    assert rig.rec.last is not None
    assert rig.rec.last.consent == Consent(
        version=1, share_task_category=True, share_time_bucket=True
    )


def test_settings_for_a_user_without_nickname_starts_at_nickname(
    qtbot: QtBot, rig: Rig
) -> None:
    first = new_dialog(qtbot, rig)
    sign_in(qtbot, rig, first)
    first.cancel_button.click()
    me = rig.rec.last
    assert me is not None and me.nickname is None

    settings = new_dialog(qtbot, rig, me)
    assert settings.page == PAGE_NICKNAME
    assert not settings.back_button.isEnabled()
    settings.nickname_edit.setText("ada_l")
    settings.next_button.click()
    wait_page(qtbot, settings, PAGE_PRIVACY)


def test_signed_out_while_open_disables_save(qtbot: QtBot, rig: Rig) -> None:
    first = new_dialog(qtbot, rig)
    sign_in(qtbot, rig, first)
    first.nickname_edit.setText("ada_l")
    first.next_button.click()
    wait_page(qtbot, first, PAGE_PRIVACY)
    rig.worker.sign_out()
    qtbot.waitUntil(lambda: rig.rec.states[-1:] == ["signed_out"], timeout=WAIT_MS)
    first.next_button.click()  # Save against a dead session
    qtbot.waitUntil(
        lambda: error_text(first, PAGE_PRIVACY) is not None, timeout=WAIT_MS
    )
    assert error_text(first, PAGE_PRIVACY) == ERROR_TEXT["unauthorized"]
    assert not first.next_button.isEnabled()


# --- preview wiring ---


def test_show_what_is_shared_follows_the_boxes(qtbot: QtBot, rig: Rig) -> None:
    me = Me(nickname="ada_l", consent=None, consented_at=None)
    dialog = new_dialog(qtbot, rig, me)
    dialog.show_shared_button.click()
    assert dialog.preview is not None and dialog.preview.isVisible()
    before = dialog.preview.example_edit.toPlainText()
    assert '"task_category": null' in before
    dialog.category_box.setChecked(True)
    assert '"task_category": "backend"' in dialog.preview.example_edit.toPlainText()
    dialog.reject()
    assert not dialog.preview.isVisible()


def test_settings_for_a_signed_in_user_are_not_titled_as_a_sign_in(
    qtbot: QtBot,
) -> None:
    signed_out = OnboardingDialog(FakeRemoteWorker())
    settings = OnboardingDialog(FakeRemoteWorker(), _ME)
    qtbot.addWidget(signed_out)
    qtbot.addWidget(settings)
    assert "sign in" in signed_out.windowTitle().lower()
    assert "sign in" not in settings.windowTitle().lower()


# --- M4-06: stack tag picker (no server needed) ---

_ME = Me(
    nickname="ada_l",
    consent=Consent(version=1, share_stack_tags=True),
    consented_at=datetime(2026, 10, 13, tzinfo=UTC),
)


def _picker(
    qtbot: QtBot,
    saved: list[tuple[StackTag, ...]],
    tags: tuple[StackTag, ...] = (),
) -> tuple[OnboardingDialog, FakeRemoteWorker]:
    worker = FakeRemoteWorker()
    dialog = OnboardingDialog(
        worker, _ME, None, stack_tags=tags, save_tags=saved.append
    )
    qtbot.addWidget(dialog)
    dialog.show()
    return dialog, worker


def _check(dialog: OnboardingDialog, *tags: StackTag) -> None:
    for tag in tags:
        dialog.tag_items[tag].setCheckState(Qt.CheckState.Checked)


def test_tag_list_has_every_vocabulary_tag_and_follows_the_share_box(
    qtbot: QtBot,
) -> None:
    dialog, _ = _picker(qtbot, [])
    assert list(dialog.tag_items) == list(get_args(StackTag))
    assert dialog.tag_list.isVisible()  # _ME shares stack tags
    dialog.stack_box.setChecked(False)
    assert not dialog.tag_list.isVisible()
    dialog.stack_box.setChecked(True)
    assert dialog.tag_list.isVisible()


def test_the_sixth_tag_cannot_be_checked(qtbot: QtBot) -> None:
    dialog, _ = _picker(qtbot, [])
    _check(dialog, "python", "go", "rust", "java", "ruby")
    assert len(dialog.selected_tags()) == 5
    sixth = dialog.tag_items["php"]
    assert not sixth.flags() & Qt.ItemFlag.ItemIsEnabled
    assert not sixth.flags() & Qt.ItemFlag.ItemIsUserCheckable
    for tag in ("python", "go", "rust", "java", "ruby"):
        assert dialog.tag_items[tag].flags() & Qt.ItemFlag.ItemIsEnabled
    dialog.tag_items["python"].setCheckState(Qt.CheckState.Unchecked)
    assert dialog.tag_items["php"].flags() & Qt.ItemFlag.ItemIsEnabled
    assert len(dialog.selected_tags()) == 4


def test_save_stores_tags_locally_and_still_sends_consent(qtbot: QtBot) -> None:
    saved: list[tuple[StackTag, ...]] = []
    dialog, worker = _picker(qtbot, saved)
    _check(dialog, "docker", "python")
    dialog.next_button.click()
    assert saved == [("python", "docker")]  # vocabulary order
    assert len(worker.calls.consents) == 1


def test_tags_are_saved_before_and_without_the_server_answer(qtbot: QtBot) -> None:
    saved: list[tuple[StackTag, ...]] = []
    dialog, worker = _picker(qtbot, saved, ("go",))
    worker.push_error("network")
    dialog.next_button.click()
    assert saved == [("go",)]


def test_reopening_shows_the_saved_tags(qtbot: QtBot) -> None:
    dialog, _ = _picker(qtbot, [], ("rust", "vue"))
    assert dialog.selected_tags() == ("rust", "vue")


def test_unknown_saved_tags_are_ignored(qtbot: QtBot) -> None:
    dialog, _ = _picker(qtbot, [], ("nope", "go"))  # type: ignore[arg-type]
    assert dialog.selected_tags() == ("go",)


def test_the_privacy_page_says_fields_are_sent_only_when_a_break_starts(
    qtbot: QtBot,
) -> None:
    dialog, _ = _picker(qtbot, [])
    assert "In this version nothing is shared" not in " ".join(
        label.text() for label in dialog.findChildren(QLabel)
    )


def test_preview_uses_the_picked_tags(qtbot: QtBot) -> None:
    dialog, _ = _picker(qtbot, [], ("go",))
    dialog.show_preview()
    assert dialog.preview is not None
    assert json.loads(dialog.preview.example_edit.toPlainText())["stack_tags"] == ["go"]
    _check(dialog, "rust")
    assert json.loads(dialog.preview.example_edit.toPlainText())["stack_tags"] == [
        "go",
        "rust",
    ]


def test_failed_credential_save_shows_its_own_text(
    qtbot: QtBot, rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_save(*_args: object) -> bool:
        raise OSError("disk full")

    monkeypatch.setattr(rig.store, "save", broken_save)
    dialog = new_dialog(qtbot, rig)
    request_code(qtbot, rig, dialog)
    dialog.code_edit.setText(rig.server.last_code())
    dialog.next_button.click()
    qtbot.waitUntil(lambda: error_text(dialog, PAGE_EMAIL) is not None, timeout=WAIT_MS)

    assert error_text(dialog, PAGE_EMAIL) == ERROR_TEXT["credentials_not_saved"]
    assert ERROR_TEXT["credentials_not_saved"] != ERROR_TEXT["network"]
    assert dialog.page == PAGE_EMAIL

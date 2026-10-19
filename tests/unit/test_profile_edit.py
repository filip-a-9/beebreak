"""U9: nickname and device name are editable in Settings.

Part one drives the dialog with a fake worker; part two renames for real
against a live server.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from PySide6.QtWidgets import QDialog, QLabel
from pytestqt.qtbot import QtBot
from sqlalchemy import select

from beebreak_companion.break_state import BreakState
from beebreak_companion.chat_state import ChatState
from beebreak_companion.config import wsl_distro_name
from beebreak_companion.onboarding import (
    DEVICE_NAME_LABEL,
    ERROR_TEXT,
    NICKNAME_INTRO,
    PAGE_DONE,
    PAGE_NICKNAME,
    PAGE_PRIVACY,
    OnboardingDialog,
)
from beebreak_companion.privacy_preview import default_device_name
from beebreak_server.models import Device
from beebreak_shared.api import Consent, Me
from tests.fakes.remote_worker import FakeRemoteWorker
from tests.unit.test_onboarding import (  # noqa: F401  (fixtures)
    Rig,
    make_rig,
    new_dialog,
    rig,
    sign_in,
    wait_page,
)

if TYPE_CHECKING:
    from tests.e2e_support.server import LiveServer

pytest_plugins = ["tests.e2e_support.server"]  # live_server fixtures

CONSENT = Consent(
    version=1,
    share_task_category=True,
    share_stack_tags=False,
    share_time_bucket=False,
)


def me(device_name: str | None = "desk") -> Me:
    return Me(
        nickname="ada_l",
        consent=CONSENT,
        consented_at="2026-10-19T10:00:00Z",
        device_name=device_name,
    )


def settings(
    qtbot: QtBot, device_name: str | None = "desk"
) -> tuple[OnboardingDialog, FakeRemoteWorker]:
    worker = FakeRemoteWorker()
    dialog = OnboardingDialog(worker, me(device_name))
    qtbot.addWidget(dialog)
    dialog.show()
    return dialog, worker


def test_settings_shows_the_current_nickname_and_device_name(qtbot: QtBot) -> None:
    dialog, _ = settings(qtbot)
    assert dialog.page == PAGE_PRIVACY
    assert dialog.profile_nickname_edit.text() == "ada_l"
    assert dialog.profile_device_edit.text() == "desk"


def test_an_older_server_falls_back_to_the_saved_default_name(qtbot: QtBot) -> None:
    dialog, _ = settings(qtbot, device_name=None)
    assert dialog.profile_device_edit.text() == default_device_name(wsl_distro_name())


def test_saving_untouched_names_sends_only_the_consent(qtbot: QtBot) -> None:
    dialog, worker = settings(qtbot)
    dialog.next_button.click()
    assert worker.calls.nicknames == []
    assert worker.calls.renames == []
    assert worker.calls.consents == [CONSENT]


def test_save_sends_changed_names_one_after_another_then_the_consent(
    qtbot: QtBot,
) -> None:
    dialog, worker = settings(qtbot)
    dialog.profile_nickname_edit.setText("grace_h")
    dialog.profile_device_edit.setText("Office PC")
    dialog.next_button.click()
    assert worker.calls.nicknames == ["grace_h"]
    assert worker.calls.renames == []  # waits for the nickname to land
    worker.push_me(me().model_copy(update={"nickname": "grace_h"}))
    assert worker.calls.renames == ["Office PC"]
    assert worker.calls.consents == []
    worker.push_me(me("Office PC").model_copy(update={"nickname": "grace_h"}))
    assert worker.calls.consents == [CONSENT]
    worker.push_me(me("Office PC"))
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.page != PAGE_DONE


def test_only_the_device_name_changed(qtbot: QtBot) -> None:
    dialog, worker = settings(qtbot)
    dialog.profile_device_edit.setText("Office PC")
    dialog.next_button.click()
    assert worker.calls.nicknames == []
    assert worker.calls.renames == ["Office PC"]


def test_settings_labels_state_each_rule_once(qtbot: QtBot) -> None:
    dialog, _ = settings(qtbot)
    page = dialog.stack.widget(PAGE_PRIVACY)
    assert page is not None
    texts = [w.text() for w in page.findChildren(QLabel) if w.isVisibleTo(dialog)]
    # One label for the nickname field, one for the device field; neither
    # reads as first-run ("Choose a nickname") and no rule is repeated.
    assert NICKNAME_INTRO not in texts
    assert DEVICE_NAME_LABEL not in texts
    assert sum("3 to 24" in t for t in texts) == 1
    assert sum("40 characters" in t for t in texts) == 1


def test_invalid_names_disable_save(qtbot: QtBot) -> None:
    dialog, _ = settings(qtbot)
    dialog.profile_nickname_edit.setText("a b")
    assert not dialog.next_button.isEnabled()
    dialog.profile_nickname_edit.setText("ada_l")
    assert dialog.next_button.isEnabled()
    dialog.profile_device_edit.setText("bad/name")
    assert not dialog.next_button.isEnabled()
    dialog.profile_device_edit.setText(" ")
    assert not dialog.next_button.isEnabled()


def test_a_taken_nickname_shows_its_text_and_stops_the_save(qtbot: QtBot) -> None:
    dialog, worker = settings(qtbot)
    dialog.profile_nickname_edit.setText("grace_h")
    dialog.profile_device_edit.setText("Office PC")
    dialog.next_button.click()
    worker.push_error("nickname_taken")
    assert dialog.privacy_error.text() == ERROR_TEXT["nickname_taken"]
    assert worker.calls.renames == []
    assert worker.calls.consents == []
    assert dialog.page == PAGE_PRIVACY
    assert dialog.next_button.isEnabled()


def test_fields_are_locked_while_searching_and_unlock_after(qtbot: QtBot) -> None:
    dialog, worker = settings(qtbot)
    assert dialog.profile_nickname_edit.isEnabled()
    worker.push_break(BreakState(phase="searching"))
    assert not dialog.profile_nickname_edit.isEnabled()
    assert not dialog.profile_device_edit.isEnabled()
    assert dialog.profile_locked_label.isVisibleTo(dialog)
    worker.push_break(BreakState())
    assert dialog.profile_nickname_edit.isEnabled()
    assert dialog.profile_device_edit.isEnabled()
    assert not dialog.profile_locked_label.isVisibleTo(dialog)


def test_fields_are_locked_in_a_live_chat_not_an_ended_one(qtbot: QtBot) -> None:
    dialog, worker = settings(qtbot)
    worker.push_chat(ChatState(match_id=uuid4()))
    assert not dialog.profile_nickname_edit.isEnabled()
    assert not dialog.profile_device_edit.isEnabled()
    ended = ChatState(match_id=uuid4(), ended="ended")
    worker.push_chat(ended)
    assert dialog.profile_nickname_edit.isEnabled()


def test_locked_at_open_when_a_break_is_already_running(qtbot: QtBot) -> None:
    worker = FakeRemoteWorker()
    worker.push_break(BreakState(phase="matched"))
    dialog = OnboardingDialog(worker, me())
    qtbot.addWidget(dialog)
    assert not dialog.profile_nickname_edit.isEnabled()


def test_a_name_locked_after_typing_is_not_sent(qtbot: QtBot) -> None:
    dialog, worker = settings(qtbot)
    dialog.profile_nickname_edit.setText("grace_h")
    worker.push_break(BreakState(phase="searching"))
    dialog.next_button.click()
    assert worker.calls.nicknames == []
    assert worker.calls.consents == [CONSENT]


def test_sign_in_pages_do_not_show_the_profile_fields(qtbot: QtBot) -> None:
    worker = FakeRemoteWorker()
    dialog = OnboardingDialog(worker)
    qtbot.addWidget(dialog)
    assert not dialog.profile_nickname_edit.isVisibleTo(dialog)
    assert not dialog.profile_device_edit.isVisibleTo(dialog)


def test_nickname_page_after_sign_in_hides_the_profile_fields(qtbot: QtBot) -> None:
    worker = FakeRemoteWorker()
    no_nick = me().model_copy(update={"nickname": None})
    dialog = OnboardingDialog(worker, no_nick)
    qtbot.addWidget(dialog)
    assert dialog.page == PAGE_NICKNAME
    dialog.nickname_edit.setText("ada_l")
    dialog.next_button.click()
    worker.push_me(me())
    assert dialog.page == PAGE_PRIVACY
    assert not dialog.profile_nickname_edit.isVisibleTo(dialog)


# --- against a live server ---


def device_names(server: LiveServer) -> list[str]:
    with server.session_factory() as session:
        return list(session.scalars(select(Device.name)))


def test_rename_through_settings_reaches_the_server(qtbot: QtBot, rig: Rig) -> None:  # noqa: F811
    first = new_dialog(qtbot, rig)
    sign_in(qtbot, rig, first)
    first.nickname_edit.setText("ada_l")
    first.next_button.click()
    wait_page(qtbot, first, PAGE_PRIVACY)
    first.next_button.click()
    wait_page(qtbot, first, PAGE_DONE)
    first.accept()
    assert rig.rec.last is not None
    # The name typed at sign-in is what Settings shows afterwards.
    assert rig.rec.last.device_name is not None

    dialog = new_dialog(qtbot, rig, rig.rec.last)
    dialog.profile_device_edit.setText("Renamed box")
    dialog.profile_nickname_edit.setText("ada_new")
    dialog.next_button.click()
    qtbot.waitUntil(lambda: dialog.result() == QDialog.DialogCode.Accepted)
    assert device_names(rig.server) == ["Renamed box"]
    assert rig.rec.last.nickname == "ada_new"
    assert rig.rec.last.device_name == "Renamed box"

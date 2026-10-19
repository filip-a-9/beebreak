"""ReportDialog (M5-06)."""

import re
from typing import get_args

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QToolTip
from pytestqt.qtbot import QtBot

from beebreak_companion.report_dialog import REASON_LABELS, ReportDialog
from beebreak_shared.api import ReportReason
from tests.fakes.chat import message_view

MESSAGES = (
    message_view(1, body="hello"),
    message_view(2, mine=True, body="hi"),
    message_view(5, body="<b>rude</b> ‮txt"),
)


@pytest.fixture
def dialog(qtbot: QtBot) -> ReportDialog:
    d = ReportDialog("bob_b", MESSAGES)
    qtbot.addWidget(d)
    return d


def _tick(dialog: ReportDialog, *rows: int) -> None:
    for row in rows:
        dialog.message_list.item(row).setCheckState(Qt.CheckState.Checked)


def test_reasons_cover_the_vocabulary_with_fixed_labels() -> None:
    assert set(REASON_LABELS) == set(get_args(ReportReason))


def test_the_intro_names_the_consequences(dialog: ReportDialog) -> None:
    assert dialog.intro_label.text() == (
        "Reporting ends the chat and blocks bob_b. "
        "Only the messages you tick are sent to the moderators."
    )
    assert dialog.intro_label.textFormat() == Qt.TextFormat.PlainText


def test_a_hostile_nickname_shows_a_question_mark(qtbot: QtBot) -> None:
    d = ReportDialog("<b>x</b>", MESSAGES)
    qtbot.addWidget(d)
    assert "blocks ?." in d.intro_label.text()
    assert "<b>" not in d.message_list.item(0).text()


def test_nothing_is_ticked_by_default(dialog: ReportDialog) -> None:
    assert dialog.message_list.count() == 3
    for row in range(3):
        assert dialog.message_list.item(row).checkState() == Qt.CheckState.Unchecked
    assert dialog._ticked() == ()


def test_a_reason_is_required(dialog: ReportDialog) -> None:
    assert not dialog.report_button.isEnabled()
    dialog.reason_buttons["hate"].setChecked(True)
    assert dialog.report_button.isEnabled()


def test_ticking_two_messages_emits_their_seqs(
    dialog: ReportDialog, qtbot: QtBot
) -> None:
    dialog.reason_buttons["harassment"].setChecked(True)
    _tick(dialog, 0, 2)
    with qtbot.waitSignal(dialog.submitted, timeout=1000) as blocker:
        dialog.report_button.click()
    assert blocker.args == ["harassment", None, (1, 5)]
    assert dialog.result() == dialog.DialogCode.Accepted


def test_reporting_without_ticking_sends_no_messages(
    dialog: ReportDialog, qtbot: QtBot
) -> None:
    dialog.reason_buttons["other"].setChecked(True)
    with qtbot.waitSignal(dialog.submitted, timeout=1000) as blocker:
        dialog.report_button.click()
    assert blocker.args == ["other", None, ()]


def test_the_note_is_normalized_and_blank_becomes_none(
    dialog: ReportDialog, qtbot: QtBot
) -> None:
    dialog.reason_buttons["spam"].setChecked(True)
    dialog.note_edit.setPlainText("  rude ‮ person  ")
    with qtbot.waitSignal(dialog.submitted, timeout=1000) as blocker:
        dialog.report_button.click()
    assert blocker.args[1] == "rude  person"


def test_a_blank_note_is_none(dialog: ReportDialog, qtbot: QtBot) -> None:
    dialog.reason_buttons["spam"].setChecked(True)
    dialog.note_edit.setPlainText("   \n ")
    with qtbot.waitSignal(dialog.submitted, timeout=1000) as blocker:
        dialog.report_button.click()
    assert blocker.args[1] is None


def test_a_note_over_500_characters_disables_the_button(
    dialog: ReportDialog,
) -> None:
    dialog.reason_buttons["spam"].setChecked(True)
    dialog.note_edit.setPlainText("x" * 500)
    assert dialog.report_button.isEnabled()
    assert re.findall(r"\d+", dialog.note_counter.text()) == ["500", "500"]
    dialog.note_edit.setPlainText("x" * 501)
    assert not dialog.report_button.isEnabled()
    assert re.findall(r"\d+", dialog.note_counter.text()) == ["501", "500"]


def test_cancel_emits_nothing(dialog: ReportDialog, qtbot: QtBot) -> None:
    dialog.reason_buttons["spam"].setChecked(True)
    _tick(dialog, 0)
    with qtbot.assertNotEmitted(dialog.submitted):
        dialog.cancel_button.click()
    assert dialog.result() == dialog.DialogCode.Rejected


def test_bodies_are_listed_as_plain_text_without_tooltips(
    dialog: ReportDialog,
) -> None:
    assert dialog.message_list.item(0).text() == "bob_b: hello"
    assert dialog.message_list.item(1).text().endswith("hi")
    assert "bob_b" not in dialog.message_list.item(1).text()
    assert dialog.message_list.item(2).text() == "bob_b: <b>rude</b> txt"
    for row in range(3):
        assert dialog.message_list.item(row).toolTip() == ""
    for label in dialog.findChildren(QLabel):
        assert label.textFormat() == Qt.TextFormat.PlainText
    assert QToolTip.text() == ""


def test_the_dialog_reads_no_message_other_than_its_own_list(qtbot: QtBot) -> None:
    # Seqs come from item data, so a body that looks like a number is harmless.
    d = ReportDialog("bob_b", (message_view(7, body="99"),))
    qtbot.addWidget(d)
    d.reason_buttons["spam"].setChecked(True)
    _tick(d, 0)
    assert d._ticked() == (7,)


def test_more_than_50_ticked_messages_cannot_be_submitted(qtbot: QtBot) -> None:
    messages = tuple(message_view(i) for i in range(1, 61))
    d = ReportDialog("bob_b", messages)
    qtbot.addWidget(d)
    d.show()
    d.reason_buttons["harassment"].setChecked(True)
    for row in range(50):
        d.message_list.item(row).setCheckState(Qt.CheckState.Checked)
    assert d.report_button.isEnabled()
    assert not d.limit_label.isVisibleTo(d)
    d.message_list.item(50).setCheckState(Qt.CheckState.Checked)
    assert not d.report_button.isEnabled()
    assert re.findall(r"\d+", d.limit_label.text()) == ["50"]
    assert d.limit_label.isVisibleTo(d)
    d.message_list.item(50).setCheckState(Qt.CheckState.Unchecked)
    assert d.report_button.isEnabled()


def test_the_tick_limit_is_the_contract_limit() -> None:
    from beebreak_companion.report_dialog import MAX_TICKED
    from beebreak_shared.api import ReportRequest

    limit = ReportRequest.model_fields["message_seqs"].metadata
    assert any(getattr(m, "max_length", None) == MAX_TICKED for m in limit)


def test_a_hint_explains_why_report_is_disabled_until_a_reason_is_picked(
    dialog: ReportDialog,
) -> None:
    dialog.show()
    assert not dialog.report_button.isEnabled()
    assert dialog.reason_hint_label.isVisible()
    dialog.reason_buttons["spam"].setChecked(True)
    assert dialog.report_button.isEnabled()
    assert not dialog.reason_hint_label.isVisible()


def test_an_empty_message_list_is_explained(qtbot: QtBot) -> None:
    empty = ReportDialog("bob_b", ())
    qtbot.addWidget(empty)
    empty.show()
    assert empty.no_messages_label.isVisible()
    assert not empty.message_list.isVisible()


def test_no_empty_list_explanation_when_there_are_messages(
    dialog: ReportDialog,
) -> None:
    dialog.show()
    assert not dialog.no_messages_label.isVisible()
    assert dialog.message_list.isVisible()

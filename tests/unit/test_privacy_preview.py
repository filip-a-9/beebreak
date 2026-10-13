import itertools
import json
import subprocess
import sys

import pytest
from PySide6.QtWidgets import QPlainTextEdit
from pytestqt.qtbot import QtBot

from breakroom_companion.privacy_preview import (
    NEVER_SHARED,
    PrivacyPreviewDialog,
    example_json,
)
from breakroom_shared.api import Consent
from breakroom_shared.presence import StackTag, build_presence

COMBOS = list(itertools.product([False, True], repeat=3))


def _consent(combo: tuple[bool, bool, bool]) -> Consent:
    cat, tags, time = combo
    return Consent(
        version=1,
        share_task_category=cat,
        share_stack_tags=tags,
        share_time_bucket=time,
    )


@pytest.mark.parametrize("combo", COMBOS)
def test_json_has_exactly_the_presence_keys_and_only_consented_values(
    combo: tuple[bool, bool, bool],
) -> None:
    data = json.loads(example_json(_consent(combo)))
    reference = json.loads(
        build_presence(
            _consent(combo),
            task_category="backend",
            stack_tags=["python"],
            remaining_time_bucket="10_20m",
        ).model_dump_json(indent=2)
    )
    cat, tags, time = combo
    assert (
        set(data)
        == set(reference)
        == {
            "schema_version",
            "presence_id",
            "task_category",
            "stack_tags",
            "remaining_time_bucket",
            "status",
        }
    )
    assert data["task_category"] == ("backend" if cat else None)
    assert data["stack_tags"] == (["python"] if tags else [])
    assert data["remaining_time_bucket"] == ("10_20m" if time else None)
    assert data["status"] == "available"


def test_dialog_is_read_only_and_labelled(qtbot: QtBot) -> None:
    dialog = PrivacyPreviewDialog(_consent((False, False, False)))
    qtbot.addWidget(dialog)
    assert isinstance(dialog.example_edit, QPlainTextEdit)
    assert dialog.example_edit.isReadOnly()
    assert dialog.example_label.text() == "Example"
    assert json.loads(dialog.example_edit.toPlainText())["status"] == "available"


def test_never_shared_list_is_shown(qtbot: QtBot) -> None:
    dialog = PrivacyPreviewDialog(_consent((True, True, True)))
    qtbot.addWidget(dialog)
    shown = dialog.never_list_label.text()
    for item in NEVER_SHARED:
        assert item in shown
    for needle in ("prompts", "Source code", "paths", "Claude session"):
        assert needle in shown


def test_set_consent_updates_the_text(qtbot: QtBot) -> None:
    dialog = PrivacyPreviewDialog(_consent((False, False, False)))
    qtbot.addWidget(dialog)
    assert json.loads(dialog.example_edit.toPlainText())["task_category"] is None
    dialog.set_consent(_consent((True, False, False)))
    assert json.loads(dialog.example_edit.toPlainText())["task_category"] == "backend"
    dialog.set_consent(_consent((False, False, False)))
    assert json.loads(dialog.example_edit.toPlainText())["task_category"] is None


def test_module_imports_no_network_library() -> None:
    code = (
        "import sys, breakroom_companion.privacy_preview;"
        "bad = {'httpx', 'websockets'} & set(sys.modules);"
        "sys.exit(1 if bad else 0)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        env={"QT_QPA_PLATFORM": "offscreen"},
        check=False,
    )
    assert result.returncode == 0


# --- M4-06: the user's real tags ---


def test_preview_uses_the_chosen_tags_when_consented() -> None:
    consent = _consent((False, True, False))
    data = json.loads(example_json(consent, ("go", "docker")))
    assert data["stack_tags"] == ["go", "docker"]


def test_preview_falls_back_to_the_example_tag_when_none_are_picked() -> None:
    consent = _consent((False, True, False))
    assert json.loads(example_json(consent))["stack_tags"] == ["python"]
    assert json.loads(example_json(consent, ()))["stack_tags"] == ["python"]


def test_preview_never_shows_tags_without_consent() -> None:
    data = json.loads(example_json(_consent((True, False, True)), ("go",)))
    assert data["stack_tags"] == []


def test_preview_matches_build_presence_for_the_chosen_tags() -> None:
    consent = _consent((True, True, True))
    tags: tuple[StackTag, ...] = ("rust", "react")
    data = json.loads(example_json(consent, tags))
    reference = json.loads(
        build_presence(
            consent,
            task_category="backend",
            stack_tags=tags,
            remaining_time_bucket="10_20m",
        ).model_dump_json()
    )
    data.pop("presence_id")
    reference.pop("presence_id")
    assert data == reference


def test_dialog_follows_tag_changes(qtbot: QtBot) -> None:
    consent = _consent((False, True, False))
    dialog = PrivacyPreviewDialog(consent, None, ("go",))
    qtbot.addWidget(dialog)
    assert json.loads(dialog.example_edit.toPlainText())["stack_tags"] == ["go"]
    dialog.set_consent(consent, ("rust",))
    assert json.loads(dialog.example_edit.toPlainText())["stack_tags"] == ["rust"]
    dialog.set_consent(consent)  # keeps the last tags
    assert json.loads(dialog.example_edit.toPlainText())["stack_tags"] == ["rust"]

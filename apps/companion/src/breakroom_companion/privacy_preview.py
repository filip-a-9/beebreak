"""Read-only "What this app shares" preview. Owned by M3-05b.

Shows an example of the only task metadata that may ever leave the machine
(ARCHITECTURE §4), built by the same `build_presence` the sender will use, plus
the fixed list of content that never leaves. Nothing here touches the network:
this module imports neither `httpx` nor `websockets` (M3 rule 13).
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from breakroom_shared.api import Consent
from breakroom_shared.presence import build_presence

EXAMPLE_HEADING = "Example"
EXAMPLE_NOTE = (
    "Example values only. Your real values are chosen locally and only the "
    "fields you tick are included."
)
NOTHING_SHARED_NOTE = "In this version nothing is shared."
NEVER_SHARED_HEADING = "Never shared"
# ARCHITECTURE §4 "Protected local content".
NEVER_SHARED: tuple[str, ...] = (
    "Your prompts",
    "Source code",
    "Command arguments",
    "Tool results",
    "Plans",
    "Stack traces",
    "Secrets and environment values",
    "File and folder paths",
    "Workspace and repository names",
    "Detailed file names",
    "Claude session identifiers",
)


def example_json(consent: Consent) -> str:
    """Pretty JSON of an example presence for `consent`."""
    return build_presence(
        consent,
        task_category="backend",
        stack_tags=["python"],
        remaining_time_bucket="10_20m",
    ).model_dump_json(indent=2)


def _plain(text: str) -> QLabel:
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


class PrivacyPreviewDialog(QDialog):
    def __init__(self, consent: Consent, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("What this app shares")
        self.example_label = _plain(EXAMPLE_HEADING)
        self.note_label = _plain(EXAMPLE_NOTE)
        self.nothing_shared_label = _plain(NOTHING_SHARED_NOTE)
        self.example_edit = QPlainTextEdit()
        self.example_edit.setReadOnly(True)
        self.never_label = _plain(NEVER_SHARED_HEADING)
        bullets = "\n".join(f"\N{BULLET} {item}" for item in NEVER_SHARED)
        self.never_list_label = _plain(bullets)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        for widget in (
            self.example_label,
            self.note_label,
            self.nothing_shared_label,
            self.example_edit,
            self.never_label,
            self.never_list_label,
            buttons,
        ):
            layout.addWidget(widget)
        self.set_consent(consent)

    def set_consent(self, consent: Consent) -> None:
        """Re-render the example for the given (possibly unsaved) choices."""
        self.example_edit.setPlainText(example_json(consent))

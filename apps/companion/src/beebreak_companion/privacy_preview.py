"""Read-only "What this app shares" preview. Owned by M3-05b.

Shows an example of the only task metadata that may ever leave the machine
(ARCHITECTURE §4), built by the same `build_presence` the sender will use, plus
the fixed list of content that never leaves. Nothing here touches the network:
this module imports neither `httpx` nor `websockets` (M3 rule 13).
"""

import re
import socket
from collections.abc import Sequence

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from beebreak_companion.config import wsl_distro_name
from beebreak_shared.api import Consent
from beebreak_shared.presence import StackTag, build_presence

EXAMPLE_HEADING = "Example"
EXAMPLE_NOTE = (
    "Example values only. Your real values are chosen locally, and fields you "
    "have not turned on are left empty."
)
SENT_ONLY_NOTE = (
    "These fields are sent only when you start a break, and only the ones you "
    "have turned on."
)
EXAMPLE_TAG: StackTag = "python"
DEFAULT_DEVICE_NAME = "WSL device"
_DEVICE_NAME_MAX = 40
_NOT_ALLOWED = re.compile(r"[^A-Za-z0-9 ._-]+")


def default_device_name(distro: str = "") -> str:
    """ "WSL device" plus the user-chosen WSL distro name, so two devices differ.

    `distro` comes from `config.wsl_distro_name()`. Falls back to the plain
    default when it is empty, has nothing usable, or equals or contains the
    machine hostname (which is only compared here, never sent: M6-C11).
    Always a valid DeviceName."""
    cleaned = _NOT_ALLOWED.sub("", distro).strip()
    host = socket.gethostname().strip().lower()
    if not cleaned or (host and host in cleaned.lower()):
        return DEFAULT_DEVICE_NAME
    return f"{DEFAULT_DEVICE_NAME} {cleaned}"[:_DEVICE_NAME_MAX].rstrip()


SIGN_IN_HEADING = "Sent when you sign in or change it in Settings"
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


def example_json(consent: Consent, tags: Sequence[StackTag] = ()) -> str:
    """Pretty JSON of an example presence for `consent`.

    Uses the user's own tags, or one example tag when none are picked."""
    return build_presence(
        consent,
        task_category="backend",
        stack_tags=list(tags) or [EXAMPLE_TAG],
        remaining_time_bucket="10_20m",
    ).model_dump_json(indent=2)


ALREADY_SENT_ITEM = "Device name: the name you chose at sign-in or in Settings"


def sign_in_items(
    device_name: str | None = None, *, already_sent: bool = False
) -> tuple[str, ...]:
    """What leaves the machine at sign-in. After sign-in the name is not stored
    locally, so the list says so instead of showing a possibly wrong name."""
    name_item = (
        ALREADY_SENT_ITEM
        if already_sent
        else f"Device name: {device_name or default_device_name(wsl_distro_name())}"
    )
    return ("Email", "Nickname", name_item)


def _plain(text: str) -> QLabel:
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


class PrivacyPreviewDialog(QDialog):
    def __init__(
        self,
        consent: Consent,
        parent: QWidget | None = None,
        tags: Sequence[StackTag] = (),
        device_name: str | None = None,
        already_sent: bool = False,
    ) -> None:
        super().__init__(parent)
        self._tags: tuple[StackTag, ...] = tuple(tags)
        self.setWindowTitle("What this app shares")
        self.example_label = _plain(EXAMPLE_HEADING)
        self.note_label = _plain(EXAMPLE_NOTE)
        self.sent_only_label = _plain(SENT_ONLY_NOTE)
        self.example_edit = QPlainTextEdit()
        self.example_edit.setReadOnly(True)
        self.signin_label = _plain(SIGN_IN_HEADING)
        self.signin_list_label = _plain("")
        self.set_device_name(device_name, already_sent=already_sent)
        self.never_label = _plain(NEVER_SHARED_HEADING)
        bullets = "\n".join(f"\N{BULLET} {item}" for item in NEVER_SHARED)
        self.never_list_label = _plain(bullets)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        for widget in (
            self.example_label,
            self.note_label,
            self.sent_only_label,
            self.example_edit,
            self.signin_label,
            self.signin_list_label,
            self.never_label,
            self.never_list_label,
            buttons,
        ):
            layout.addWidget(widget)
        self.set_consent(consent)
        self.resize(max(480, self.sizeHint().width()), self.sizeHint().height())

    def set_consent(
        self, consent: Consent, tags: Sequence[StackTag] | None = None
    ) -> None:
        """Re-render the example for the given (possibly unsaved) choices."""
        if tags is not None:
            self._tags = tuple(tags)
        self.example_edit.setPlainText(example_json(consent, self._tags))
        # Tall enough for the whole example JSON, whatever the font (it was
        # clipped).
        edit = self.example_edit
        lines = edit.document().blockCount()
        margins = 2 * (edit.frameWidth() + int(edit.document().documentMargin()))
        edit.setMinimumHeight((lines + 1) * edit.fontMetrics().lineSpacing() + margins)

    def set_device_name(
        self, device_name: str | None, *, already_sent: bool = False
    ) -> None:
        """Show the name that will be sent at sign-in (None: the default), or,
        once it was sent, that it is the name chosen then."""
        items = sign_in_items(device_name, already_sent=already_sent)
        bullets = "\n".join(f"\N{BULLET} {item}" for item in items)
        self.signin_list_label.setText(bullets)

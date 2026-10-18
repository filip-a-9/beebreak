"""Report dialog opened from the chat window. Owned by M5-06.

Nothing is ticked by default; only the ticked messages' seqs leave the
dialog. Message bodies are plain text (rule 17): list items render as plain
text, there are no tooltips, and bodies are normalized again for display.
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from beebreak_companion.window import safe_nickname
from beebreak_shared.api import MAX_REPORT_SEQS, MessageView, ReportReason
from beebreak_shared.chat import MAX_NOTE_CHARS, normalize_text

REASON_LABELS: dict[ReportReason, str] = {
    "harassment": "Harassment or abuse",
    "spam": "Spam or advertising",
    "sexual": "Sexual content",
    "hate": "Hate speech",
    "other": "Something else",
}
REPORT = "Report and block"
CANCEL = "Cancel"
NOTE_PLACEHOLDER = "Optional note"
MESSAGES_HEADING = "Messages to include (none are included unless you tick them):"
YOU = "You"
MAX_TICKED = MAX_REPORT_SEQS  # derived: cannot drift from the contract
TOO_MANY = f"Tick at most {MAX_TICKED} messages."


def display_text(body: str) -> str:
    """The body as shown: normalized again so no control or bidi character
    reaches a widget, whatever the server sent."""
    return normalize_text(body, max_chars=max(len(body), 1)) or ""


class ReportDialog(QDialog):
    submitted = Signal(str, object, object)  # reason, note | None, seqs

    def __init__(
        self,
        nickname: str,
        messages: tuple[MessageView, ...],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        peer = safe_nickname(nickname)
        self.setWindowTitle("Report")

        self.intro_label = QLabel(
            f"Reporting ends the chat and blocks {peer}. "
            "Only the messages you tick are sent to the moderators."
        )
        self.intro_label.setTextFormat(Qt.TextFormat.PlainText)
        self.intro_label.setWordWrap(True)

        self.reason_buttons: dict[ReportReason, QRadioButton] = {}
        layout = QVBoxLayout(self)
        layout.addWidget(self.intro_label)
        for reason, label in REASON_LABELS.items():
            button = QRadioButton(label)
            button.toggled.connect(self._refresh)
            self.reason_buttons[reason] = button
            layout.addWidget(button)

        self.note_edit = QPlainTextEdit()
        self.note_edit.setPlaceholderText(NOTE_PLACEHOLDER)
        self.note_edit.setFixedHeight(70)
        self.note_edit.textChanged.connect(self._refresh)
        self.limit_label = QLabel(TOO_MANY)
        self.limit_label.setTextFormat(Qt.TextFormat.PlainText)
        self.note_counter = QLabel()
        self.note_counter.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.note_edit)
        layout.addWidget(self.note_counter)

        heading = QLabel(MESSAGES_HEADING)
        heading.setTextFormat(Qt.TextFormat.PlainText)
        heading.setWordWrap(True)
        layout.addWidget(heading)
        self.message_list = QListWidget()
        self.message_list.setWordWrap(True)
        for message in messages:
            who = YOU if message.mine else peer
            item = QListWidgetItem(f"{who}: {display_text(message.body)}")
            item.setData(Qt.ItemDataRole.UserRole, message.seq)
            item.setFlags(
                Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsUserCheckable
                | Qt.ItemFlag.ItemIsSelectable
            )
            item.setCheckState(Qt.CheckState.Unchecked)
            self.message_list.addItem(item)
        layout.addWidget(self.message_list)
        layout.addWidget(self.limit_label)
        self.message_list.itemChanged.connect(self._refresh)

        self.report_button = QPushButton(REPORT)
        self.cancel_button = QPushButton(CANCEL)
        self.report_button.clicked.connect(self._on_report)
        self.cancel_button.clicked.connect(self.reject)
        layout.addWidget(self.report_button)
        layout.addWidget(self.cancel_button)
        self._refresh()

    def _reason(self) -> ReportReason | None:
        for reason, button in self.reason_buttons.items():
            if button.isChecked():
                return reason
        return None

    def _ticked(self) -> tuple[int, ...]:
        seqs: list[int] = []
        for row in range(self.message_list.count()):
            item = self.message_list.item(row)
            if item.checkState() == Qt.CheckState.Checked:
                seqs.append(int(item.data(Qt.ItemDataRole.UserRole)))
        return tuple(seqs)

    def _refresh(self) -> None:
        count = len(self.note_edit.toPlainText())
        self.note_counter.setText(f"{count} / {MAX_NOTE_CHARS}")
        too_many = len(self._ticked()) > MAX_TICKED
        self.limit_label.setVisible(too_many)
        self.report_button.setEnabled(
            self._reason() is not None and count <= MAX_NOTE_CHARS and not too_many
        )

    def _on_report(self) -> None:
        reason = self._reason()
        text = self.note_edit.toPlainText()
        if reason is None or len(text) > MAX_NOTE_CHARS:
            return
        if len(self._ticked()) > MAX_TICKED:
            return
        note = normalize_text(text, max_chars=MAX_NOTE_CHARS)
        self.submitted.emit(reason, note, self._ticked())
        self.accept()

"""Report dialog opened from the chat window. Owned by M5-06.

Nothing is ticked by default; only the ticked messages' seqs leave the
dialog. Message bodies are plain text (rule 17).
"""

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QDialog, QWidget

from breakroom_shared.api import MessageView


class ReportDialog(QDialog):
    submitted = Signal(str, object, object)  # reason, note | None, seqs

    def __init__(
        self,
        nickname: str,
        messages: tuple[MessageView, ...],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)

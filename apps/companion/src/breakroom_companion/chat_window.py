"""Compact chat window, one per live or just-ended chat. Owned by M5-06.

Peer text is plain text everywhere (rule 17): every body and nickname is shown
with `Qt.TextFormat.PlainText`, never linkified. The window only renders the
`ChatState` it is given and emits the user's intents; `app.py` wires those to
`RemoteWorker`.
"""

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QWidget

from breakroom_companion.chat_state import ChatState


class ChatWindow(QWidget):
    send_requested = Signal(str)  # raw text; the worker normalizes
    retry_requested = Signal(object)  # UUID of a failed pending message
    extend_requested = Signal()
    extension_answered = Signal(bool)  # accept / decline (or cancel own request)
    end_requested = Signal()
    block_requested = Signal()
    report_requested = Signal(str, object, object)  # reason, note | None, seqs
    close_requested = Signal()  # an ended chat's window was closed

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

    def set_chat(self, state: ChatState) -> None:
        """Render `state` (a fresh `ChatState()` means no chat)."""

    def set_connected(self, connected: bool) -> None:
        """This companion's own connection (the "Reconnecting…" line)."""

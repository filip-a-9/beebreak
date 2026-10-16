"""Compact always-on-top companion window. Owned by M1-04.

Shows one fixed string per state and never any event-derived text (T11).
The account row (M3-05b) shows fixed strings too; the nickname is the only
server text, re-validated against the Nickname pattern and shown as plain text.
"""

from collections.abc import Iterable

from pydantic import TypeAdapter, ValidationError
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from breakroom_companion.break_state import BreakState
from breakroom_shared.api import Me, Nickname
from breakroom_shared.ipc import InvitationState
from breakroom_shared.presence import RemainingTimeBucket, StackTag, TaskCategory

_LABELS: dict[InvitationState, str] = {
    "disabled": "Break suggestions are off",
    "idle": "Idle \N{EM DASH} will suggest a break when appropriate",
    "suggested": "Your agent is busy. Want to take a short break?",
}

# Fixed strings only (T11). under_5m never appears: it vetoes the invitation.
_ESTIMATE_LABELS: dict[RemainingTimeBucket, str] = {
    "5_10m": "~5\N{EN DASH}10 min left",
    "10_20m": "~10\N{EN DASH}20 min left",
    "20m_plus": "20+ min left",
}

# Account row. Fixed strings only; ConnectionState values as keys.
_ACCOUNT_LABELS: dict[str, str] = {
    "signed_out": "Not signed in",
    "signing_in": "Signing in\N{HORIZONTAL ELLIPSIS}",
    "connecting": "Connecting\N{HORIZONTAL ELLIPSIS}",
    "connected": "Connected",
    "offline": "Offline \N{EN DASH} retrying",
}

# Break area (M4-06). Fixed strings only. Peer text never reaches a label as
# received: tags and categories go through these maps, unknown values are dropped.
FIND = "Find a break partner"
SEARCHING = "Looking for a break partner\N{HORIZONTAL ELLIPSIS}"
CANCEL = "Cancel"
CHATTING_WITH = "Chatting with"
OPEN_CHAT = "Open chat"
IN_COMMON = "In common:"
BLOCK = "Block"
OK = "OK"
NOBODY = "Nobody is around right now. Try again later."
BREAK_ENDED = "Break ended."
BLOCK_TITLE = "Block this person?"
BLOCK_CONFIRM = "You won't be matched with this person again."
# Worker error codes with a line in the break area; all others show nothing
# here (the chat window and onboarding show their own).
BREAK_ERROR_TEXT: dict[str, str] = {
    "offline": "Not connected. Try again in a moment.",
}
ERROR_SECONDS = 5  # how long a failed-action line stays (also the chat window)

TAG_LABELS: dict[StackTag, str] = {
    "python": "Python",
    "javascript": "JavaScript",
    "typescript": "TypeScript",
    "go": "Go",
    "rust": "Rust",
    "java": "Java",
    "kotlin": "Kotlin",
    "csharp": "C#",
    "cpp": "C++",
    "ruby": "Ruby",
    "php": "PHP",
    "swift": "Swift",
    "react": "React",
    "vue": "Vue",
    "node": "Node.js",
    "django": "Django",
    "fastapi": "FastAPI",
    "postgres": "PostgreSQL",
    "docker": "Docker",
    "kubernetes": "Kubernetes",
}
# "other" is deliberately absent: it is never shown.
CATEGORY_LABELS: dict[TaskCategory, str] = {
    "backend": "Backend",
    "frontend": "Frontend",
    "devops": "DevOps",
    "data": "Data",
    "testing": "Testing",
}
_ACTIVE_PHASES = ("joining", "searching", "matched")


def shared_labels(tags: Iterable[object], category: object) -> list[str]:
    """Display strings for what both people shared; unknown values are dropped."""
    labels = [TAG_LABELS[t] for t in tags if isinstance(t, str) and t in TAG_LABELS]
    if isinstance(category, str) and category in CATEGORY_LABELS:
        labels.insert(0, CATEGORY_LABELS[category])
    return labels


CONNECTED_AS = "Connected as"
UNKNOWN_NICKNAME = "?"
SERVER_REFUSED = "Server address not allowed"
UNENCRYPTED_NOTE = "Stored unencrypted"
_NICKNAME = TypeAdapter(Nickname)


def safe_nickname(raw: object) -> str:
    """The nickname if it still matches the Nickname pattern, else `?`."""
    try:
        return _NICKNAME.validate_python(raw, strict=True)
    except ValidationError:
        return UNKNOWN_NICKNAME


class CompanionWindow(QWidget):
    accepted = Signal()
    declined = Signal()
    enabled_changed = Signal(bool)
    sign_in_requested = Signal()
    settings_requested = Signal()
    sign_out_requested = Signal()
    find_requested = Signal()
    cancel_requested = Signal()
    block_requested = Signal()
    dismiss_requested = Signal()
    open_chat_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Breakroom")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.resize(320, 220)

        self.status_label = QLabel()
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setWordWrap(True)
        self.estimate_label = QLabel()
        self.estimate_label.setTextFormat(Qt.TextFormat.PlainText)
        self.accept_button = QPushButton("Accept")
        self.decline_button = QPushButton("Decline")
        # Consent must be a deliberate click: a keypress meant for the terminal
        # must never land on these buttons (THREAT_MODEL T10).
        for button in (self.accept_button, self.decline_button):
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.enabled_checkbox = QCheckBox("Suggest breaks")

        self.account_label = QLabel()
        self.account_label.setTextFormat(Qt.TextFormat.PlainText)
        self.account_label.setWordWrap(True)
        self.account_note = QLabel(UNENCRYPTED_NOTE)
        self.account_note.setTextFormat(Qt.TextFormat.PlainText)
        self.sign_in_button = QPushButton("Sign in")
        self.finish_setup_button = QPushButton("Finish setup")
        self.settings_button = QPushButton("Settings")
        self.sign_out_button = QPushButton("Sign out")
        self._account_buttons = (
            self.sign_in_button,
            self.finish_setup_button,
            self.settings_button,
            self.sign_out_button,
        )
        for button in self._account_buttons:
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.break_label = QLabel()
        self.break_label.setTextFormat(Qt.TextFormat.PlainText)
        self.break_label.setWordWrap(True)
        self.shared_label = QLabel()
        self.shared_label.setTextFormat(Qt.TextFormat.PlainText)
        self.shared_label.setWordWrap(True)
        self.break_error_label = QLabel()
        self.break_error_label.setTextFormat(Qt.TextFormat.PlainText)
        self.break_error_label.setWordWrap(True)
        self.break_error_label.setVisible(False)
        self._break_error_timer = QTimer(self)
        self._break_error_timer.setSingleShot(True)
        self._break_error_timer.setInterval(ERROR_SECONDS * 1000)
        self._break_error_timer.timeout.connect(self._clear_break_error)
        self.find_button = QPushButton(FIND)
        self.cancel_button = QPushButton(CANCEL)
        self.block_button = QPushButton(BLOCK)
        self.ok_button = QPushButton(OK)
        self.open_chat_button = QPushButton(OPEN_CHAT)
        self._break_buttons = (
            self.find_button,
            self.cancel_button,
            self.open_chat_button,
            self.block_button,
            self.ok_button,
        )
        self._unread = 0
        self._chat_held = False
        for button in self._break_buttons:
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._break = BreakState()
        self._ready = False
        self._invitation: InvitationState = "idle"
        self._remote_state = "signed_out"
        self._me: Me | None = None
        self._store_encrypted = True
        self._remote_unavailable = False

        buttons = QHBoxLayout()
        buttons.addWidget(self.accept_button)
        buttons.addWidget(self.decline_button)
        layout = QVBoxLayout(self)
        layout.addWidget(self.status_label)
        layout.addWidget(self.estimate_label)
        layout.addLayout(buttons)
        layout.addWidget(self.break_label)
        layout.addWidget(self.shared_label)
        layout.addWidget(self.break_error_label)
        break_buttons = QHBoxLayout()
        for button in self._break_buttons:
            break_buttons.addWidget(button)
        layout.addLayout(break_buttons)
        layout.addStretch()
        layout.addWidget(self.enabled_checkbox)
        layout.addWidget(self.account_label)
        layout.addWidget(self.account_note)
        account_buttons = QHBoxLayout()
        for button in self._account_buttons:
            account_buttons.addWidget(button)
        layout.addLayout(account_buttons)

        self.accept_button.clicked.connect(self.accepted)
        self.decline_button.clicked.connect(self.declined)
        # Only user clicks emit; set_state uses setChecked, which doesn't.
        self.enabled_checkbox.clicked.connect(self.enabled_changed)

        self.find_button.clicked.connect(self.find_requested)
        self.cancel_button.clicked.connect(self.cancel_requested)
        self.block_button.clicked.connect(self._on_block_clicked)
        self.ok_button.clicked.connect(self.dismiss_requested)
        self.open_chat_button.clicked.connect(self.open_chat_requested)

        self.sign_in_button.clicked.connect(self.sign_in_requested)
        self.finish_setup_button.clicked.connect(self.settings_requested)
        self.settings_button.clicked.connect(self.settings_requested)
        self.sign_out_button.clicked.connect(self.sign_out_requested)

        self.set_state("idle")
        self._render_break()
        self._render_account()

    def set_state(
        self, state: InvitationState, estimate: RemainingTimeBucket | None = None
    ) -> None:
        self.status_label.setText(_LABELS[state])
        text = (
            _ESTIMATE_LABELS.get(estimate)
            if estimate and state == "suggested"
            else None
        )
        self.estimate_label.setText(text or "")
        self.estimate_label.setVisible(text is not None)
        self._invitation = state
        offered = state == "suggested" and not self._break_active
        self.accept_button.setVisible(offered)
        self.decline_button.setVisible(offered)
        self.enabled_checkbox.setChecked(state != "disabled")
        if state == "suggested":
            self.show()
            self.raise_()

    # --- break area (M4-06) ---

    @property
    def _break_active(self) -> bool:
        return self._break.phase in _ACTIVE_PHASES

    def set_ready(self, ready: bool) -> None:
        """Connected with nickname and consent saved: Find may be offered."""
        self._ready = ready
        self._render_break()

    def set_break(self, state: BreakState) -> None:
        self._break = state
        if self._break_active:
            self._clear_break_error()
        self._render_break()
        offered = self._invitation == "suggested" and not self._break_active
        self.accept_button.setVisible(offered)
        self.decline_button.setVisible(offered)
        if state.phase in ("matched", "nobody_available"):
            self.show()
            self.raise_()

    def show_error(self, code: str) -> None:
        """A failed break action, as a fixed line that clears itself. The
        server's own text is never shown."""
        text = BREAK_ERROR_TEXT.get(code)
        if text is None:
            return
        self.break_error_label.setText(text)
        self.break_error_label.setVisible(True)
        self._break_error_timer.start()

    def _clear_break_error(self) -> None:
        self._break_error_timer.stop()
        self.break_error_label.setText("")
        self.break_error_label.setVisible(False)

    def set_chat_held(self, held: bool) -> None:
        """The worker holds an ended chat nobody has closed yet: its window
        (and a late report) stays reachable through Open chat."""
        self._chat_held = held
        self._render_break()

    def set_unread(self, count: int) -> None:
        """Peer messages since the chat window was last active."""
        self._unread = max(0, count)
        self._render_break()

    def _render_break(self) -> None:
        phase = self._break.phase
        text = ""
        shared = ""
        if phase in ("joining", "searching"):
            text = SEARCHING
        elif phase == "matched":
            match = self._break.match
            peer = safe_nickname(match.peer_nickname if match else None)
            text = f"{CHATTING_WITH} {peer}"
            if match is not None:
                labels = shared_labels(match.shared_tags, match.shared_category)
                if labels:
                    shared = f"{IN_COMMON} {', '.join(labels)}"
        elif phase == "nobody_available":
            text = NOBODY
        elif phase == "ended":
            text = BREAK_ENDED
        self.break_label.setText(text)
        self.break_label.setVisible(bool(text))
        self.shared_label.setText(shared)
        self.shared_label.setVisible(bool(shared))
        find = self._ready and phase in ("none", "nobody_available")
        self.find_button.setVisible(find)
        self.cancel_button.setVisible(phase in ("joining", "searching"))
        self.open_chat_button.setText(
            f"{OPEN_CHAT} ({self._unread})" if self._unread else OPEN_CHAT
        )
        self.open_chat_button.setVisible(phase == "matched" or self._chat_held)
        # End and Block live in the chat window while chatting (M5-06). After
        # the peer ended it, Block is still offered while the match is known,
        # unless this user already blocked or reported them (X4).
        can_block = (
            phase == "ended"
            and self._break.match is not None
            and not self._break.blocked
        )
        self.block_button.setVisible(can_block)
        self.ok_button.setVisible(phase in ("nobody_available", "ended"))

    def _on_block_clicked(self) -> None:
        answer = QMessageBox.question(
            self,
            BLOCK_TITLE,
            BLOCK_CONFIRM,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.block_requested.emit()

    # --- account row (M3-05b) ---

    def set_remote_state(self, state: str) -> None:
        if state in _ACCOUNT_LABELS:
            self._remote_state = state
            self._render_account()

    def set_me(self, me: Me | None) -> None:
        self._me = me
        self._render_account()

    def set_store_encrypted(self, encrypted: bool) -> None:
        self._store_encrypted = encrypted
        self._render_account()

    def set_remote_unavailable(self) -> None:
        """The configured server address was refused: the row stays disabled."""
        self._remote_unavailable = True
        self._render_account()

    def _render_account(self) -> None:
        if self._remote_unavailable:
            self.account_label.setText(SERVER_REFUSED)
            self.account_note.setVisible(False)
            for button in self._account_buttons:
                button.setVisible(False)
            return
        state = self._remote_state
        me = self._me
        text = _ACCOUNT_LABELS[state]
        if state == "connected" and me is not None and me.nickname is not None:
            text = f"{CONNECTED_AS} {safe_nickname(me.nickname)}"
        self.account_label.setText(text)
        signed_in = state in ("connecting", "connected", "offline")
        incomplete = (
            signed_in and me is not None and (me.nickname is None or me.consent is None)
        )
        self.account_note.setVisible(signed_in and not self._store_encrypted)
        self.sign_in_button.setVisible(state == "signed_out")
        self.finish_setup_button.setVisible(incomplete)
        self.settings_button.setVisible(signed_in and me is not None and not incomplete)
        self.sign_out_button.setVisible(signed_in or state == "signing_in")

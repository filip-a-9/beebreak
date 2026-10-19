"""Compact always-on-top companion window. Owned by M1-04.

Shows one fixed string per state and never any event-derived text (T11).
The account row (M3-05b) shows fixed strings too; the nickname is the only
server text, re-validated against the Nickname pattern and shown as plain text.
"""

from collections.abc import Iterable
from datetime import UTC, datetime

from pydantic import TypeAdapter, ValidationError
from PySide6.QtCore import QEvent, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QResizeEvent
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from beebreak_companion import theme
from beebreak_companion.break_state import BreakPhase, BreakState
from beebreak_companion.config import TriggerConfig, queue_wait_s
from beebreak_shared.api import Me, Nickname
from beebreak_shared.ipc import InvitationState, NotReadyReason
from beebreak_shared.presence import RemainingTimeBucket, StackTag, TaskCategory

_LABELS: dict[InvitationState, str] = {
    "disabled": "Break suggestions are off",
    "idle": "Idle \N{EM DASH} will suggest a break when appropriate",
    "suggested": "Your agent is busy. Want to take a short break?",
}

# Shown instead of the idle line while the app cannot offer a break (U10 D2).
_NOT_READY_LABELS: dict[NotReadyReason, str] = {
    "signed_out": "Not signed in \N{EM DASH} sign in to get break suggestions",
    "connecting": "Connecting to the server\N{HORIZONTAL ELLIPSIS}",
    "offline": "Can't reach the server \N{EM DASH} retrying",
    "no_nickname": "Choose a nickname to get break suggestions",
    "no_consent": "Finish setup to get break suggestions",
}
NO_ACTIVITY = "No agent activity yet"
AGENT_BUSY = "Agent is working now"
OFFER_EXPIRED = "The break offer expired."
EXTENSION_DECLINED = "Your extension request was declined."
LEAVE_TITLE = "Leave the break and quit?"
LEAVE_TEXT = "A search or chat is active. Closing the last window quits Beebreak."

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
FIND_TIP = (
    "Match now with another person who is free, whether or not your agent is busy."
)
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

# Which theme state (pill look) each state shows; colours live in theme.py.
_INVITATION_PILL: dict[InvitationState, str] = {
    "disabled": "off",
    "idle": "connected",
    "suggested": "searching",
}
_BREAK_PILL: dict[BreakPhase, str] = {
    "none": "off",
    "joining": "searching",
    "searching": "searching",
    "matched": "connected",
    "nobody_available": "off",
    "ended": "off",
}
_ACCOUNT_PILL: dict[str, str] = {
    "signed_out": "off",
    "signing_in": "searching",
    "connecting": "searching",
    "connected": "connected",
    "offline": "offline",
}
MARK_SIZE = 22
WORDMARK = "beebreak"


def shared_labels(tags: Iterable[object], category: object) -> list[str]:
    """Display strings for what both people shared; unknown values are dropped."""
    labels = [TAG_LABELS[t] for t in tags if isinstance(t, str) and t in TAG_LABELS]
    if isinstance(category, str) and category in CATEGORY_LABELS:
        labels.insert(0, CATEGORY_LABELS[category])
    return labels


CONNECTED_AS = "Connected as"
UNKNOWN_NICKNAME = "?"
SERVER_REFUSED = "Server address not allowed"
UNENCRYPTED_NOTE = (
    "Your sign-in is stored unencrypted, in a file only you can read. To encrypt "
    "it, install a Secret Service keyring (such as gnome-keyring) and sign in again."
)
_NICKNAME = TypeAdapter(Nickname)


def _span(seconds: int) -> str:
    """`90 s`, or whole minutes (`2 min`) when it divides evenly."""
    if seconds >= 60 and seconds % 60 == 0:
        return f"{seconds // 60} min"
    return f"{seconds} s"


def offer_rule(config: TriggerConfig) -> str:
    """The idle line's rule: when an offer appears and how long it stays."""
    return (
        f"A break is offered after {_span(config.activity_threshold_s)} of agent "
        f"work and stays for {_span(config.suggestion_timeout_s)}."
    )


def _reserve(widget: QWidget) -> None:
    """Keep the widget's space when hidden, so nothing below it shifts (O2)."""
    policy = widget.sizePolicy()
    policy.setRetainSizeWhenHidden(True)
    widget.setSizePolicy(policy)


def confirm_leave(parent: QWidget) -> bool:
    """Ask before the last window closes during a search or chat (D2)."""
    answer = QMessageBox.question(
        parent,
        LEAVE_TITLE,
        LEAVE_TEXT,
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No,
    )
    return answer == QMessageBox.StandardButton.Yes


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

    def __init__(self, trigger: TriggerConfig | None = None) -> None:
        super().__init__()
        self._rule = offer_rule(trigger or TriggerConfig())
        self.setWindowTitle("Beebreak")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.resize(320, 220)

        self.mark_label = QLabel()
        self.mark_label.setPixmap(theme.mark_pixmap(theme.active_scheme(), MARK_SIZE))
        self.wordmark_label = QLabel(WORDMARK)
        self.wordmark_label.setObjectName("wordmark")
        self.wordmark_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label = QLabel()
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setWordWrap(True)
        # Two lines are always reserved: the idle line and the offer line wrap
        # differently, and the buttons below must not move between them (O2).
        self.status_label.setMinimumHeight(2 * self.status_label.fontMetrics().height())
        self.activity_label = QLabel()
        self.activity_label.setObjectName("muted")
        self.activity_label.setTextFormat(Qt.TextFormat.PlainText)
        self.rule_label = QLabel(self._rule)
        self.rule_label.setObjectName("muted")
        self.rule_label.setTextFormat(Qt.TextFormat.PlainText)
        self.rule_label.setWordWrap(True)
        self.estimate_label = QLabel()
        self.estimate_label.setObjectName("muted")
        self.estimate_label.setTextFormat(Qt.TextFormat.PlainText)
        self.expired_label = QLabel(OFFER_EXPIRED)
        self.expired_label.setObjectName("muted")
        self.expired_label.setTextFormat(Qt.TextFormat.PlainText)
        self.extension_label = QLabel(EXTENSION_DECLINED)
        self.extension_label.setTextFormat(Qt.TextFormat.PlainText)
        self.extension_label.setWordWrap(True)
        self.extension_label.setVisible(False)
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
        self.account_note.setObjectName("muted")
        self.account_note.setTextFormat(Qt.TextFormat.PlainText)
        self.account_note.setWordWrap(True)
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
        theme.set_state(self.break_error_label, "offline")
        self._break_error_timer = QTimer(self)
        self._break_error_timer.setSingleShot(True)
        self._break_error_timer.setInterval(ERROR_SECONDS * 1000)
        self._break_error_timer.timeout.connect(self._clear_break_error)
        self.find_button = QPushButton(FIND)
        self.find_button.setToolTip(FIND_TIP)
        self.cancel_button = QPushButton(CANCEL)
        self.block_button = QPushButton(BLOCK)
        self.ok_button = QPushButton(OK)
        self.open_chat_button = QPushButton(OPEN_CHAT)
        for primary in (self.find_button, self.accept_button, self.open_chat_button):
            primary.setObjectName("primary")
        self._break_buttons = (
            self.find_button,
            self.cancel_button,
            self.open_chat_button,
            self.block_button,
            self.ok_button,
        )
        # The offer area keeps its size whether or not an offer shows (O2).
        for offer_widget in (
            self.activity_label,
            self.rule_label,
            self.estimate_label,
            self.expired_label,
            self.accept_button,
            self.decline_button,
        ):
            _reserve(offer_widget)
        self._unread = 0
        self._chat_held = False
        for button in self._break_buttons:
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._break = BreakState()
        self._ready = False
        self._invitation: InvitationState = "idle"
        self._not_ready: NotReadyReason | None = None
        self._last_activity: tuple[datetime | None, datetime | None] = (None, None)
        self._agent_active = False
        self._remote_state = "signed_out"
        self._me: Me | None = None
        self._store_encrypted = True
        self._remote_unavailable = False

        buttons = QHBoxLayout()
        buttons.addWidget(self.accept_button)
        buttons.addWidget(self.decline_button)
        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        header.addWidget(self.mark_label)
        header.addWidget(self.wordmark_label)
        header.addStretch()
        layout.addLayout(header)
        card = QFrame()
        card.setObjectName("card")
        card_layout = QVBoxLayout(card)
        card_layout.addWidget(self.status_label)
        card_layout.addWidget(self.activity_label)
        card_layout.addWidget(self.rule_label)
        card_layout.addWidget(self.estimate_label)
        card_layout.addWidget(self.expired_label)
        card_layout.addLayout(buttons)
        card_layout.addWidget(self.break_label)
        card_layout.addWidget(self.shared_label)
        card_layout.addWidget(self.extension_label)
        card_layout.addWidget(self.break_error_label)
        break_buttons = QHBoxLayout()
        for button in self._break_buttons:
            break_buttons.addWidget(button)
        card_layout.addLayout(break_buttons)
        layout.addWidget(card)
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
        self,
        state: InvitationState,
        estimate: RemainingTimeBucket | None = None,
        *,
        expired: bool = False,
    ) -> None:
        """`expired`: the offer ended by timing out; the notice lasts until the
        next state (the controller knows why an offer ended, the window not)."""
        self._invitation = state
        self.expired_label.setVisible(expired and state == "idle")
        self._render_status()
        text = (
            _ESTIMATE_LABELS.get(estimate)
            if estimate and state == "suggested"
            else None
        )
        self.estimate_label.setText(text or "")
        self.estimate_label.setVisible(text is not None)
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

    def set_not_ready_reason(self, reason: NotReadyReason | None) -> None:
        """Why no break can be offered yet; None once the app is ready."""
        self._not_ready = reason
        self._render_status()

    def set_last_activity(
        self, last: datetime | None, now: datetime, *, active: bool = False
    ) -> None:
        """Latest agent event time (None: none yet), aged against `now`.
        `active`: the agent is working right now."""
        self._last_activity = (last, now)
        self._agent_active = active
        self._render_status()

    def _render_status(self) -> None:
        """The status row: the invitation line, or in the idle view the
        not-ready reason, plus the last-activity line. A pending offer or a
        break in progress always wins over both."""
        state = self._invitation
        idle_view = state == "idle" and not self._break_active
        reason = self._not_ready if idle_view else None
        if reason is not None:
            self.status_label.setText(_NOT_READY_LABELS[reason])
            theme.set_state(self.status_label, "off")
        else:
            self.status_label.setText(_LABELS[state])
            theme.set_state(self.status_label, _INVITATION_PILL[state])
        # The idle line says nothing useful while a break runs (U09); the
        # break label below shows the phase.
        self.status_label.setVisible(idle_view or state != "idle")
        self.activity_label.setText(self._activity_text())
        self.activity_label.setVisible(idle_view)
        self.rule_label.setVisible(idle_view)

    def _activity_text(self) -> str:
        if self._agent_active:
            return AGENT_BUSY
        last, now = self._last_activity
        if last is None or now is None:
            return NO_ACTIVITY
        seconds = max(0, int((now - last).total_seconds()))
        if seconds < 60:
            age = f"{seconds} s"
        elif seconds < 3600:
            age = f"{seconds // 60} min"
        else:
            age = f"{seconds // 3600} h"
        return f"Last agent activity: {age} ago"

    def set_ready(self, ready: bool) -> None:
        """Connected with nickname and consent saved: Find may be offered."""
        self._ready = ready
        self._render_break()

    def set_break(self, state: BreakState) -> None:
        self._break = state
        if self._break_active:
            self._clear_break_error()
            self.expired_label.setVisible(False)
        self._render_break()
        self._render_status()
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

    def set_extension_declined(self, declined: bool) -> None:
        """Cue for a declined extension request while the chat window is hidden."""
        self.extension_label.setVisible(declined)

    def set_chat_held(self, held: bool) -> None:
        """The worker holds an ended chat nobody has closed yet: its window
        (and a late report) stays reachable through Open chat."""
        self._chat_held = held
        self._render_break()

    def set_unread(self, count: int) -> None:
        """Peer messages since the chat window was last active."""
        self._unread = max(0, count)
        self._render_break()

    def _wait_text(self) -> str:
        """How long a search waits: the server's real expiry once queued, the
        configured wait while the join is still going out."""
        entry = self._break.entry
        if self._break.phase == "searching" and entry is not None:
            left = (entry.expires_at - datetime.now(UTC)).total_seconds()
            seconds = max(0, round(left))
        else:
            seconds = round(queue_wait_s())
        return f"up to {_span(seconds)}"

    def _render_break(self) -> None:
        phase = self._break.phase
        text = ""
        shared = ""
        if phase in ("joining", "searching"):
            text = f"{SEARCHING} ({self._wait_text()})"
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
        theme.set_state(self.break_label, _BREAK_PILL[phase])
        self.break_label.setVisible(bool(text))
        self.shared_label.setText(shared)
        self.shared_label.setVisible(bool(shared))
        find = self._ready and phase in ("none", "nobody_available", "ended")
        self.find_button.setVisible(find)
        self.cancel_button.setVisible(phase in ("joining", "searching"))
        self.open_chat_button.setText(
            f"{OPEN_CHAT} ({self._unread})" if self._unread else OPEN_CHAT
        )
        # A reported or blocked chat is not offered again (U3).
        held = self._chat_held and not self._break.blocked
        self.open_chat_button.setVisible(phase == "matched" or held)
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

    def _fit_to_content(self) -> None:
        """L1: never smaller than the content, including wrapped text, whose
        height depends on the width. Qt's own minimum ignores that."""
        layout = self.layout()
        if layout is None:
            return
        width = max(self.width(), layout.minimumSize().width())
        wanted = QSize(layout.minimumSize().width(), layout.totalHeightForWidth(width))
        if wanted != self.minimumSize():
            self.setMinimumSize(wanted)

    def event(self, event: QEvent) -> bool:
        handled = super().event(event)
        if event.type() == QEvent.Type.LayoutRequest:
            self._fit_to_content()
        return handled

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._fit_to_content()

    def may_close(self) -> bool:
        """Closing the last visible window quits (Qt's default); during a search
        or chat, ask first (D2)."""
        others = any(
            w is not self
            and w.isVisible()
            and w.windowType() == Qt.WindowType.Window  # not dialogs or popups
            for w in QApplication.topLevelWidgets()
        )
        return not self._break_active or others or confirm_leave(self)

    def closeEvent(self, event: QCloseEvent) -> None:
        # Only the user's own close (title bar, Alt+F4) asks. A close made by
        # code, such as a test teardown or a shutdown, must never block.
        if event.spontaneous() and not self.may_close():
            event.ignore()
            return
        super().closeEvent(event)

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
            theme.set_state(self.account_label, "offline")
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
        theme.set_state(self.account_label, _ACCOUNT_PILL[state])
        signed_in = state in ("connecting", "connected", "offline")
        incomplete = (
            signed_in and me is not None and (me.nickname is None or me.consent is None)
        )
        self.account_note.setVisible(signed_in and not self._store_encrypted)
        self.sign_in_button.setVisible(state == "signed_out")
        self.finish_setup_button.setVisible(incomplete)
        self.settings_button.setVisible(signed_in and me is not None and not incomplete)
        self.sign_out_button.setVisible(signed_in or state == "signing_in")

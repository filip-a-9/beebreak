"""Compact always-on-top companion window. Owned by M1-04.

Shows one fixed string per state and never any event-derived text (T11).
The account row (M3-05b) shows fixed strings too; the nickname is the only
server text, re-validated against the Nickname pattern and shown as plain text.
"""

from pydantic import TypeAdapter, ValidationError
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from breakroom_shared.api import Me, Nickname
from breakroom_shared.ipc import InvitationState
from breakroom_shared.presence import RemainingTimeBucket

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

        self.sign_in_button.clicked.connect(self.sign_in_requested)
        self.finish_setup_button.clicked.connect(self.settings_requested)
        self.settings_button.clicked.connect(self.settings_requested)
        self.sign_out_button.clicked.connect(self.sign_out_requested)

        self.set_state("idle")
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
        self.accept_button.setVisible(state == "suggested")
        self.decline_button.setVisible(state == "suggested")
        self.enabled_checkbox.setChecked(state != "disabled")
        if state == "suggested":
            self.show()
            self.raise_()

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

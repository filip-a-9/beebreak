"""Compact always-on-top companion window. Owned by M1-04.

Shows one fixed string per state and never any event-derived text (T11).
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

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


class CompanionWindow(QWidget):
    accepted = Signal()
    declined = Signal()
    enabled_changed = Signal(bool)

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Breakroom")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.resize(320, 140)

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

        buttons = QHBoxLayout()
        buttons.addWidget(self.accept_button)
        buttons.addWidget(self.decline_button)
        layout = QVBoxLayout(self)
        layout.addWidget(self.status_label)
        layout.addWidget(self.estimate_label)
        layout.addLayout(buttons)
        layout.addStretch()
        layout.addWidget(self.enabled_checkbox)

        self.accept_button.clicked.connect(self.accepted)
        self.decline_button.clicked.connect(self.declined)
        # Only user clicks emit; set_state uses setChecked, which doesn't.
        self.enabled_checkbox.clicked.connect(self.enabled_changed)

        self.set_state("idle")

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

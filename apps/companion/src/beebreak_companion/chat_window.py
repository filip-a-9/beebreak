"""Compact chat window, one per live or just-ended chat. Owned by M5-06.

Peer text is plain text everywhere (rule 17): every body and nickname is shown
with `Qt.TextFormat.PlainText`, never linkified. The window only renders the
`ChatState` it is given and emits the user's intents; `app.py` wires those to
`RemoteWorker`. It never logs or notifies anything it renders (rule 16).
"""

import math
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from PySide6.QtCore import QEvent, QPoint, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QCloseEvent,
    QContextMenuEvent,
    QHideEvent,
    QKeyEvent,
    QKeySequence,
    QShowEvent,
)
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from beebreak_companion import config, theme
from beebreak_companion.chat_state import ChatEnd, ChatState, PendingMessage
from beebreak_companion.report_dialog import ReportDialog, display_text
from beebreak_companion.window import (
    BLOCK_CONFIRM,
    BLOCK_TITLE,
    BREAK_ENDED,
    ERROR_SECONDS,
    IN_COMMON,
    CompanionWindow,
    confirm_leave,
    safe_nickname,
)
from beebreak_shared.api import ChatStatus
from beebreak_shared.chat import MAX_MESSAGE_CHARS, normalize_text

COUNTER_FROM = 900
TICK_INTERVAL_MS = 1000

SEND = "Send"
END = "End"
BLOCK = "Block"
REPORT = "Report"
CLOSE = "Close"
EXTEND = "Extend"
CANCEL = "Cancel"
TIME_TIP = "A chat can be extended near its end if you both agree."
YES = "Yes"
NO = "No"
SENDING = "sending\N{HORIZONTAL ELLIPSIS}"
NOT_SENT = "not sent, retry"
RECONNECTING = "Reconnecting\N{HORIZONTAL ELLIPSIS}"
FOOTER = "Not end-to-end encrypted. Messages are deleted 24 h after the chat ends."
END_TITLE = "End this chat?"
END_CONFIRM = "The chat ends for both of you."
ERROR_TEXT: dict[str, str] = {
    "invalid_request": "That didn't work.",
    "rate_limited": "Too fast. Wait a moment.",
    "report_window_closed": "The report window has closed.",
    "already_reported": "You already reported this chat.",
    "chat_ended": "The chat has ended.",
    "no_extensions_left": "No extensions left.",
    "no_extension_request": "There is no request to answer.",
    "not_found": "That chat is no longer available.",
    "network": "Network problem. Try again.",
}
REPORTED = "Reported and blocked."
NOT_SENT_ENDED = "not sent"
ENDED_LINES: dict[ChatEnd, str] = {
    # Same opening words as the main window's card (U5).
    "ended": BREAK_ENDED,
    "expired": f"{BREAK_ENDED} Time is up.",
    "disconnected": f"{BREAK_ENDED} The connection was lost.",
}


def _count(seconds: float) -> tuple[int, str]:
    """Whole minutes (at least one) and the unit word."""
    minutes = max(1, round(seconds / 60))
    return minutes, "minute" if minutes == 1 else "minutes"


def _extension_note(minutes: int, left: int) -> str:
    """The system line after an accepted extension (X5)."""
    if left <= 0:
        remaining = "No more extensions."
    else:
        remaining = (
            f"{left} extension left." if left == 1 else f"{left} extensions left."
        )
    return f"Extended by {minutes} min. {remaining}"


def _minutes(seconds: float) -> str:
    minutes, unit = _count(seconds)
    return f"{minutes} {unit}"


class _InputEdit(QPlainTextEdit):
    """Enter submits, Shift+Enter is a newline."""

    submit = Signal()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not (
            event.modifiers() & Qt.KeyboardModifier.ShiftModifier
        ):
            self.submit.emit()
            return
        super().keyPressEvent(event)


_WORD_BREAK_EVERY = 20
_ZERO_WIDTH_SPACE = "\u200b"


def _breakable(text: str) -> str:
    """Display copy of `text` with break points inside very long words.

    A QLabel only wraps at spaces, so one unbroken word would widen the
    transcript and scroll it sideways. The stored body is not changed.
    """
    return re.sub(
        rf"\S{{{_WORD_BREAK_EVERY}}}(?=\S)",
        lambda m: m.group(0) + _ZERO_WIDTH_SPACE,
        text,
    )


class _BodyLabel(QLabel):
    """A message body whose every copy path yields the text without the
    break points `_breakable` added (a pasted URL must be intact)."""

    def copy_selection(self) -> None:
        text = self.selectedText().replace(_ZERO_WIDTH_SPACE, "")
        if text:
            QApplication.clipboard().setText(text)

    def select_all(self) -> None:
        self.setSelection(0, len(self.text()))

    def keyPressEvent(self, ev: QKeyEvent) -> None:
        if ev.matches(QKeySequence.StandardKey.Copy):
            self.copy_selection()
            ev.accept()
            return
        super().keyPressEvent(ev)

    def build_context_menu(self) -> QMenu:
        menu = QMenu(self)
        copy = menu.addAction("Copy")
        copy.setObjectName("copy")
        copy.setEnabled(self.hasSelectedText())
        copy.triggered.connect(self.copy_selection)
        select_all = menu.addAction("Select All")
        select_all.setObjectName("select_all")
        select_all.triggered.connect(self.select_all)
        return menu

    def contextMenuEvent(self, ev: QContextMenuEvent) -> None:
        self.build_context_menu().exec(ev.globalPos())


PLACEMENT_GAP_PX = 12  # between the main window and a chat window beside it


def chat_position(size: QSize, main: QRect | None, available: QRect) -> QPoint:
    """Top-left for a chat window of `size`, wholly inside `available`.

    Beside the main window when there is room (right first, then left), level
    with its top; else it overlaps. Never above the top of `available`.
    """
    width, height = size.width(), size.height()
    if main is None:
        x = available.left() + (available.width() - width) // 2
        y = available.top() + (available.height() - height) // 2
    else:
        x = main.right() + 1 + PLACEMENT_GAP_PX
        if x + width - 1 > available.right():
            x = main.left() - PLACEMENT_GAP_PX - width
        y = main.top()
    x = max(available.left(), min(x, available.right() - width + 1))
    y = max(available.top(), min(y, available.bottom() - height + 1))
    return QPoint(x, y)


class ChatWindow(QWidget):
    send_requested = Signal(str)  # raw text; the worker normalizes
    retry_requested = Signal(object)  # UUID of a failed pending message
    extend_requested = Signal()
    extension_answered = Signal(bool)  # accept / decline (or cancel own request)
    end_requested = Signal()
    block_requested = Signal()
    report_requested = Signal(str, object, object)  # reason, note | None, seqs
    close_requested = Signal()  # an ended chat's window was closed
    # The window came into view (True: shown or activated) or lost focus (False).
    activity_changed = Signal(bool)

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        busy: Callable[[], bool] = lambda: False,
    ) -> None:
        super().__init__(parent)
        self._busy = busy  # a search or match is running (D2)
        self._clock = clock
        self._now = now
        self._state = ChatState()
        self._connected = True
        self._shared = ""
        # Remaining seconds at `_anchor` (a monotonic reading).
        self._remaining0 = 0.0
        self._anchor = 0.0
        self._time_key: tuple[object, float] | None = None
        self._transcript_key: tuple[object, ...] | None = None
        self._stick = True
        self._report_dialog: ReportDialog | None = None
        self._retry_buttons: list[QPushButton] = []
        self._body_labels: list[_BodyLabel] = []
        # The peer's "no" shows as a banner line until this monotonic reading;
        # None until the window is first on screen with the flag set.
        self._declined_until: float | None = None
        # Extension click guards (X1-X3). In flight: a button was clicked and
        # no newer chat status has arrived (until this reading, then it gives
        # up). Swap guard: a Yes/No/Cancel just replaced other buttons.
        self._inflight_until: float | None = None
        self._swap_until = 0.0
        self._shown: tuple[QPushButton, ...] = ()
        # Accepted extensions as (messages before it, line), per match (X5).
        self._notes: list[tuple[int, str]] = []

        self.setWindowTitle("Beebreak chat")
        self.resize(360, 480)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)

        self.avatar_label = QLabel()
        self.avatar_label.setObjectName("avatar")
        self.avatar_label.setTextFormat(Qt.TextFormat.PlainText)
        self.avatar_label.setFixedSize(36, 36)
        self.avatar_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.peer_label = QLabel()
        self.peer_label.setObjectName("peerName")
        self.shared_label = QLabel()
        self.shared_label.setObjectName("muted")
        self.time_label = QLabel()
        self.time_label.setObjectName("timePill")
        self.time_label.setToolTip(TIME_TIP)
        self.time_hint_label = QLabel()
        self.time_hint_label.setObjectName("muted")
        self.status_label = QLabel()
        self.banner_label = QLabel()
        self.ended_label = QLabel()
        self.error_label = QLabel()
        self.reported_label = QLabel(REPORTED)
        theme.set_state(self.reported_label, "off")
        self.counter_label = QLabel()
        self.footer_label = QLabel(FOOTER)
        self.footer_label.setObjectName("footer")
        theme.set_state(self.status_label, "offline")
        theme.set_state(self.ended_label, "off")
        theme.set_state(self.error_label, "offline")
        for label in (
            self.peer_label,
            self.shared_label,
            self.time_label,
            self.time_hint_label,
            self.status_label,
            self.banner_label,
            self.ended_label,
            self.reported_label,
            self.error_label,
            self.counter_label,
            self.footer_label,
        ):
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setWordWrap(True)
            label.setOpenExternalLinks(False)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self.peer_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )

        self.transcript = QWidget()
        self.transcript.setObjectName("transcript")
        # Rows on top via a stretch, not an alignment flag: an aligned layout
        # is laid out at its sizeHint height, which ignores height-for-width
        # and squeezes wrapped rows (X3).
        transcript_layout = QVBoxLayout(self.transcript)
        self._rows = QVBoxLayout()
        transcript_layout.addLayout(self._rows)
        transcript_layout.addStretch()
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setWidget(self.transcript)
        self.scroll_area.verticalScrollBar().rangeChanged.connect(self._on_range)

        self.extend_button = QPushButton(EXTEND)
        self.cancel_extension_button = QPushButton(CANCEL)
        self.yes_button = QPushButton(YES)
        self.no_button = QPushButton(NO)
        # No sits left of everything else: the Yes/No pair replaces Extend at
        # the right edge, so a click aimed at Extend lands on Yes, not No (U15).
        self._banner_buttons = (
            self.no_button,
            self.extend_button,
            self.cancel_extension_button,
            self.yes_button,
        )
        for primary in (self.yes_button,):
            primary.setObjectName("primary")
        self.banner = QFrame()
        self.banner.setObjectName("banner")
        banner_layout = QHBoxLayout(self.banner)
        banner_layout.setContentsMargins(12, 6, 12, 6)
        banner_layout.addWidget(self.banner_label, 1)
        for button in self._banner_buttons:
            banner_layout.addWidget(button)

        self.input = _InputEdit()
        self.input.setFixedHeight(64)
        self.input.setTabChangesFocus(True)
        self.send_button = QPushButton(SEND)
        self.end_button = QPushButton(END)
        self.block_button = QPushButton(BLOCK)
        self.report_button = QPushButton(REPORT)
        self.close_button = QPushButton(CLOSE)
        self.send_button.setObjectName("primary")
        self.report_button.setObjectName("danger")
        self._buttons = (
            self.send_button,
            self.end_button,
            self.block_button,
            self.report_button,
            self.close_button,
            *self._banner_buttons,
        )
        # A keypress meant for the terminal must never land on a button.
        for button in self._buttons:
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        input_row = QHBoxLayout()
        input_row.addWidget(self.input, 1)
        input_row.addWidget(self.send_button)
        actions = QHBoxLayout()
        actions.addWidget(self.end_button)
        actions.addWidget(self.block_button)
        actions.addWidget(self.report_button)
        actions.addStretch()
        actions.addWidget(self.close_button)
        layout = QVBoxLayout(self)
        names = QVBoxLayout()
        names.setSpacing(0)
        names.addWidget(self.peer_label)
        names.addWidget(self.shared_label)
        header = QHBoxLayout()
        header.addWidget(self.avatar_label)
        header.addLayout(names, 1)
        time_column = QVBoxLayout()
        time_column.setSpacing(0)
        time_column.addWidget(self.time_label, 0, Qt.AlignmentFlag.AlignRight)
        time_column.addWidget(self.time_hint_label, 0, Qt.AlignmentFlag.AlignRight)
        header.addLayout(time_column)
        layout.addLayout(header)
        layout.addWidget(self.status_label)
        layout.addWidget(self.scroll_area, 1)
        layout.addWidget(self.banner)
        layout.addWidget(self.reported_label)
        layout.addWidget(self.ended_label)
        layout.addWidget(self.error_label)
        layout.addLayout(input_row)
        layout.addWidget(self.counter_label)
        layout.addLayout(actions)
        layout.addWidget(self.footer_label)

        self.input.textChanged.connect(self._on_text_changed)
        self.input.submit.connect(self._submit)
        self.send_button.clicked.connect(self._submit)
        self.extend_button.clicked.connect(self._on_extend_clicked)
        self.cancel_extension_button.clicked.connect(lambda: self._answer(False))
        self.yes_button.clicked.connect(lambda: self._answer(True))
        self.no_button.clicked.connect(lambda: self._answer(False))
        self.end_button.clicked.connect(self._on_end_clicked)
        self.block_button.clicked.connect(self._on_block_clicked)
        self.report_button.clicked.connect(self._on_report_clicked)
        self.close_button.clicked.connect(self.close_requested)

        self._error_timer = QTimer(self)
        self._error_timer.setSingleShot(True)
        self._error_timer.setInterval(ERROR_SECONDS * 1000)
        self._error_timer.timeout.connect(self._clear_error)
        self._guard_timer = QTimer(self)
        self._guard_timer.setSingleShot(True)
        self._guard_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._guard_timer.timeout.connect(self._render_banner)
        self._timer = QTimer(self)
        self._timer.setInterval(TICK_INTERVAL_MS)
        self._timer.timeout.connect(self.tick)

        self._clear_error()
        self._render()

    # --- public API ---

    def set_chat(self, state: ChatState) -> None:
        """Render `state` (a fresh `ChatState()` means no chat)."""
        previous = self._state
        self._state = state
        if (
            not state.extension_declined
            or not previous.extension_declined
            or state.match_id != previous.match_id
        ):
            self._declined_until = None
        if state.match_id != previous.match_id:
            self._notes = []
            self._inflight_until = None
            self.input.clear()
            self._clear_error()
            # A report dialog belongs to the chat it was opened for.
            if self._report_dialog is not None:
                self._report_dialog.submitted.disconnect(self.report_requested)
                self._report_dialog.reject()
                self._report_dialog = None
        if state.match_id is None:
            self._shared = ""
        self._note_extension(previous, state)
        if state.status is not previous.status:
            self._inflight_until = None  # the awaited status has arrived
        self._anchor_time(state)
        self._render()
        if state.match_id is None and self.isVisible():
            self.hide()

    def set_connected(self, connected: bool) -> None:
        """This companion's own connection (the "Reconnecting…" line)."""
        self._connected = connected
        self._render()

    def show_error(self, code: str) -> None:
        """A failed chat action, as a fixed line. Codes that no chat action can
        produce (sign-in and the like) and a missing chat show nothing; the
        server's own text is never shown."""
        text = ERROR_TEXT.get(code)
        if text is None or self._state.match_id is None:
            return
        self.error_label.setText(text)
        self.error_label.setVisible(True)
        self._error_timer.start()

    def _clear_error(self) -> None:
        self._error_timer.stop()
        self.error_label.setText("")
        self.error_label.setVisible(False)

    def set_shared(self, labels: list[str]) -> None:
        """What both people shared (the M4 labels), already display strings."""
        self._shared = f"{IN_COMMON} {', '.join(labels)}" if labels else ""
        self._render()

    def tick(self) -> None:
        """Refresh the countdown and the grace banner (the 1 s timer)."""
        self._render_time()
        self._render_banner()

    def open_for_user(self) -> None:
        """The user asked for the window: show, raise and focus the input."""
        if not self.isVisible():
            self._place()
        self.show()
        self.raise_()
        self.activateWindow()
        if self._live:
            self.input.setFocus()

    def _place(self) -> None:
        """Move beside the visible main window, on its screen (a no-op on
        platforms where the compositor chooses window positions)."""
        main = next(
            (
                w
                for w in QApplication.topLevelWidgets()
                if isinstance(w, CompanionWindow) and w.isVisible()
            ),
            None,
        )
        screen = (main or self).screen()
        if screen is None:
            return
        frame = self.frameGeometry()
        size = QSize(frame.width(), frame.height())
        origin = chat_position(
            size,
            main.frameGeometry() if main is not None else None,
            screen.availableGeometry(),
        )
        self.move(origin)

    def _note_extension(self, previous: ChatState, state: ChatState) -> None:
        """An accepted extension moves the deadline later and uses one up."""
        old, new = previous.status, state.status
        if (
            old is None
            or new is None
            or state.match_id != previous.match_id
            or new.ends_at <= old.ends_at
            or new.extensions_left >= old.extensions_left
        ):
            return
        added = round((new.ends_at - old.ends_at).total_seconds() / 60)
        line = _extension_note(max(1, added), new.extensions_left)
        self._notes.append((len(state.messages), line))

    # --- extension buttons ---

    def _on_extend_clicked(self) -> None:
        self._begin_inflight()
        self.extend_requested.emit()

    def _answer(self, accepted: bool) -> None:
        self._begin_inflight()
        self.extension_answered.emit(accepted)

    def _begin_inflight(self) -> None:
        self._inflight_until = self._clock() + config.extension_inflight_s()
        self._render_banner()

    # --- time ---

    def _anchor_time(self, state: ChatState) -> None:
        status = state.status
        if status is None:
            self._time_key = None
            return
        key = (status.ends_at, state.server_offset_s)
        if key != self._time_key:
            server_now = self._now() + timedelta(seconds=state.server_offset_s)
            self._remaining0 = (status.ends_at - server_now).total_seconds()
            self._anchor = self._clock()
            self._time_key = key

    def remaining(self) -> float:
        """Seconds to the deadline by the server's clock; never from wall time
        after the chat state arrived."""
        return self._remaining0 - (self._clock() - self._anchor)

    # --- rendering ---

    @property
    def _live(self) -> bool:
        return self._state.match_id is not None and self._state.ended is None

    @property
    def _nick(self) -> str:
        return safe_nickname(self._state.peer_nickname)

    def _render(self) -> None:
        state = self._state
        has_chat = state.match_id is not None
        live = self._live
        self.peer_label.setText(self._nick if has_chat else "")
        self.avatar_label.setText(self._nick[:1].upper() if has_chat else "")
        self.avatar_label.setVisible(has_chat)
        self.shared_label.setText(self._shared)
        self.shared_label.setVisible(bool(self._shared) and has_chat)
        self._render_time()
        self._render_status_line()
        self._render_banner()
        self._render_transcript()
        ended = state.ended
        self.ended_label.setText(ENDED_LINES[ended] if ended else "")
        self.ended_label.setVisible(ended is not None)
        self.reported_label.setVisible(state.reported)
        self.end_button.setVisible(live)
        self.block_button.setVisible(has_chat and not state.reported)
        self.report_button.setVisible(has_chat)
        self.report_button.setEnabled(has_chat and not state.reported)
        self.close_button.setVisible(ended is not None)
        self.input.setEnabled(live)
        self._on_text_changed()
        if live and self.isVisible():
            self._timer.start()
        elif not live:
            self._timer.stop()

    def _render_time(self) -> None:
        status = self._state.status
        show = self._live and status is not None
        self.time_label.setVisible(show)
        self._render_time_hint()
        if not show:
            self.time_label.setText("")
            return
        self.time_label.setText(f"{self._left()} left")

    def _left(self) -> str:
        """The time to the deadline as the chip words it; the banner uses the
        same text so the two never disagree (X4)."""
        left = self.remaining()
        return "< 1 min" if left < 60 else f"{math.ceil(left / 60)} min"

    def _in_grace(self, status: ChatStatus) -> bool:
        return self.remaining() <= (status.ends_at - status.grace_at).total_seconds()

    def _render_time_hint(self) -> None:
        """Under the time chip: when extending becomes possible (U14)."""
        status = self._state.status
        show = (
            self._live
            and status is not None
            and status.extensions_left > 0
            and status.extension == "none"
            and not self._in_grace(status)
        )
        if show and status is not None:
            grace = _minutes((status.ends_at - status.grace_at).total_seconds())
            self.time_hint_label.setText(f"You can extend in the last {grace}")
        else:
            self.time_hint_label.setText("")
        self.time_hint_label.setVisible(show)

    def _render_status_line(self) -> None:
        text = ""
        status = self._state.status
        if self._live and status is not None:
            if not self._connected:
                text = RECONNECTING
            elif not status.peer_online:
                text = f"{self._nick} is reconnecting\N{HORIZONTAL ELLIPSIS}"
        self.status_label.setText(text)
        self.status_label.setVisible(bool(text))

    def _banner(self, status: ChatStatus) -> tuple[str, tuple[QPushButton, ...]]:
        nick = self._nick
        extension = _count(status.extension_s)
        if status.extension == "requested_by_peer":
            text = f"{nick} wants {extension[0]} more {extension[1]}."
            return text, (self.yes_button, self.no_button)
        if status.extension == "requested_by_me":
            text = f"Waiting for {nick}\N{HORIZONTAL ELLIPSIS}"
            return text, (self.cancel_extension_button,)
        if self._state.extension_declined:
            if self._declined_until is None and self.isVisible():
                self._declined_until = self._clock() + ERROR_SECONDS
            if self._declined_until is None or self._clock() < self._declined_until:
                return f"{nick} said no.", ()
        if not self._in_grace(status):
            return "", ()
        left = self._left()
        if status.extensions_left > 0:
            text = f"{left} left. Extend by {_minutes(status.extension_s)}?"
            return text, (self.extend_button,)
        return f"{left} left.", ()

    def _render_banner(self) -> None:
        status = self._state.status
        text = ""
        shown: tuple[QPushButton, ...] = ()
        if self._live and status is not None:
            text, shown = self._banner(status)
        self.banner_label.setText(text)
        now = self._clock()
        if shown and shown != self._shown:
            delay = config.extension_swap_guard_s()
            self._swap_until = now + delay
            self._guard_timer.start(int(delay * 1000) + 20)
        self._shown = shown
        if self._inflight_until is not None and now >= self._inflight_until:
            self._inflight_until = None
        usable = self._inflight_until is None and now >= self._swap_until
        for button in self._banner_buttons:
            button.setVisible(button in shown)
            button.setEnabled(usable)
        self.banner.setVisible(bool(text))

    def _on_range(self, _low: int, high: int) -> None:
        if self._stick:
            self.scroll_area.verticalScrollBar().setValue(high)

    def _render_transcript(self) -> None:
        state = self._state
        key: tuple[object, ...] = (
            state.match_id,
            state.messages,
            state.pending,
            state.peer_nickname,
            state.ended,
            tuple(self._notes),
        )
        if key == self._transcript_key:
            return
        self._transcript_key = key
        bar = self.scroll_area.verticalScrollBar()
        self._stick = bar.value() >= bar.maximum() - 4
        while self._rows.count():
            item = self._rows.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                # Hidden explicitly: a queued show from the layout would
                # otherwise pop the orphan up as its own window.
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
        self._retry_buttons = []
        self._body_labels = []
        notes = list(self._notes)
        for index, message in enumerate(state.messages):
            while notes and notes[0][0] <= index:
                self._add_note(notes.pop(0)[1])
            self._add_row(message.mine, display_text(message.body), None)
        for _, line in notes:
            self._add_note(line)
        for pending in state.pending:
            self._add_row(True, display_text(pending.body), pending)
        # The range change may have happened before the layout settled.
        QTimer.singleShot(0, self, self._scroll_if_sticking)

    def _scroll_if_sticking(self) -> None:
        if self._stick:
            bar = self.scroll_area.verticalScrollBar()
            bar.setValue(bar.maximum())

    def _body_label(self, text: str) -> _BodyLabel:
        label = _BodyLabel(_breakable(text))
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        label.setOpenExternalLinks(False)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._body_labels.append(label)
        return label

    def _add_note(self, text: str) -> None:
        note = QLabel(text)
        note.setObjectName("muted")
        note.setTextFormat(Qt.TextFormat.PlainText)
        note.setWordWrap(True)
        note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._rows.addWidget(note)

    def _add_row(self, mine: bool, body: str, pending: PendingMessage | None) -> None:
        row = QWidget()
        column = QVBoxLayout(row)
        column.setSpacing(2)
        label = self._body_label(body)
        theme.set_role(
            label, "pending" if pending is not None else "mine" if mine else "peer"
        )
        # The bubble leaves room on the side it does not belong to.
        column.setContentsMargins(*((40, 0, 0, 0) if mine else (0, 0, 40, 0)))
        align = Qt.AlignmentFlag.AlignRight if mine else Qt.AlignmentFlag.AlignLeft
        label.setAlignment(align)
        # No alignment flag here: with one, the layout sizes the label at its
        # sizeHint width and computes the wrapped height for the wrong width,
        # clipping the last lines (X3). The text itself is aligned above.
        column.addWidget(label)
        if pending is not None:
            if pending.status == "failed" and self._state.ended is not None:
                mark = QLabel(NOT_SENT_ENDED)  # nothing can be retried any more
                mark.setObjectName("captionFailed")
                mark.setTextFormat(Qt.TextFormat.PlainText)
                column.addWidget(mark, 0, align)
            elif pending.status == "failed":
                retry = QPushButton(NOT_SENT)
                retry.setFlat(True)
                retry.setObjectName("retry")
                retry.setFocusPolicy(Qt.FocusPolicy.NoFocus)
                retry.clicked.connect(
                    lambda _checked=False, cid=pending.client_message_id: (
                        self.retry_requested.emit(cid)
                    )
                )
                self._retry_buttons.append(retry)
                column.addWidget(retry, 0, align)
            else:
                mark = QLabel(SENDING)
                mark.setObjectName("caption")
                mark.setTextFormat(Qt.TextFormat.PlainText)
                column.addWidget(mark, 0, align)
        self._rows.addWidget(row)

    # --- input ---

    def _text_ok(self) -> bool:
        return normalize_text(self.input.toPlainText()) is not None

    def _on_text_changed(self) -> None:
        length = len(self.input.toPlainText())
        self.counter_label.setText(f"{length} / {MAX_MESSAGE_CHARS}")
        self.counter_label.setVisible(length >= COUNTER_FROM)
        self.send_button.setEnabled(self._live and self._text_ok())

    def _submit(self) -> None:
        if not (self._live and self._text_ok()):
            return
        text = self.input.toPlainText()
        self.input.clear()
        self.send_requested.emit(text)

    # --- buttons ---

    def _confirm(self, title: str, text: str) -> bool:
        answer = QMessageBox.question(
            self,
            title,
            text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _on_end_clicked(self) -> None:
        if self._confirm(END_TITLE, END_CONFIRM):
            self.end_requested.emit()

    def _on_block_clicked(self) -> None:
        if self._confirm(BLOCK_TITLE, BLOCK_CONFIRM):
            self.block_requested.emit()

    def _on_report_clicked(self) -> None:
        if self._state.match_id is None or self._state.reported:
            return
        dialog = ReportDialog(self._state.peer_nickname, self._state.messages, self)
        dialog.submitted.connect(self.report_requested)
        dialog.finished.connect(lambda _result: setattr(self, "_report_dialog", None))
        self._report_dialog = dialog
        dialog.open()

    # --- window events ---

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        if self._live:
            self._timer.start()
        self._render_banner()
        # Shown counts as seen: WSLg may never activate a window shown
        # without activation (U13).
        self.activity_changed.emit(True)

    def hideEvent(self, event: QHideEvent) -> None:
        super().hideEvent(event)
        self._timer.stop()
        # WSLg keeps drawing a hidden Wayland toplevel, so the window looked
        # unclosable. Dropping the native window removes it; show() makes a new
        # one. Deferred: not while Qt is still inside the hide.
        QTimer.singleShot(0, self._release_native_window)

    def _release_native_window(self) -> None:
        handle = self.windowHandle()
        if handle is not None and not self.isVisible():
            handle.destroy()

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.ActivationChange:
            self.activity_changed.emit(self.isActiveWindow())

    def closeEvent(self, event: QCloseEvent) -> None:
        """Closing a live chat hides the window while the main window is visible;
        an ended one is forgotten."""
        ended = self._state.match_id is not None and self._state.ended is not None
        last = not any(
            isinstance(w, CompanionWindow) and w.isVisible()
            for w in QApplication.topLevelWidgets()
        )
        if last:
            # Nothing else is on screen: hiding would leave the app running
            # with no window. Closing lets it quit (lastWindowClosed), after
            # asking if a chat or search is active. A close made by code never
            # asks. This is the only place that asks, so never twice.
            if event.spontaneous() and not self.may_quit():
                event.ignore()
                return
        elif not ended:
            event.ignore()
            self.hide()
            return
        event.accept()
        if ended:
            self.close_requested.emit()

    def may_quit(self) -> bool:
        """Closing this last window quits; during a live chat or a search,
        ask first (D2)."""
        live = self._state.match_id is not None and self._state.ended is None
        return not (live or self._busy()) or confirm_leave(self)

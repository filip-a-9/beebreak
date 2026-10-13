"""Sign-in and privacy onboarding dialog. Owned by M3-05b.

Opened only by a user click (THREAT_MODEL T10); nothing an agent does can open
it. All text is fixed. The user's own input (email, codes, nickname) is never
echoed back and never logged. Server replies reach the dialog only as the
worker's signals and error codes, each mapped to a fixed string below.

Pages: 0 email + invite code, 1 code, 2 nickname (only if none yet),
3 privacy choices, 4 done. Cancel after the code was verified leaves the user
signed in with setup incomplete; the account row offers "Finish setup".
"""

import contextlib
import re
from collections.abc import Callable
from typing import get_args

from pydantic import TypeAdapter, ValidationError
from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from breakroom_companion.privacy_preview import PrivacyPreviewDialog
from breakroom_companion.remote import RemoteWorker
from breakroom_companion.state_store import MAX_STACK_TAGS, normalize_tags
from breakroom_companion.window import TAG_LABELS
from breakroom_shared.api import Consent, Me, Nickname
from breakroom_shared.presence import StackTag

PAGE_EMAIL, PAGE_CODE, PAGE_NICKNAME, PAGE_PRIVACY, PAGE_DONE = range(5)

TITLE = "Sign in to Breakroom"
EMAIL_LABEL = "Email"
INVITE_LABEL = "Invite code (first sign-in only)"
CODE_INTRO = "We emailed you an 8-character code. Type it here."
CODE_LABEL = "Code"
NICKNAME_INTRO = "Choose a nickname. Letters, digits, _ and -, 3 to 24 characters."
NICKNAME_LABEL = "Nickname"
PRIVACY_INTRO = (
    "Choose what may be shared with other people on a break. Each choice is "
    "separate and off until you turn it on. Breaks you accept are a separate "
    "decision."
)
CATEGORY_LABEL = "Share the kind of task (for example backend)"
STACK_LABEL = "Share stack tags (for example python)"
TIME_LABEL = "Share roughly how much time is left"
SENT_ONLY = (
    "These fields are sent only when you start a break, and only the ones you turn on."
)
TAGS_INTRO = "Pick up to 5 tags. They stay on this computer until you start a break."
SHOW_SHARED_LABEL = "Show what is shared"
DONE_TEXT = "You are all set."
BACK, NEXT, SAVE, CLOSE, CANCEL = "Back", "Next", "Save", "Close", "Cancel"

GENERIC_ERROR = "Something went wrong. Please try again."
ERROR_TEXT: dict[str, str] = {
    "invalid_code": "That code is not right. Check it and try again.",
    "expired": "That code has expired. Start again to get a new one.",
    "nickname_taken": "That nickname is already taken.",
    "nickname_reserved": "That nickname is not allowed.",
    "rate_limited": "Too many attempts. Please wait a while and try again.",
    "network": "Could not reach the server. Check your connection and try again.",
    "invalid_request": "That was not accepted. Check what you typed.",
    "unauthorized": "You have been signed out. Please sign in again.",
}

_EMAIL_RE = re.compile(r"[^@\s]{1,64}@[^@\s]+\.[^@\s]+")
_NICKNAME = TypeAdapter(Nickname)


def valid_email(raw: str) -> bool:
    text = raw.strip()
    return (
        len(text) <= 254
        and text.isascii()
        and text.isprintable()
        and _EMAIL_RE.fullmatch(text) is not None
    )


def valid_code(raw: str) -> bool:
    """8 letters/digits, one hyphen allowed anywhere (server ignores it)."""
    text = raw.strip()
    return (
        len(text) <= 9
        and text.count("-") <= 1
        and len(text.replace("-", "")) == 8
        and text.replace("-", "").isascii()
        and text.replace("-", "").isalnum()
    )


def valid_nickname(raw: str) -> bool:
    try:
        _NICKNAME.validate_python(raw)
    except ValidationError:
        return False
    return True


def _label(text: str) -> QLabel:
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


def _error_label() -> QLabel:
    label = _label("")
    label.setVisible(False)
    return label


def _page(*widgets: QWidget) -> QWidget:
    page = QWidget()
    layout = QVBoxLayout(page)
    for widget in widgets:
        layout.addWidget(widget)
    layout.addStretch()
    return page


class OnboardingDialog(QDialog):
    """Pass `me` to reopen the later pages for a user who is already signed in."""

    def __init__(
        self,
        worker: RemoteWorker,
        me: Me | None = None,
        parent: QWidget | None = None,
        *,
        stack_tags: tuple[StackTag, ...] = (),
        save_tags: Callable[[tuple[StackTag, ...]], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.worker = worker
        self._save_tags = save_tags
        self.setWindowTitle(TITLE)
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self._state: str = worker.state
        self._verified = me is not None
        self._started = False  # begin_sign_in sent, code not yet verified
        self._begin_pending = False
        self._busy = False
        self._dead = False  # signed out under us
        self.preview: PrivacyPreviewDialog | None = None

        # page 0
        self.email_edit = QLineEdit()
        self.email_edit.setMaxLength(254)
        self.invite_edit = QLineEdit()
        self.invite_edit.setMaxLength(9)
        self.email_error = _error_label()
        # page 1
        self.code_edit = QLineEdit()
        self.code_edit.setMaxLength(9)
        self.code_error = _error_label()
        # page 2
        self.nickname_edit = QLineEdit()
        self.nickname_edit.setMaxLength(24)
        self.nickname_error = _error_label()
        # page 3
        self.category_box = QCheckBox(CATEGORY_LABEL)
        self.stack_box = QCheckBox(STACK_LABEL)
        self.time_box = QCheckBox(TIME_LABEL)
        self.show_shared_button = QPushButton(SHOW_SHARED_LABEL)
        self.tags_intro = _label(TAGS_INTRO)
        self.tag_list = QListWidget()
        self.tag_items: dict[StackTag, QListWidgetItem] = {}
        self._syncing_tags = False
        chosen = set(normalize_tags(stack_tags))
        for tag in get_args(StackTag):
            item = QListWidgetItem(TAG_LABELS[tag])
            item.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            item.setCheckState(
                Qt.CheckState.Checked if tag in chosen else Qt.CheckState.Unchecked
            )
            self.tag_list.addItem(item)
            self.tag_items[tag] = item
        self._sync_tag_limit()
        self.privacy_error = _error_label()
        self.done_label = _label(DONE_TEXT)

        self._errors = {
            PAGE_EMAIL: self.email_error,
            PAGE_CODE: self.code_error,
            PAGE_NICKNAME: self.nickname_error,
            PAGE_PRIVACY: self.privacy_error,
        }
        self.stack = QStackedWidget()
        self.stack.addWidget(
            _page(
                _label(EMAIL_LABEL),
                self.email_edit,
                _label(INVITE_LABEL),
                self.invite_edit,
                self.email_error,
            )
        )
        self.stack.addWidget(
            _page(
                _label(CODE_INTRO), _label(CODE_LABEL), self.code_edit, self.code_error
            )
        )
        self.stack.addWidget(
            _page(
                _label(NICKNAME_INTRO),
                _label(NICKNAME_LABEL),
                self.nickname_edit,
                self.nickname_error,
            )
        )
        self.stack.addWidget(
            _page(
                _label(PRIVACY_INTRO),
                self.category_box,
                self.stack_box,
                self.tags_intro,
                self.tag_list,
                self.time_box,
                _label(SENT_ONLY),
                self.show_shared_button,
                self.privacy_error,
            )
        )
        self.stack.addWidget(_page(self.done_label))

        self.back_button = QPushButton(BACK)
        self.next_button = QPushButton(NEXT)
        self.cancel_button = QPushButton(CANCEL)
        self.next_button.setDefault(True)
        buttons = QHBoxLayout()
        buttons.addWidget(self.cancel_button)
        buttons.addStretch()
        buttons.addWidget(self.back_button)
        buttons.addWidget(self.next_button)
        layout = QVBoxLayout(self)
        layout.addWidget(self.stack)
        layout.addLayout(buttons)

        for edit in (self.email_edit, self.invite_edit, self.code_edit):
            edit.textChanged.connect(self._refresh)
        self.nickname_edit.textChanged.connect(self._refresh)
        for box in (self.category_box, self.stack_box, self.time_box):
            box.toggled.connect(self._consent_toggled)
        self.stack_box.toggled.connect(self._sync_tag_list)
        self.tag_list.itemChanged.connect(self._tag_changed)
        self.show_shared_button.clicked.connect(self.show_preview)
        self.next_button.clicked.connect(self._next)
        self.back_button.clicked.connect(self._back)
        self.cancel_button.clicked.connect(self.reject)

        worker.state_changed.connect(self._on_state)
        worker.me_changed.connect(self._on_me)
        worker.error.connect(self._on_error)

        if me is None:
            self._go(PAGE_EMAIL)
        else:
            self._load_consent(me.consent)
            self._go(PAGE_NICKNAME if me.nickname is None else PAGE_PRIVACY)
        self._sync_tag_list()

    # --- state ---

    @property
    def page(self) -> int:
        return self.stack.currentIndex()

    def consent(self) -> Consent:
        return Consent(
            version=1,
            share_task_category=self.category_box.isChecked(),
            share_stack_tags=self.stack_box.isChecked(),
            share_time_bucket=self.time_box.isChecked(),
        )

    def selected_tags(self) -> tuple[StackTag, ...]:
        return tuple(
            tag
            for tag, item in self.tag_items.items()
            if item.checkState() == Qt.CheckState.Checked
        )

    def _sync_tag_list(self) -> None:
        self.tags_intro.setVisible(self.stack_box.isChecked())
        self.tag_list.setVisible(self.stack_box.isChecked())

    def _sync_tag_limit(self) -> None:
        """At the limit, the unchecked boxes are disabled."""
        full = len(self.selected_tags()) >= MAX_STACK_TAGS
        for item in self.tag_items.values():
            checked = item.checkState() == Qt.CheckState.Checked
            flags = Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled
            item.setFlags(flags if checked or not full else Qt.ItemFlag.NoItemFlags)

    @Slot(QListWidgetItem)
    def _tag_changed(self, _item: QListWidgetItem) -> None:
        if self._syncing_tags:
            return  # our own flag change below
        self._syncing_tags = True
        try:
            self._sync_tag_limit()
        finally:
            self._syncing_tags = False
        self._consent_toggled()

    def _load_consent(self, consent: Consent | None) -> None:
        for box, value in (
            (self.category_box, consent is not None and consent.share_task_category),
            (self.stack_box, consent is not None and consent.share_stack_tags),
            (self.time_box, consent is not None and consent.share_time_bucket),
        ):
            box.setChecked(value)

    def _go(self, page: int) -> None:
        self.stack.setCurrentIndex(page)
        self._clear_errors()
        self._refresh()

    def _clear_errors(self) -> None:
        for label in self._errors.values():
            label.clear()
            label.setVisible(False)

    def _show_error(self, page: int, code: str) -> None:
        label = self._errors.get(page)
        if label is not None:
            label.setText(ERROR_TEXT.get(code, GENERIC_ERROR))
            label.setVisible(True)

    def _input_valid(self) -> bool:
        page = self.page
        if page == PAGE_EMAIL:
            invite = self.invite_edit.text()
            return valid_email(self.email_edit.text()) and (
                not invite.strip() or valid_code(invite)
            )
        if page == PAGE_CODE:
            return valid_code(self.code_edit.text())
        if page == PAGE_NICKNAME:
            return valid_nickname(self.nickname_edit.text())
        return True

    def _refresh(self) -> None:
        page = self.page
        self.next_button.setText({PAGE_PRIVACY: SAVE, PAGE_DONE: CLOSE}.get(page, NEXT))
        self.next_button.setEnabled(
            page == PAGE_DONE
            or (self._input_valid() and not self._busy and not self._dead)
        )
        # Back only where it can undo something: the code page returns to the
        # email page. Once the code is verified there is nothing to go back to.
        self.back_button.setEnabled(page == PAGE_CODE and not self._busy)
        self.cancel_button.setVisible(page != PAGE_DONE)

    # --- user actions ---

    @Slot()
    def _next(self) -> None:
        page = self.page
        if page == PAGE_DONE:
            self.accept()
            return
        if not self.next_button.isEnabled():
            return
        self._clear_errors()
        if page == PAGE_EMAIL:
            invite = self.invite_edit.text().strip()
            self._started = True
            self._begin_pending = True
            self.worker.begin_sign_in(self.email_edit.text().strip(), invite or None)
            self._go(PAGE_CODE)
        elif page == PAGE_CODE:
            self._begin_pending = False
            self._busy = True
            self.worker.verify(self.code_edit.text().strip())
        elif page == PAGE_NICKNAME:
            self._busy = True
            self.worker.set_nickname(self.nickname_edit.text())
        elif page == PAGE_PRIVACY:
            self._busy = True
            # Tags are local: saved whatever the server answers.
            if self._save_tags is not None:
                self._save_tags(self.selected_tags())
            self.worker.set_consent(self.consent())  # even with every box off
        self._refresh()

    @Slot()
    def _back(self) -> None:
        if self.page == PAGE_CODE and not self._busy:
            self.code_edit.clear()
            self._go(PAGE_EMAIL)

    @Slot()
    def _consent_toggled(self) -> None:
        if self.preview is not None and self.preview.isVisible():
            self.preview.set_consent(self.consent(), self.selected_tags())

    @Slot()
    def show_preview(self) -> None:
        if self.preview is None:
            self.preview = PrivacyPreviewDialog(
                self.consent(), self, self.selected_tags()
            )
            self.preview.setModal(False)
        else:
            self.preview.set_consent(self.consent(), self.selected_tags())
        self.preview.show()
        self.preview.raise_()

    def reject(self) -> None:
        """Cancel. Before the code is verified this abandons the sign-in; after
        it, the user stays signed in with setup incomplete."""
        if self._started and not self._verified:
            self.worker.sign_out()  # abandons an unfinished sign-in
        super().reject()

    def done(self, result: int) -> None:
        for signal, slot in (
            (self.worker.state_changed, self._on_state),
            (self.worker.me_changed, self._on_me),
            (self.worker.error, self._on_error),
        ):
            with contextlib.suppress(RuntimeError, TypeError):
                signal.disconnect(slot)
        if self.preview is not None:
            self.preview.close()
        super().done(result)

    # --- worker signals ---

    @Slot(str)
    def _on_state(self, state: str) -> None:
        self._state = state

    @Slot(object)
    def _on_me(self, me: object) -> None:
        if me is None or not isinstance(me, Me):
            return
        page = self.page
        if page == PAGE_CODE and self._busy:
            self._busy = False
            self._started = False
            self._verified = True
            self._begin_pending = False
            self._load_consent(me.consent)
            self._go(PAGE_NICKNAME if me.nickname is None else PAGE_PRIVACY)
        elif page == PAGE_NICKNAME and self._busy and me.nickname is not None:
            self._busy = False
            self._go(PAGE_PRIVACY)
        elif page == PAGE_PRIVACY and self._busy and me.consent is not None:
            self._busy = False
            self._go(PAGE_DONE)

    @Slot(str)
    def _on_error(self, code: str) -> None:
        page = self.page
        if code == "unauthorized" and self._verified:
            self._dead = True
            self._busy = False
            self._show_error(page, code)
            self._refresh()
            return
        if not (self._begin_pending or self._busy):
            return  # a background error (e.g. offline); not ours to show
        if page == PAGE_CODE and self._state == "signed_out":
            # The sign-in itself failed or the code expired: start over.
            self._begin_pending = self._busy = self._started = False
            self._go(PAGE_EMAIL)
            self._show_error(PAGE_EMAIL, code)
            return
        self._begin_pending = False
        self._busy = False
        self._show_error(page, code)
        self._refresh()

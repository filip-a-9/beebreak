"""Sign-in and privacy onboarding dialog. Owned by M3-05b.

Opened only by a user click (THREAT_MODEL T10); nothing an agent does can open
it. All text is fixed. The user's own input (email, codes, nickname) is never
echoed back and never logged. Server replies reach the dialog only as the
worker's signals and error codes, each mapped to a fixed string below.

Pages: 0 email + invite code, 1 code, 2 nickname (only if none yet),
3 privacy choices (in Settings it also shows the nickname and this device's
name, editable), 4 done, 5 method choice and 6 GitHub (only when the server
offers GitHub sign-in; the dialog starts on page 0 and moves on when the
server's methods arrive). Cancel after the code was verified leaves the user
signed in with setup incomplete; the account row offers "Finish setup".
"""

import contextlib
import re
from collections.abc import Callable
from typing import get_args
from urllib.parse import urlsplit

from pydantic import TypeAdapter, ValidationError
from PySide6.QtCore import Qt, QUrl, Slot
from PySide6.QtGui import QDesktopServices
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

from beebreak_companion.break_state import BreakState
from beebreak_companion.chat_state import ChatState
from beebreak_companion.config import wsl_distro_name
from beebreak_companion.privacy_preview import (
    PrivacyPreviewDialog,
    default_device_name,
)
from beebreak_companion.remote import RemoteWorker
from beebreak_companion.state_store import MAX_STACK_TAGS, normalize_tags
from beebreak_companion.window import FIND, TAG_LABELS
from beebreak_shared.api import (
    AuthMethods,
    Consent,
    DeviceName,
    GitHubStartResponse,
    Me,
    Nickname,
)
from beebreak_shared.presence import StackTag

(
    PAGE_EMAIL,
    PAGE_CODE,
    PAGE_NICKNAME,
    PAGE_PRIVACY,
    PAGE_DONE,
    PAGE_METHOD,
    PAGE_GITHUB,
) = range(7)

TITLE = "Sign in to Beebreak"
SETTINGS_TITLE = "Beebreak settings"
EMAIL_LABEL = "Email"
INVITE_LABEL = "Invite code (first sign-in only)"
CODE_INTRO = "We emailed you an 8-character code. Type it here."
CODE_LABEL = "Code"
DEVICE_NAME_LABEL = (
    "Name for this device. Letters, digits, space, . _ and -, up to 40 characters."
)
NICKNAME_INTRO = "Choose a nickname. Letters, digits, _ and -, 3 to 24 characters."
NICKNAME_LABEL = "Nickname"
PROFILE_DEVICE_LABEL = "Name of this device"
PROFILE_LOCKED = "Names cannot change during a break search or a chat."
PRIVACY_INTRO = (
    "Choose what may be shared with other people on a break. Each choice is "
    f'separate and off until you turn it on. Pressing "{FIND}" is your yes to '
    "a break: you are matched with the next person who is free."
)
CATEGORY_LABEL = "Share the kind of task (for example backend)"
STACK_LABEL = "Share stack tags (for example python)"
TIME_LABEL = "Share roughly how much time is left"
SENT_ONLY = (
    "These fields are sent only when you start a break, and only the ones you turn on."
)
TAGS_INTRO = "Pick up to 5 tags. They stay on this computer until you start a break."
SHOW_SHARED_LABEL = "Show what is shared"
SHOW_SENT_LABEL = "Show what is sent"
DONE_TEXT = (
    "You are all set. Beebreak suggests a break while Claude Code works on its "
    'own; to take one right away, press "Find a break partner".'
)
METHOD_INTRO = "How do you want to sign in?"
GITHUB_BUTTON = "Sign in with GitHub"
EMAIL_BUTTON = "Email me a code"
GITHUB_INTRO = (
    "GitHub will show you a short code to confirm. Beebreak learns only your "
    "numeric GitHub user id, not your name, email or repositories."
)
GITHUB_CODE_INTRO = "Open GitHub and enter this code:"
GITHUB_OPEN = "Open GitHub"
GITHUB_WAITING = "Waiting for you to confirm on GitHub..."
BACK, NEXT, SAVE, CLOSE, CANCEL = "Back", "Next", "Save", "Close", "Cancel"

GENERIC_ERROR = "Something went wrong. Please try again."
ERROR_TEXT: dict[str, str] = {
    "invalid_code": "That code is not right. Check it and try again.",
    "expired": "That code has expired. Start again to get a new one.",
    "nickname_taken": "That nickname is already taken.",
    "nickname_reserved": "That nickname is not allowed.",
    "rate_limited": "Too many attempts. Please wait a while and try again.",
    "network": "Could not reach the server. Check your connection and try again.",
    "credentials_not_saved": (
        "Signed in, but saving the login on this computer failed. "
        "Check disk space and permissions, then sign in again."
    ),
    "credentials_unreadable": (
        "The saved login on this computer could not be read. Sign in again."
    ),
    "invalid_request": "That was not accepted. Check what you typed.",
    "unauthorized": "You have been signed out. Please sign in again.",
    "not_invited": "This GitHub account has not been invited to the beta yet.",
    "access_denied": "The sign-in was declined on GitHub.",
    "github_unavailable": (
        "GitHub could not be reached, or is not set up for this server. "
        "Try again later."
    ),
    "not_found": "That sign-in method is not available on this server.",
}

_EMAIL_RE = re.compile(r"[^@\s]{1,64}@[^@\s]+\.[^@\s]+")
_NICKNAME = TypeAdapter(Nickname)
_DEVICE_NAME = TypeAdapter(DeviceName)


def safe_verification_uri(raw: str) -> bool:
    """Only an https page (or plain http on this computer, for local testing)
    may be opened from a server-supplied link."""
    parts = urlsplit(raw)
    if parts.scheme == "https":
        return bool(parts.hostname)
    return parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost")


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


def valid_device_name(raw: str) -> bool:
    try:
        _DEVICE_NAME.validate_python(raw.strip())
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
        open_url: Callable[[str], bool] | None = None,
    ) -> None:
        super().__init__(parent)
        self.worker = worker
        self._open_url = open_url or (lambda url: QDesktopServices.openUrl(QUrl(url)))
        self.methods = AuthMethods(email=True, github=False)  # until the server says
        self.github_start: GitHubStartResponse | None = None
        self._save_tags = save_tags
        self.setWindowTitle(TITLE if me is None else SETTINGS_TITLE)
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self._state: str = worker.state
        self._verified = me is not None
        self._started = False  # begin_sign_in sent, code not yet verified
        self._begin_pending = False
        self._busy = False
        self._dead = False  # signed out under us
        self.preview: PrivacyPreviewDialog | None = None
        # Settings only: the names can be edited next to the privacy choices.
        self._edit_profile = me is not None and me.nickname is not None
        self._base_nickname = me.nickname if me is not None else None
        self._base_device = (
            me.device_name if me is not None else None
        ) or default_device_name(wsl_distro_name())
        self._steps: list[Callable[[], None]] = []
        self._locked = False

        # page 0
        self.email_edit = QLineEdit()
        self.email_edit.setMaxLength(254)
        self.invite_edit = QLineEdit()
        self.invite_edit.setMaxLength(9)
        self.email_error = _error_label()
        # page 1
        self.code_edit = QLineEdit()
        self.code_edit.setMaxLength(9)
        self.device_name_edit = QLineEdit(default_device_name(wsl_distro_name()))
        self.device_name_edit.setMaxLength(40)
        self.code_error = _error_label()
        # page 5: method choice
        self.github_choice_button = QPushButton(GITHUB_BUTTON)
        self.email_choice_button = QPushButton(EMAIL_BUTTON)
        # page 6: GitHub
        self.github_device_name_edit = QLineEdit(default_device_name(wsl_distro_name()))
        self.github_device_name_edit.setMaxLength(40)
        self.github_show_sent_button = QPushButton(SHOW_SENT_LABEL)
        self.github_code_intro = _label(GITHUB_CODE_INTRO)
        self.github_code_label = _label("")
        self.github_open_button = QPushButton(GITHUB_OPEN)
        self.github_waiting = _label(GITHUB_WAITING)
        self.github_error = _error_label()
        # page 2
        self.nickname_edit = QLineEdit()
        self.nickname_edit.setMaxLength(24)
        self.nickname_error = _error_label()
        # page 3
        self.category_box = QCheckBox(CATEGORY_LABEL)
        self.stack_box = QCheckBox(STACK_LABEL)
        self.time_box = QCheckBox(TIME_LABEL)
        self.show_shared_button = QPushButton(SHOW_SHARED_LABEL)
        self.show_sent_button = QPushButton(SHOW_SENT_LABEL)
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
        self.profile_nickname_edit = QLineEdit(self._base_nickname or "")
        self.profile_nickname_edit.setMaxLength(24)
        self.profile_device_edit = QLineEdit(self._base_device)
        self.profile_device_edit.setMaxLength(40)
        self.profile_locked_label = _label(PROFILE_LOCKED)
        self.done_label = _label(DONE_TEXT)

        self._errors = {
            PAGE_EMAIL: self.email_error,
            PAGE_CODE: self.code_error,
            PAGE_NICKNAME: self.nickname_error,
            PAGE_PRIVACY: self.privacy_error,
            PAGE_GITHUB: self.github_error,
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
                _label(CODE_INTRO),
                _label(CODE_LABEL),
                self.code_edit,
                _label(DEVICE_NAME_LABEL),
                self.device_name_edit,
                self.show_sent_button,
                self.code_error,
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
                *self._profile_widgets(),
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
        self.stack.addWidget(
            _page(
                _label(METHOD_INTRO),
                self.github_choice_button,
                self.email_choice_button,
            )
        )
        self.stack.addWidget(
            _page(
                _label(GITHUB_INTRO),
                _label(DEVICE_NAME_LABEL),
                self.github_device_name_edit,
                self.github_show_sent_button,
                self.github_code_intro,
                self.github_code_label,
                self.github_open_button,
                self.github_waiting,
                self.github_error,
            )
        )
        self._show_github_code(None)

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

        for edit in (
            self.email_edit,
            self.invite_edit,
            self.code_edit,
            self.device_name_edit,
        ):
            edit.textChanged.connect(self._refresh)
        self.device_name_edit.textChanged.connect(self._device_name_changed)
        self.github_device_name_edit.textChanged.connect(self._refresh)
        self.github_device_name_edit.textChanged.connect(self._device_name_changed)
        self.github_choice_button.clicked.connect(self._choose_github)
        self.email_choice_button.clicked.connect(self._choose_email)
        self.github_open_button.clicked.connect(self.open_verification)
        self.github_show_sent_button.clicked.connect(self.show_preview)
        self.nickname_edit.textChanged.connect(self._refresh)
        self.profile_nickname_edit.textChanged.connect(self._refresh)
        self.profile_device_edit.textChanged.connect(self._refresh)
        self.profile_device_edit.textChanged.connect(self._device_name_changed)
        for box in (self.category_box, self.stack_box, self.time_box):
            box.toggled.connect(self._consent_toggled)
        self.stack_box.toggled.connect(self._sync_tag_list)
        self.tag_list.itemChanged.connect(self._tag_changed)
        self.show_shared_button.clicked.connect(self.show_preview)
        self.show_sent_button.clicked.connect(self.show_preview)
        self.next_button.clicked.connect(self._next)
        self.back_button.clicked.connect(self._back)
        self.cancel_button.clicked.connect(self.reject)

        worker.state_changed.connect(self._on_state)
        worker.me_changed.connect(self._on_me)
        worker.error.connect(self._on_error)
        worker.methods_changed.connect(self._on_methods)
        worker.github_started.connect(self._on_github_started)
        worker.break_changed.connect(self._on_activity)
        worker.chat_changed.connect(self._on_activity)
        self._sync_lock()

        if me is None:
            self._go(PAGE_EMAIL)
            worker.fetch_methods()
        else:
            self._load_consent(me.consent)
            self._go(PAGE_NICKNAME if me.nickname is None else PAGE_PRIVACY)
        self._sync_tag_list()

    def _profile_widgets(self) -> list[QWidget]:
        if not self._edit_profile:
            return []
        self.profile_locked_label.setVisible(False)
        return [
            _label(NICKNAME_INTRO),
            self.profile_nickname_edit,
            _label(PROFILE_DEVICE_LABEL),
            _label(DEVICE_NAME_LABEL),
            self.profile_device_edit,
            self.profile_locked_label,
        ]

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
            return valid_code(self.code_edit.text()) and valid_device_name(
                self.device_name_edit.text()
            )
        if page == PAGE_NICKNAME:
            return valid_nickname(self.nickname_edit.text())
        if page == PAGE_GITHUB:
            return self.github_start is None and valid_device_name(
                self.github_device_name_edit.text()
            )
        if page == PAGE_PRIVACY and self._edit_profile and not self._locked:
            return valid_nickname(self.profile_nickname_edit.text()) and (
                valid_device_name(self.profile_device_edit.text())
            )
        return page != PAGE_METHOD  # there the two buttons decide

    def _refresh(self) -> None:
        page = self.page
        self.next_button.setText({PAGE_PRIVACY: SAVE, PAGE_DONE: CLOSE}.get(page, NEXT))
        self.next_button.setEnabled(
            page == PAGE_DONE
            or (self._input_valid() and not self._busy and not self._dead)
        )
        # Back only where it can undo something: the code page returns to the
        # email page. Once the code is verified there is nothing to go back to.
        can_go_back = (page == PAGE_CODE and not self._busy) or (
            page == PAGE_GITHUB and self.methods.email and not self._verified
        )
        self.back_button.setEnabled(can_go_back)
        # Hidden, not just greyed, where there is nothing to go back to.
        self.back_button.setVisible(
            page == PAGE_CODE or (page == PAGE_GITHUB and self.methods.email)
        )
        self.next_button.setVisible(page != PAGE_METHOD)
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
        elif page == PAGE_GITHUB:
            self._started = True
            self._begin_pending = True
            self._busy = True
            self.worker.begin_github_sign_in(
                self.github_device_name_edit.text().strip()
            )
        elif page == PAGE_CODE:
            self._begin_pending = False
            self._busy = True
            self.worker.verify(
                self.code_edit.text().strip(), self.device_name_edit.text().strip()
            )
        elif page == PAGE_NICKNAME:
            self._busy = True
            self.worker.set_nickname(self.nickname_edit.text())
        elif page == PAGE_PRIVACY:
            self._busy = True
            # Tags are local: saved whatever the server answers.
            if self._save_tags is not None:
                self._save_tags(self.selected_tags())
            # Changed names go first, one request at a time; the consent last
            # (even with every box off). Each reply starts the next step.
            self._steps = []
            nickname = self.profile_nickname_edit.text()
            device = self.profile_device_edit.text().strip()
            if self._edit_profile and not self._locked:
                if nickname != self._base_nickname:
                    self._steps.append(lambda: self.worker.set_nickname(nickname))
                if device != self._base_device:
                    self._steps.append(lambda: self.worker.set_device_name(device))
            consent = self.consent()
            self._steps.append(lambda: self.worker.set_consent(consent))
            self._steps.pop(0)()
        self._refresh()

    @Slot()
    def _back(self) -> None:
        if self.page == PAGE_CODE and not self._busy:
            self.code_edit.clear()
            self._go(PAGE_EMAIL)
        elif self.page == PAGE_GITHUB and self.methods.email and not self._verified:
            self._abandon_github()
            self._go(PAGE_METHOD if self.methods.github else PAGE_EMAIL)

    @Slot()
    def _choose_github(self) -> None:
        self._go(PAGE_GITHUB)

    @Slot()
    def _choose_email(self) -> None:
        self._go(PAGE_EMAIL)

    def _abandon_github(self) -> None:
        """Drop a GitHub attempt in progress (the worker stops polling)."""
        if self._started and not self._verified:
            self.worker.sign_out()
        self._started = self._begin_pending = self._busy = False
        self._show_github_code(None)

    def _show_github_code(self, started: GitHubStartResponse | None) -> None:
        self.github_start = started
        shown = started is not None
        self.github_code_intro.setVisible(shown)
        self.github_code_label.setVisible(shown)
        self.github_open_button.setVisible(shown)
        self.github_waiting.setVisible(shown)
        self.github_code_label.setText(started.user_code if started else "")

    @Slot()
    def open_verification(self) -> None:
        """Open GitHub's code page. Only on this click, and only a safe link."""
        started = self.github_start
        if started is not None and safe_verification_uri(started.verification_uri):
            self._open_url(started.verification_uri)

    def _device_name(self) -> str:
        if self.page == PAGE_GITHUB:
            edit = self.github_device_name_edit
        elif self._edit_profile:
            edit = self.profile_device_edit
        else:
            edit = self.device_name_edit
        return edit.text().strip()

    @Slot()
    def _device_name_changed(self) -> None:
        # In Settings the name is sent when saved, so the preview follows it.
        if self.preview is not None and (not self._verified or self._edit_profile):
            self.preview.set_device_name(self._device_name())

    @Slot()
    def _consent_toggled(self) -> None:
        if self.preview is not None and self.preview.isVisible():
            self.preview.set_consent(self.consent(), self.selected_tags())

    @Slot()
    def show_preview(self) -> None:
        if self.preview is None:
            self.preview = PrivacyPreviewDialog(
                self.consent(),
                self,
                self.selected_tags(),
                self._device_name(),
                already_sent=self._verified,
            )
            self.preview.setModal(False)
        else:
            self.preview.set_consent(self.consent(), self.selected_tags())
            self.preview.set_device_name(
                self._device_name(), already_sent=self._verified
            )
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
            (self.worker.break_changed, self._on_activity),
            (self.worker.chat_changed, self._on_activity),
        ):
            with contextlib.suppress(RuntimeError, TypeError):
                signal.disconnect(slot)
        for signal2, slot2 in (
            (self.worker.methods_changed, self._on_methods),
            (self.worker.github_started, self._on_github_started),
        ):
            with contextlib.suppress(RuntimeError, TypeError):
                signal2.disconnect(slot2)
        if self.preview is not None:
            self.preview.close()
        super().done(result)

    # --- worker signals ---

    @Slot(str)
    def _on_state(self, state: str) -> None:
        self._state = state

    @Slot(object)
    def _on_activity(self, _state: object = None) -> None:
        self._sync_lock()
        self._refresh()

    def _sync_lock(self) -> None:
        """Names are fixed while searching or in a live chat."""
        brk: BreakState = self.worker.break_state
        chat: ChatState = self.worker.chat_state
        self._locked = brk.phase in ("joining", "searching", "matched") or (
            chat.match_id is not None and chat.ended is None
        )
        for edit in (self.profile_nickname_edit, self.profile_device_edit):
            edit.setEnabled(not self._locked)
        self.profile_locked_label.setVisible(self._locked and self._edit_profile)

    @Slot(object)
    def _on_methods(self, methods: object) -> None:
        if not isinstance(methods, AuthMethods):
            return
        self.methods = methods
        untouched = (
            self.page == PAGE_EMAIL
            and not self._started
            and not self._verified
            and not self.email_edit.text()
            and not self.invite_edit.text()
        )
        if untouched and methods.github:
            self._go(PAGE_METHOD if methods.email else PAGE_GITHUB)
        else:
            self._refresh()

    @Slot(object)
    def _on_github_started(self, started: object) -> None:
        if not isinstance(started, GitHubStartResponse) or self.page != PAGE_GITHUB:
            return
        self._begin_pending = False  # now waiting for the user, not the server
        self._show_github_code(started)
        self._refresh()

    @Slot(object)
    def _on_me(self, me: object) -> None:
        if me is None or not isinstance(me, Me):
            return
        page = self.page
        if page == PAGE_GITHUB and self._busy:
            self._busy = False
            self._started = False
            self._verified = True
            self._begin_pending = False
            self._show_github_code(None)
            self._load_consent(me.consent)
            self._go(PAGE_NICKNAME if me.nickname is None else PAGE_PRIVACY)
        elif page == PAGE_CODE and self._busy:
            self._busy = False
            self._started = False
            self._verified = True
            self._begin_pending = False
            self._load_consent(me.consent)
            self._go(PAGE_NICKNAME if me.nickname is None else PAGE_PRIVACY)
        elif page == PAGE_NICKNAME and self._busy and me.nickname is not None:
            self._busy = False
            self._go(PAGE_PRIVACY)
        elif page == PAGE_PRIVACY and self._busy:
            self._base_nickname = me.nickname
            self._base_device = me.device_name or self._base_device
            if self._steps:
                self._steps.pop(0)()
            elif me.consent is not None:
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
        if page == PAGE_GITHUB:
            # The attempt is over (declined, expired, not invited, ...): back to
            # the start of this page with the reason shown.
            self._begin_pending = self._busy = self._started = False
            self._show_github_code(None)
            self._refresh()
            self._show_error(page, code)
            return
        if page == PAGE_CODE and self._state == "signed_out":
            # The sign-in itself failed or the code expired: start over.
            self._begin_pending = self._busy = self._started = False
            self._go(PAGE_EMAIL)
            self._show_error(PAGE_EMAIL, code)
            return
        self._begin_pending = False
        self._busy = False
        self._steps = []
        self._show_error(page, code)
        self._refresh()

"""OS notification for a new chat message. Owned by M5-06.

Callers pass fixed text with the nickname only, never a message body (rule 16).
The Windows toast (W6) ignores what the caller passes: Windows keeps toasts in
its notification history, so only the constants below ever reach it.
"""

import base64
import os
import subprocess
import threading
from collections.abc import Callable
from typing import Protocol

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QStyle, QSystemTrayIcon, QWidget

from beebreak_companion.config import (
    powershell_path,
    toast_timeout_s,
    windows_toast_enabled,
    wsl_distro_name,
)

# Fixed text only: nothing user- or server-derived may be shown in a toast.
TOAST_TITLE = "Beebreak"
TOAST_TEXT = "Your break partner is waiting."
# Windows PowerShell's own AppUserModelID: Beebreak has none registered.
_TOAST_AUMID = (
    r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"
)
_TOAST_SCRIPT = (
    "$ErrorActionPreference='Stop';"
    "[void][Windows.UI.Notifications.ToastNotificationManager,"
    "Windows.UI.Notifications,ContentType=WindowsRuntime];"
    "[void][Windows.Data.Xml.Dom.XmlDocument,"
    "Windows.Data.Xml.Dom.XmlDocument,ContentType=WindowsRuntime];"
    "$x=New-Object Windows.Data.Xml.Dom.XmlDocument;"
    f'$x.LoadXml(\'<toast><visual><binding template="ToastText02">'
    f'<text id="1">{TOAST_TITLE}</text><text id="2">{TOAST_TEXT}</text>'
    "</binding></visual></toast>');"
    "$t=New-Object Windows.UI.Notifications.ToastNotification $x;"
    f"[Windows.UI.Notifications.ToastNotificationManager]::"
    f"CreateToastNotifier('{_TOAST_AUMID}').Show($t)"
)
# -EncodedCommand (base64 of UTF-16LE) survives the Windows command line intact.
_TOAST_ARGV_TAIL = [
    "-NoProfile",
    "-NonInteractive",
    "-WindowStyle",
    "Hidden",
    "-EncodedCommand",
    base64.b64encode(_TOAST_SCRIPT.encode("utf-16-le")).decode("ascii"),
]


def _reap(proc: "subprocess.Popen[bytes]", timeout: float) -> None:
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def show_windows_toast() -> bool:
    """Fire and forget a fixed-text Windows toast; False if it is unavailable.

    Only under WSL, only when the switch is on and the PowerShell executable
    exists. Takes no arguments, so no caller data can reach the command line.
    """
    exe = powershell_path()
    if not (windows_toast_enabled() and wsl_distro_name()):
        return False
    if not (os.path.isfile(exe) and os.access(exe, os.X_OK)):
        return False
    try:
        proc = subprocess.Popen(
            [exe, *_TOAST_ARGV_TAIL],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
            start_new_session=True,
        )
    except OSError:
        return False
    # The reaper only waits (no Qt, no allocation-heavy work), so it cannot
    # trigger a cyclic collection of its own beyond what gc_policy allows.
    threading.Thread(target=_reap, args=(proc, toast_timeout_s()), daemon=True).start()
    return True


class TrayLike(Protocol):
    def show(self) -> None: ...

    def showMessage(self, title: str, message: str) -> None: ...


def _default_tray() -> TrayLike:
    app = QApplication.instance()
    icon = QIcon()
    if isinstance(app, QApplication):
        icon = app.windowIcon()  # the Beebreak mark once the theme is applied
        if icon.isNull():
            icon = app.style().standardIcon(
                QStyle.StandardPixmap.SP_MessageBoxInformation
            )
    return QSystemTrayIcon(icon)


class DesktopNotifier:
    """`window` is what `QApplication.alert` flashes when there is no tray.

    The tray icon is created on first use and never when no tray exists.
    """

    def __init__(
        self,
        window: QWidget | None = None,
        *,
        tray_available: Callable[[], bool] = QSystemTrayIcon.isSystemTrayAvailable,
        tray_factory: Callable[[], TrayLike] = _default_tray,
        on_click: Callable[[], None] | None = None,
    ) -> None:
        # Called when the user clicks the tray message (set by the app wiring).
        self.on_click = on_click
        self._window = window
        self._tray_available = tray_available
        self._tray_factory = tray_factory
        self._tray: TrayLike | None = None

    def _clicked(self) -> None:
        if self.on_click is not None:
            self.on_click()

    def notify(self, title: str, text: str) -> None:
        """A system tray message when a tray exists. Else a fixed-text Windows
        toast under WSL (ignoring `title` and `text`), and `QApplication.alert`."""
        if self._tray_available():
            if self._tray is None:
                self._tray = self._tray_factory()
                clicked = getattr(self._tray, "messageClicked", None)
                if clicked is not None:
                    clicked.connect(self._clicked)
                self._tray.show()
            self._tray.showMessage(title, text)
            return
        show_windows_toast()
        if self._window is not None:
            QApplication.alert(self._window)

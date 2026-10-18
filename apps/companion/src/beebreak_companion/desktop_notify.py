"""OS notification for a new chat message. Owned by M5-06.

Callers pass fixed text with the nickname only, never a message body (rule 16).
"""

from collections.abc import Callable
from typing import Protocol

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QStyle, QSystemTrayIcon, QWidget


class TrayLike(Protocol):
    def show(self) -> None: ...

    def showMessage(self, title: str, message: str) -> None: ...


def _default_tray() -> TrayLike:
    app = QApplication.instance()
    icon = QIcon()
    if isinstance(app, QApplication):
        icon = app.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxInformation)
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
        """A system tray message when a tray exists, else `QApplication.alert`."""
        if self._tray_available():
            if self._tray is None:
                self._tray = self._tray_factory()
                clicked = getattr(self._tray, "messageClicked", None)
                if clicked is not None:
                    clicked.connect(self._clicked)
                self._tray.show()
            self._tray.showMessage(title, text)
        elif self._window is not None:
            QApplication.alert(self._window)

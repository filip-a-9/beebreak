"""OS notification for a new chat message. Owned by M5-06.

Callers pass fixed text with the nickname only, never a message body (rule 16).
"""


class DesktopNotifier:
    def notify(self, title: str, text: str) -> None:
        """A system tray message when a tray exists, else `QApplication.alert`."""

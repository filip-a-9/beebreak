"""Measure whether the chat window steals focus on a match (dev tool, F6).

Opens a "focus holder" window, activates it, then runs one candidate of the
match path on a real ChatWindow and logs isActiveWindow() of both plus the
ActivationChange events for a few seconds. Opens real windows briefly and
closes them all. Run with Qt's default platform and with QT_QPA_PLATFORM=xcb.

    uv run python tools/measure_chat_focus.py CANDIDATE [--seconds 3]

Candidates: baseline (show + raise_, the old app.py), no-raise (show only),
nofocus-flag (WindowDoesNotAcceptFocus until the user acts), minimized
(showMinimized), tool (Qt.Tool), on-bottom (WindowStaysOnBottomHint).
The result line is JSON, fixed strings and booleans only.
"""

from __future__ import annotations

import argparse
import json
import sys

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtWidgets import QApplication, QLabel, QWidget

from beebreak_companion.chat_window import ChatWindow
from beebreak_companion.gc_policy import collect_on_gui_thread

CANDIDATES = ("baseline", "no-raise", "nofocus-flag", "minimized", "tool", "on-bottom")


class _Watch(QObject):
    def __init__(self, name: str, log: list[str]) -> None:
        super().__init__()
        self._name = name
        self._log = log

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.ActivationChange and isinstance(
            watched, QWidget
        ):
            self._log.append(f"{self._name}:activation={watched.isActiveWindow()}")
        return False


def _trigger(candidate: str, chat: ChatWindow) -> None:
    if candidate == "baseline":
        chat.show()
        chat.raise_()
    elif candidate == "no-raise":
        chat.show()
    elif candidate == "nofocus-flag":
        chat.setWindowFlag(Qt.WindowType.WindowDoesNotAcceptFocus, True)
        chat.show()
    elif candidate == "minimized":
        chat.showMinimized()
    elif candidate == "tool":
        chat.setWindowFlag(Qt.WindowType.Tool, True)
        chat.show()
    elif candidate == "on-bottom":
        chat.setWindowFlag(Qt.WindowType.WindowStaysOnBottomHint, True)
        chat.show()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("candidate", choices=CANDIDATES)
    parser.add_argument("--seconds", type=float, default=3.0)
    args = parser.parse_args()

    app = QApplication(sys.argv[:1])
    collect_on_gui_thread(app)
    log: list[str] = []
    result: dict[str, object] = {
        "candidate": args.candidate,
        "platform": app.platformName(),
    }

    holder = QLabel("focus holder")
    holder.setWindowTitle("Beebreak focus holder")
    holder.resize(200, 100)
    chat = ChatWindow()
    holder_watch = _Watch("holder", log)
    chat_watch = _Watch("chat", log)
    holder.installEventFilter(holder_watch)
    chat.installEventFilter(chat_watch)

    def step_trigger() -> None:
        result["holder_active_before"] = holder.isActiveWindow()
        log.append("trigger")
        _trigger(args.candidate, chat)

    def step_report() -> None:
        result["holder_active_after"] = holder.isActiveWindow()
        result["chat_active_after"] = chat.isActiveWindow()
        result["chat_visible"] = chat.isVisible()
        result["chat_minimized"] = chat.isMinimized()
        result["events"] = log
        print(json.dumps(result))
        chat.close()
        holder.close()
        app.quit()

    holder.show()
    holder.raise_()
    holder.activateWindow()
    QTimer.singleShot(1000, step_trigger)
    QTimer.singleShot(1000 + round(args.seconds * 1000), step_report)
    app.exec()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

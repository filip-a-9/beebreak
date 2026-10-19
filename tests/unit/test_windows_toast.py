"""Windows toast for DesktopNotifier when there is no tray (finding W6).

A real subprocess runs: the PowerShell path points at a stub script that
records its argv and stdin. Nothing user-specific may reach it.
"""

import os
import stat
import time
from pathlib import Path
from uuid import uuid4

import pytest
from PySide6.QtWidgets import QApplication, QWidget
from pytestqt.qtbot import QtBot

from beebreak_companion.app import wire_chat
from beebreak_companion.chat_window import ChatWindow
from beebreak_companion.config import (
    POWERSHELL_ENV,
    WINDOWS_TOAST_ENV,
    powershell_path,
    windows_toast_enabled,
)
from beebreak_companion.desktop_notify import DesktopNotifier
from beebreak_companion.window import CompanionWindow
from tests.fakes.chat import T0, chat_state, message_view
from tests.fakes.remote_worker import FakeRemoteWorker

NICK = "SENSITIVE-nick-9f3a"
BODY = "SENSITIVE-body-4be1"


@pytest.fixture
def stub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A stub powershell.exe; returns the file it appends its argv/stdin to."""
    log = tmp_path / "calls.txt"
    exe = tmp_path / "powershell.exe"
    exe.write_text(
        "#!/bin/sh\n"
        f'for a in "$@"; do printf "ARG:%s\\n" "$a" >> "{log}"; done\n'
        f'printf "STDIN:" >> "{log}"; cat >> "{log}"; echo >> "{log}"\n'
    )
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv(POWERSHELL_ENV, str(exe))
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu-test")
    monkeypatch.delenv(WINDOWS_TOAST_ENV, raising=False)
    monkeypatch.setattr(QApplication, "alert", lambda *a: None)
    return log


def _notifier(qtbot: QtBot, *, tray: bool = False) -> DesktopNotifier:
    window = QWidget()
    qtbot.addWidget(window)
    return DesktopNotifier(window, tray_available=lambda: tray)


def _wait_for(path: Path) -> str:
    for _ in range(100):
        if path.exists() and "STDIN:" in path.read_text():
            return path.read_text()
        time.sleep(0.05)
    return path.read_text() if path.exists() else ""


def _stays_empty(path: Path) -> bool:
    time.sleep(0.3)
    return not path.exists()


def test_without_a_tray_under_wsl_powershell_is_run(qtbot: QtBot, stub: Path) -> None:
    _notifier(qtbot).notify("t", "x")
    assert "ARG:" in _wait_for(stub)


def test_nothing_the_caller_passes_reaches_powershell(qtbot: QtBot, stub: Path) -> None:
    _notifier(qtbot).notify(NICK, f"New message from {NICK} {BODY}")
    recorded = _wait_for(stub)
    assert "ARG:" in recorded
    assert NICK not in recorded and BODY not in recorded


def test_no_toast_when_a_tray_exists(qtbot: QtBot, stub: Path) -> None:
    class Tray:
        def show(self) -> None: ...

        def showMessage(self, title: str, message: str) -> None: ...

    window = QWidget()
    qtbot.addWidget(window)
    DesktopNotifier(window, tray_available=lambda: True, tray_factory=Tray).notify(
        "t", "x"
    )
    assert _stays_empty(stub)


@pytest.mark.parametrize("value", ["0", "false", "off", "No"])
def test_no_toast_when_switched_off(
    qtbot: QtBot, stub: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv(WINDOWS_TOAST_ENV, value)
    _notifier(qtbot).notify("t", "x")
    assert _stays_empty(stub)


def test_no_toast_outside_wsl(
    qtbot: QtBot, stub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WSL_DISTRO_NAME", "")
    _notifier(qtbot).notify("t", "x")
    assert _stays_empty(stub)


def test_no_toast_and_no_error_when_the_executable_is_missing(
    qtbot: QtBot, stub: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(POWERSHELL_ENV, str(tmp_path / "absent.exe"))
    _notifier(qtbot).notify("t", "x")
    assert _stays_empty(stub)


def test_a_failing_powershell_does_not_raise(
    qtbot: QtBot, stub: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    broken = tmp_path / "broken.exe"
    broken.write_text("not a program")
    broken.chmod(0o755)
    monkeypatch.setenv(POWERSHELL_ENV, str(broken))
    _notifier(qtbot).notify("t", "x")


def test_the_window_is_still_alerted(
    qtbot: QtBot, stub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[object] = []
    monkeypatch.setattr(QApplication, "alert", lambda w, *a: seen.append(w))
    notifier = _notifier(qtbot)
    notifier.notify("t", "x")
    assert len(seen) == 1


def test_config_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(POWERSHELL_ENV, raising=False)
    monkeypatch.delenv(WINDOWS_TOAST_ENV, raising=False)
    assert powershell_path().endswith("WindowsPowerShell/v1.0/powershell.exe")
    assert windows_toast_enabled() is True
    monkeypatch.setenv(WINDOWS_TOAST_ENV, "garbage")
    assert windows_toast_enabled() is True
    monkeypatch.setenv(POWERSHELL_ENV, "   ")
    assert powershell_path().endswith("powershell.exe")


def test_one_toast_per_throttle_window_and_no_user_data(
    qtbot: QtBot, stub: Path
) -> None:
    window = CompanionWindow()
    chat = ChatWindow(now=lambda: T0)
    qtbot.addWidget(window)
    qtbot.addWidget(chat)
    remote = FakeRemoteWorker()
    now = [100.0]
    wire_chat(
        window,
        chat,
        remote,
        DesktopNotifier(chat, tray_available=lambda: False),
        clock=lambda: now[0],
        is_active=lambda: False,
        in_view=lambda: False,
        main_window_active=lambda: False,
    )
    match_id = uuid4()
    msgs = [message_view(1, body=BODY)]
    remote.push_chat(
        chat_state(match_id=match_id, peer_nickname=NICK, messages=tuple(msgs))
    )
    first = _wait_for(stub)
    assert first.count("STDIN:") == 1
    now[0] += 5
    msgs.append(message_view(2, body=BODY))
    remote.push_chat(
        chat_state(match_id=match_id, peer_nickname=NICK, messages=tuple(msgs))
    )
    time.sleep(0.5)
    assert stub.read_text().count("STDIN:") == 1
    assert NICK not in stub.read_text() and BODY not in stub.read_text()
    assert os.path.exists(stub)


def _wired(qtbot: QtBot, *, main_active: bool) -> FakeRemoteWorker:
    window = CompanionWindow()
    chat = ChatWindow(now=lambda: T0)
    qtbot.addWidget(window)
    qtbot.addWidget(chat)
    remote = FakeRemoteWorker()
    wire_chat(
        window,
        chat,
        remote,
        DesktopNotifier(chat, tray_available=lambda: False),
        clock=lambda: 100.0,
        is_active=lambda: False,
        in_view=lambda: False,
        main_window_active=lambda: main_active,
    )
    return remote


def test_a_match_while_the_main_window_is_inactive_toasts_once(
    qtbot: QtBot, stub: Path
) -> None:
    remote = _wired(qtbot, main_active=False)
    match_id = uuid4()
    remote.push_chat(chat_state(match_id=match_id, peer_nickname=NICK))
    assert _wait_for(stub).count("STDIN:") == 1
    remote.push_chat(chat_state(match_id=match_id, peer_nickname=NICK))
    time.sleep(0.4)
    recorded = stub.read_text()
    assert recorded.count("STDIN:") == 1
    assert NICK not in recorded


def test_a_match_while_the_main_window_is_active_opens_without_a_toast(
    qtbot: QtBot, stub: Path
) -> None:
    remote = _wired(qtbot, main_active=True)
    remote.push_chat(chat_state(match_id=uuid4(), peer_nickname=NICK))
    assert _stays_empty(stub)


def test_an_ended_match_does_not_toast(qtbot: QtBot, stub: Path) -> None:
    remote = _wired(qtbot, main_active=False)
    remote.push_chat(chat_state(match_id=uuid4(), ended="ended"))
    assert _stays_empty(stub)

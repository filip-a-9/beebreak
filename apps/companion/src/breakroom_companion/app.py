"""Companion entry point: wires Controller, CompanionWindow and IpcWorker. M1-04."""

import signal
import sys
from datetime import UTC, datetime

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from breakroom_companion.controller import Controller
from breakroom_companion.ipc_server import CompanionAlreadyRunning
from breakroom_companion.window import CompanionWindow
from breakroom_companion.worker import IpcWorker
from breakroom_shared.events import NormalizedEvent
from breakroom_shared.ipc import InvitationState, StatusReply
from breakroom_shared.paths import UnsafeRuntimeDir, socket_path

# Fixed cadence of the controller's clock (not user configuration).
TICK_INTERVAL_MS = 1000
EXIT_START_FAILED = 1
EXIT_ALREADY_RUNNING = 2


def initial_status(controller: Controller) -> StatusReply:
    """Snapshot of the controller as a StatusReply."""
    return StatusReply(
        kind="status",
        version=2,
        invitation=controller.state,
        last_event_at=controller.last_event_at,
        activity=controller.activity(datetime.now(UTC)),
        estimate=controller.estimate,
    )


def wire(controller: Controller, window: CompanionWindow, worker: IpcWorker) -> QTimer:
    """Connect the three parts; returns the started tick timer (child of window)."""

    def apply(new: InvitationState | None) -> None:
        if new is not None:
            window.set_state(new, controller.estimate)
        worker.set_status(initial_status(controller))

    def on_event(event: object) -> None:
        assert isinstance(event, NormalizedEvent)
        apply(controller.on_event(event))

    worker.event_received.connect(on_event)
    window.accepted.connect(
        lambda: apply(controller.on_outcome("accepted", now=datetime.now(UTC)))
    )
    window.declined.connect(
        lambda: apply(controller.on_outcome("declined", now=datetime.now(UTC)))
    )
    window.enabled_changed.connect(lambda on: apply(controller.set_enabled(on)))

    timer = QTimer(window)
    timer.setInterval(TICK_INTERVAL_MS)
    timer.timeout.connect(lambda: apply(controller.tick(datetime.now(UTC))))
    timer.start()
    return timer


def main() -> int:
    try:
        sock = socket_path()
    except (UnsafeRuntimeDir, OSError):
        print("breakroom-companion: unsafe runtime directory", file=sys.stderr)
        return EXIT_START_FAILED
    try:
        controller = Controller()
    except ValueError:  # a BREAKROOM_* setting is invalid
        print("breakroom-companion: invalid configuration", file=sys.stderr)
        return EXIT_START_FAILED
    app = QApplication(sys.argv)
    window = CompanionWindow()
    window.set_state(controller.state, controller.estimate)
    worker = IpcWorker(sock, initial_status(controller))
    wire(controller, window, worker)
    try:
        worker.start()
    except CompanionAlreadyRunning:
        print("breakroom-companion: already running", file=sys.stderr)
        return EXIT_ALREADY_RUNNING
    except RuntimeError:
        print("breakroom-companion: could not open the IPC socket", file=sys.stderr)
        return EXIT_START_FAILED
    app.aboutToQuit.connect(worker.stop)
    # Python runs signal handlers only when the interpreter has control; the
    # 1 s tick timer returns to Python, so Ctrl+C / SIGTERM quit within a tick.
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: app.quit())
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())

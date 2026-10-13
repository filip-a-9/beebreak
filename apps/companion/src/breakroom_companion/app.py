"""Companion entry point: wires Controller, CompanionWindow and IpcWorker. M1-04."""

import signal
import sys
from datetime import UTC, datetime

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication

from breakroom_companion.controller import Controller
from breakroom_companion.credentials import default_credential_store
from breakroom_companion.endpoint import EndpointRefused
from breakroom_companion.ipc_server import CompanionAlreadyRunning
from breakroom_companion.onboarding import OnboardingDialog
from breakroom_companion.remote import RemoteWorker
from breakroom_companion.remote_config import RemoteConfig
from breakroom_companion.window import CompanionWindow
from breakroom_companion.worker import IpcWorker
from breakroom_shared.api import Me
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
        version=3,
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


def wire_remote(window: CompanionWindow, worker: RemoteWorker, encrypted: bool) -> None:
    """Connect the account row and the user-opened onboarding dialog."""
    current: dict[str, object] = {"me": None, "dialog": None}

    def on_me(me: object) -> None:
        current["me"] = me
        window.set_me(me if isinstance(me, Me) else None)

    def open_dialog(me: Me | None) -> None:
        existing = current["dialog"]
        if isinstance(existing, OnboardingDialog):
            existing.raise_()
            return
        dialog = OnboardingDialog(worker, me, window)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        dialog.finished.connect(lambda _result: current.update(dialog=None))
        current["dialog"] = dialog
        dialog.open()

    def on_sign_in() -> None:
        if worker.state == "signed_out":
            open_dialog(None)

    def on_settings() -> None:
        me = current["me"]
        if isinstance(me, Me):
            open_dialog(me)

    window.set_store_encrypted(encrypted)
    worker.state_changed.connect(window.set_remote_state)
    worker.me_changed.connect(on_me)
    window.sign_in_requested.connect(on_sign_in)
    window.settings_requested.connect(on_settings)
    window.sign_out_requested.connect(worker.sign_out)


def setup_remote(window: CompanionWindow) -> RemoteWorker | None:
    """Build the server connection from the environment, or None if the
    configured address is refused or there is no private place to keep a login
    (the account row then stays disabled)."""
    try:
        config = RemoteConfig.from_env()
    except EndpointRefused:
        window.set_remote_unavailable()
        return None
    try:
        store = default_credential_store(config.server_url)
    except (UnsafeRuntimeDir, OSError):  # the state dir is not private or usable
        window.set_remote_unavailable()
        return None
    worker = RemoteWorker(config, store)
    wire_remote(window, worker, store.encrypted)
    return worker


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
    try:
        RemoteConfig.from_env()
    except EndpointRefused:
        pass  # handled below: the companion runs, the account row is disabled
    except ValueError:  # a BREAKROOM_* network setting is invalid
        print("breakroom-companion: invalid configuration", file=sys.stderr)
        return EXIT_START_FAILED
    app = QApplication(sys.argv)
    window = CompanionWindow()
    window.set_state(controller.state, controller.estimate)
    worker = IpcWorker(sock, initial_status(controller))
    wire(controller, window, worker)
    # Built before the IPC socket exists: once the socket is up, clients (and
    # the signal-shutdown test) expect the process to be ready to quit.
    # Python runs signal handlers only when the interpreter has control; the
    # 1 s tick timer returns to Python, so Ctrl+C / SIGTERM quit within a tick.
    # Installed before the IPC socket appears (clients may signal as soon as it
    # does) and quitting through the event queue, so a signal that arrives
    # before exec() starts is not lost.
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: QTimer.singleShot(0, app.quit))
    remote = setup_remote(window)
    try:
        worker.start()
    except CompanionAlreadyRunning:
        print("breakroom-companion: already running", file=sys.stderr)
        return EXIT_ALREADY_RUNNING
    except RuntimeError:
        print("breakroom-companion: could not open the IPC socket", file=sys.stderr)
        return EXIT_START_FAILED
    if remote is not None:
        app.aboutToQuit.connect(remote.stop)  # before the IPC worker
    app.aboutToQuit.connect(worker.stop)
    window.show()
    if remote is not None:
        # Inside the event loop, after the window shows; connects only if a
        # login is stored. Not before exec(): a quit signal that arrives
        # first would be lost.
        QTimer.singleShot(0, remote.start)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())

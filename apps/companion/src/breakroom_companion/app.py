"""Companion entry point: wires Controller, CompanionWindow and IpcWorker. M1-04."""

import signal
import sys
import threading
from collections.abc import Callable
from datetime import UTC, datetime

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication

from breakroom_companion.break_state import BreakPhase, BreakState
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
from breakroom_shared.ipc import InvitationState, LeaveResult, QueueState, StatusReply
from breakroom_shared.paths import UnsafeRuntimeDir, socket_path
from breakroom_shared.presence import RemainingTimeBucket, build_presence

# Fixed cadence of the controller's clock (not user configuration).
TICK_INTERVAL_MS = 1000
EXIT_START_FAILED = 1
EXIT_ALREADY_RUNNING = 2


class BreakTracker:
    """Latest break phase and connection state, readable from the IPC thread.

    The Qt main thread writes it from the worker's signals; the IPC thread reads
    it to answer `queue` in the status and an agent's leave-queue request.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._remote: RemoteWorker | None = None
        self._connected = False
        self._phase: BreakPhase = "none"

    def attach(self, remote: RemoteWorker | None) -> None:
        with self._lock:
            self._remote = remote

    def set_connected(self, connected: bool) -> None:
        with self._lock:
            self._connected = connected

    def set_phase(self, phase: BreakPhase) -> None:
        with self._lock:
            self._phase = phase

    def queue_state(self) -> QueueState:
        with self._lock:
            phase = self._phase
        if phase in ("joining", "searching"):
            return "searching"
        return "chatting" if phase == "matched" else "none"

    def leave_queue(self) -> LeaveResult:
        """IPC thread. Never ends a match; does not wait for the server."""
        with self._lock:
            remote, connected, phase = self._remote, self._connected, self._phase
        if remote is None or not connected:
            return "unavailable"
        if phase in ("joining", "searching"):
            remote.leave_queue()  # thread-safe: schedules onto the worker loop
            return "cancelled"
        return "not_searching"


def initial_status(controller: Controller, queue: QueueState = "none") -> StatusReply:
    """Snapshot of the controller as a StatusReply."""
    return StatusReply(
        kind="status",
        version=4,
        invitation=controller.state,
        last_event_at=controller.last_event_at,
        activity=controller.activity(datetime.now(UTC)),
        estimate=controller.estimate,
        queue=queue,
    )


def wire(
    controller: Controller,
    window: CompanionWindow,
    worker: IpcWorker,
    *,
    queue: Callable[[], QueueState] | None = None,
    on_accepted: Callable[[RemainingTimeBucket | None], None] | None = None,
) -> QTimer:
    """Connect the three parts; returns the started tick timer (child of window).

    `on_accepted` gets the invitation's bucket, read before the controller
    clears it, when an Accept took effect."""

    def apply(new: InvitationState | None) -> None:
        if new is not None:
            window.set_state(new, controller.estimate)
        worker.set_status(initial_status(controller, queue() if queue else "none"))

    def on_event(event: object) -> None:
        assert isinstance(event, NormalizedEvent)
        apply(controller.on_event(event))

    worker.event_received.connect(on_event)

    def on_accept() -> None:
        bucket = controller.estimate  # on_outcome clears it
        new = controller.on_outcome("accepted", now=datetime.now(UTC))
        apply(new)
        if new is not None and on_accepted is not None:
            on_accepted(bucket)

    window.accepted.connect(on_accept)
    window.declined.connect(
        lambda: apply(controller.on_outcome("declined", now=datetime.now(UTC)))
    )
    window.enabled_changed.connect(lambda on: apply(controller.set_enabled(on)))

    timer = QTimer(window)
    timer.setInterval(TICK_INTERVAL_MS)
    timer.timeout.connect(lambda: apply(controller.tick(datetime.now(UTC))))
    timer.start()
    return timer


def wire_break(
    controller: Controller,
    window: CompanionWindow,
    remote: RemoteWorker | None,
    tracker: BreakTracker,
    publish: Callable[[], None],
) -> Callable[[RemainingTimeBucket | None], None]:
    """Connect the break area, readiness and the break lifecycle.

    This is the only place `build_presence` is called for sending (M4 rule 13).
    Returns the Accept hook for `wire(on_accepted=...)`.
    """
    tracker.attach(remote)
    if remote is None:
        window.set_ready(False)
        controller.set_ready(False)
        return lambda _bucket: None

    ready_inputs: dict[str, object] = {"connected": False, "me": None}
    phase: dict[str, BreakPhase] = {"value": "none"}
    reached = {"value": False}  # the break got past joining

    def apply(new: InvitationState | None) -> None:
        if new is not None:
            window.set_state(new, controller.estimate)
        publish()

    def me_ready() -> Me | None:
        me = ready_inputs["me"]
        if (
            ready_inputs["connected"] is True
            and isinstance(me, Me)
            and me.nickname is not None
            and me.consent is not None
        ):
            return me
        return None

    def refresh() -> None:
        ready = me_ready() is not None
        window.set_ready(ready)
        apply(controller.set_ready(ready))

    def on_state(state: str) -> None:
        connected = state == "connected"
        ready_inputs["connected"] = connected
        tracker.set_connected(connected)
        refresh()

    def on_me(me: object) -> None:
        ready_inputs["me"] = me if isinstance(me, Me) else None
        refresh()

    def on_break(state: object) -> None:
        if not isinstance(state, BreakState):
            return
        phase["value"] = state.phase
        tracker.set_phase(state.phase)
        window.set_break(state)
        active = state.phase in ("joining", "searching", "matched")
        if state.phase in ("searching", "matched"):
            reached["value"] = True
        # A join that failed (joining -> none) is not a break: no cooldown.
        apply(
            controller.set_break_active(
                active, datetime.now(UTC), cooldown=reached["value"]
            )
        )
        if not active:
            reached["value"] = False

    def send(bucket: RemainingTimeBucket | None) -> None:
        me = me_ready()
        if me is None or me.consent is None:
            return
        if phase["value"] in ("joining", "searching", "matched"):
            return
        remote.join_queue(
            build_presence(
                me.consent,
                task_category="other",
                stack_tags=controller.stack_tags,
                remaining_time_bucket=bucket or "unknown",
            )
        )

    remote.state_changed.connect(on_state)
    remote.me_changed.connect(on_me)
    remote.break_changed.connect(on_break)
    window.find_requested.connect(lambda: send("unknown"))
    window.cancel_requested.connect(remote.leave_queue)
    window.end_requested.connect(remote.end_match)
    window.block_requested.connect(remote.block_peer)
    window.dismiss_requested.connect(remote.dismiss_break)
    on_state(remote.state)
    return send


def wire_remote(
    window: CompanionWindow,
    worker: RemoteWorker,
    encrypted: bool,
    controller: Controller | None = None,
) -> None:
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
        dialog = OnboardingDialog(
            worker,
            me,
            window,
            stack_tags=controller.stack_tags if controller else (),
            save_tags=controller.set_stack_tags if controller else None,
        )
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


def setup_remote(
    window: CompanionWindow, controller: Controller | None = None
) -> RemoteWorker | None:
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
    wire_remote(window, worker, store.encrypted, controller)
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
    tracker = BreakTracker()
    worker = IpcWorker(
        sock, initial_status(controller), leave_queue=tracker.leave_queue
    )
    # Built before the IPC socket exists: once the socket is up, clients (and
    # the signal-shutdown test) expect the process to be ready to quit.
    # Python runs signal handlers only when the interpreter has control; the
    # 1 s tick timer returns to Python, so Ctrl+C / SIGTERM quit within a tick.
    # Installed before the IPC socket appears (clients may signal as soon as it
    # does) and quitting through the event queue, so a signal that arrives
    # before exec() starts is not lost.
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: QTimer.singleShot(0, app.quit))
    remote = setup_remote(window, controller)
    on_accepted = wire_break(
        controller,
        window,
        remote,
        tracker,
        lambda: worker.set_status(initial_status(controller, tracker.queue_state())),
    )
    wire(
        controller,
        window,
        worker,
        queue=tracker.queue_state,
        on_accepted=on_accepted,
    )
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

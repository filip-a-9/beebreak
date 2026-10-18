"""Cyclic garbage collection on the GUI thread only.

Python's cyclic GC runs on whichever thread happens to cross an allocation
threshold. When a QObject sits in a reference cycle (a websockets exception
traceback can pin a worker's whole stack, `self` included), a worker thread
would destroy it, off the thread that owns it, which can crash Qt. So automatic
collection is switched off and a timer on the GUI thread collects instead.

The youngest generation is collected on every tick: its count is net
allocations, so a burst a worker allocated and freed again leaves it under the
threshold while the cycles from that burst wait. The older generations count
collections of the younger one and run once past their thresholds, as
automatic collection would.
"""

import gc

from PySide6.QtCore import QCoreApplication, QTimer

from beebreak_companion.config import gc_interval_s


def _collect_due() -> None:
    counts = gc.get_count()
    thresholds = gc.get_threshold()
    for generation in (2, 1):  # the oldest due one also sweeps the younger
        if counts[generation] > thresholds[generation]:
            gc.collect(generation)
            return
    gc.collect(0)


def collect_on_gui_thread(app: QCoreApplication) -> QTimer:
    """Disable automatic GC and collect from a timer on the calling thread,
    which must be the GUI thread. Returns the running timer (owned by `app`)."""
    gc.disable()
    timer = QTimer(app)
    timer.timeout.connect(_collect_due)
    timer.start(round(gc_interval_s() * 1000))
    return timer

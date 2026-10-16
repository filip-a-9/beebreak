"""Cyclic garbage is collected on the GUI thread only (remote-thread segfault).

A QObject destroyed off the thread that owns it can crash Qt. The cyclic GC runs
on whichever thread happens to allocate, so a worker thread could free a QObject
the GUI dropped. These tests drop a QObject into a reference cycle, make another
thread allocate enough to trigger automatic collection, and record which thread
frees it.
"""

import gc
import threading
import weakref
from collections.abc import Iterator

import pytest
from PySide6.QtCore import QCoreApplication, QObject, QTimer
from pytestqt.qtbot import QtBot

from breakroom_companion.gc_policy import collect_on_gui_thread

WAIT_MS = 5000


class Cyclic(QObject):
    def __init__(self) -> None:
        super().__init__()
        self.me = self


def _allocate_on_worker_thread() -> None:
    """Allocate far past every automatic-collection threshold (700/10/10)."""

    def churn() -> None:
        keep: list[list[int]] = [[] for _ in range(200_000)]
        del keep

    worker = threading.Thread(target=churn, name="allocator")
    worker.start()
    worker.join()


def _drop_cyclic_qobject(freed_on: list[str], *, old: bool = False) -> None:
    obj = Cyclic()
    # Runs on the thread that frees the object.
    weakref.finalize(obj, lambda: freed_on.append(threading.current_thread().name))
    if old:
        gc.collect()  # survives a full collection: now in the oldest generation
    del obj


@pytest.fixture
def policy(qapp: QCoreApplication, monkeypatch: pytest.MonkeyPatch) -> Iterator[QTimer]:
    # Short ticks: the oldest generation is due only after 100 of them.
    monkeypatch.setenv("BREAKROOM_GC_INTERVAL_S", "0.001")
    was_enabled = gc.isenabled()
    gc.enable()
    timer = collect_on_gui_thread(qapp)
    yield timer
    timer.stop()
    timer.deleteLater()
    if was_enabled:
        gc.enable()
    else:
        gc.disable()


def test_a_cyclic_qobject_is_freed_on_the_gui_thread_not_an_allocating_worker(
    qtbot: QtBot, policy: QTimer
) -> None:
    freed_on: list[str] = []
    _drop_cyclic_qobject(freed_on)
    _allocate_on_worker_thread()
    qtbot.waitUntil(lambda: freed_on != [], timeout=WAIT_MS)
    assert freed_on == [threading.main_thread().name]


def test_a_long_lived_qobject_dropped_into_a_cycle_is_still_freed(
    qtbot: QtBot, policy: QTimer
) -> None:
    """The case behind the crash: a worker that lived for the whole session."""
    freed_on: list[str] = []
    _drop_cyclic_qobject(freed_on, old=True)
    _allocate_on_worker_thread()
    qtbot.waitUntil(lambda: freed_on != [], timeout=WAIT_MS)
    assert freed_on == [threading.main_thread().name]

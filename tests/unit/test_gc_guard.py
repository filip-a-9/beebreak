"""The suite-wide GC guard in tests/conftest.py (rule 13, see gc_policy).

Automatic cyclic collection stays off during every test, so a worker thread
that allocates cannot free a test's QObject cycle off the GUI thread; the main
thread collects it afterwards. These tests do not enable the collector
themselves: they rely on the autouse fixture and fail if it stops guarding.
"""

import gc
import threading
import weakref

from PySide6.QtCore import QObject


class Cyclic(QObject):
    def __init__(self) -> None:
        super().__init__()
        self.me = self


def _churn_on_worker_thread() -> None:
    def churn() -> None:
        keep: list[list[int]] = [[] for _ in range(200_000)]
        del keep

    worker = threading.Thread(target=churn, name="allocator")
    worker.start()
    worker.join()


def test_automatic_collection_is_off_during_tests() -> None:
    assert not gc.isenabled()


def test_a_worker_thread_cannot_free_a_qobject_cycle(qapp: object) -> None:
    freed_on: list[str] = []
    obj = Cyclic()
    weakref.finalize(obj, lambda: freed_on.append(threading.current_thread().name))
    del obj

    _churn_on_worker_thread()
    assert freed_on == []  # the worker allocated past every threshold

    gc.collect()  # what the fixture does after each test, on the main thread
    assert freed_on == [threading.main_thread().name]

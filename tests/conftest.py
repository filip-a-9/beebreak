import gc
import os
from collections.abc import Iterator

import pytest

# Qt tests run headless; the real window is checked manually under WSLg.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(autouse=True)
def _isolate_beebreak_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Tests see defaults even in a shell set up with BEEBREAK_* demo values."""
    for name in [n for n in os.environ if n.startswith("BEEBREAK_")]:
        monkeypatch.delenv(name)
    yield


@pytest.fixture(autouse=True, scope="session")
def _freeze_imported_objects() -> Iterator[None]:
    """Move everything alive at session start (imports of Qt, SQLAlchemy,
    FastAPI, ...) out of the collector's sight. The per-test full collect then
    walks only what the tests created, so its cost no longer grows with the
    size of the imported libraries. Frozen objects are never freed, which is
    fine for modules that live for the whole run."""
    gc.collect()
    gc.freeze()
    yield
    gc.unfreeze()


@pytest.fixture(autouse=True)
def _collect_cycles_on_main_thread() -> Iterator[None]:
    """The companion's GC policy for the suite (see gc_policy): no automatic
    collection a worker or server thread could run, which would free a test's
    QObjects off the GUI thread; the main thread collects after each test."""
    gc.disable()
    yield
    gc.collect()

import os
from collections.abc import Iterator

import pytest

# Qt tests run headless; the real window is checked manually under WSLg.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(autouse=True)
def _isolate_breakroom_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Tests see defaults even in a shell set up with BREAKROOM_* demo values."""
    for name in [n for n in os.environ if n.startswith("BREAKROOM_")]:
        monkeypatch.delenv(name)
    yield

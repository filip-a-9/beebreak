"""The real start-up path themes the QApplication before any window exists."""

import os
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

PROBE = """
import os
from PySide6.QtWidgets import QApplication
from beebreak_companion import app

def fake_exec():
    a = QApplication.instance()
    print(a.palette().window().color().name(), a.font().family(), flush=True)
    os._exit(0)

QApplication.exec = staticmethod(fake_exec)
app.main()
"""


@pytest.fixture
def runtime() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(dir="/tmp") as d:
        yield Path(d)


def test_start_applies_the_configured_theme(runtime: Path) -> None:
    env = {
        **os.environ,
        "XDG_RUNTIME_DIR": str(runtime),
        "XDG_STATE_HOME": str(runtime / "state"),
        "BEEBREAK_THEME": "dark",
    }
    done = subprocess.run(
        [sys.executable, "-c", PROBE],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert done.stdout.split() == ["#1e1a16", "Nunito"]

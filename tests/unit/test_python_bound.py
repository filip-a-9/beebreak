"""The install is pinned to the one Python that CI tests (`.python-version`)."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest
from packaging.specifiers import SpecifierSet

ROOT = Path(__file__).resolve().parents[2]
PACKAGES = [
    "packages/shared",
    "packages/agent_bridge",
    "apps/companion",
    "apps/server",
]
DOCS = ["README.md", "docs/M6_CHECKLIST.md", "docs/ARCHITECTURE.md"]


@pytest.mark.parametrize("package", PACKAGES)
def test_requires_python_includes_312_and_excludes_313(package: str) -> None:
    data = tomllib.loads((ROOT / package / "pyproject.toml").read_text())
    spec = SpecifierSet(data["project"]["requires-python"])
    assert "3.12.0" in spec
    assert "3.12.99" in spec
    assert "3.13.0" not in spec
    assert "3.15.0" not in spec


@pytest.mark.parametrize("doc", DOCS)
def test_every_documented_tool_install_pins_python(doc: str) -> None:
    text = (ROOT / doc).read_text()
    # A command runs to the end of its (possibly backslash-continued) line.
    commands = re.findall(r"uv tool install(?:[^\n]*\\\n)*[^\n]*", text)
    assert commands, f"{doc} no longer shows a uv tool install command"
    for command in commands:
        assert "--python 3.12" in command

"""Rule 13 (M3 README): no companion code path may put OutboundPresence on the wire.

Static check over every module of `breakroom_companion`. A module that imports
`httpx` or `websockets` (the only network libraries) must not mention
`OutboundPresence` or `build_presence` in any form. `privacy_preview.py` (the
"What this app shares" page) is the one module that builds presence for
display; it must not import either network library. It may be absent.
"""

import ast
from pathlib import Path

import pytest

import breakroom_companion

NETWORK_LIBS = frozenset({"httpx", "websockets"})
PRESENCE_NAMES = frozenset({"OutboundPresence", "build_presence"})
PREVIEW_MODULE = "privacy_preview.py"
DYNAMIC = "<dynamic>"


def _package_files() -> list[Path]:
    root = Path(breakroom_companion.__file__).parent
    return sorted(root.rglob("*.py"))


def imported_libs(tree: ast.AST) -> set[str]:
    """Top-level package names imported anywhere in the module (also lazily)."""
    libs: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            libs.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            libs.add(node.module.split(".")[0])
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "__import__"
        ) or (isinstance(node, ast.Attribute) and node.attr == "import_module"):
            libs.add(DYNAMIC)  # a dynamic import could hide a network library
    return libs


def presence_references(tree: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in PRESENCE_NAMES:
            found.add(node.id)
        elif isinstance(node, ast.Attribute) and node.attr in PRESENCE_NAMES:
            found.add(node.attr)
        elif isinstance(node, ast.alias) and node.name.split(".")[-1] in PRESENCE_NAMES:
            found.add(node.name)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.update(n for n in PRESENCE_NAMES if n in node.value)
    return found


def violations(name: str, source: str) -> list[str]:
    tree = ast.parse(source)
    network = imported_libs(tree) & (NETWORK_LIBS | {DYNAMIC})
    if name == PREVIEW_MODULE:
        return [f"{name} imports {sorted(network)}"] if network else []
    refs = presence_references(tree)
    if network and refs:
        return [f"{name} imports {sorted(network)} and references {sorted(refs)}"]
    return []


def test_walk_covers_the_modules_that_use_the_network() -> None:
    # Guards against the walk silently covering nothing.
    names = {p.name for p in _package_files()}
    assert {"api_client.py", "realtime_client.py", "remote.py"} <= names


def test_no_network_module_references_presence() -> None:
    problems: list[str] = []
    for path in _package_files():
        problems += violations(path.name, path.read_text(encoding="utf-8"))
    assert problems == []


@pytest.mark.parametrize(
    "source",
    [
        "import httpx\nfrom breakroom_shared.presence import OutboundPresence\n",
        "from websockets.asyncio.client import connect\nx = build_presence\n",
        "import httpx\nimport breakroom_shared.presence as p\np.build_presence()\n",
        "import httpx\nx = 'OutboundPresence'\n",
        "from httpx import AsyncClient\ndef f(p: 'OutboundPresence'): ...\n",
    ],
)
def test_checker_flags_a_network_module_that_touches_presence(source: str) -> None:
    assert violations("sender.py", source) != []


@pytest.mark.parametrize(
    "source",
    [
        "import httpx\nx = 1\n",
        "from breakroom_shared.presence import build_presence\n",
    ],
)
def test_checker_allows_each_concern_alone(source: str) -> None:
    assert violations("ok.py", source) == []


@pytest.mark.parametrize(
    "source",
    [
        "import httpx\n",
        "import websockets.asyncio.client\n",
        "def f():\n    from httpx import get\n",
        "import importlib\nimportlib.import_module('ht' + 'tpx')\n",
    ],
)
def test_checker_flags_a_preview_module_that_imports_the_network(source: str) -> None:
    assert violations(PREVIEW_MODULE, source) != []

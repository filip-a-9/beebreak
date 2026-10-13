"""Rule 13 (M4 README): task metadata leaves the companion by exactly one path.

Static check over every module of `breakroom_companion`:

* `build_presence` is referenced only by `app.py` (called only in `wire_break`,
  the one sender) and `privacy_preview.py` (display only).
* `OutboundPresence` is referenced only by `remote.py`.
* `QueueJoinRequest` is referenced only by `remote.py` (built only in
  `RemoteWorker._join_queue`) and `api_client.py` (the HTTP call).
* `join_queue` is called only by `wire_break` (the UI hands the presence to the
  worker) and by `_join_queue` (the worker hands the request to the API client).
* `privacy_preview.py` imports neither `httpx` nor `websockets`, and no module
  that talks to the network through the realtime socket mentions presence.
"""

import ast
from pathlib import Path

import pytest

import breakroom_companion

NETWORK_LIBS = frozenset({"httpx", "websockets"})
DYNAMIC = "<dynamic>"
PREVIEW_MODULE = "privacy_preview.py"

# name -> modules that may reference it at all.
REFERENCE_ALLOWED: dict[str, frozenset[str]] = {
    "build_presence": frozenset({"app.py", PREVIEW_MODULE}),
    "OutboundPresence": frozenset({"remote.py"}),
    "QueueJoinRequest": frozenset({"remote.py", "api_client.py"}),
}
# (name, call kind) -> {(module, outermost enclosing function)} that may call it.
CALL_ALLOWED: dict[str, frozenset[tuple[str, str]]] = {
    "build_presence": frozenset(
        {("app.py", "wire_break"), (PREVIEW_MODULE, "example_json")}
    ),
    "QueueJoinRequest": frozenset({("remote.py", "_join_queue")}),
    "join_queue": frozenset({("app.py", "wire_break"), ("remote.py", "_join_queue")}),
}
CALL_NAMES = frozenset(CALL_ALLOWED)
ALL_NAMES = frozenset(REFERENCE_ALLOWED) | CALL_NAMES


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


def _name_of(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def references(tree: ast.AST, names: frozenset[str]) -> set[str]:
    """Names mentioned anywhere: identifiers, attributes, imports, strings."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name | ast.Attribute):
            name = _name_of(node)
            if name in names:
                found.add(name)
        elif isinstance(node, ast.alias):
            last = node.name.split(".")[-1]
            if last in names:
                found.add(last)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.update(n for n in names if n in node.value)
    return found


def call_sites(tree: ast.Module, names: frozenset[str]) -> set[tuple[str, str]]:
    """(name, outermost enclosing function or '<module>') of every call."""
    sites: set[tuple[str, str]] = set()

    def visit(node: ast.AST, outer: str) -> None:
        for child in ast.iter_child_nodes(node):
            where = outer
            if outer == "<module>" and isinstance(
                child, ast.FunctionDef | ast.AsyncFunctionDef
            ):
                where = child.name
            if isinstance(child, ast.Call):
                name = _name_of(child.func)
                if name in names:
                    sites.add((name, where))
            visit(child, where)

    visit(tree, "<module>")
    return sites


def violations(name: str, source: str) -> list[str]:
    tree = ast.parse(source)
    problems: list[str] = []
    if name == PREVIEW_MODULE:
        network = imported_libs(tree) & (NETWORK_LIBS | {DYNAMIC})
        if network:
            problems.append(f"{name} imports {sorted(network)}")
    mentioned = references(tree, ALL_NAMES)
    for ref, modules in REFERENCE_ALLOWED.items():
        if ref in mentioned and name not in modules:
            problems.append(f"{name} references {ref}")
    for ref, where in sorted(call_sites(tree, CALL_NAMES)):
        if (name, where) not in CALL_ALLOWED[ref]:
            problems.append(f"{name} calls {ref} in {where}")
    return problems


def test_walk_covers_the_modules_that_matter() -> None:
    # Guards against the walk silently covering nothing.
    names = {p.name for p in _package_files()}
    assert {
        "api_client.py",
        "realtime_client.py",
        "remote.py",
        "app.py",
        PREVIEW_MODULE,
    } <= names


def test_presence_reaches_the_network_only_through_the_one_path() -> None:
    problems: list[str] = []
    for path in _package_files():
        problems += violations(path.name, path.read_text(encoding="utf-8"))
    assert problems == []


def test_the_one_path_is_really_there() -> None:
    """The pin is not vacuous: each allowed call site exists."""
    found: set[tuple[str, str, str]] = set()
    for path in _package_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found |= {(path.name, n, w) for n, w in call_sites(tree, CALL_NAMES)}
    for name, sites in CALL_ALLOWED.items():
        for module, where in sites:
            assert (module, name, where) in found


def test_the_realtime_client_never_mentions_presence() -> None:
    tree = ast.parse(
        (Path(breakroom_companion.__file__).parent / "realtime_client.py").read_text(
            encoding="utf-8"
        )
    )
    assert references(tree, ALL_NAMES) == set()


@pytest.mark.parametrize(
    ("name", "source"),
    [
        ("sender.py", "from breakroom_shared.presence import build_presence\n"),
        ("remote.py", "x = build_presence()\n"),
        ("app.py", "def other():\n    return build_presence()\n"),
        (
            "app.py",
            "def wire_break():\n    def f():\n        x = build_presence()\n"
            "def sneaky():\n    build_presence()\n",
        ),
        ("api_client.py", "x = OutboundPresence\n"),
        ("window.py", "def f(p: 'OutboundPresence'): ...\n"),
        ("window.py", "x = 'QueueJoinRequest'\n"),
        ("remote.py", "def send(w):\n    w.join_queue(1)\n"),
        ("worker.py", "def f(r):\n    r.join_queue(1)\n"),
        ("remote.py", "def other():\n    QueueJoinRequest()\n"),
        ("api_client.py", "def f():\n    QueueJoinRequest()\n"),
        ("privacy_preview.py", "import httpx\n"),
        ("privacy_preview.py", "import websockets.asyncio.client\n"),
        ("privacy_preview.py", "def f():\n    from httpx import get\n"),
        (
            "privacy_preview.py",
            "import importlib\nimportlib.import_module('ht'+'tpx')\n",
        ),
        ("privacy_preview.py", "def g(r):\n    r.join_queue(1)\n"),
    ],
)
def test_checker_flags_each_violation(name: str, source: str) -> None:
    assert violations(name, source) != []


@pytest.mark.parametrize(
    ("name", "source"),
    [
        (
            "app.py",
            "def wire_break():\n    def send():\n        build_presence()\n"
            "        remote.join_queue(1)\n",
        ),
        (
            "remote.py",
            "async def _join_queue():\n    QueueJoinRequest()\n    api.join_queue()\n",
        ),
        ("remote.py", "from breakroom_shared.presence import OutboundPresence\n"),
        ("api_client.py", "from breakroom_shared.api import QueueJoinRequest\n"),
        ("privacy_preview.py", "def example_json():\n    build_presence()\n"),
        ("window.py", "import httpx\n"),
    ],
)
def test_checker_allows_the_one_path(name: str, source: str) -> None:
    assert violations(name, source) == []

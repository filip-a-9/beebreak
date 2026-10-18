"""The plugin's hook and MCP launchers, run through a real shell (M6-02).

Claude Code runs a hook `command` with `sh -c` and an MCP server as `command`
plus `args`, with ${CLAUDE_PLUGIN_ROOT} substituted. A missing binary must not
make a hook fail (Claude Code shows a notice for any non-zero exit, 127 included).
"""

import json
import os
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
PLUGIN = ROOT / "integrations" / "claude-code"
MARKETPLACE = ROOT / ".claude-plugin" / "marketplace.json"


def hook_command() -> str:
    config = json.loads((PLUGIN / "hooks" / "hooks.json").read_text())
    commands = {
        hook["command"]
        for groups in config["hooks"].values()
        for group in groups
        for hook in group["hooks"]
    }
    assert len(commands) == 1
    return str(commands.pop())


def mcp_launcher() -> list[str]:
    manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
    server = manifest["mcpServers"]["beebreak"]
    return [server["command"], *server["args"]]


@pytest.fixture
def sandbox() -> Iterator[tuple[Path, Path]]:
    """(fake HOME, fake plugin root two levels below a fake repo root)."""
    with tempfile.TemporaryDirectory(dir="/tmp") as d:
        home = Path(d) / "home"
        (home / ".local" / "bin").mkdir(parents=True)
        root = Path(d) / "cache" / "a" / "plugin"
        root.mkdir(parents=True)
        yield home, root


def run(
    argv: list[str], home: Path, root: Path, stdin: str
) -> subprocess.CompletedProcess[str]:
    # A minimal PATH: sh and coreutils, but none of the project's binaries.
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin"}
    substituted = [a.replace("${CLAUDE_PLUGIN_ROOT}", str(root)) for a in argv]
    return subprocess.run(
        substituted, input=stdin, capture_output=True, text=True, env=env, timeout=10
    )


def fake_binary(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)


def test_missing_hook_binary_is_silent_success(sandbox: tuple[Path, Path]) -> None:
    home, root = sandbox
    done = run(["sh", "-c", hook_command()], home, root, "payload")
    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")


def test_hook_runs_the_binary_from_local_bin_with_stdin(
    sandbox: tuple[Path, Path],
) -> None:
    home, root = sandbox
    out = home / "seen"
    fake_binary(home / ".local" / "bin", "beebreak-hook", f"cat > {out}")
    done = run(["sh", "-c", hook_command()], home, root, "payload")
    assert done.returncode == 0
    assert out.read_text() == "payload"


def test_hook_prefers_the_repo_checkout_venv(sandbox: tuple[Path, Path]) -> None:
    home, root = sandbox
    # A checkout loads the plugin from <repo>/integrations/claude-code.
    repo_plugin = root.parents[1] / "integrations" / "claude-code"
    repo_plugin.mkdir(parents=True)
    venv_bin = repo_plugin.parents[1] / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    out = home / "seen"
    fake_binary(venv_bin, "beebreak-hook", f"echo venv > {out}")
    fake_binary(home / ".local" / "bin", "beebreak-hook", f"echo tool > {out}")
    run(["sh", "-c", hook_command()], home, repo_plugin, "")
    assert out.read_text().strip() == "venv"


def test_hook_exit_code_of_a_present_binary_is_not_masked(
    sandbox: tuple[Path, Path],
) -> None:
    home, root = sandbox
    fake_binary(home / ".local" / "bin", "beebreak-hook", "exit 7")
    assert run(["sh", "-c", hook_command()], home, root, "").returncode == 7


def test_mcp_launcher_runs_the_installed_binary(sandbox: tuple[Path, Path]) -> None:
    home, root = sandbox
    out = home / "seen"
    fake_binary(home / ".local" / "bin", "beebreak-mcp", f"cat > {out}")
    done = run(mcp_launcher(), home, root, "rpc")
    assert done.returncode == 0
    assert out.read_text() == "rpc"


def test_marketplace_lists_the_plugin_at_an_existing_relative_path() -> None:
    market = json.loads(MARKETPLACE.read_text())
    manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
    (entry,) = market["plugins"]
    assert entry["name"] == manifest["name"]
    assert entry["source"].startswith("./")
    assert ".." not in entry["source"]
    assert (ROOT / entry["source"] / ".claude-plugin" / "plugin.json").is_file()
    assert market["name"] and market["owner"]["name"]


def test_release_wheels_expose_the_three_entry_points() -> None:
    # The scripts the launchers and README rely on, declared where uv looks.
    import tomllib

    declared = set()
    for pyproject in (
        ROOT / "packages" / "agent_bridge" / "pyproject.toml",
        ROOT / "apps" / "companion" / "pyproject.toml",
    ):
        declared |= set(tomllib.loads(pyproject.read_text())["project"]["scripts"])
    assert declared == {"beebreak-companion", "beebreak-hook", "beebreak-mcp"}
    assert os.access(ROOT / "scripts" / "build_release.sh", os.X_OK)

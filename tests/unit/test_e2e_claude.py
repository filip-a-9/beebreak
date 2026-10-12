import subprocess
import sys
import time
from pathlib import Path

import pytest

from tools.e2e_claude import MAX_BUDGET_USD, claude_command, claude_env

TOOL = Path(__file__).parents[2] / "tools" / "e2e_claude.py"


def test_budget_above_ceiling_is_refused_before_anything_starts() -> None:
    start = time.monotonic()
    proc = subprocess.run(
        [sys.executable, str(TOOL), "--budget-usd", str(MAX_BUDGET_USD + 0.01)],
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 2
    assert time.monotonic() - start < 5  # no companion or claude was launched


@pytest.mark.parametrize("budget", ["0", "-1", "abc"])
def test_non_positive_or_invalid_budget_is_refused(budget: str) -> None:
    proc = subprocess.run(
        [sys.executable, str(TOOL), "--budget-usd", budget],
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 2


def test_env_strips_every_api_billing_variable() -> None:
    base = {
        "PATH": "/usr/bin",
        "ANTHROPIC_API_KEY": "k",
        "ANTHROPIC_AUTH_TOKEN": "t",
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "CLAUDE_CODE_USE_VERTEX": "1",
    }
    env = claude_env(base, runtime=Path("/tmp/rt"))
    assert env["PATH"] == "/usr/bin"
    assert env["XDG_RUNTIME_DIR"] == "/tmp/rt"
    for name in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
    ):
        assert name not in env


def test_claude_command_always_carries_the_spend_cap() -> None:
    cmd = claude_command("do it", budget_usd=0.4, model="haiku")
    assert cmd[cmd.index("--max-budget-usd") + 1] == "0.4"
    assert cmd[cmd.index("--model") + 1] == "haiku"
    assert "-p" in cmd
    plugin_dir = Path(cmd[cmd.index("--plugin-dir") + 1])
    assert (plugin_dir / ".claude-plugin" / "plugin.json").is_file()

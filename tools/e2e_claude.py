"""Real end-to-end run: headless Claude Code + plugin + companion (dev tool).

Starts the companion offscreen in a private runtime dir, runs one `claude -p`
session with the plugin loaded, watches the companion's status while Claude
works, and prints one JSON object. Exit 0 only if exactly one invitation was
raised and everything shut down cleanly.

Spend: runs on the developer's claude.ai login, never API billing (API-key and
cloud-provider variables are stripped). Every run carries --max-budget-usd,
capped at MAX_BUDGET_USD, plus a wall-clock timeout.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "integrations" / "claude-code"
COMPANION = ROOT / ".venv" / "bin" / "breakroom-companion"

# Filip's per-invocation ceiling (2026-10-12); lower it with --budget-usd.
MAX_BUDGET_USD = 1.00
# Variables that would move a run from the claude.ai plan to API billing.
BILLING_VARS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
)
# Two 18 s calls qualify whether the model runs them in parallel (Haiku does)
# or in sequence: each call's own gap stays under the 20 s quiet gap, and the
# span from the prompt exceeds the 15 s demo threshold. A single call longer
# than the quiet gap would not qualify (see follow-up F3).
PROMPT = (
    "Run the Bash command `sleep 18` twice, as two separate Bash tool calls. "
    "Do nothing else and reply only 'done'."
)
COMPANION_ENV = {
    "BREAKROOM_ACTIVITY_THRESHOLD_S": "15",
    "BREAKROOM_MIN_TOOL_EVENTS": "2",
    "QT_QPA_PLATFORM": "offscreen",
}
POLL_S = 0.25


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def claude_env(base: Mapping[str, str], *, runtime: Path) -> dict[str, str]:
    env = {k: v for k, v in base.items() if k not in BILLING_VARS}
    env["XDG_RUNTIME_DIR"] = str(runtime)
    return env


def claude_command(prompt: str, *, budget_usd: float, model: str) -> list[str]:
    return [
        "claude",
        "-p",
        prompt,
        "--plugin-dir",
        str(PLUGIN_DIR),
        "--model",
        model,
        "--max-budget-usd",
        str(budget_usd),
        "--output-format",
        "json",
        "--allowedTools",
        "Bash(sleep:*)",
    ]


def _budget(value: str) -> float:
    try:
        budget = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("not a number") from None
    if not 0 < budget <= MAX_BUDGET_USD:
        raise argparse.ArgumentTypeError(f"must be in (0, {MAX_BUDGET_USD}]")
    return budget


class StatusWatcher(threading.Thread):
    """Polls the companion and records invitation transitions."""

    def __init__(self, socket_path: Path) -> None:
        super().__init__(daemon=True)
        self._socket_path = socket_path
        self._halt = threading.Event()
        self.suggestions = 0
        self.event_times: set[str] = set()

    def run(self) -> None:
        from breakroom_agent_bridge.ipc_client import request_status

        previous = None
        while not self._halt.is_set():
            reply = request_status(socket_path=self._socket_path)
            if reply is not None:
                if reply.invitation == "suggested" and previous != "suggested":
                    self.suggestions += 1
                if reply.last_event_at is not None:
                    self.event_times.add(reply.last_event_at.isoformat())
                previous = reply.invitation
            time.sleep(POLL_S)

    def stop(self) -> None:
        self._halt.set()
        self.join(timeout=5)


def _wait_for_socket(sock: Path, timeout_s: float) -> bool:
    from breakroom_agent_bridge.ipc_client import request_status

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if request_status(socket_path=sock) is not None:
            return True
        time.sleep(0.05)
    return False


def run(budget_usd: float, model: str, timeout_s: float) -> dict[str, object]:
    with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
        runtime = Path(tmp)
        sock = runtime / "breakroom" / "companion.sock"
        env = claude_env(os.environ, runtime=runtime)
        companion = subprocess.Popen(
            [str(COMPANION)],
            env={**env, **COMPANION_ENV},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        result: dict[str, object] = {"budget_usd": budget_usd, "model": model}
        try:
            if not _wait_for_socket(sock, 10):
                result["error"] = "companion did not start"
                return result
            watcher = StatusWatcher(sock)
            watcher.start()
            work = Path(tmp) / "work"  # empty cwd: Claude has nothing to touch
            work.mkdir()
            try:
                proc = subprocess.run(
                    claude_command(PROMPT, budget_usd=budget_usd, model=model),
                    cwd=work,
                    env=env,
                    capture_output=True,
                    timeout=timeout_s,
                    check=False,
                )
                result["claude_exit"] = proc.returncode
                with contextlib.suppress(ValueError):
                    out = json.loads(proc.stdout)
                    result["cost_usd"] = out.get("total_cost_usd")
                    result["num_turns"] = out.get("num_turns")
                    result["claude_result"] = out.get("subtype")
            except subprocess.TimeoutExpired:
                result["error"] = f"claude exceeded {timeout_s}s"
            time.sleep(1)  # let the last async hooks land
            watcher.stop()
            result["suggestions"] = watcher.suggestions
            result["distinct_event_times"] = len(watcher.event_times)
        finally:
            companion.send_signal(signal.SIGINT)
            try:
                result["companion_exit"] = companion.wait(timeout=5)
            except subprocess.TimeoutExpired:
                companion.kill()
                result["companion_exit"] = "killed"
            result["socket_removed"] = not sock.exists()
    result["passed"] = (
        "error" not in result
        and result.get("claude_exit") == 0
        and result.get("suggestions") == 1
        and result.get("companion_exit") == 0
        and result.get("socket_removed") is True
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--budget-usd",
        type=_budget,
        default=_budget(os.environ.get("BREAKROOM_E2E_BUDGET_USD", "1.00")),
    )
    parser.add_argument(
        "--model", default=os.environ.get("BREAKROOM_E2E_MODEL", "haiku")
    )
    parser.add_argument(
        "--timeout-s", type=float, default=_env_float("BREAKROOM_E2E_TIMEOUT_S", 240)
    )
    args = parser.parse_args()
    result = run(args.budget_usd, args.model, args.timeout_s)
    print(json.dumps(result))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())

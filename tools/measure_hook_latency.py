"""Measure wall-clock latency of the Breakroom hook command (dev tool).

Runs the hook command N times with a fixed synthetic payload (no real data)
on stdin and prints one JSON object. A run that exceeds the per-run timeout is
killed and counted as a failure (it is included in ``nonzero_exits``).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Synthetic payload: field names match a real hook event, values are inert.
SYNTHETIC_PAYLOAD = json.dumps(
    {
        "hook_event_name": "PreToolUse",
        "session_id": "synthetic-session-0000",
        "tool_name": "Bash",
    }
).encode()


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def default_command() -> str:
    python = ROOT / ".venv" / "bin" / "python"
    return f"{shlex.quote(str(python))} -m breakroom_agent_bridge.hook"


def percentile(sorted_values: list[float], fraction: float) -> float:
    """Nearest-rank percentile of an ascending list."""
    rank = max(1, math.ceil(fraction * len(sorted_values)))
    return sorted_values[rank - 1]


def run_once(argv: list[str], timeout_s: float) -> tuple[float, bool, int]:
    """Return (elapsed_ms, failed, stdout_bytes) for one invocation."""
    started = time.perf_counter()
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        out, _ = proc.communicate(SYNTHETIC_PAYLOAD, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        return (time.perf_counter() - started) * 1000, True, 0
    elapsed = (time.perf_counter() - started) * 1000
    return elapsed, proc.returncode != 0, len(out)


def measure(command: str, runs: int, timeout_s: float) -> dict[str, float | int]:
    argv = shlex.split(command)
    times: list[float] = []
    failures = 0
    stdout_bytes = 0
    for _ in range(runs):
        elapsed, failed, nbytes = run_once(argv, timeout_s)
        times.append(elapsed)
        failures += failed
        stdout_bytes += nbytes
    times.sort()
    return {
        "runs": runs,
        "p50_ms": round(percentile(times, 0.50), 2),
        "p95_ms": round(percentile(times, 0.95), 2),
        "max_ms": round(times[-1], 2),
        "nonzero_exits": failures,
        "stdout_bytes": stdout_bytes,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runs", type=int, default=_env_int("BREAKROOM_LATENCY_RUNS", 50)
    )
    parser.add_argument(
        "--timeout-s",
        type=float,
        default=_env_float("BREAKROOM_LATENCY_TIMEOUT_S", 5.0),
    )
    parser.add_argument("--command", default=default_command())
    args = parser.parse_args()
    if args.runs < 1 or args.timeout_s <= 0:
        parser.error("--runs and --timeout-s must be positive")
    print(json.dumps(measure(args.command, args.runs, args.timeout_s)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

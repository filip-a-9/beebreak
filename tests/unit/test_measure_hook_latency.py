import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "measure_hook_latency.py"
KEYS = {"runs", "p50_ms", "p95_ms", "max_ms", "nonzero_exits", "stdout_bytes"}


def run_tool(
    command: str, runs: int, timeout_s: float = 5.0
) -> tuple[subprocess.CompletedProcess[str], float]:
    started = time.monotonic()
    proc = subprocess.run(
        [
            sys.executable,
            str(TOOL),
            "--runs",
            str(runs),
            "--timeout-s",
            str(timeout_s),
            "--command",
            command,
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return proc, time.monotonic() - started


def test_trivial_command_reports_ordered_percentiles() -> None:
    cmd = f'{sys.executable} -c "import sys; sys.stdin.read()"'
    proc, _ = run_tool(cmd, runs=5)
    result = json.loads(proc.stdout)
    assert result.keys() >= KEYS
    assert result["runs"] == 5
    assert result["nonzero_exits"] == 0
    assert result["stdout_bytes"] == 0
    assert 0 <= result["p50_ms"] <= result["p95_ms"] <= result["max_ms"]


def test_failing_command_counts_every_run() -> None:
    cmd = f'{sys.executable} -c "import sys; sys.stdin.read(); sys.exit(1)"'
    proc, _ = run_tool(cmd, runs=3)
    assert json.loads(proc.stdout)["nonzero_exits"] == 3


def test_stdout_bytes_are_counted() -> None:
    cmd = f"{sys.executable} -c \"import sys; sys.stdin.read(); print('abcd')\""
    proc, _ = run_tool(cmd, runs=2)
    assert json.loads(proc.stdout)["stdout_bytes"] > 0


def test_hanging_command_times_out_instead_of_hanging() -> None:
    cmd = f'{sys.executable} -c "import time; time.sleep(30)"'
    proc, elapsed = run_tool(cmd, runs=2, timeout_s=0.5)
    result = json.loads(proc.stdout)
    assert result["nonzero_exits"] == 2
    assert elapsed < 20

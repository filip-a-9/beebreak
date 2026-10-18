import json
import os
import subprocess
import sys
from pathlib import Path

from beebreak_companion.config import TriggerConfig
from tests.fixtures.traces import synthetic as syn
from tools.replay_trace import load_events, replay

TOOL = Path(__file__).parents[2] / "tools" / "replay_trace.py"
SECRET = "SECRET-LINE-CONTENT-4417"


def _write(path: Path, bad: list[str]) -> None:
    lines = [e.model_dump_json() for e in syn.s02_steady_five_minutes()]
    lines[5:5] = bad
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("BEEBREAK_")}
    return subprocess.run(
        [sys.executable, str(TOOL), *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def test_cli_prints_parseable_summary(tmp_path: Path) -> None:
    trace = tmp_path / "t.jsonl"
    _write(trace, [])
    result = _run(str(trace))
    assert result.returncode == 0
    summary = json.loads(result.stdout.strip().splitlines()[-1])
    assert summary["invitations"] == 1
    assert 60 <= summary["first_invitation_s"] <= 70
    assert set(summary["activity_s"]) == {"unknown", "active", "waiting_input", "idle"}
    assert summary["invalid_lines"] == 0


def test_cli_counts_malformed_line_and_never_echoes_it(tmp_path: Path) -> None:
    trace = tmp_path / "t.jsonl"
    _write(trace, [f'{{"secret": "{SECRET}"', f'{{"type": "{SECRET}"}}'])
    result = _run(str(trace))
    assert result.returncode == 0
    assert SECRET not in result.stdout + result.stderr
    summary = json.loads(result.stdout.strip().splitlines()[-1])
    assert summary["invalid_lines"] == 2
    assert summary["invitations"] == 1


def test_cli_respond_decline_runs(tmp_path: Path) -> None:
    trace = tmp_path / "t.jsonl"
    _write(trace, [])
    result = _run(str(trace), "--respond", "decline")
    assert result.returncode == 0
    assert json.loads(result.stdout.strip().splitlines()[-1])["invitations"] == 1


def test_cli_missing_file_fails_cleanly(tmp_path: Path) -> None:
    result = _run(str(tmp_path / "nope.jsonl"))
    assert result.returncode == 2


def test_cli_honours_env_threshold(tmp_path: Path) -> None:
    trace = tmp_path / "t.jsonl"
    _write(trace, [])
    env = {k: v for k, v in os.environ.items() if not k.startswith("BEEBREAK_")}
    env["BEEBREAK_ACTIVITY_THRESHOLD_S"] = "20"
    result = subprocess.run(
        [sys.executable, str(TOOL), str(trace)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    summary = json.loads(result.stdout.strip().splitlines()[-1])
    assert 20 <= summary["first_invitation_s"] <= 30


def test_load_events_skips_invalid_and_blank() -> None:
    good = syn.s15_escape_cancel()[0].model_dump_json()
    events, invalid = load_events([good, "", "not json", "{}"])
    assert len(events) == 1
    assert invalid == 2


def test_replay_empty_trace() -> None:
    assert replay([], TriggerConfig()) == []


def test_cli_empty_trace_fails(tmp_path: Path) -> None:
    trace = tmp_path / "t.jsonl"
    trace.write_text("", encoding="utf-8")
    result = _run(str(trace))
    assert result.returncode != 0


def test_cli_all_invalid_trace_fails_without_echo(tmp_path: Path) -> None:
    trace = tmp_path / "t.jsonl"
    trace.write_text(f'{{"secret": "{SECRET}"\n{{"type": "{SECRET}"}}\n')
    result = _run(str(trace))
    assert result.returncode != 0
    assert SECRET not in result.stdout + result.stderr


def test_cli_tail_lets_waiting_input_reach_unknown(tmp_path: Path) -> None:
    trace = tmp_path / "t.jsonl"
    trace.write_text(
        "\n".join(e.model_dump_json() for e in syn.s04_waiting_with_open_tool()) + "\n",
        encoding="utf-8",
    )
    result = _run(str(trace))
    assert result.returncode == 0
    assert "activity=unknown" in result.stdout
    summary = json.loads(result.stdout.strip().splitlines()[-1])
    assert summary["activity_s"]["unknown"] > 0

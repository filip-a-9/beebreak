"""M2 regression set: synthetic traces replayed through the Controller.

Expected times are literals from the M2-05 spec table, never computed here.
Offsets are seconds from the trace epoch.
"""

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from beebreak_companion.config import TriggerConfig
from beebreak_shared.events import NormalizedEvent
from tests.fixtures.traces import synthetic as syn
from tools.replay_trace import Respond, Transition, replay

Builder = Callable[[], list[NormalizedEvent]]

ALLOWED_INVITATIONS = {"idle", "suggested", "disabled"}
ALLOWED_ACTIVITY = {"unknown", "active", "waiting_input", "idle"}


def run(
    build: Builder, *, tail_s: float = 240.0, respond: Respond = "none"
) -> list[Transition]:
    return replay(build(), TriggerConfig(), tail_s=tail_s, respond=respond)


def off(t: Transition) -> float:
    return (t.at - syn.EPOCH).total_seconds()


def invites(tr: list[Transition]) -> list[Transition]:
    out: list[Transition] = []
    prev = "idle"
    for t in tr:
        if t.invitation == "suggested" and prev != "suggested":
            out.append(t)
        prev = t.invitation
    return out


def invite_times(tr: list[Transition]) -> list[float]:
    return [off(t) for t in invites(tr)]


def activity_at(tr: list[Transition], seconds: float) -> str | None:
    state = None
    for t in tr:
        if off(t) <= seconds:
            state = t.activity
    return state


def first_activity_change_after(
    tr: list[Transition], seconds: float, activity: str
) -> float | None:
    for t in tr:
        if off(t) > seconds and t.activity == activity:
            return off(t)
    return None


def test_all_states_are_known_values() -> None:
    tr = run(syn.s07_stop_at_fifty)
    assert {t.invitation for t in tr} <= ALLOWED_INVITATIONS
    assert {t.activity for t in tr} <= ALLOWED_ACTIVITY


def test_01_short_task_never_invites() -> None:
    tr = run(syn.s01_short_task)
    assert invite_times(tr) == []
    assert tr[-1].activity == "idle"


def test_02_steady_tools_invite_once() -> None:
    times = invite_times(run(syn.s02_steady_five_minutes))
    assert len(times) == 1
    assert 60 <= times[0] <= 70


def test_03_long_silent_tool_does_not_invite() -> None:
    # Was: one invite at ~60 s. Decision 2026-10-17 dropped the open-tool escape
    # (an Esc on a running tool sends no event): a lone tool, even one running
    # over 10 min, no longer earns an invite. Documented cost.
    assert invite_times(run(syn.s03_long_silent_tool)) == []


def test_04_waiting_input_blocks_and_then_goes_unknown() -> None:
    tr = run(syn.s04_waiting_with_open_tool, tail_s=360)
    assert invite_times(tr) == []
    assert activity_at(tr, 31) == "waiting_input"
    assert activity_at(tr, 329) == "waiting_input"
    changed = first_activity_change_after(tr, 30, "unknown")
    assert changed is not None
    assert 329 <= changed <= 333


def test_05_resume_after_waiting_starts_new_run() -> None:
    times = invite_times(run(syn.s05_waiting_then_new_run))
    assert len(times) == 1
    assert 260 <= times[0] <= 270


def test_06_failed_tool_then_silence_invites_and_goes_unknown() -> None:
    tr = run(syn.s06_failing_tool_then_silence)
    times = invite_times(tr)
    assert len(times) == 1
    assert 59 <= times[0] <= 63
    changed = first_activity_change_after(tr, 100, "unknown")
    assert changed is not None
    assert 185 <= changed <= 189


def test_07_stop_is_idle_and_claims_nothing() -> None:
    tr = run(syn.s07_stop_at_fifty)
    assert invite_times(tr) == []
    assert tr[-1].activity == "idle"
    assert activity_at(tr, 51) == "idle"
    assert {t.activity for t in tr} <= ALLOWED_ACTIVITY


def test_07_cli_output_never_claims_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An exported setting in the owner's shell must not reach the CLI.
    monkeypatch.setenv("BEEBREAK_ACTIVITY_THRESHOLD_S", "not-a-number")
    trace = tmp_path / "s07.jsonl"
    trace.write_text(
        "\n".join(e.model_dump_json() for e in syn.s07_stop_at_fifty()) + "\n",
        encoding="utf-8",
    )
    tool = Path(__file__).parents[2] / "tools" / "replay_trace.py"
    env = {k: v for k, v in os.environ.items() if not k.startswith("BEEBREAK_")}
    result = subprocess.run(
        [sys.executable, str(tool), str(trace)],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    out = (result.stdout + result.stderr).lower()
    assert out.strip()
    for word in ("finish", "done", "complet"):
        assert word not in out


def test_08_headless_without_stop_invites_then_unknown() -> None:
    tr = run(syn.s08_headless_no_stop)
    times = invite_times(tr)
    assert len(times) == 1
    assert 60 <= times[0] <= 70
    assert tr[-1].activity == "unknown"


def test_09_invitation_waits_for_other_session_to_resume() -> None:
    tr = run(syn.s09_other_session_waiting)
    times = invite_times(tr)
    assert len(times) == 1
    assert 200 <= times[0] <= 215
    assert activity_at(tr, 100) == "waiting_input"


def test_10_duplicates_and_swaps_do_not_change_the_timeline() -> None:
    assert run(syn.s10_duplicated_and_swapped) == run(syn.s02_steady_five_minutes)


def test_11_four_open_tasks_carry_5_10m() -> None:
    inv = invites(run(syn.s11_four_open_tasks))
    assert len(inv) == 1
    assert inv[0].estimate == "5_10m"
    assert 120 <= off(inv[0]) <= 122


def test_12_one_open_task_is_vetoed_as_under_5m() -> None:
    assert invite_times(run(syn.s12_one_open_task)) == []


def test_13_declined_invitation_then_next_run_after_cooldown() -> None:
    times = invite_times(run(syn.s13_repeated_turns, respond="decline"))
    assert len(times) == 2
    assert 59 <= times[0] <= 63
    assert 961 <= times[1] <= 970


def test_14_first_event_a_tool_behaves_like_s02() -> None:
    times = invite_times(run(syn.s14_no_turn_submitted))
    assert len(times) == 1
    assert 60 <= times[0] <= 70
    assert run(syn.s14_no_turn_submitted) == run(syn.s02_steady_five_minutes)


def test_15_cancelled_tool_no_longer_invites() -> None:
    # Was a documented false positive (one invite at ~60 s). Decision
    # 2026-10-17: a lone open tool is not enough evidence, so the Esc case is
    # now silent; same documented cost as test 03.
    assert invite_times(run(syn.s15_escape_cancel)) == []


def test_16_stale_waiting_session_stops_blocking() -> None:
    tr = run(syn.s16_stale_waiting_other_session)
    times = invite_times(tr)
    assert len(times) == 1
    assert 310 <= times[0] <= 313
    assert activity_at(tr, 200) == "waiting_input"
    changed = first_activity_change_after(tr, 10, "active")
    assert changed is not None
    assert 310 <= changed <= 313


def test_17_late_event_cannot_resurrect_closed_session() -> None:
    tr = run(syn.s17_late_event_after_close)
    assert invite_times(tr) == []
    assert tr[-1].activity == "unknown"


def test_18_tasks_without_completion_invite_unknown_after_wait() -> None:
    inv = invites(run(syn.s18_tasks_never_complete))
    assert len(inv) == 1
    assert inv[0].estimate == "unknown"
    assert 300 <= off(inv[0]) <= 302

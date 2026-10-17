"""Recorded trace: Esc on a running foreground tool (Claude Code 2.1.302).

Claude Code sent no event after the Esc, so the run looks like one long tool.
Decision 2026-10-17: a lone open tool is not enough evidence, and the open-tool
cap is 600 s. Times are literals, never computed from the code under test.
"""

from pathlib import Path

from breakroom_companion.config import TriggerConfig
from breakroom_shared.events import NormalizedEvent
from tools.replay_trace import load_events, replay

TRACE = (
    Path(__file__).parents[1] / "fixtures/traces/recorded/esc-cancel-running-tool.jsonl"
)


def _events(*, keep_close: bool) -> list[NormalizedEvent]:
    with TRACE.open(encoding="utf-8") as f:
        events, invalid = load_events(f)
    assert invalid == 0
    if not keep_close:
        events = [e for e in events if e.type != "session_closed"]
    return events


def test_default_open_tool_cap_is_ten_minutes() -> None:
    assert TriggerConfig().open_tool_max_s == 600


def test_lone_running_tool_never_invites_as_recorded() -> None:
    tr = replay(_events(keep_close=True), TriggerConfig(), tail_s=900)
    assert [t for t in tr if t.invitation == "suggested"] == []


def test_lone_running_tool_never_invites_past_ten_minutes() -> None:
    # No session_closed: the silent tool stays open for the whole timeline.
    tr = replay(_events(keep_close=False), TriggerConfig(), tail_s=900)
    assert [t for t in tr if t.invitation == "suggested"] == []


def test_silent_session_goes_stale_by_600_seconds() -> None:
    events = _events(keep_close=False)
    tr = replay(events, TriggerConfig(), tail_s=900)
    started = events[2].observed_at  # the tool_started event
    unknown = [t for t in tr if t.activity == "unknown"]
    assert unknown
    stale_at = (unknown[0].at - started).total_seconds()
    assert 599 <= stale_at <= 602
    # Still active just before the cap.
    assert all(
        t.activity == "active" for t in tr if 0 < (t.at - started).total_seconds() < 599
    )

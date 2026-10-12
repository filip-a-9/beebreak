from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from breakroom_companion.config import TriggerConfig
from breakroom_companion.trigger import ActivityTrigger
from breakroom_shared.events import EventType, NormalizedEvent

T0 = datetime(2026, 10, 12, 12, 0, 0, tzinfo=UTC)
CFG = TriggerConfig(
    activity_threshold_s=60,
    quiet_gap_s=20,
    min_tool_events=2,
    cooldown_s=900,
    suggestion_timeout_s=60,
)


def ev(
    t: float,
    type_: EventType = "tool_completed",
    session: str = "s1",
    event_id: UUID | None = None,
) -> NormalizedEvent:
    data: dict[str, Any] = {
        "event_version": 1,
        "event_id": event_id or uuid4(),
        "local_session_key": session,
        "agent": "claude_code",
        "type": type_,
        "observed_at": T0 + timedelta(seconds=t),
    }
    return NormalizedEvent.model_validate(data)


def feed_all(trigger: ActivityTrigger, events: list[NormalizedEvent]) -> list[int]:
    return [i for i, e in enumerate(events) if trigger.feed(e)]


def steady(start: float, end: float, step: float = 10) -> list[NormalizedEvent]:
    out = []
    t = start
    while t <= end:
        out.append(ev(t))
        t += step
    return out


def test_config_defaults_and_env(monkeypatch: pytest.MonkeyPatch) -> None:
    assert TriggerConfig.from_env().activity_threshold_s == 60
    monkeypatch.setenv("BREAKROOM_COOLDOWN_S", "5")
    assert TriggerConfig.from_env().cooldown_s == 5


@pytest.mark.parametrize("bad", ["0", "-3", "abc"])
def test_config_rejects_bad_values(monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
    monkeypatch.setenv("BREAKROOM_QUIET_GAP_S", bad)
    with pytest.raises(ValueError):
        TriggerConfig.from_env()


def test_short_task_never_qualifies() -> None:
    t = ActivityTrigger(CFG)
    events = [ev(0, "turn_submitted"), *steady(5, 50, 5)]
    assert feed_all(t, events) == []


def test_steady_activity_qualifies_at_first_event_past_threshold() -> None:
    t = ActivityTrigger(CFG)
    events = [ev(0, "turn_submitted"), *steady(10, 100)]
    hits = feed_all(t, events)
    assert events[hits[0]].observed_at == T0 + timedelta(seconds=60)


def test_run_without_turn_submitted_starts_at_first_tool_event() -> None:
    t = ActivityTrigger(CFG)
    events = steady(100, 200)
    hits = feed_all(t, events)
    assert events[hits[0]].observed_at == T0 + timedelta(seconds=160)


def test_min_tool_events_required() -> None:
    t = ActivityTrigger(CFG)
    assert not t.feed(ev(0, "turn_submitted"))
    assert not t.feed(ev(70))  # only one tool event, long elapsed time


def test_silent_gap_restarts_run() -> None:
    t = ActivityTrigger(CFG)
    events = [ev(0, "turn_submitted"), *steady(10, 40), *steady(100, 150)]
    # elapsed since turn_submitted is large, but the run restarted at t=100
    assert feed_all(t, events) == []


def test_turn_stopped_ends_run() -> None:
    t = ActivityTrigger(CFG)
    events = [
        ev(0, "turn_submitted"),
        *steady(10, 40),
        ev(45, "turn_stopped"),
        ev(50, "turn_submitted"),
        *steady(60, 100),
    ]
    assert feed_all(t, events) == []


def test_session_closed_ends_run() -> None:
    t = ActivityTrigger(CFG)
    events = [*steady(0, 50), ev(55, "session_closed"), *steady(60, 100)]
    assert feed_all(t, events) == []


def test_heartbeat_does_not_extend_or_count() -> None:
    t = ActivityTrigger(CFG)
    events = [ev(0, "turn_submitted"), ev(30, "heartbeat"), ev(70, "heartbeat")]
    assert feed_all(t, events) == []


def test_duplicate_event_id_ignored() -> None:
    t = ActivityTrigger(CFG)
    dup = uuid4()
    t.feed(ev(0, "turn_submitted"))
    assert not t.feed(ev(10, event_id=dup))
    # replaying the same id at a qualifying time must not count as a second tool
    assert not t.feed(ev(70, event_id=dup))


def test_out_of_order_event_ignored_for_timing() -> None:
    t = ActivityTrigger(CFG)
    t.feed(ev(0, "turn_submitted"))
    t.feed(ev(15))
    assert not t.feed(ev(10))  # late: must not move the run's last event back
    t.feed(ev(30))  # 15s after t=15 (would be 20s after the late event)
    t.feed(ev(45))
    assert t.feed(ev(60))


def test_sessions_tracked_separately() -> None:
    t = ActivityTrigger(CFG)
    results_a, results_b = [], []
    for step in range(5):
        kind: EventType = "turn_submitted" if step == 0 else "tool_completed"
        results_a.append(t.feed(ev(step * 15, kind, "a")))
        results_b.append(t.feed(ev(step * 15 + 100, kind, "b")))
    assert results_a == [False, False, False, False, True]
    assert results_b == [False, False, False, False, True]


def test_one_long_tool_call_qualifies_at_its_completion() -> None:
    t = ActivityTrigger(CFG)
    events = [
        ev(0, "turn_submitted"),
        ev(6, "tool_started"),
        ev(606, "tool_completed"),
    ]
    assert feed_all(t, events) == [2]


def test_long_open_tool_qualifies_earlier_when_another_event_arrives() -> None:
    t = ActivityTrigger(CFG)
    events = [
        ev(0, "turn_submitted"),
        ev(6, "tool_started"),
        ev(300, "tool_started"),
        ev(606, "tool_completed"),
    ]
    assert feed_all(t, events)[0] == 2


def test_parallel_long_calls_keep_the_run_alive_until_all_complete() -> None:
    t = ActivityTrigger(CFG)
    events = [
        ev(0, "turn_submitted"),
        ev(5, "tool_started"),
        ev(6, "tool_started"),
        ev(400, "tool_completed"),  # one call still open
        ev(405, "tool_completed"),
    ]
    assert feed_all(t, events) == [3, 4]


def test_gap_after_all_tools_completed_still_restarts_run() -> None:
    t = ActivityTrigger(CFG)
    events = [
        ev(0, "turn_submitted"),
        ev(5, "tool_started"),
        ev(10, "tool_completed"),
        ev(100, "tool_started"),  # 90 s with no open tool: quiet
        ev(105, "tool_completed"),
    ]
    assert feed_all(t, events) == []


def test_stray_completion_does_not_go_negative() -> None:
    t = ActivityTrigger(CFG)
    events = [
        ev(0, "turn_submitted"),
        ev(5, "tool_completed"),  # no matching start
        ev(10, "tool_started"),
        ev(100, "tool_started"),  # first open tool is still open: alive
    ]
    assert feed_all(t, events) == [3]


@pytest.mark.parametrize("reset", ["turn_submitted", "turn_stopped", "session_closed"])
def test_lost_tool_completed_does_not_keep_a_run_alive_across_turns(
    reset: EventType,
) -> None:
    # Headless runs never delivered turn_stopped, and a tool_completed can be
    # lost: the next boundary event must clear the open-tool count.
    t = ActivityTrigger(CFG)
    events = [
        ev(0, "turn_submitted"),
        ev(5, "tool_started"),  # never completed
        ev(6, "tool_started"),  # never completed
        ev(30, reset),
        ev(35, "turn_submitted"),
        ev(40, "tool_completed"),
        ev(110, "tool_completed"),  # 70 s gap, no open tool: new run
    ]
    assert feed_all(t, events) == []

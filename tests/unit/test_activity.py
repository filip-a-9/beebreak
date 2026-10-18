from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from beebreak_companion.activity import ActivityTracker
from beebreak_companion.config import TriggerConfig
from beebreak_shared.events import EventType, NormalizedEvent

T0 = datetime(2026, 10, 12, 12, 0, 0, tzinfo=UTC)
CFG = TriggerConfig(
    activity_threshold_s=60,
    min_tool_events=2,
    cooldown_s=900,
    suggestion_timeout_s=60,
    stale_after_s=180,
    open_tool_max_s=1800,
    waiting_max_s=300,
    forget_after_s=7200,
)


def at(t: float) -> datetime:
    return T0 + timedelta(seconds=t)


def ev(
    t: float,
    type_: EventType,
    session: str = "s1",
    event_id: UUID | None = None,
) -> NormalizedEvent:
    data: dict[str, Any] = {
        "event_version": 1,
        "event_id": event_id or uuid4(),
        "local_session_key": session,
        "agent": "claude_code",
        "type": type_,
        "observed_at": at(t),
    }
    return NormalizedEvent.model_validate(data)


def tracker(*events: NormalizedEvent) -> ActivityTracker:
    tr = ActivityTracker(CFG)
    for e in events:
        tr.feed(e)
    return tr


def snap(tr: ActivityTracker, now: float, session: str = "s1"):  # type: ignore[no-untyped-def]
    return tr.sessions(at(now))[session]


# --- table rows -----------------------------------------------------------


def test_session_opened_creates_idle_session() -> None:
    s = snap(tracker(ev(0, "session_opened")), 1)
    assert s.state == "idle"
    assert s.run_started_at is None


def test_turn_submitted_starts_run_and_clears_open_tools() -> None:
    tr = tracker(ev(0, "tool_started"), ev(5, "turn_submitted"))
    s = snap(tr, 6)
    assert s.state == "active"
    assert s.open_tools == 0
    assert s.run_started_at == at(5)
    assert s.run_tool_events == 0


def test_turn_submitted_increments_run_id_each_time() -> None:
    tr = tracker(ev(0, "turn_submitted"), ev(5, "turn_submitted"))
    first = tracker(ev(0, "turn_submitted"))
    assert snap(tr, 6).run_id == snap(first, 6).run_id + 1


def test_tool_events_count_and_track_open_tools() -> None:
    tr = tracker(
        ev(0, "turn_submitted"),
        ev(1, "tool_started"),
        ev(2, "tool_started"),
        ev(3, "tool_completed"),
    )
    s = snap(tr, 4)
    assert (s.open_tools, s.run_tool_events) == (1, 3)
    assert s.run_started_at == at(0)


def test_tool_completed_floors_open_tools_at_zero() -> None:
    tr = tracker(ev(0, "turn_submitted"), ev(1, "tool_completed"))
    assert snap(tr, 2).open_tools == 0


def test_tool_event_in_same_run_keeps_run_id() -> None:
    tr = tracker(ev(0, "turn_submitted"))
    before = snap(tr, 1).run_id
    tr.feed(ev(2, "tool_started"))
    assert snap(tr, 3).run_id == before


def test_tool_event_on_idle_starts_new_run() -> None:
    tr = tracker(ev(0, "session_opened"))
    tr.feed(ev(10, "tool_started"))
    s = snap(tr, 11)
    assert s.state == "active"
    assert s.run_started_at == at(10)
    assert s.run_tool_events == 1


def test_turn_stopped_goes_idle_and_clears_open_tools() -> None:
    tr = tracker(ev(0, "tool_started"), ev(1, "turn_stopped"))
    s = snap(tr, 2)
    assert s.state == "idle"
    assert s.open_tools == 0
    assert s.run_started_at is None


def test_nothing_means_finished_after_stop_or_task_completed() -> None:
    tr = tracker(
        ev(0, "turn_submitted"),
        ev(1, "task_created"),
        ev(2, "task_completed"),
        ev(3, "turn_stopped"),
    )
    # A stop is idle, never completion: the session is still known and
    # its open-task count is unchanged by the turn ending.
    s = snap(tr, 4)
    assert s.state == "idle"
    assert s.open_tasks == 0
    assert tr.aggregate(at(4)) == "idle"


def test_task_event_on_idle_starts_run() -> None:
    tr = tracker(ev(0, "session_opened"), ev(1, "task_created"))
    assert snap(tr, 2).state == "active"


def test_task_event_does_not_end_waiting() -> None:
    tr = tracker(ev(0, "turn_submitted"), ev(1, "waiting_input"), ev(2, "task_created"))
    assert snap(tr, 3).state == "waiting_input"


def test_task_event_in_active_run_keeps_run_id() -> None:
    tr = tracker(ev(0, "turn_submitted"))
    before = snap(tr, 1).run_id
    tr.feed(ev(2, "task_completed"))
    assert snap(tr, 3).run_id == before


def test_waiting_input_clears_run_start_and_keeps_open_tools() -> None:
    tr = tracker(ev(0, "turn_submitted"), ev(1, "tool_started"), ev(2, "waiting_input"))
    s = snap(tr, 3)
    assert s.state == "waiting_input"
    assert s.run_started_at is None
    assert s.open_tools == 1


def test_tool_event_after_waiting_starts_new_run() -> None:
    tr = tracker(ev(0, "turn_submitted"), ev(1, "waiting_input"))
    old = snap(tr, 2).run_id
    tr.feed(ev(50, "tool_completed"))
    s = snap(tr, 51)
    assert s.state == "active"
    assert s.run_id == old + 1
    assert s.run_started_at == at(50)
    assert s.run_tool_events == 1


def test_session_closed_removes_session() -> None:
    tr = tracker(ev(0, "turn_submitted"), ev(1, "session_closed"))
    assert "s1" not in tr.sessions(at(2))


def test_straggler_older_than_close_does_not_revive_session() -> None:
    tr = tracker(ev(0, "turn_submitted"), ev(10, "session_closed"))
    assert not tr.feed(ev(9, "tool_started"))
    assert "s1" not in tr.sessions(at(11))


def test_event_after_close_starts_fresh_session() -> None:
    tr = tracker(ev(0, "turn_submitted"), ev(10, "session_closed"))
    assert tr.feed(ev(20, "session_opened"))
    assert snap(tr, 21).state == "idle"


def test_session_closed_for_unknown_session_creates_nothing() -> None:
    tr = tracker(ev(0, "session_closed"))
    assert dict(tr.sessions(at(1))) == {}


def test_heartbeat_is_ignored() -> None:
    tr = tracker(ev(0, "turn_submitted"))
    assert tr.feed(ev(100, "heartbeat"))
    assert snap(tr, 101).last_event_at == at(0)
    assert dict(tracker(ev(0, "heartbeat")).sessions(at(1))) == {}


def test_first_seen_mid_run_without_turn_submitted() -> None:
    tr = tracker(ev(5, "tool_completed"))
    s = snap(tr, 6)
    assert s.state == "active"
    assert s.run_started_at == at(5)
    assert s.run_tool_events == 1
    assert s.open_tools == 0


def test_first_seen_through_waiting_input() -> None:
    assert snap(tracker(ev(0, "waiting_input")), 1).state == "waiting_input"


# --- duplicates and ordering ----------------------------------------------


def test_duplicate_event_id_ignored() -> None:
    tr = ActivityTracker(CFG)
    eid = uuid4()
    assert tr.feed(ev(0, "tool_started", event_id=eid))
    assert not tr.feed(ev(1, "tool_started", event_id=eid))
    assert snap(tr, 2).open_tools == 1


def test_dedupe_window_is_bounded_at_256() -> None:
    tr = ActivityTracker(CFG)
    first = uuid4()
    tr.feed(ev(0, "heartbeat", event_id=first))
    for _ in range(256):
        tr.feed(ev(0, "heartbeat", event_id=uuid4()))
    assert tr.feed(ev(0, "heartbeat", event_id=first))


def test_out_of_order_event_ignored() -> None:
    tr = tracker(ev(10, "tool_started"))
    assert not tr.feed(ev(5, "tool_started"))
    s = snap(tr, 11)
    assert (s.open_tools, s.last_event_at) == (1, at(10))


def test_equal_timestamp_is_accepted() -> None:
    tr = tracker(ev(10, "tool_started"))
    assert tr.feed(ev(10, "tool_completed"))


def test_sessions_are_independent() -> None:
    tr = tracker(ev(0, "tool_started", "a"), ev(1, "session_opened", "b"))
    assert snap(tr, 2, "a").state == "active"
    assert snap(tr, 2, "b").state == "idle"


# --- staleness ------------------------------------------------------------


def test_active_without_open_tools_goes_unknown_at_stale_after() -> None:
    tr = tracker(ev(0, "turn_submitted"))
    assert snap(tr, 179.9).state == "active"
    s = snap(tr, 180)
    assert s.state == "unknown"
    assert s.run_started_at is None


def test_long_open_tool_stays_active_past_stale_after() -> None:
    tr = tracker(ev(0, "tool_started"))
    assert snap(tr, 1000).state == "active"
    assert snap(tr, 1799.9).state == "active"
    assert snap(tr, 1800).state == "unknown"


def test_waiting_goes_unknown_at_waiting_max() -> None:
    # Esc on a prompt fires no hook; a silent wait must stop blocking others.
    tr = tracker(ev(0, "waiting_input"))
    assert snap(tr, 299.9).state == "waiting_input"
    assert snap(tr, 300).state == "unknown"


def test_waiting_with_open_tool_uses_waiting_max() -> None:
    tr = tracker(ev(0, "tool_started"), ev(1, "waiting_input"))
    assert snap(tr, 300.9).state == "waiting_input"
    assert snap(tr, 301).state == "unknown"


def test_idle_does_not_go_stale() -> None:
    tr = tracker(ev(0, "turn_stopped"))
    assert snap(tr, 7199).state == "idle"


def test_forget_after_drops_session() -> None:
    tr = tracker(ev(0, "turn_stopped"), ev(0, "turn_stopped", "other"))
    assert "s1" in tr.sessions(at(7199.9))
    assert dict(tr.sessions(at(7200))) == {}
    assert tr.aggregate(at(7200)) == "unknown"


def test_forgotten_session_restarts_fresh() -> None:
    tr = tracker(ev(0, "turn_submitted"), ev(1, "task_created"))
    tr.sessions(at(8000))
    tr.feed(ev(8001, "tool_started"))
    s = snap(tr, 8002)
    assert s.open_tasks == 0
    assert s.task_intervals_s == ()


def test_stale_unknown_session_starts_new_run_on_event() -> None:
    tr = tracker(ev(0, "turn_submitted"))
    old = snap(tr, 1).run_id
    tr.feed(ev(500, "tool_started"))
    s = snap(tr, 501)
    assert s.state == "active"
    assert s.run_id == old + 1
    assert s.run_started_at == at(500)
    assert s.run_tool_events == 1


def test_stale_open_tool_does_not_leak_into_new_run() -> None:
    tr = tracker(ev(0, "tool_started"))
    tr.feed(ev(2000, "tool_started"))
    assert snap(tr, 2001).open_tools == 1


def test_stale_unknown_task_event_starts_run() -> None:
    tr = tracker(ev(0, "turn_submitted"))
    tr.feed(ev(500, "task_created"))
    assert snap(tr, 501).state == "active"


def test_sessions_is_a_read_only_snapshot() -> None:
    tr = tracker(ev(0, "turn_submitted"))
    assert snap(tr, 500).state == "unknown"
    assert snap(tr, 10).state == "active"


# --- aggregate ------------------------------------------------------------


def test_aggregate_empty_is_unknown() -> None:
    assert ActivityTracker(CFG).aggregate(at(0)) == "unknown"


def test_aggregate_precedence() -> None:
    tr = tracker(ev(0, "turn_stopped", "i"))
    assert tr.aggregate(at(1)) == "idle"
    tr.feed(ev(1, "turn_submitted", "a"))
    assert tr.aggregate(at(2)) == "active"
    tr.feed(ev(2, "waiting_input", "w"))
    assert tr.aggregate(at(3)) == "waiting_input"


def test_aggregate_prefers_idle_over_unknown() -> None:
    tr = tracker(ev(0, "turn_submitted", "a"), ev(0, "turn_stopped", "i"))
    assert tr.aggregate(at(500)) == "idle"


def test_aggregate_all_stale_is_unknown() -> None:
    tr = tracker(ev(0, "turn_submitted", "a"))
    assert tr.aggregate(at(500)) == "unknown"


# --- tasks ----------------------------------------------------------------


def test_task_counts_and_floor() -> None:
    tr = tracker(ev(0, "task_created"), ev(1, "task_created"))
    assert snap(tr, 2).open_tasks == 2
    tr.feed(ev(2, "task_completed"))
    tr.feed(ev(3, "task_completed"))
    tr.feed(ev(4, "task_completed"))
    assert snap(tr, 5).open_tasks == 0


def test_task_intervals_between_progress_points() -> None:
    tr = tracker(
        ev(0, "task_created"),  # progress (open was 0)
        ev(10, "task_created"),  # not progress (open was 1)
        ev(40, "task_completed"),  # progress, 40s after first
        ev(100, "task_completed"),  # progress, 60s later
    )
    assert snap(tr, 101).task_intervals_s == (40.0, 60.0)


def test_task_created_after_all_closed_is_progress() -> None:
    tr = tracker(
        ev(0, "task_created"), ev(10, "task_completed"), ev(30, "task_created")
    )
    assert snap(tr, 31).task_intervals_s == (10.0, 20.0)


def test_task_intervals_keep_last_eight_oldest_first() -> None:
    events = [ev(0, "task_created")]
    for i in range(1, 11):
        events.append(ev(i * i, "task_completed"))
    s = snap(tracker(*events), 200)
    assert s.task_intervals_s == tuple(float(2 * i - 1) for i in range(3, 11))


def test_tasks_survive_across_runs() -> None:
    tr = tracker(ev(0, "task_created"), ev(1, "turn_stopped"))
    tr.feed(ev(2, "turn_submitted"))
    assert snap(tr, 3).open_tasks == 1


def test_tasks_reset_when_session_closed() -> None:
    tr = tracker(ev(0, "task_created"), ev(1, "session_closed"))
    tr.feed(ev(2, "task_created"))
    s = snap(tr, 3)
    assert (s.open_tasks, s.task_intervals_s) == (1, ())

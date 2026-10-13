"""Synthetic event traces for the M2 regression set.

One builder per scenario of M2-05. Timestamps are explicit offsets (seconds)
from a fixed epoch; there is no wall clock and no randomness.
"""

from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_OID, uuid5

from breakroom_shared.events import EventType, NormalizedEvent, ToolClass

EPOCH = datetime(2026, 1, 5, 9, 0, 0, tzinfo=UTC)


class _Trace:
    def __init__(self, scenario: str) -> None:
        self._scenario = scenario
        self._n = 0
        self.events: list[NormalizedEvent] = []

    def add(
        self,
        t: float,
        kind: EventType,
        session: str = "A",
        tool_class: ToolClass | None = None,
    ) -> NormalizedEvent:
        self._n += 1
        event = NormalizedEvent(
            event_version=1,
            event_id=uuid5(NAMESPACE_OID, f"{self._scenario}-{self._n}"),
            local_session_key=f"{self._scenario}-{session}",
            agent="claude_code",
            type=kind,
            observed_at=EPOCH + timedelta(seconds=t),
            tool_class=tool_class,
        )
        self.events.append(event)
        return event

    def tool(self, t: float, session: str = "A", dur: float = 1.0) -> None:
        self.add(t, "tool_started", session, "shell")
        self.add(t + dur, "tool_completed", session, "shell")

    def steady(
        self, start: float, stop: float, session: str = "A", every: float = 10.0
    ) -> None:
        t = start
        while t <= stop:
            self.tool(t, session)
            t += every


def _sorted(events: list[NormalizedEvent]) -> list[NormalizedEvent]:
    return sorted(events, key=lambda e: e.observed_at)


def s01_short_task() -> list[NormalizedEvent]:
    tr = _Trace("s01")
    tr.add(0, "turn_submitted")
    tr.steady(0, 40)
    tr.add(45, "turn_stopped")
    return tr.events


def s02_steady_five_minutes() -> list[NormalizedEvent]:
    tr = _Trace("s02")
    tr.add(0, "turn_submitted")
    tr.steady(0, 290)
    return tr.events


def s03_long_silent_tool() -> list[NormalizedEvent]:
    tr = _Trace("s03")
    tr.add(0, "turn_submitted")
    tr.add(6, "tool_started", tool_class="shell")
    tr.add(606, "tool_completed", tool_class="shell")
    return tr.events


def s04_waiting_with_open_tool() -> list[NormalizedEvent]:
    tr = _Trace("s04")
    tr.add(0, "turn_submitted")
    tr.steady(0, 20)
    tr.add(30, "tool_started", tool_class="shell")
    tr.add(30, "waiting_input")
    return tr.events


def s05_waiting_then_new_run() -> list[NormalizedEvent]:
    tr = _Trace("s05")
    tr.add(0, "turn_submitted")
    tr.steady(0, 20)
    tr.add(30, "tool_started", tool_class="shell")
    tr.add(30, "waiting_input")
    tr.add(200, "tool_completed", tool_class="shell")
    tr.steady(210, 400)
    return tr.events


def s06_failing_tool_then_silence() -> list[NormalizedEvent]:
    tr = _Trace("s06")
    tr.add(0, "turn_submitted")
    tr.add(5, "tool_started", tool_class="shell")
    tr.add(6, "tool_completed", tool_class="shell")  # PostToolUseFailure
    return tr.events


def s07_stop_at_fifty() -> list[NormalizedEvent]:
    tr = _Trace("s07")
    tr.add(0, "turn_submitted")
    tr.steady(0, 40)
    tr.add(50, "turn_stopped")
    return tr.events


def s08_headless_no_stop() -> list[NormalizedEvent]:
    tr = _Trace("s08")
    tr.steady(0, 120)
    return tr.events


def s09_other_session_waiting() -> list[NormalizedEvent]:
    tr = _Trace("s09")
    tr.add(0, "turn_submitted", "A")
    tr.add(0, "turn_submitted", "B")
    tr.add(5, "waiting_input", "B")
    tr.steady(0, 400, "A")
    tr.add(200, "tool_completed", "B", "shell")
    tr.add(205, "turn_stopped", "B")
    return _sorted(tr.events)


def s10_duplicated_and_swapped() -> list[NormalizedEvent]:
    events = list(s02_steady_five_minutes())
    by_time = {e.observed_at: i for i, e in enumerate(events)}
    # Duplicate deliveries of the events around the invitation point.
    for offset in (30, 60, 61):
        i = by_time[EPOCH + timedelta(seconds=offset)]
        events.insert(i + 1, events[i])
        by_time = {e.observed_at: j for j, e in enumerate(events)}
    # Adjacent events delivered in the wrong order.
    for offset in (100, 150, 200, 250):
        i = by_time[EPOCH + timedelta(seconds=offset)]
        events[i], events[i + 1] = events[i + 1], events[i]
        by_time = {e.observed_at: j for j, e in enumerate(events)}
    # A duplicate after the last swap: if dedupe breaks, the open tool count
    # stays at 1 and the session never goes unknown.
    i = by_time[EPOCH + timedelta(seconds=280)]
    events.insert(i + 1, events[i])
    return events


# Scenarios 11, 12 and 18: tools run from 0 s alongside the task list. The
# engine waits for the first completed task (up to task_evidence_wait_s from the
# run start) before deciding, so the estimate and the under_5m veto can apply.


def s11_four_open_tasks() -> list[NormalizedEvent]:
    tr = _Trace("s11")
    tr.add(0, "turn_submitted")
    for t in range(5):
        tr.add(t, "task_created")
    tr.steady(5, 300)
    tr.add(120, "task_completed")  # 4 open, interval 120 s
    return _sorted(tr.events)


def s12_one_open_task() -> list[NormalizedEvent]:
    tr = _Trace("s12")
    tr.add(0, "turn_submitted")
    tr.add(0, "task_created")
    tr.add(1, "task_created")
    tr.steady(5, 300)
    tr.add(60, "task_completed")  # 1 open, interval 60 s
    return _sorted(tr.events)


def s18_tasks_never_complete() -> list[NormalizedEvent]:
    tr = _Trace("s18")
    tr.add(0, "turn_submitted")
    for t in range(3):
        tr.add(t, "task_created")
    tr.steady(5, 400)
    return _sorted(tr.events)


def s13_repeated_turns() -> list[NormalizedEvent]:
    tr = _Trace("s13")
    for start in (0, 300, 600, 900):
        tr.add(start, "turn_submitted")
        tr.steady(start + 10, start + 290)
    return tr.events


def s14_no_turn_submitted() -> list[NormalizedEvent]:
    tr = _Trace("s14")
    tr.steady(0, 290)
    return tr.events


def s15_escape_cancel() -> list[NormalizedEvent]:
    tr = _Trace("s15")
    tr.add(0, "turn_submitted")
    tr.add(6, "tool_started", tool_class="shell")
    return tr.events


def s16_stale_waiting_other_session() -> list[NormalizedEvent]:
    tr = _Trace("s16")
    tr.add(0, "turn_submitted", "A")
    tr.add(0, "turn_submitted", "B")
    tr.add(10, "waiting_input", "B")
    tr.steady(0, 500, "A")
    return _sorted(tr.events)


def s17_late_event_after_close() -> list[NormalizedEvent]:
    tr = _Trace("s17")
    tr.add(50, "turn_submitted")
    tr.tool(50)
    tr.add(100, "session_closed")
    tr.add(99, "tool_started", tool_class="shell")
    return tr.events

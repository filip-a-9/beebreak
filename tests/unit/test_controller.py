from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from breakroom_companion.controller import Controller
from breakroom_companion.trigger_config import TriggerConfig
from breakroom_shared.events import EventType, NormalizedEvent
from breakroom_shared.ipc import InvitationState

T0 = datetime(2026, 10, 12, 12, 0, 0, tzinfo=UTC)
CFG = TriggerConfig(
    activity_threshold_s=60,
    quiet_gap_s=20,
    min_tool_events=2,
    cooldown_s=900,
    suggestion_timeout_s=60,
)


def at(t: float) -> datetime:
    return T0 + timedelta(seconds=t)


def ev(
    t: float, type_: EventType = "tool_completed", session: str = "s1"
) -> NormalizedEvent:
    data: dict[str, Any] = {
        "event_version": 1,
        "event_id": uuid4(),
        "local_session_key": session,
        "agent": "claude_code",
        "type": type_,
        "observed_at": at(t),
    }
    return NormalizedEvent.model_validate(data)


def run_long(c: Controller, start: float, end: float) -> list[InvitationState]:
    """Feed submit + steady tool events; return the non-None results."""
    out = [c.on_event(ev(start, "turn_submitted"))]
    t = start + 10
    while t <= end:
        out.append(c.on_event(ev(t)))
        t += 10
    return [r for r in out if r is not None]


def test_short_task_no_suggestion() -> None:
    c = Controller(CFG)
    assert run_long(c, 0, 50) == []
    assert c.state == "idle"


def test_long_activity_suggests_exactly_once() -> None:
    c = Controller(CFG)
    assert run_long(c, 0, 200) == ["suggested"]
    assert c.state == "suggested"


def test_last_event_at_updates() -> None:
    c = Controller(CFG)
    c.on_event(ev(5))
    assert c.last_event_at == at(5)


def test_decline_then_cooldown_then_suggest_again() -> None:
    c = Controller(CFG)
    run_long(c, 0, 100)
    assert c.on_outcome("declined", at(105)) == "idle"
    assert run_long(c, 200, 400) == []  # inside the cooldown
    assert c.state == "idle"
    assert run_long(c, 1100, 1200) == ["suggested"]  # after the cooldown


def test_declined_run_never_suggests_again_until_next_turn() -> None:
    c = Controller(CFG)
    run_long(c, 0, 100)
    c.on_outcome("declined", at(105))
    # Same run, unbroken, continues well past the cooldown.
    later = [c.on_event(ev(t)) for t in range(110, 1300, 10)]
    assert "suggested" not in later
    assert run_long(c, 1300, 1400) == ["suggested"]  # a new turn re-arms


def test_timed_out_run_never_suggests_again_until_next_turn() -> None:
    c = Controller(CFG)
    run_long(c, 0, 100)
    c.tick(at(130))
    later = [c.on_event(ev(t)) for t in range(110, 1300, 10)]
    assert "suggested" not in later


def test_spent_session_does_not_block_other_sessions() -> None:
    c = Controller(CFG)
    run_long(c, 0, 100)  # session s1
    c.on_outcome("declined", at(105))
    for t in range(110, 1010, 10):
        c.on_event(ev(t))  # s1's run stays unbroken through the cooldown
    c.on_event(ev(1000, "turn_submitted", "s2"))
    results = []
    for t in range(1010, 1200, 10):
        results.append(c.on_event(ev(t)))  # s1's spent run keeps going
        results.append(c.on_event(ev(t, session="s2")))
    assert results.count("suggested") == 1
    assert results.index("suggested") % 2 == 1  # came from s2, not s1


def test_accept_returns_idle_and_starts_cooldown() -> None:
    c = Controller(CFG)
    run_long(c, 0, 100)
    assert c.on_outcome("accepted", at(105)) == "idle"
    assert run_long(c, 200, 400) == []


def test_outcome_ignored_unless_suggested() -> None:
    c = Controller(CFG)
    assert c.on_outcome("declined", at(0)) is None
    assert c.state == "idle"


def test_unanswered_suggestion_times_out_into_cooldown() -> None:
    c = Controller(CFG)
    run_long(c, 0, 100)  # suggested at t=60
    assert c.tick(at(100)) is None
    assert c.state == "suggested"
    assert c.tick(at(130)) == "idle"
    assert run_long(c, 200, 400) == []


def test_disabled_never_suggests_and_reenable_returns_idle() -> None:
    c = Controller(CFG)
    assert c.set_enabled(False) == "disabled"
    assert run_long(c, 0, 200) == []
    assert c.state == "disabled"
    assert c.set_enabled(True) == "idle"


def test_disable_while_suggested() -> None:
    c = Controller(CFG)
    run_long(c, 0, 100)
    assert c.set_enabled(False) == "disabled"
    assert c.on_outcome("declined", at(200)) is None


def test_duplicate_events_no_double_suggestion() -> None:
    c = Controller(CFG)
    events = [ev(0, "turn_submitted"), ev(15), ev(30), ev(45), ev(60), ev(70)]
    results = [c.on_event(e) for e in events]
    assert results.count("suggested") == 1
    assert c.on_event(events[4]) is None


def test_at_most_one_outstanding_suggestion_across_sessions() -> None:
    c = Controller(CFG)
    results = []
    for s in ("a", "b"):
        results.append(c.on_event(ev(0, "turn_submitted", s)))
        for t in (15, 30, 45, 60, 65):
            results.append(c.on_event(ev(t, session=s)))
    assert results.count("suggested") == 1


def test_default_config_comes_from_env() -> None:
    assert Controller().state == "idle"

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from breakroom_companion.config import TriggerConfig
from breakroom_companion.controller import Controller
from breakroom_companion.state_store import FileStateStore, PersistedState
from breakroom_shared.events import EventType, NormalizedEvent
from breakroom_shared.ipc import InvitationState

T0 = datetime(2026, 10, 12, 12, 0, 0, tzinfo=UTC)
CFG = TriggerConfig(
    activity_threshold_s=60,
    min_tool_events=2,
    cooldown_s=900,
    suggestion_timeout_s=60,
)


class MemoryStore:
    """In-memory StateStore for tests; never touches the real state dir."""

    def __init__(self, state: PersistedState | None = None, ok: bool = True) -> None:
        self.state = state or PersistedState()
        self.ok = ok
        self.saves = 0

    def load(self) -> PersistedState:
        return self.state

    def save(self, state: PersistedState) -> bool:
        self.saves += 1
        if self.ok:
            self.state = state
        return self.ok


@pytest.fixture(autouse=True)
def _state_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Controllers built without a store must not touch ~/.local/state."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


def new(cfg: TriggerConfig = CFG, store: MemoryStore | None = None) -> Controller:
    return Controller(cfg, store=store or MemoryStore())


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
    c = new()
    assert run_long(c, 0, 50) == []
    assert c.state == "idle"


def test_long_activity_suggests_exactly_once() -> None:
    c = new()
    assert run_long(c, 0, 200) == ["suggested"]
    assert c.state == "suggested"


def test_last_event_at_updates() -> None:
    c = new()
    c.on_event(ev(5))
    assert c.last_event_at == at(5)


def test_decline_then_cooldown_then_suggest_again() -> None:
    c = new()
    run_long(c, 0, 100)
    assert c.on_outcome("declined", at(105)) == "idle"
    assert run_long(c, 200, 400) == []  # inside the cooldown
    assert c.state == "idle"
    assert run_long(c, 1100, 1200) == ["suggested"]  # after the cooldown


def test_declined_run_never_suggests_again_until_next_turn() -> None:
    c = new()
    run_long(c, 0, 100)
    c.on_outcome("declined", at(105))
    # Same run, unbroken, continues well past the cooldown.
    later = [c.on_event(ev(t)) for t in range(110, 1300, 10)]
    assert "suggested" not in later
    assert run_long(c, 1300, 1400) == ["suggested"]  # a new turn re-arms


def test_timed_out_run_never_suggests_again_until_next_turn() -> None:
    c = new()
    run_long(c, 0, 100)
    c.tick(at(130))
    later = [c.on_event(ev(t)) for t in range(110, 1300, 10)]
    assert "suggested" not in later


def test_spent_session_does_not_block_other_sessions() -> None:
    c = new()
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
    # s2's run reaches 60 s at t=1060 (index 10, 2 results per step); s1's spent
    # run must not have invited earlier.
    assert results.index("suggested") >= 10


def test_accept_returns_idle_and_starts_cooldown() -> None:
    c = new()
    run_long(c, 0, 100)
    assert c.on_outcome("accepted", at(105)) == "idle"
    assert run_long(c, 200, 400) == []


def test_outcome_ignored_unless_suggested() -> None:
    c = new()
    assert c.on_outcome("declined", at(0)) is None
    assert c.state == "idle"


def test_unanswered_suggestion_times_out_into_cooldown() -> None:
    c = new()
    run_long(c, 0, 100)  # suggested at t=60
    assert c.tick(at(100)) is None
    assert c.state == "suggested"
    assert c.tick(at(130)) == "idle"
    assert run_long(c, 200, 400) == []


def test_disabled_never_suggests_and_reenable_returns_idle() -> None:
    c = new()
    assert c.set_enabled(False) == "disabled"
    assert run_long(c, 0, 200) == []
    assert c.state == "disabled"
    assert c.set_enabled(True) == "idle"


def test_disable_while_suggested() -> None:
    c = new()
    run_long(c, 0, 100)
    assert c.set_enabled(False) == "disabled"
    assert c.on_outcome("declined", at(200)) is None


def test_duplicate_events_no_double_suggestion() -> None:
    c = new()
    events = [ev(0, "turn_submitted"), ev(15), ev(30), ev(45), ev(60), ev(70)]
    results = [c.on_event(e) for e in events]
    assert results.count("suggested") == 1
    assert c.on_event(events[4]) is None


def test_at_most_one_outstanding_suggestion_across_sessions() -> None:
    c = new()
    results = []
    for s in ("a", "b"):
        results.append(c.on_event(ev(0, "turn_submitted", s)))
        for t in (15, 30, 45, 60, 65):
            results.append(c.on_event(ev(t, session=s)))
    assert results.count("suggested") == 1


def test_default_config_and_store_come_from_env(tmp_path: Path) -> None:
    assert Controller().state == "idle"
    Controller().set_enabled(False)
    assert (tmp_path / "state" / "breakroom" / "companion-state.json").exists()
    assert Controller().state == "disabled"


def test_unsafe_state_dir_starts_disabled(tmp_path: Path) -> None:
    d = tmp_path / "state" / "breakroom"
    d.mkdir(parents=True, mode=0o700)
    d.chmod(0o770)
    c = Controller()
    assert c.state == "disabled"
    assert c.set_enabled(False) is None  # persisting fails; still runs
    assert c.set_enabled(True) == "idle"


# --- persistence -----------------------------------------------------------


def test_decline_cooldown_survives_restart() -> None:
    store = MemoryStore()
    c = new(store=store)
    run_long(c, 0, 100)
    c.on_outcome("declined", at(105))
    assert store.state.cooldown_until == at(105 + CFG.cooldown_s)
    c2 = new(store=store)
    assert run_long(c2, 200, 400) == []
    assert run_long(c2, 1100, 1200) == ["suggested"]


def test_disable_survives_restart_and_enable_too() -> None:
    store = MemoryStore()
    new(store=store).set_enabled(False)
    c2 = new(store=store)
    assert c2.state == "disabled"
    assert run_long(c2, 0, 200) == []
    c2.set_enabled(True)
    assert new(store=store).state == "idle"


def test_persisted_through_a_real_file_store(tmp_path: Path) -> None:
    path = tmp_path / "s.json"
    c = Controller(CFG, store=FileStateStore(path))
    run_long(c, 0, 100)
    c.on_outcome("accepted", at(105))
    c.set_enabled(False)
    c2 = Controller(CFG, store=FileStateStore(path))
    assert c2.state == "disabled"
    c2.set_enabled(True)
    assert run_long(c2, 200, 400) == []  # cooldown restored


def test_failed_save_keeps_the_controller_working() -> None:
    c = new(store=MemoryStore(ok=False))
    run_long(c, 0, 100)
    assert c.on_outcome("declined", at(105)) == "idle"
    assert run_long(c, 200, 400) == []  # cooldown still held in memory
    assert c.set_enabled(False) == "disabled"


def test_cooldown_applies_after_timeout_and_is_persisted() -> None:
    store = MemoryStore()
    c = new(store=store)
    run_long(c, 0, 100)  # suggested at 60
    assert c.tick(at(130)) == "idle"
    assert store.state.cooldown_until == at(130 + CFG.cooldown_s)


# --- silent long tools and waiting -------------------------------------------


def test_silent_long_tool_invites_on_tick_not_on_completion() -> None:
    c = new()
    assert c.on_event(ev(0, "turn_submitted")) is None
    assert c.on_event(ev(6, "tool_started")) is None
    assert c.tick(at(30)) is None
    assert c.tick(at(59)) is None
    assert c.tick(at(60)) == "suggested"
    assert c.tick(at(61)) is None


def test_waiting_session_blocks_invitations() -> None:
    c = new()
    c.on_event(ev(0, "turn_submitted", "a"))
    c.on_event(ev(10, "waiting_input", "b"))
    assert [c.on_event(ev(t, session="a")) for t in range(10, 200, 10)] == [None] * 19
    assert c.tick(at(200)) is None
    assert c.activity(at(200)) == "waiting_input"


def test_suggestion_withdrawn_when_a_session_starts_waiting() -> None:
    store = MemoryStore()
    c = new(store=store)
    assert run_long(c, 0, 100) == ["suggested"]
    assert c.estimate is not None
    assert c.on_event(ev(105, "waiting_input", "b")) == "idle"
    assert c.estimate is None
    assert store.state.cooldown_until is None  # no cooldown for a withdrawal


def test_suggestion_from_a_tick_is_withdrawn_by_a_waiting_event() -> None:
    c = new()
    c.on_event(ev(0, "turn_submitted", "a"))
    c.on_event(ev(5, "tool_started", "a"))
    assert c.tick(at(60)) == "suggested"
    assert c.on_event(ev(61, "waiting_input", "a")) == "idle"


def test_withdrawn_run_is_not_reinvited_but_a_new_turn_is() -> None:
    c = new()
    run_long(c, 0, 100)  # s1 suggested
    c.on_event(ev(105, "waiting_input", "b"))
    assert c.state == "idle"
    c.on_event(ev(106, "turn_stopped", "b"))  # waiting over
    later = [c.on_event(ev(t)) for t in range(110, 250, 10)]
    assert "suggested" not in later  # same run stays spent
    assert run_long(c, 300, 400) == ["suggested"]  # a new turn is a new run


def test_withdrawn_suggestion_allows_another_session_immediately() -> None:
    c = new()
    run_long(c, 0, 100)
    c.on_event(ev(105, "waiting_input", "b"))
    c.on_event(ev(106, "turn_stopped", "b"))
    c.on_event(ev(110, "turn_submitted", "c"))
    results = [c.on_event(ev(t, session="c")) for t in range(120, 200, 10)]
    assert "suggested" in results


def test_estimate_reflects_the_engine_bucket() -> None:
    cfg = TriggerConfig(activity_threshold_s=350, min_tool_events=2)
    c = new(cfg)
    c.on_event(ev(0, "turn_submitted"))
    c.on_event(ev(0, "task_created"))
    c.on_event(ev(1, "task_created"))
    for t in (100, 200, 300):
        c.on_event(ev(t))
    c.on_event(ev(310, "task_completed"))  # one task left, 310 s per task
    c.on_event(ev(340))
    assert c.tick(at(350)) == "suggested"
    assert c.estimate == "5_10m"


def test_estimate_is_none_outside_suggested() -> None:
    c = new()
    assert c.estimate is None
    run_long(c, 0, 100)
    c.on_outcome("declined", at(105))
    assert c.estimate is None


def test_suggestion_from_late_event_lasts_full_timeout_from_last_tick() -> None:
    # Events stamped earlier than the latest tick (late IPC delivery) must not
    # shorten the time the invitation stays up.
    c = new()
    c.tick(at(100))
    c.on_event(ev(0, "turn_submitted"))
    c.on_event(ev(30))
    assert c.on_event(ev(61)) == "suggested"
    assert c.tick(at(159)) is None
    assert c.tick(at(160)) == "idle"


def test_closed_session_key_reused_can_be_invited_again() -> None:
    cfg = TriggerConfig(
        activity_threshold_s=60,
        min_tool_events=2,
        cooldown_s=10,
        suggestion_timeout_s=60,
    )
    c = new(cfg)
    assert "suggested" in run_long(c, 0, 70)
    c.on_outcome("declined", at(71))
    c.on_event(ev(72, "session_closed"))
    # Same key, fresh session: its run_id restarts, so a stale spent pair
    # would block it forever.
    assert "suggested" in run_long(c, 100, 170)


def test_unsafe_state_dir_is_reported_on_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    d = tmp_path / "state" / "breakroom"
    d.mkdir(parents=True, mode=0o700)
    d.chmod(0o770)
    Controller()
    assert capsys.readouterr().err.strip()

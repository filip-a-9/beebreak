from collections.abc import Collection, Mapping
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from breakroom_companion.activity import SessionActivity
from breakroom_companion.config import TriggerConfig
from breakroom_companion.decision import Decision, DecisionResult, HeuristicEngine
from breakroom_shared.ipc import ActivityState

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
CFG = TriggerConfig(activity_threshold_s=60, min_tool_events=2)


def sess(
    *,
    state: ActivityState = "active",
    age_s: float = 120,
    run_id: int = 1,
    events: int = 5,
    open_tools: int = 0,
    open_tasks: int = 0,
    intervals: tuple[float, ...] = (),
) -> SessionActivity:
    return SessionActivity(
        state=state,
        run_id=run_id,
        run_started_at=NOW - timedelta(seconds=age_s) if state == "active" else None,
        run_tool_events=events,
        open_tools=open_tools,
        open_tasks=open_tasks,
        task_intervals_s=intervals,
        last_event_at=NOW,
    )


def decide(
    sessions: Mapping[str, SessionActivity],
    exclude: Collection[tuple[str, int]] = (),
) -> Decision:
    return HeuristicEngine(CFG).decide(sessions, NOW, exclude)


def test_sustained_activity_is_suitable() -> None:
    d = decide({"a": sess()})
    assert d.result.invite_suitable
    assert d.result.reason_code == "sustained_activity"
    assert d.result.task_category == "other"
    assert d.result.confidence == 1.0
    assert (d.session_key, d.run_id) == ("a", 1)


def test_empty_is_insufficient() -> None:
    d = decide({})
    assert not d.result.invite_suitable
    assert d.result.reason_code == "insufficient_evidence"
    assert d.session_key is None and d.run_id is None


def test_waiting_in_one_session_blocks_other() -> None:
    d = decide({"a": sess(), "b": sess(state="waiting_input")})
    assert not d.result.invite_suitable
    assert d.result.reason_code == "waiting_for_user"
    assert d.session_key is None and d.run_id is None


def test_exactly_at_threshold_qualifies() -> None:
    assert decide({"a": sess(age_s=60)}).result.invite_suitable


def test_below_threshold_does_not() -> None:
    d = decide({"a": sess(age_s=59.9)})
    assert not d.result.invite_suitable
    assert d.result.reason_code == "insufficient_evidence"


@pytest.mark.parametrize("state", ["idle", "unknown"])
def test_non_active_not_candidate(state: ActivityState) -> None:
    assert not decide({"a": sess(state=state)}).result.invite_suitable


def test_too_few_events_without_open_tool() -> None:
    assert not decide({"a": sess(events=1)}).result.invite_suitable


def test_min_events_exact_qualifies() -> None:
    assert decide({"a": sess(events=2)}).result.invite_suitable


def test_single_open_tool_with_one_event_does_not_qualify() -> None:
    # Decision 2026-10-17: an open tool alone is not evidence (Esc sends nothing).
    assert not decide({"a": sess(events=1, open_tools=1)}).result.invite_suitable


def test_excluded_run_skipped_next_candidate_chosen() -> None:
    sessions = {"a": sess(age_s=300), "b": sess(age_s=100, run_id=7)}
    d = decide(sessions, exclude=[("a", 1)])
    assert (d.session_key, d.run_id) == ("b", 7)


def test_exclude_matches_run_id_too() -> None:
    d = decide({"a": sess(run_id=2)}, exclude=[("a", 1)])
    assert d.session_key == "a"


def test_all_excluded_is_insufficient() -> None:
    d = decide({"a": sess()}, exclude={("a", 1)})
    assert not d.result.invite_suitable
    assert d.session_key is None


def test_oldest_run_wins() -> None:
    d = decide({"a": sess(age_s=100), "b": sess(age_s=500)})
    assert d.session_key == "b"


def test_tie_breaks_on_lower_key() -> None:
    d = decide({"b": sess(age_s=100), "a": sess(age_s=100)})
    assert d.session_key == "a"


@pytest.mark.parametrize(
    ("interval", "bucket", "suitable"),
    [
        (149, "under_5m", False),  # 2 tasks -> 298 s
        (150, "5_10m", True),  # 300
        (299, "5_10m", True),  # 598
        (300, "10_20m", True),  # 600
        (599, "10_20m", True),  # 1198
        (600, "20m_plus", True),  # 1200
    ],
)
def test_bucket_boundaries(interval: float, bucket: str, suitable: bool) -> None:
    d = decide({"a": sess(open_tasks=2, intervals=(interval,))})
    assert d.result.invite_suitable is suitable
    assert d.result.remaining_time_bucket == bucket


def test_under_5m_vetoes_with_no_session() -> None:
    d = decide({"a": sess(open_tasks=1, intervals=(10.0,))})
    assert not d.result.invite_suitable
    assert d.result.reason_code == "insufficient_evidence"
    assert d.session_key is None and d.run_id is None


def test_vetoed_oldest_candidate_is_skipped_for_next() -> None:
    d = decide(
        {
            "old": sess(age_s=300, open_tasks=1, intervals=(10.0,)),
            "young": sess(age_s=90),
        }
    )
    assert d.result.invite_suitable
    assert d.session_key == "young"
    assert d.result.remaining_time_bucket == "unknown"


def test_all_candidates_vetoed_is_insufficient() -> None:
    d = decide(
        {
            "a": sess(age_s=300, open_tasks=1, intervals=(10.0,)),
            "b": sess(age_s=90, open_tasks=2, intervals=(20.0,)),
        }
    )
    assert not d.result.invite_suitable
    assert d.session_key is None


def test_median_used_not_mean() -> None:
    # median 100, mean ~ 700; 3 tasks -> 300 s -> 5_10m
    d = decide({"a": sess(open_tasks=3, intervals=(100, 100, 2000))})
    assert d.result.remaining_time_bucket == "5_10m"


def test_median_even_count() -> None:
    # median (100+200)/2=150; 2 tasks -> 300
    d = decide({"a": sess(open_tasks=2, intervals=(100, 200))})
    assert d.result.remaining_time_bucket == "5_10m"


def test_no_tasks_unknown_and_suitable() -> None:
    d = decide({"a": sess(open_tasks=0, intervals=(10.0,))})
    assert d.result.remaining_time_bucket == "unknown"
    assert d.result.invite_suitable


def test_open_tasks_without_intervals_wait_for_evidence() -> None:
    # No task has completed yet: deciding now would invite with "unknown" and
    # spend the run before an estimate (or an under_5m veto) could exist.
    d = decide({"a": sess(age_s=299, open_tasks=3)})
    assert not d.result.invite_suitable
    assert d.result.reason_code == "insufficient_evidence"
    assert d.session_key is None


def test_open_tasks_without_intervals_unknown_after_wait() -> None:
    d = decide({"a": sess(age_s=300, open_tasks=3)})
    assert d.result.remaining_time_bucket == "unknown"
    assert d.result.invite_suitable


def test_waiting_for_evidence_is_per_candidate() -> None:
    sessions = {
        "a": sess(age_s=200, open_tasks=3),
        "b": sess(age_s=100),
    }
    d = decide(sessions)
    assert d.session_key == "b"
    assert d.result.invite_suitable


def test_estimate_is_of_chosen_session_only() -> None:
    sessions = {
        "a": sess(age_s=500, open_tasks=1, intervals=(1300.0,)),
        "b": sess(age_s=100, open_tasks=1, intervals=(10.0,)),
    }
    d = decide(sessions)
    assert d.session_key == "a"
    assert d.result.remaining_time_bucket == "20m_plus"


def test_result_rejects_bad_confidence_and_extra() -> None:
    base = {
        "invite_suitable": True,
        "confidence": 1.0,
        "task_category": "other",
        "remaining_time_bucket": "unknown",
        "reason_code": "other",
    }
    DecisionResult.model_validate(base)
    for bad in (-0.1, 1.1):
        with pytest.raises(ValidationError):
            DecisionResult.model_validate({**base, "confidence": bad})
    with pytest.raises(ValidationError):
        DecisionResult.model_validate({**base, "extra": 1})

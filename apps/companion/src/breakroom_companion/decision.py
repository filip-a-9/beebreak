"""Invitation decision contract and heuristic engine (spec §7.1-7.2).

Contract frozen by the M2 README; HeuristicEngine implemented by M2-03.
Engines never see cooldown, consent or outstanding suggestions: the
controller owns eligibility.
"""

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime
from statistics import median
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from breakroom_companion.activity import SessionActivity
from breakroom_companion.config import TriggerConfig
from breakroom_shared.presence import RemainingTimeBucket, TaskCategory


class DecisionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    invite_suitable: bool
    # Model score in [0, 1], not a calibrated probability.
    confidence: float = Field(ge=0, le=1)
    task_category: TaskCategory
    remaining_time_bucket: RemainingTimeBucket
    reason_code: Literal[
        "sustained_activity", "insufficient_evidence", "waiting_for_user", "other"
    ]


@dataclass(frozen=True)
class Decision:
    result: DecisionResult
    # The qualifying session and run when invite_suitable, else None.
    session_key: str | None
    run_id: int | None


class DecisionEngine(Protocol):
    def decide(
        self,
        sessions: Mapping[str, SessionActivity],
        now: datetime,
        exclude: Collection[tuple[str, int]] = (),
    ) -> Decision: ...


class HeuristicEngine:
    def __init__(self, config: TriggerConfig) -> None:
        self._config = config

    def decide(
        self,
        sessions: Mapping[str, SessionActivity],
        now: datetime,
        exclude: Collection[tuple[str, int]] = (),
    ) -> Decision:
        if any(s.state == "waiting_input" for s in sessions.values()):
            return _no("waiting_for_user")
        cfg = self._config
        candidates: list[tuple[datetime, str, SessionActivity]] = []
        for key, s in sessions.items():
            if s.state != "active" or s.run_started_at is None:
                continue
            if (key, s.run_id) in exclude:
                continue
            if (now - s.run_started_at).total_seconds() < cfg.activity_threshold_s:
                continue
            if s.run_tool_events < cfg.min_tool_events:
                continue
            candidates.append((s.run_started_at, key, s))
        if not candidates:
            return _no("insufficient_evidence")
        # Oldest run first, ties on the lower key; an under_5m estimate vetoes
        # only its own session, and the next candidate is tried.
        vetoed: RemainingTimeBucket = "unknown"
        for started, key, chosen in sorted(candidates, key=lambda c: (c[0], c[1])):
            if (
                chosen.open_tasks > 0
                and not chosen.task_intervals_s
                and (now - started).total_seconds() < cfg.task_evidence_wait_s
            ):
                # Tasks are open but none has completed: deciding now would
                # spend the run on an "unknown" estimate before a label or an
                # under_5m veto could exist. Wait for the first interval.
                continue
            bucket = _bucket(chosen)
            if bucket == "under_5m":
                vetoed = bucket
                continue
            return Decision(
                _result(True, "sustained_activity", bucket), key, chosen.run_id
            )
        return _no("insufficient_evidence", vetoed)


def _result(
    suitable: bool,
    reason: Literal["sustained_activity", "insufficient_evidence", "waiting_for_user"],
    bucket: RemainingTimeBucket = "unknown",
) -> DecisionResult:
    return DecisionResult(
        invite_suitable=suitable,
        confidence=1.0,
        task_category="other",
        remaining_time_bucket=bucket,
        reason_code=reason,
    )


def _no(
    reason: Literal["insufficient_evidence", "waiting_for_user"],
    bucket: RemainingTimeBucket = "unknown",
) -> Decision:
    return Decision(_result(False, reason, bucket), None, None)


def _bucket(s: SessionActivity) -> RemainingTimeBucket:
    if s.open_tasks <= 0 or not s.task_intervals_s:
        return "unknown"
    seconds = s.open_tasks * median(s.task_intervals_s)
    if seconds < 300:
        return "under_5m"
    if seconds < 600:
        return "5_10m"
    if seconds < 1200:
        return "10_20m"
    return "20m_plus"

"""Invitation decision contract and heuristic engine (spec §7.1-7.2).

Contract frozen by the M2 README; HeuristicEngine implemented by M2-03.
Engines never see cooldown, consent or outstanding suggestions: the
controller owns eligibility.
"""

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime
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
        raise NotImplementedError

"""Outbound queue presence (spec §4): the only task metadata that leaves the machine.

Every field is optional except identity and status, so a user with technical
sharing disabled sends presence alone. Free text has no field to live in.
"""

from collections.abc import Sequence
from typing import Literal, get_args
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from beebreak_shared.consent import Consent

TaskCategory = Literal["backend", "frontend", "devops", "data", "testing", "other"]

# Spec §7.2 names; §4's sample "10-20m" is the same bucket, spelled inconsistently.
RemainingTimeBucket = Literal["unknown", "under_5m", "5_10m", "10_20m", "20m_plus"]

# PROPOSED starter vocabulary (spec §4 rule 2); extend by review, never from input.
StackTag = Literal[
    "python",
    "javascript",
    "typescript",
    "go",
    "rust",
    "java",
    "kotlin",
    "csharp",
    "cpp",
    "ruby",
    "php",
    "swift",
    "react",
    "vue",
    "node",
    "django",
    "fastapi",
    "postgres",
    "docker",
    "kubernetes",
]


class OutboundPresence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    # Fresh per break, never derived from a Claude session ID.
    presence_id: UUID
    task_category: TaskCategory | None = None
    stack_tags: tuple[StackTag, ...] = Field(default=(), max_length=5)
    remaining_time_bucket: RemainingTimeBucket | None = None
    status: Literal["available"]


def build_presence(
    consent: Consent,
    *,
    task_category: TaskCategory | None,
    stack_tags: Sequence[StackTag],
    remaining_time_bucket: RemainingTimeBucket | None,
) -> OutboundPresence:
    """Presence with a fresh presence_id and only the consented fields. M3-04."""
    wanted = set(stack_tags)
    tags = tuple(t for t in get_args(StackTag) if t in wanted)[:5]
    return OutboundPresence(
        schema_version=1,
        presence_id=uuid4(),
        task_category=task_category if consent.share_task_category else None,
        stack_tags=tags if consent.share_stack_tags else (),
        remaining_time_bucket=(
            remaining_time_bucket if consent.share_time_bucket else None
        ),
        status="available",
    )

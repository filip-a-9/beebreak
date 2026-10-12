"""Normalized local agent events (spec §5). Never leaves the developer's machine."""

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict

EventType = Literal[
    "session_opened",
    "turn_submitted",
    "tool_started",
    "tool_completed",
    "task_created",
    "task_completed",
    "turn_stopped",
    "session_closed",
    "heartbeat",
]

# Broad categories only; a hook that cannot classify safely uses "other".
ToolClass = Literal["file_edit", "shell", "read", "other"]


class NormalizedEvent(BaseModel):
    # forbid: raw hook fields (cwd, prompt, tool_input, ...) must fail, not drop.
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_version: Literal[1]
    event_id: UUID
    local_session_key: str
    agent: Literal["claude_code"]
    type: EventType
    observed_at: AwareDatetime
    tool_class: ToolClass | None = None

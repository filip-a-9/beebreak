"""Strict, pure reducer: hook payload -> NormalizedEvent (spec §4 rule 1).

Reads exactly three keys: hook_event_name, session_id, tool_name.
"""

from collections.abc import Mapping
from datetime import datetime
from uuid import UUID

from breakroom_agent_bridge.session_key import derive_key
from breakroom_shared.events import EventType, NormalizedEvent, ToolClass

EVENT_TYPES: dict[str, EventType] = {
    "SessionStart": "session_opened",
    "UserPromptSubmit": "turn_submitted",
    "PreToolUse": "tool_started",
    "PostToolUse": "tool_completed",
    "TaskCreated": "task_created",
    "TaskCompleted": "task_completed",
    "Stop": "turn_stopped",
    "SessionEnd": "session_closed",
}

_TOOL_EVENTS = {"tool_started", "tool_completed"}

TOOL_CLASSES: dict[str, ToolClass] = {
    "Edit": "file_edit",
    "Write": "file_edit",
    "MultiEdit": "file_edit",
    "NotebookEdit": "file_edit",
    "Bash": "shell",
    "Read": "read",
    "Grep": "read",
    "Glob": "read",
    "LS": "read",
}


def reduce(
    payload: Mapping[str, object], *, salt: bytes, now: datetime, event_id: UUID
) -> NormalizedEvent | None:
    name = payload.get("hook_event_name")
    session_id = payload.get("session_id")
    if not isinstance(name, str) or not isinstance(session_id, str):
        return None
    event_type = EVENT_TYPES.get(name)
    if event_type is None:
        return None
    tool_class: ToolClass | None = None
    if event_type in _TOOL_EVENTS:
        tool = payload.get("tool_name")
        tool_class = (
            TOOL_CLASSES.get(tool, "other") if isinstance(tool, str) else "other"
        )
    return NormalizedEvent(
        event_version=1,
        event_id=event_id,
        local_session_key=derive_key(session_id, salt),
        agent="claude_code",
        type=event_type,
        observed_at=now,
        tool_class=tool_class,
    )

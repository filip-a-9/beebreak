"""Realistic hook payloads with every free-text field filled with sensitive strings."""

from typing import Any

from tests.fixtures.privacy_negative import SENSITIVE_STRINGS

RAW_SESSION_ID = "sess-7f3a9c1e-RAWID-0042"

_S = SENSITIVE_STRINGS


def _base(event: str) -> dict[str, Any]:
    return {
        "session_id": RAW_SESSION_ID,
        "transcript_path": _S[0],
        "cwd": _S[3],
        "permission_mode": "default",
        "hook_event_name": event,
    }


def _tool(event: str, tool: str) -> dict[str, Any]:
    p = _base(event) | {
        "tool_name": tool,
        "tool_use_id": "toolu_01ABC",
        "tool_input": {"command": _S[5], "file_path": _S[1], "content": _S[6]},
    }
    if event == "PostToolUseFailure":
        p["error"] = _S[7]
    if event == "PostToolUse":
        p["tool_response"] = {"stdout": _S[7], "stderr": _S[6], "path": _S[2]}
    return p


PAYLOADS: dict[str, dict[str, Any]] = {
    "SessionStart": _base("SessionStart") | {"source": _S[3], "model": _S[4]},
    "UserPromptSubmit": _base("UserPromptSubmit") | {"prompt": _S[4] + _S[8]},
    "PreToolUse": _tool("PreToolUse", "Bash"),
    "PostToolUse": _tool("PostToolUse", "Edit"),
    "TaskCreated": _base("TaskCreated")
    | {"task_id": "1", "task_subject": _S[4], "task_description": _S[8]},
    "TaskCompleted": _base("TaskCompleted")
    | {"task_id": "1", "task_subject": _S[4], "task_description": _S[9]},
    "Stop": _base("Stop") | {"stop_hook_active": False, "message": _S[7]},
    "PermissionRequest": _tool("PermissionRequest", "Bash")
    | {"permission_suggestions": [{"type": "addRules", "rules": [_S[5]]}]},
    "Notification": _base("Notification")
    | {
        "message": _S[4],
        "title": _S[3],
        "notification_type": "permission_prompt",
    },
    "PostToolUseFailure": _tool("PostToolUseFailure", "Bash"),
    "StopFailure": _base("StopFailure")
    | {
        "error": "rate_limit",
        "error_details": _S[7],
        "last_assistant_message": _S[8],
    },
    "SessionEnd": _base("SessionEnd") | {"reason": _S[8]},
}

import json
from pathlib import Path
from typing import Any

HOOKS_JSON = (
    Path(__file__).parents[2] / "integrations" / "claude-code" / "hooks" / "hooks.json"
)
EXPECTED_EVENTS = {
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "TaskCreated",
    "TaskCompleted",
    "Stop",
    "SessionEnd",
    "PermissionRequest",
    "Notification",
    "PostToolUseFailure",
    "StopFailure",
}


def _commands() -> list[tuple[str, dict[str, Any]]]:
    config = json.loads(HOOKS_JSON.read_text())
    return [
        (event, hook)
        for event, groups in config["hooks"].items()
        for group in groups
        for hook in group["hooks"]
    ]


def test_registers_exactly_the_mapped_events() -> None:
    assert {event for event, _ in _commands()} == EXPECTED_EVENTS


def test_every_hook_is_async_and_bounded() -> None:
    for event, hook in _commands():
        assert hook["type"] == "command", event
        assert hook["async"] is True, event
        assert 0 < hook["timeout"] <= 5, event


def test_every_hook_runs_the_reducer_module() -> None:
    for event, hook in _commands():
        assert hook["command"].endswith(
            "/.venv/bin/python -m breakroom_agent_bridge.hook"
        ), event


def test_notification_matcher_is_exactly_the_two_waiting_types() -> None:
    config = json.loads(HOOKS_JSON.read_text())
    groups = config["hooks"]["Notification"]
    assert [g.get("matcher") for g in groups] == [
        "permission_prompt|elicitation_dialog"
    ]


def test_tool_hooks_match_all_tools() -> None:
    config = json.loads(HOOKS_JSON.read_text())
    for event in ("PermissionRequest", "PostToolUseFailure"):
        assert [g.get("matcher") for g in config["hooks"][event]] == ["*"], event

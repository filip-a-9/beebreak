from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from breakroom_agent_bridge.reducer import reduce
from breakroom_agent_bridge.session_key import derive_key
from breakroom_shared.events import NormalizedEvent
from tests.fixtures.hook_payloads import PAYLOADS, RAW_SESSION_ID
from tests.fixtures.privacy_negative import SENSITIVE_STRINGS

SALT = b"t" * 32
NOW = datetime(2026, 10, 12, 13, 20, tzinfo=UTC)


def _reduce(payload: Mapping[str, object]) -> NormalizedEvent | None:
    return reduce(payload, salt=SALT, now=NOW, event_id=uuid4())


def _must(payload: Mapping[str, object]) -> NormalizedEvent:
    event = _reduce(payload)
    assert event is not None
    return event


@pytest.mark.parametrize("name", PAYLOADS)
def test_event_contains_no_sensitive_string_or_raw_session_id(name: str) -> None:
    dumped = _must(PAYLOADS[name]).model_dump_json()
    for s in SENSITIVE_STRINGS:
        assert s not in dumped
    assert RAW_SESSION_ID not in dumped


@pytest.mark.parametrize(
    ("name", "etype"),
    [
        ("SessionStart", "session_opened"),
        ("UserPromptSubmit", "turn_submitted"),
        ("PreToolUse", "tool_started"),
        ("PostToolUse", "tool_completed"),
        ("TaskCreated", "task_created"),
        ("TaskCompleted", "task_completed"),
        ("Stop", "turn_stopped"),
        ("SessionEnd", "session_closed"),
    ],
)
def test_hook_event_maps_to_event_type(name: str, etype: str) -> None:
    event = _must(PAYLOADS[name])
    assert event.type == etype
    assert event.local_session_key == derive_key(RAW_SESSION_ID, SALT)
    assert event.observed_at == NOW
    assert event.agent == "claude_code"


@pytest.mark.parametrize(
    ("tool", "expected"),
    [
        ("Edit", "file_edit"),
        ("Write", "file_edit"),
        ("MultiEdit", "file_edit"),
        ("NotebookEdit", "file_edit"),
        ("Bash", "shell"),
        ("Read", "read"),
        ("Grep", "read"),
        ("Glob", "read"),
        ("LS", "read"),
        ("WebFetch", "other"),
        ("mcp__server__tool", "other"),
        ("", "other"),
    ],
)
@pytest.mark.parametrize("hook", ["PreToolUse", "PostToolUse"])
def test_tool_class_table(hook: str, tool: str, expected: str) -> None:
    event = _must(PAYLOADS[hook] | {"tool_name": tool})
    assert event.tool_class == expected


def test_missing_or_non_string_tool_name_is_other() -> None:
    payload = {k: v for k, v in PAYLOADS["PreToolUse"].items() if k != "tool_name"}
    assert _must(payload).tool_class == "other"
    assert _must(payload | {"tool_name": 5}).tool_class == "other"


@pytest.mark.parametrize(
    "name", ["SessionStart", "UserPromptSubmit", "TaskCreated", "Stop", "SessionEnd"]
)
def test_non_tool_events_have_no_tool_class(name: str) -> None:
    event = _must(PAYLOADS[name] | {"tool_name": "Bash"})
    assert event.tool_class is None


@pytest.mark.parametrize("name", ["Notification", "PreCompact", "", "stop"])
def test_unknown_event_gives_none(name: str) -> None:
    assert _reduce(PAYLOADS["Stop"] | {"hook_event_name": name}) is None


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {"session_id": "x"},
        {"hook_event_name": "Stop"},
        {"hook_event_name": 1, "session_id": "x"},
        {"hook_event_name": "Stop", "session_id": 7},
        {"hook_event_name": None, "session_id": None},
        {"hook_event_name": ["Stop"], "session_id": "x"},
    ],
)
def test_malformed_payload_gives_none(bad: dict[str, Any]) -> None:
    assert _reduce(bad) is None


class _SpyDict(dict[str, Any]):
    def __init__(self, data: Mapping[str, Any]) -> None:
        super().__init__(data)
        self.read: set[str] = set()

    def __getitem__(self, key: str) -> Any:
        self.read.add(key)
        return super().__getitem__(key)

    def get(self, key: str, default: Any = None) -> Any:
        self.read.add(key)
        return super().get(key, default)


def test_reducer_reads_only_allowlisted_keys() -> None:
    spy = _SpyDict(PAYLOADS["PostToolUse"])
    assert _reduce(spy) is not None
    assert spy.read <= {"hook_event_name", "session_id", "tool_name"}

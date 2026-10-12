import json
from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from breakroom_shared.events import NormalizedEvent

VALID_EVENT_JSON = """{
  "event_version": 1,
  "event_id": "0b6f3c2e-8a51-4c8e-9a43-5d2f1e7b9c10",
  "local_session_key": "7d1e4b6a-2f3c-4e5d-8a9b-0c1d2e3f4a5b",
  "agent": "claude_code",
  "type": "tool_completed",
  "observed_at": "2026-10-12T13:20:00Z",
  "tool_class": "file_edit"
}"""


def test_spec_sample_event_parses() -> None:
    event = NormalizedEvent.model_validate_json(VALID_EVENT_JSON)

    assert event.event_id == UUID("0b6f3c2e-8a51-4c8e-9a43-5d2f1e7b9c10")
    assert event.type == "tool_completed"
    assert event.tool_class == "file_edit"
    assert event.observed_at == datetime(2026, 10, 12, 13, 20, tzinfo=UTC)


# Raw Claude Code hook fields that carry paths, prompts or tool payloads.
RAW_HOOK_FIELDS = [
    ("session_id", "abc123"),
    ("cwd", "/home/dev/acme-secret-project"),
    ("transcript_path", "/home/dev/.claude/projects/x/t.jsonl"),
    ("tool_input", {"command": "cat .env"}),
    ("prompt", "fix the billing bug for client X"),
    ("last_assistant_message", "Done. I edited src/payments.py"),
]


@pytest.mark.parametrize(("field", "value"), RAW_HOOK_FIELDS)
def test_raw_hook_field_is_rejected(field: str, value: object) -> None:
    payload = json.loads(VALID_EVENT_JSON) | {field: value}

    with pytest.raises(ValidationError):
        NormalizedEvent.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("type", "file_written"),
        ("tool_class", "rm -rf build"),
        ("tool_class", "/src/app.py"),
        ("agent", "codex"),
        ("event_version", 2),
        ("observed_at", "2026-10-12T13:20:00"),
    ],
)
def test_value_outside_contract_is_rejected(field: str, value: object) -> None:
    payload = json.loads(VALID_EVENT_JSON) | {field: value}

    with pytest.raises(ValidationError):
        NormalizedEvent.model_validate(payload)

from uuid import UUID

import pytest
from pydantic import ValidationError

from breakroom_shared.presence import OutboundPresence
from tests.fixtures.privacy_negative import SENSITIVE_STRINGS

PRESENCE_ID = "3f2c9a4e-6b1d-4f8a-9c7e-2a5b8d0e1f34"

FULL_PRESENCE = {
    "schema_version": 1,
    "presence_id": PRESENCE_ID,
    "task_category": "backend",
    "stack_tags": ["python", "fastapi"],
    "remaining_time_bucket": "10_20m",
    "status": "available",
}


def test_full_presence_parses() -> None:
    presence = OutboundPresence.model_validate(FULL_PRESENCE)

    assert presence.presence_id == UUID(PRESENCE_ID)
    assert presence.stack_tags == ("python", "fastapi")
    assert presence.remaining_time_bucket == "10_20m"


def test_presence_with_technical_sharing_disabled_parses() -> None:
    presence = OutboundPresence.model_validate(
        {"schema_version": 1, "presence_id": PRESENCE_ID, "status": "available"}
    )

    assert presence.task_category is None
    assert presence.stack_tags == ()
    assert presence.remaining_time_bucket is None


@pytest.mark.parametrize(
    "field", ["prompt", "cwd", "repository", "file_path", "session_id", "plan"]
)
def test_unlisted_field_is_rejected(field: str) -> None:
    with pytest.raises(ValidationError):
        OutboundPresence.model_validate(FULL_PRESENCE | {field: "anything"})


@pytest.mark.parametrize("value", SENSITIVE_STRINGS)
@pytest.mark.parametrize(
    "field", ["presence_id", "task_category", "remaining_time_bucket", "status"]
)
def test_sensitive_string_in_scalar_field_is_rejected(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        OutboundPresence.model_validate(FULL_PRESENCE | {field: value})


@pytest.mark.parametrize("value", SENSITIVE_STRINGS)
def test_sensitive_string_as_stack_tag_is_rejected(value: str) -> None:
    with pytest.raises(ValidationError):
        OutboundPresence.model_validate(
            FULL_PRESENCE | {"stack_tags": ["python", value]}
        )


def test_unknown_schema_version_is_rejected() -> None:
    with pytest.raises(ValidationError):
        OutboundPresence.model_validate(FULL_PRESENCE | {"schema_version": 2})


def test_more_than_five_stack_tags_is_rejected() -> None:
    tags = ["python", "fastapi", "postgres", "docker", "react", "typescript"]

    with pytest.raises(ValidationError):
        OutboundPresence.model_validate(FULL_PRESENCE | {"stack_tags": tags})

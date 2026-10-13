import json
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from breakroom_shared.api import Consent
from breakroom_shared.presence import OutboundPresence, build_presence
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


# build_presence (M3-04)

ALLOWLIST = {
    "schema_version",
    "presence_id",
    "task_category",
    "stack_tags",
    "remaining_time_bucket",
    "status",
}


def _build(consent: Consent, **overrides: Any) -> OutboundPresence:
    args: dict[str, Any] = {
        "task_category": "backend",
        "stack_tags": ["python", "fastapi"],
        "remaining_time_bucket": "10_20m",
    }
    return build_presence(consent, **(args | overrides))


def test_build_presence_all_consent_off_sends_identity_and_status_only() -> None:
    presence = _build(Consent(version=1))

    assert set(presence.model_dump(exclude_none=True, exclude_defaults=True)) == {
        "schema_version",
        "presence_id",
        "status",
    }
    assert presence.task_category is None
    assert presence.stack_tags == ()
    assert presence.remaining_time_bucket is None
    assert presence.status == "available"


@pytest.mark.parametrize(
    ("flag", "field", "expected"),
    [
        ("share_task_category", "task_category", "backend"),
        ("share_stack_tags", "stack_tags", ("python", "fastapi")),
        ("share_time_bucket", "remaining_time_bucket", "10_20m"),
    ],
)
def test_build_presence_each_flag_adds_exactly_its_field(
    flag: str, field: str, expected: object
) -> None:
    presence = _build(Consent(version=1, **{flag: True}))

    dumped = presence.model_dump(exclude_none=True, exclude_defaults=True)
    assert set(dumped) == {"schema_version", "presence_id", "status", field}
    assert getattr(presence, field) == expected


def test_build_presence_ids_differ_per_call() -> None:
    consent = Consent(version=1)

    assert _build(consent).presence_id != _build(consent).presence_id


def test_build_presence_json_keys_are_exactly_the_allowlist() -> None:
    everything = Consent(
        version=1,
        share_task_category=True,
        share_stack_tags=True,
        share_time_bucket=True,
    )

    assert set(json.loads(_build(everything).model_dump_json())) == ALLOWLIST
    assert set(json.loads(_build(Consent(version=1)).model_dump_json())) == ALLOWLIST


def test_build_presence_tags_deduplicated_in_vocabulary_order_max_five() -> None:
    consent = Consent(version=1, share_stack_tags=True)
    tags = ["docker", "python", "docker", "react", "go", "rust", "java", "python"]

    presence = _build(consent, stack_tags=tags)

    # Vocabulary order: python, javascript, typescript, go, rust, java, ...
    assert presence.stack_tags == ("python", "go", "rust", "java", "react")


def test_build_presence_consented_field_with_no_value_stays_empty() -> None:
    presence = _build(Consent(version=1, share_task_category=True), task_category=None)

    assert presence.task_category is None

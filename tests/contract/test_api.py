from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from breakroom_shared.api import (
    WS_CLOSE,
    AuthVerifyRequest,
    ClientWsFrame,
    Consent,
    Me,
    NicknameUpdate,
    ServerWsFrame,
    WsAuth,
    WsReady,
)


def test_consent_defaults_all_off() -> None:
    consent = Consent(version=1)
    assert not (
        consent.share_task_category
        or consent.share_stack_tags
        or consent.share_time_bucket
    )


@pytest.mark.parametrize("nick", ["ab", "a" * 25, "a b", "<b>x</b>", "Fílip"])
def test_nickname_pattern_rejects(nick: str) -> None:
    with pytest.raises(ValidationError):
        NicknameUpdate(nickname=nick)


def test_me_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        Me.model_validate(
            {"nickname": None, "consent": None, "consented_at": None, "x": 1}
        )


def test_device_name_charset() -> None:
    AuthVerifyRequest(challenge_id=uuid4(), code="ABCD1234", device_name="wsl 1")
    with pytest.raises(ValidationError):
        AuthVerifyRequest(challenge_id=uuid4(), code="ABCD1234", device_name="a/b")


def test_client_frames_discriminate_on_type() -> None:
    frame = ClientWsFrame.model_validate_json(
        '{"type": "auth", "version": 1, "access_token": "bra_x"}'
    )
    assert isinstance(frame.root, WsAuth)
    with pytest.raises(ValidationError):
        ClientWsFrame.model_validate_json('{"type": "match", "version": 1}')


def test_server_frames_need_aware_time() -> None:
    frame = ServerWsFrame.model_validate(
        {
            "type": "session.ready",
            "version": 1,
            "event_id": str(uuid4()),
            "server_time": datetime.now(UTC).isoformat(),
            "nickname": None,
        }
    )
    assert isinstance(frame.root, WsReady)
    with pytest.raises(ValidationError):
        ServerWsFrame.model_validate(
            {
                "type": "pong",
                "version": 1,
                "event_id": str(uuid4()),
                "server_time": "2026-10-13T10:00:00",
            }
        )


def test_close_codes_are_private_range() -> None:
    assert all(4000 <= code < 5000 for code in WS_CLOSE.values())

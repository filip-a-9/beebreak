from datetime import UTC, datetime
from typing import get_args
from uuid import uuid4

import pytest
from pydantic import ValidationError

from beebreak_shared.api import (
    MAX_SERVER_FRAME_BYTES,
    MAX_WS_FRAME_BYTES,
    WS_CLOSE,
    AuthVerifyRequest,
    ChatStatus,
    ClientWsFrame,
    Consent,
    ErrorCode,
    MatchEndReason,
    Me,
    MessagePage,
    MessageView,
    NicknameUpdate,
    ReportRequest,
    SendMessageRequest,
    ServerWsFrame,
    WsAuth,
    WsChatMessage,
    WsChatStatus,
    WsReady,
)
from beebreak_shared.chat import MAX_MESSAGE_CHARS


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


# --- M5 chat contract ---------------------------------------------------------


def _chat_status(**overrides: object) -> dict[str, object]:
    status: dict[str, object] = {
        "match_id": str(uuid4()),
        "ends_at": "2026-10-14T12:15:00+00:00",
        "grace_at": "2026-10-14T12:13:00+00:00",
        "extensions_left": 2,
        "extension_s": 600,
        "extension": "none",
        "peer_online": True,
    }
    return status | overrides


def test_chat_status_rejects_negative_and_unknown_values() -> None:
    ChatStatus.model_validate(_chat_status())
    for bad in (
        {"extensions_left": -1},
        {"extension_s": 0},
        {"extension": "requested_by_bob"},
        {"peer_id": str(uuid4())},
    ):
        with pytest.raises(ValidationError):
            ChatStatus.model_validate(_chat_status(**bad))


def test_match_end_reason_never_names_block_or_report() -> None:
    assert set(get_args(MatchEndReason)) == {"ended", "expired", "disconnected"}


def test_new_error_codes() -> None:
    assert {
        "chat_ended",
        "no_extensions_left",
        "no_extension_request",
        "already_reported",
        "report_window_closed",
    } <= set(get_args(ErrorCode))


def test_send_message_request_bounds_raw_body() -> None:
    SendMessageRequest(client_message_id=uuid4(), body="x" * 4000)
    with pytest.raises(ValidationError):
        SendMessageRequest(client_message_id=uuid4(), body="x" * 4001)


def test_report_request_bounds() -> None:
    match_id = uuid4()
    ReportRequest(match_id=match_id, reason="spam", message_seqs=tuple(range(1, 51)))
    with pytest.raises(ValidationError):
        ReportRequest(
            match_id=match_id, reason="spam", message_seqs=tuple(range(1, 52))
        )
    with pytest.raises(ValidationError):
        ReportRequest.model_validate({"match_id": match_id, "reason": "rude"})
    with pytest.raises(ValidationError):
        ReportRequest(match_id=match_id, reason="spam", note="n" * 2001)


def test_message_page_holds_at_most_100() -> None:
    message = MessageView(
        seq=1,
        mine=True,
        body="hi",
        sent_at=datetime(2026, 10, 14, tzinfo=UTC),
        client_message_id=uuid4(),
    )
    MessagePage(messages=(message,) * 100, last_seq=1)
    with pytest.raises(ValidationError):
        MessagePage(messages=(message,) * 101, last_seq=1)


def test_chat_frames_parse_as_server_frames() -> None:
    now = datetime(2026, 10, 14, tzinfo=UTC).isoformat()
    match_id = str(uuid4())
    message = {
        "type": "chat.message",
        "version": 1,
        "event_id": str(uuid4()),
        "server_time": now,
        "match_id": match_id,
        "message": {
            "seq": 1,
            "mine": False,
            "body": "hi",
            "sent_at": now,
            "client_message_id": None,
        },
    }
    assert isinstance(ServerWsFrame.model_validate(message).root, WsChatMessage)
    status = {
        "type": "chat.status",
        "version": 1,
        "event_id": str(uuid4()),
        "server_time": now,
        "status": _chat_status(match_id=match_id),
        "change": "peer_offline",
    }
    assert isinstance(ServerWsFrame.model_validate(status).root, WsChatStatus)
    with pytest.raises(ValidationError):
        ServerWsFrame.model_validate(status | {"change": "typing"})


def test_longest_message_frame_fits_the_client_limit() -> None:
    body = "\U0001f600" * MAX_MESSAGE_CHARS
    frame = WsChatMessage(
        type="chat.message",
        version=1,
        event_id=uuid4(),
        server_time=datetime(2026, 10, 14, tzinfo=UTC),
        match_id=uuid4(),
        message=MessageView(
            seq=2**31 - 1,
            mine=True,
            body=body,
            sent_at=datetime(2026, 10, 14, tzinfo=UTC),
            client_message_id=uuid4(),
        ),
    )
    size = len(frame.model_dump_json().encode())
    assert MAX_WS_FRAME_BYTES < size <= MAX_SERVER_FRAME_BYTES

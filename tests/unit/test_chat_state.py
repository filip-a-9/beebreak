"""The chat reducer (M5-05): pure, table-driven over every event."""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID, uuid4

import pytest

from breakroom_companion.chat_state import (
    ChatEvent,
    ChatFrame,
    ChatState,
    Closed,
    Ended,
    Matched,
    Messages,
    PendingMessage,
    Reported,
    SendFailed,
    Sending,
    ServerTime,
    Settled,
    SignedOut,
    StatusEvent,
    reduce,
)
from breakroom_shared.api import MatchView, MessagePage, MessageView, WsChatMessage
from tests.fakes.chat import T0, chat_state, chat_status, message_view

MATCH_ID = UUID(int=1)
OTHER_ID = UUID(int=2)
CID = UUID(int=50)
OTHER_CID = UUID(int=51)
ENDS = T0 + timedelta(minutes=15)


def match_view(match_id: UUID = MATCH_ID, *, nickname: str = "bob") -> MatchView:
    return MatchView(
        match_id=match_id,
        peer_nickname=nickname,
        shared_tags=("python",),
        shared_category=None,
        ends_at=ENDS,
        chat=chat_status(match_id, ENDS),
        last_seq=0,
    )


def page(*messages: MessageView) -> MessagePage:
    return MessagePage(messages=messages, last_seq=max(m.seq for m in messages))


def chat_frame(match_id: UUID, message: MessageView) -> WsChatMessage:
    return WsChatMessage(
        type="chat.message",
        version=1,
        event_id=uuid4(),
        server_time=T0,
        match_id=match_id,
        message=message,
    )


def reduced(state: ChatState, event: ChatEvent) -> ChatState:
    return reduce(state, event, T0)


def live(**kwargs: object) -> ChatState:
    return chat_state(match_id=MATCH_ID, **kwargs)  # type: ignore[arg-type]


def ended(**kwargs: object) -> ChatState:
    return live(ended="ended", **kwargs)


M1 = message_view(1)
M2 = message_view(2, mine=True, client_message_id=CID)
M3 = message_view(3)
SENDING = PendingMessage(CID, "hi", "sending")
FAILED = PendingMessage(OTHER_CID, "yo", "failed")
FAILED_SENDING = PendingMessage(CID, "hi", "failed")
NEW_STATUS = chat_status(
    MATCH_ID, ENDS + timedelta(minutes=10), extension="requested_by_peer"
)

# --- table: (name, state, event, expected) over every event ---

TABLE: list[tuple[str, ChatState, ChatEvent, ChatState]] = [
    # matched
    (
        "matched_from_nothing",
        ChatState(),
        Matched(match_view()),
        ChatState(
            match_id=MATCH_ID,
            peer_nickname="bob",
            status=chat_status(MATCH_ID, ENDS),
        ),
    ),
    (
        "matched_keeps_offset_for_a_new_chat",
        ChatState(server_offset_s=2.5),
        Matched(match_view()),
        ChatState(
            match_id=MATCH_ID,
            peer_nickname="bob",
            status=chat_status(MATCH_ID, ENDS),
            server_offset_s=2.5,
        ),
    ),
    (
        "matched_new_id_replaces_a_live_chat",
        live(messages=(M1,), pending=(SENDING,), reported=True),
        Matched(match_view(OTHER_ID, nickname="eve")),
        ChatState(
            match_id=OTHER_ID,
            peer_nickname="eve",
            status=chat_status(OTHER_ID, ENDS),
        ),
    ),
    (
        "matched_new_id_replaces_an_ended_chat",
        ended(messages=(M1,)),
        Matched(match_view(OTHER_ID)),
        ChatState(
            match_id=OTHER_ID, peer_nickname="bob", status=chat_status(OTHER_ID, ENDS)
        ),
    ),
    (
        "matched_same_id_refreshes_status_and_keeps_messages",
        live(messages=(M1, M2), pending=(FAILED,), status=NEW_STATUS),
        Matched(match_view(nickname="bobby")),
        live(
            messages=(M1, M2),
            pending=(FAILED,),
            peer_nickname="bobby",
            status=chat_status(MATCH_ID, ENDS),
        ),
    ),
    (
        "matched_same_id_never_revives_an_ended_chat",
        ended(messages=(M1,)),
        Matched(match_view()),
        ended(messages=(M1,)),
    ),
    # messages
    (
        "messages_merge_sorted",
        live(messages=(M3,)),
        Messages(MATCH_ID, page(M1, M2)),
        live(messages=(M1, M2, M3)),
    ),
    (
        "messages_duplicates_are_dropped",
        live(messages=(M1, M2)),
        Messages(MATCH_ID, page(M1, M2, M3)),
        live(messages=(M1, M2, M3)),
    ),
    (
        "messages_out_of_order_page_is_sorted_by_seq_not_time",
        live(),
        Messages(
            MATCH_ID,
            page(
                message_view(2, sent_at=T0),
                message_view(1, sent_at=T0 + timedelta(hours=1)),
            ),
        ),
        live(
            messages=(
                message_view(1, sent_at=T0 + timedelta(hours=1)),
                message_view(2, sent_at=T0),
            )
        ),
    ),
    (
        "messages_echo_confirms_its_pending",
        live(pending=(SENDING, FAILED)),
        Messages(MATCH_ID, page(M2)),
        live(messages=(M2,), pending=(FAILED,)),
    ),
    (
        "messages_late_fill_applies_to_an_ended_chat",
        ended(messages=(M1,)),
        Messages(MATCH_ID, page(M2)),
        ended(messages=(M1, M2)),
    ),
    (
        "messages_other_match_ignored",
        live(messages=(M1,)),
        Messages(OTHER_ID, page(M2)),
        live(messages=(M1,)),
    ),
    (
        "messages_without_a_chat_ignored",
        ChatState(),
        Messages(MATCH_ID, page(M1)),
        ChatState(),
    ),
    # frame
    (
        "frame_appends",
        live(messages=(M1,)),
        ChatFrame(chat_frame(MATCH_ID, M3)),
        live(messages=(M1, M3)),
    ),
    (
        "frame_with_a_gap_is_kept_in_order",
        live(messages=(M3,)),
        ChatFrame(chat_frame(MATCH_ID, M1)),
        live(messages=(M1, M3)),
    ),
    (
        "frame_duplicate_ignored",
        live(messages=(M1,)),
        ChatFrame(chat_frame(MATCH_ID, M1)),
        live(messages=(M1,)),
    ),
    (
        "frame_echo_confirms_pending",
        live(pending=(SENDING,)),
        ChatFrame(chat_frame(MATCH_ID, M2)),
        live(messages=(M2,)),
    ),
    (
        "frame_from_the_peer_never_confirms_pending",
        live(pending=(SENDING,)),
        ChatFrame(chat_frame(MATCH_ID, M1)),
        live(messages=(M1,), pending=(SENDING,)),
    ),
    (
        "frame_other_match_ignored",
        live(),
        ChatFrame(chat_frame(OTHER_ID, M1)),
        live(),
    ),
    # status
    ("status_replaces", live(), StatusEvent(NEW_STATUS), live(status=NEW_STATUS)),
    (
        "status_other_match_ignored",
        live(),
        StatusEvent(chat_status(OTHER_ID, ENDS)),
        live(),
    ),
    ("status_after_the_end_ignored", ended(), StatusEvent(NEW_STATUS), ended()),
    (
        "status_without_a_chat_ignored",
        ChatState(),
        StatusEvent(NEW_STATUS),
        ChatState(),
    ),
    # sending
    ("sending_adds", live(), Sending(CID, "hi"), live(pending=(SENDING,))),
    (
        "sending_queues_in_order",
        live(pending=(FAILED,)),
        Sending(CID, "hi"),
        live(pending=(FAILED, SENDING)),
    ),
    (
        "sending_again_is_a_retry_in_place",
        live(pending=(PendingMessage(CID, "hi", "failed"), FAILED)),
        Sending(CID, "hi"),
        live(pending=(SENDING, FAILED)),
    ),
    ("sending_after_the_end_ignored", ended(), Sending(CID, "hi"), ended()),
    ("sending_without_a_chat_ignored", ChatState(), Sending(CID, "hi"), ChatState()),
    # send_failed
    (
        "send_failed_marks_only_that_message",
        live(pending=(SENDING, PendingMessage(OTHER_CID, "yo", "sending"))),
        SendFailed(CID),
        live(
            pending=(
                PendingMessage(CID, "hi", "failed"),
                PendingMessage(OTHER_CID, "yo", "sending"),
            )
        ),
    ),
    (
        "send_failed_unknown_id_ignored",
        live(pending=(SENDING,)),
        SendFailed(OTHER_CID),
        live(pending=(SENDING,)),
    ),
    # ended
    (
        "ended_by_frame",
        live(messages=(M1,)),
        Ended(MATCH_ID, "expired"),
        live(messages=(M1,), ended="expired"),
    ),
    (
        "ended_by_sync_without_the_match",
        live(),
        Ended(None, None),
        live(ended="ended"),
    ),
    (
        "ended_by_sync_shows_every_pending_as_sending_until_settled",
        live(pending=(FAILED,)),
        Ended(None, None, hold_pending=True),
        live(pending=(PendingMessage(OTHER_CID, "yo", "sending"),), ended="ended"),
    ),
    (
        "settled_fails_what_is_still_pending",
        ended(pending=(SENDING,)),
        Settled(MATCH_ID),
        ended(pending=(FAILED_SENDING,)),
    ),
    (
        "settled_for_another_match_or_a_live_chat_changes_nothing",
        live(pending=(SENDING,)),
        Settled(MATCH_ID),
        live(pending=(SENDING,)),
    ),
    (
        "ended_fails_every_pending_message",
        live(pending=(SENDING, FAILED)),
        Ended(MATCH_ID, "disconnected"),
        live(
            pending=(PendingMessage(CID, "hi", "failed"), FAILED),
            ended="disconnected",
        ),
    ),
    (
        "ended_none_reason_means_ended",
        live(),
        Ended(MATCH_ID, None),
        live(ended="ended"),
    ),
    (
        "ended_twice_keeps_the_first_reason",
        ended(),
        Ended(MATCH_ID, "expired"),
        ended(),
    ),
    ("ended_other_match_ignored", live(), Ended(OTHER_ID, "ended"), live()),
    ("ended_without_a_chat_ignored", ChatState(), Ended(None, None), ChatState()),
    # reported
    ("reported_live", live(), Reported(MATCH_ID), live(reported=True)),
    ("reported_ended", ended(), Reported(MATCH_ID), ended(reported=True)),
    ("reported_other_match_ignored", live(), Reported(OTHER_ID), live()),
    # server time
    (
        "server_time_sets_the_offset_with_a_chat",
        live(),
        ServerTime(T0 + timedelta(seconds=3), T0),
        live(server_offset_s=3.0),
    ),
    (
        "server_time_sets_a_negative_offset_without_a_chat",
        ChatState(),
        ServerTime(T0 - timedelta(milliseconds=500), T0),
        ChatState(server_offset_s=-0.5),
    ),
    (
        "server_time_applies_to_an_ended_chat",
        ended(),
        ServerTime(T0 + timedelta(seconds=1), T0),
        ended(server_offset_s=1.0),
    ),
    # closed
    ("closed_while_live_ignored", live(messages=(M1,)), Closed(), live(messages=(M1,))),
    (
        "closed_ended_forgets",
        ended(messages=(M1,), reported=True),
        Closed(),
        ChatState(),
    ),
    (
        "closed_without_a_chat_keeps_the_offset",
        ChatState(server_offset_s=1.0),
        Closed(),
        ChatState(server_offset_s=1.0),
    ),
    # signed out
    (
        "signed_out_from_live",
        live(messages=(M1,), pending=(SENDING,)),
        SignedOut(),
        ChatState(),
    ),
    ("signed_out_from_ended", ended(server_offset_s=4.0), SignedOut(), ChatState()),
    ("signed_out_from_nothing", ChatState(), SignedOut(), ChatState()),
]


@pytest.mark.parametrize(
    ("state", "event", "expected"),
    [pytest.param(s, e, x, id=name) for name, s, e, x in TABLE],
)
def test_reduce_table(state: ChatState, event: ChatEvent, expected: ChatState) -> None:
    assert reduce(state, event, T0) == expected


def test_the_table_covers_every_event_type_in_live_and_ended_states() -> None:
    events = {type(event) for _, _, event, _ in TABLE}
    assert events == {
        Matched,
        Messages,
        ChatFrame,
        StatusEvent,
        Sending,
        SendFailed,
        Ended,
        Reported,
        ServerTime,
        Closed,
        SignedOut,
        Settled,
    }
    assert {s.ended is None for _, s, _, _ in TABLE if s.match_id} == {True, False}


def test_reduce_never_mutates_or_returns_a_changed_state_for_a_no_op() -> None:
    state = live(messages=(M1,), pending=(SENDING,))

    assert reduced(state, Messages(MATCH_ID, page(M1))) is state
    assert reduced(state, Reported(OTHER_ID)) is state
    assert state == live(messages=(M1,), pending=(SENDING,))


def test_a_send_confirmed_by_reply_then_echo_leaves_one_message() -> None:
    state = reduced(live(), Sending(CID, "hi"))
    state = reduced(state, Messages(MATCH_ID, page(M2)))  # the HTTP reply
    state = reduced(state, ChatFrame(chat_frame(MATCH_ID, M2)))  # the frame

    assert state.messages == (M2,) and state.pending == ()


def test_a_full_scenario_gap_fill_after_frames() -> None:
    state = reduced(ChatState(), Matched(match_view()))
    state = reduced(state, ChatFrame(chat_frame(MATCH_ID, message_view(3))))
    state = reduced(state, ChatFrame(chat_frame(MATCH_ID, message_view(5))))
    state = reduced(
        state,
        Messages(
            MATCH_ID,
            page(
                message_view(1),
                message_view(2),
                message_view(3),
                message_view(4),
                message_view(5),
            ),
        ),
    )

    assert [m.seq for m in state.messages] == [1, 2, 3, 4, 5]


def test_a_pending_message_the_server_stored_shows_once_after_the_end() -> None:
    state = reduced(live(), Sending(CID, "hi"))
    state = reduced(state, Ended(None, None))
    assert [p.status for p in state.pending] == ["failed"]  # until the gap fill
    stored = message_view(1, mine=True, body="hi", client_message_id=CID)
    state = reduced(state, Messages(MATCH_ID, page(stored)))
    assert [m.seq for m in state.messages] == [1]
    assert state.pending == ()

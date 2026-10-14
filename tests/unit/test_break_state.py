from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from breakroom_companion.break_state import (
    BreakEvent,
    BreakPhase,
    BreakState,
    Dismiss,
    Frame,
    Joined,
    JoinFailed,
    Joining,
    SignedOut,
    Sync,
    reduce,
)
from breakroom_shared.api import (
    MatchView,
    QueueEntryView,
    WsMatchCreated,
    WsMatchEnded,
    WsQueueEnded,
    WsQueueJoined,
)
from tests.fakes.chat import chat_status

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
ENTRY = QueueEntryView(
    entry_id=UUID(int=1), status="queued", expires_at=NOW + timedelta(seconds=90)
)
OTHER_ENTRY = ENTRY.model_copy(update={"entry_id": UUID(int=2)})
MATCH = MatchView(
    match_id=UUID(int=10),
    peer_nickname="bobby",
    shared_tags=("python",),
    ends_at=NOW + timedelta(minutes=15),
    chat=chat_status(UUID(int=10), NOW + timedelta(minutes=15)),
    last_seq=0,
)
OTHER_MATCH = MATCH.model_copy(update={"match_id": UUID(int=11)})


def common() -> dict[str, Any]:
    return {"version": 1, "event_id": uuid4(), "server_time": NOW}


def queue_joined(entry: QueueEntryView) -> Frame:
    return Frame(WsQueueJoined(type="queue.joined", entry=entry, **common()))


def queue_ended(entry_id: UUID, reason: Any) -> Frame:
    return Frame(
        WsQueueEnded(type="queue.ended", entry_id=entry_id, reason=reason, **common())
    )


def match_created(match: MatchView) -> Frame:
    return Frame(
        WsMatchCreated(
            type="match.created", entry_id=ENTRY.entry_id, match=match, **common()
        )
    )


def match_ended(match_id: UUID) -> Frame:
    return Frame(
        WsMatchEnded(type="match.ended", match_id=match_id, reason="ended", **common())
    )


STATES: dict[BreakPhase, BreakState] = {
    "none": BreakState(),
    "joining": BreakState(phase="joining"),
    "searching": BreakState(phase="searching", entry=ENTRY),
    "matched": BreakState(phase="matched", match=MATCH),
    "nobody_available": BreakState(phase="nobody_available"),
    "ended": BreakState(phase="ended", match=MATCH),
}

EVENTS: dict[str, BreakEvent] = {
    "joining": Joining(),
    "joined": Joined(ENTRY),
    "join_failed_other": JoinFailed("rate_limited"),
    "join_failed_already_queued": JoinFailed("already_queued"),
    "join_failed_already_matched": JoinFailed("already_matched"),
    "sync_nothing": Sync(None, None),
    "sync_nothing_in_flight": Sync(None, None, join_in_flight=True),
    "sync_queue": Sync(ENTRY, None),
    "sync_match": Sync(None, MATCH),
    "queue_joined_current": queue_joined(ENTRY),
    "queue_joined_other": queue_joined(OTHER_ENTRY),
    "queue_ended_expired": queue_ended(ENTRY.entry_id, "expired"),
    "queue_ended_cancelled": queue_ended(ENTRY.entry_id, "cancelled"),
    "queue_ended_other": queue_ended(OTHER_ENTRY.entry_id, "expired"),
    "match_created": match_created(MATCH),
    "match_ended_current": match_ended(MATCH.match_id),
    "match_ended_other": match_ended(OTHER_MATCH.match_id),
    "signed_out": SignedOut(),
    "dismiss": Dismiss(),
}

NONE = BreakState()
SEARCHING = BreakState(phase="searching", entry=ENTRY)
MATCHED = BreakState(phase="matched", match=MATCH)

# Every pair not listed here must leave the state unchanged.
CHANGES: dict[tuple[BreakPhase, str], BreakState] = {
    ("none", "joining"): BreakState(phase="joining"),
    ("nobody_available", "joining"): BreakState(phase="joining"),
    ("ended", "joining"): BreakState(phase="joining"),
    ("joining", "joined"): SEARCHING,
    ("joining", "join_failed_other"): NONE,
    ("joining", "join_failed_already_queued"): NONE,
    ("joining", "join_failed_already_matched"): NONE,
    ("joining", "sync_nothing"): NONE,
    ("none", "sync_queue"): SEARCHING,
    ("none", "sync_match"): MATCHED,
    ("joining", "sync_queue"): SEARCHING,
    ("joining", "sync_match"): MATCHED,
    ("searching", "sync_nothing"): NONE,
    ("searching", "sync_nothing_in_flight"): NONE,
    # The match ended while this device was offline (M5-05).
    ("matched", "sync_nothing_in_flight"): BreakState(phase="ended", match=MATCH),
    ("searching", "sync_match"): MATCHED,
    ("matched", "sync_nothing"): BreakState(phase="ended", match=MATCH),
    ("matched", "sync_queue"): SEARCHING,
    ("none", "queue_joined_current"): SEARCHING,
    ("joining", "queue_joined_current"): SEARCHING,
    ("none", "queue_joined_other"): BreakState(phase="searching", entry=OTHER_ENTRY),
    ("joining", "queue_joined_other"): BreakState(phase="searching", entry=OTHER_ENTRY),
    ("searching", "queue_ended_expired"): BreakState(phase="nobody_available"),
    ("searching", "queue_ended_cancelled"): NONE,
    ("none", "match_created"): MATCHED,  # another device's queue.joined was missed
    ("searching", "match_created"): MATCHED,
    ("joining", "match_created"): MATCHED,
    ("matched", "match_ended_current"): BreakState(phase="ended", match=MATCH),
    ("ended", "dismiss"): NONE,
    ("nobody_available", "dismiss"): NONE,
}
for _phase in STATES:
    if _phase != "none":
        CHANGES[(_phase, "signed_out")] = NONE


@pytest.mark.parametrize("event_name", EVENTS)
@pytest.mark.parametrize("phase", STATES)
def test_every_phase_and_event_pair(phase: BreakPhase, event_name: str) -> None:
    state = STATES[phase]

    result = reduce(state, EVENTS[event_name], NOW)

    assert result == CHANGES.get((phase, event_name), state)


def test_a_new_search_clears_the_previous_result() -> None:
    assert reduce(STATES["ended"], Joining(), NOW) == BreakState(phase="joining")


def test_join_reply_after_its_frame_changes_nothing() -> None:
    searching = reduce(STATES["joining"], queue_joined(ENTRY), NOW)

    assert reduce(searching, Joined(ENTRY), NOW) is searching


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("expired", BreakState(phase="nobody_available")),
        ("cancelled", BreakState()),
        ("matched", BreakState(phase="joining")),  # match.created follows
    ],
)
def test_an_idempotent_replay_of_a_finished_entry(
    status: str, expected: BreakState
) -> None:
    entry = ENTRY.model_copy(update={"status": status})

    assert reduce(STATES["joining"], Joined(entry), NOW) == expected


def test_a_match_from_another_device_wins_over_our_search() -> None:
    result = reduce(STATES["searching"], match_created(OTHER_MATCH), NOW)

    assert result == BreakState(phase="matched", match=OTHER_MATCH)


def test_reducing_does_not_mutate_the_input() -> None:
    state = STATES["searching"]

    reduce(state, EVENTS["queue_ended_expired"], NOW)

    assert state == BreakState(phase="searching", entry=ENTRY)


def test_second_device_never_sticks_in_joining_after_already_queued() -> None:
    # This device shows the end of a previous break; the other device's search
    # was announced by queue.joined, which "ended" ignores.
    state = BreakState(phase="ended", match=MATCH)
    state = reduce(state, queue_joined(ENTRY), NOW)
    state = reduce(state, Dismiss(), NOW)
    state = reduce(state, Joining(), NOW)

    state = reduce(state, JoinFailed("already_queued"), NOW)

    assert state.phase == "none"
    # A reconnect's sync shows the real search.
    assert reduce(state, Sync(ENTRY, None), NOW).phase == "searching"


def test_an_empty_sync_replaces_joining_only_when_no_request_is_outstanding() -> None:
    joining = BreakState(phase="joining")

    assert reduce(joining, Sync(None, None, join_in_flight=True), NOW) == joining
    assert reduce(joining, Sync(None, None), NOW) == BreakState()

"""Dev-only: replay a recorded event trace through the companion's Controller.

Usage: python tools/replay_trace.py TRACE.jsonl [--respond none|accept|decline]

Drives the public Controller API with a virtual clock (no sleeping, no wall
clock) and prints the timeline of changes in (invitation, activity, estimate),
with times relative to the first event, followed by one JSON summary line.
Configuration comes from TriggerConfig.from_env(), so the BREAKROOM_* variables
that tune the companion tune the replay too. Invalid trace lines are counted and
skipped; their content is never printed.
"""

import argparse
import json
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from breakroom_companion.config import TriggerConfig
from breakroom_companion.controller import Controller
from breakroom_companion.state_store import PersistedState
from breakroom_shared.events import NormalizedEvent
from breakroom_shared.ipc import ActivityState, InvitationState
from breakroom_shared.presence import RemainingTimeBucket

Respond = Literal["none", "accept", "decline"]

_ACTIVITY_STATES: tuple[ActivityState, ...] = (
    "unknown",
    "active",
    "waiting_input",
    "idle",
)


@dataclass(frozen=True)
class Transition:
    at: datetime
    invitation: InvitationState
    activity: ActivityState
    estimate: RemainingTimeBucket | None


class MemoryStateStore:
    """In-memory StateStore: replays never touch the real state file."""

    def __init__(self) -> None:
        self._state = PersistedState()

    def load(self) -> PersistedState:
        return self._state

    def save(self, state: PersistedState) -> bool:
        self._state = state
        return True


def _run(
    events: Sequence[NormalizedEvent],
    config: TriggerConfig,
    *,
    tick_s: float,
    tail_s: float,
    respond: Respond,
) -> tuple[list[Transition], datetime | None]:
    if tick_s <= 0 or tail_s < 0:
        raise ValueError("tick_s must be positive and tail_s non-negative")
    if not events:
        return [], None
    controller = Controller(config, store=MemoryStateStore())
    t0 = events[0].observed_at
    clock = t0
    k = 1
    transitions: list[Transition] = []
    last: tuple[InvitationState, ActivityState, RemainingTimeBucket | None] | None = (
        None
    )

    def record(now: datetime) -> None:
        nonlocal last
        current = (controller.state, controller.activity(now), controller.estimate)
        if current != last:
            last = current
            transitions.append(Transition(now, *current))

    def tick_until(limit: datetime) -> None:
        nonlocal clock, k
        while (t := t0 + timedelta(seconds=k * tick_s)) <= limit:
            k += 1
            clock = t
            if respond != "none" and controller.state == "suggested":
                controller.on_outcome(
                    "accepted" if respond == "accept" else "declined", t
                )
            else:
                controller.tick(t)
            record(t)

    for event in events:
        tick_until(event.observed_at)
        controller.on_event(event)
        # Out-of-order events stay out of order; time never runs backwards.
        clock = max(clock, event.observed_at)
        record(clock)
    end = clock + timedelta(seconds=tail_s)
    tick_until(end)
    return transitions, max(clock, end)


def replay(
    events: Sequence[NormalizedEvent],
    config: TriggerConfig,
    *,
    tick_s: float = 1.0,
    tail_s: float = 240.0,
    respond: Respond = "none",
) -> list[Transition]:
    """Feed events in order, ticking every tick_s of virtual time (and for
    tail_s after the last event); return every change of the observable state."""
    return _run(events, config, tick_s=tick_s, tail_s=tail_s, respond=respond)[0]


def load_events(lines: Iterable[str]) -> tuple[list[NormalizedEvent], int]:
    """Parse JSON Lines; return (events, number of invalid lines skipped)."""
    events: list[NormalizedEvent] = []
    invalid = 0
    for line in lines:
        if not line.strip():
            continue
        try:
            events.append(NormalizedEvent.model_validate_json(line))
        except (ValidationError, ValueError):
            invalid += 1
    return events, invalid


def summarize(
    transitions: Sequence[Transition], end: datetime | None, invalid: int
) -> dict[str, object]:
    invitations = 0
    first: float | None = None
    seconds: dict[str, float] = {s: 0.0 for s in _ACTIVITY_STATES}
    if transitions and end is not None:
        t0 = transitions[0].at
        prev_invitation: InvitationState | None = None
        for i, tr in enumerate(transitions):
            if tr.invitation == "suggested" and prev_invitation != "suggested":
                invitations += 1
                if first is None:
                    first = round((tr.at - t0).total_seconds(), 3)
            prev_invitation = tr.invitation
            until = transitions[i + 1].at if i + 1 < len(transitions) else end
            seconds[tr.activity] += (until - tr.at).total_seconds()
    return {
        "invitations": invitations,
        "first_invitation_s": first,
        "activity_s": {k: round(v, 3) for k, v in seconds.items()},
        "invalid_lines": invalid,
    }


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="replay_trace.py")
    parser.add_argument("trace", type=Path)
    parser.add_argument(
        "--respond", choices=["none", "accept", "decline"], default="none"
    )
    args = parser.parse_args(list(argv))
    try:
        with args.trace.open(encoding="utf-8", errors="replace") as f:
            events, invalid = load_events(f)
    except OSError:
        print("cannot read trace file", file=sys.stderr)
        return 2
    try:
        config = TriggerConfig.from_env()
    except ValueError:
        print("invalid BREAKROOM_* trigger setting", file=sys.stderr)
        return 2
    if not events:
        print("no valid events", file=sys.stderr)
        return 1
    # Long enough for a final waiting_input / quiet session to reach unknown.
    tail_s = max(240, config.waiting_max_s, config.stale_after_s) + 1
    transitions, end = _run(
        events, config, tick_s=1.0, tail_s=tail_s, respond=args.respond
    )
    t0 = transitions[0].at if transitions else None
    for tr in transitions:
        assert t0 is not None
        rel = (tr.at - t0).total_seconds()
        print(
            f"{rel:9.1f}s  invitation={tr.invitation}  activity={tr.activity}"
            f"  estimate={tr.estimate}"
        )
    print(json.dumps(summarize(transitions, end, invalid)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

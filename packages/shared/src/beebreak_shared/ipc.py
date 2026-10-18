"""Local IPC wire protocol between agent-side processes and the companion.

One frame per connection: the client writes a single UTF-8 JSON line of at most
MAX_FRAME_BYTES. For an EventFrame the server replies nothing and closes; for a
StatusRequest it replies one StatusReply line and closes; for a
LeaveQueueRequest it replies one LeaveQueueReply line and closes.
"""

from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, RootModel

from beebreak_shared.events import NormalizedEvent
from beebreak_shared.presence import RemainingTimeBucket

MAX_FRAME_BYTES = 16 * 1024

InvitationState = Literal["disabled", "idle", "suggested"]

# Aggregate local agent activity (spec §6.1). Never "finished": a stop is idle.
ActivityState = Literal["unknown", "active", "waiting_input", "idle"]

# The only break information exposed to the agent side (rule 14): no peer data.
# "chatting" (M5) replaces M4's "matched": the match is the chat.
QueueState = Literal["none", "searching", "chatting"]

LeaveResult = Literal["cancelled", "not_searching", "unavailable"]


class EventFrame(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["event"]
    event: NormalizedEvent


class StatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["status_request"]


class StatusReply(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["status"]
    version: Literal[4]
    invitation: InvitationState
    last_event_at: AwareDatetime | None = None
    activity: ActivityState = "unknown"
    # Remaining-time bucket of the current suggestion; None unless "suggested".
    estimate: RemainingTimeBucket | None = None
    queue: QueueState = "none"


class LeaveQueueRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["leave_queue"]


class LeaveQueueReply(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["leave_queue_result"]
    result: LeaveResult


class ClientFrame(
    RootModel[
        Annotated[
            EventFrame | StatusRequest | LeaveQueueRequest,
            Field(discriminator="kind"),
        ]
    ]
):
    """Server-side parser: ClientFrame.model_validate_json(line).root."""

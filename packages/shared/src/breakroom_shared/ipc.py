"""Local IPC wire protocol between agent-side processes and the companion.

One frame per connection: the client writes a single UTF-8 JSON line of at most
MAX_FRAME_BYTES. For an EventFrame the server replies nothing and closes; for a
StatusRequest it replies one StatusReply line and closes.
"""

from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, RootModel

from breakroom_shared.events import NormalizedEvent

MAX_FRAME_BYTES = 16 * 1024

InvitationState = Literal["disabled", "idle", "suggested"]


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
    version: Literal[1]
    invitation: InvitationState
    last_event_at: AwareDatetime | None = None


class ClientFrame(
    RootModel[Annotated[EventFrame | StatusRequest, Field(discriminator="kind")]]
):
    """Server-side parser: ClientFrame.model_validate_json(line).root."""

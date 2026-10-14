"""Chat status values for tests that build a MatchView by hand (M5)."""

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from breakroom_shared.api import ChatStatus


def chat_status(match_id: UUID, ends_at: datetime) -> ChatStatus:
    """A fresh chat: no extension used or pending, peer online."""
    return ChatStatus(
        match_id=match_id,
        ends_at=ends_at,
        grace_at=ends_at - timedelta(seconds=120),
        extensions_left=2,
        extension_s=600,
        extension="none",
        peer_online=True,
    )


def chat_json(match_id: UUID | str, ends_at: datetime | str) -> dict[str, Any]:
    """`chat_status` as wire JSON."""
    when = datetime.fromisoformat(ends_at) if isinstance(ends_at, str) else ends_at
    status = chat_status(UUID(str(match_id)), when)
    result: dict[str, Any] = status.model_dump(mode="json")
    return result

"""Beebreak MCP server: ``beebreak_status`` (read-only) and ``beebreak_leave_queue``.

Everything returned here enters Claude's context, so the result has a fixed,
tiny shape (THREAT_MODEL T9). No tool changes consent or accepts invitations
(T10); accepting is a GUI click only. ``beebreak_leave_queue`` can only cancel
a search the user started; no tool starts a search or ends a match (rule 14).
"""

from collections.abc import Callable
from typing import Literal, TypedDict

from mcp.server import MCPServer

from beebreak_agent_bridge.config import McpSettings
from beebreak_agent_bridge.ipc_client import (
    LegacyStatusReply,
    request_leave_queue,
    request_status,
)
from beebreak_shared.ipc import (
    ActivityState,
    InvitationState,
    LeaveResult,
    QueueState,
    StatusReply,
)
from beebreak_shared.paths import UnsafeRuntimeDir, socket_path
from beebreak_shared.presence import RemainingTimeBucket


class BeebreakStatus(TypedDict):
    companion: Literal["running", "not_running"]
    invitation: InvitationState | None
    last_event_at: str | None
    activity: ActivityState | None
    estimate: RemainingTimeBucket | None
    queue: QueueState | None


class LeaveQueueResult(TypedDict):
    result: LeaveResult | Literal["companion_not_running"]


StatusProvider = Callable[[], StatusReply | LegacyStatusReply | None]
LeaveProvider = Callable[[], LeaveResult | None]


def _no_companion() -> LeaveResult | None:
    return None


def build_server(
    status_provider: StatusProvider, leave_provider: LeaveProvider = _no_companion
) -> MCPServer:
    server = MCPServer("beebreak")

    @server.tool(
        name="beebreak_status",
        description=(
            "Report local Beebreak status: whether the companion app is running, "
            "its invitation state, the aggregate agent activity state and, while "
            "an invitation is showing, a coarse remaining-time bucket. "
            "queue is none, searching (looking for a break partner) or chatting "
            "(the user is in a break chat; Claude cannot see or take part in the "
            "chat). Read-only, no side effects."
        ),
    )
    def beebreak_status() -> BeebreakStatus:
        reply = status_provider()
        if reply is None:
            return {
                "companion": "not_running",
                "invitation": None,
                "last_event_at": None,
                "activity": None,
                "estimate": None,
                "queue": None,
            }
        return {
            "companion": "running",
            "invitation": reply.invitation,
            "last_event_at": (
                reply.last_event_at.isoformat() if reply.last_event_at else None
            ),
            "activity": reply.activity,
            "estimate": reply.estimate,
            "queue": reply.queue if isinstance(reply, StatusReply) else None,
        }

    @server.tool(
        name="beebreak_leave_queue",
        description=(
            "Cancel the break-partner search the user started in the Beebreak "
            "app. It cannot start a search and never ends a conversation: "
            "while the user is chatting it does nothing. Claude cannot see or "
            "take part in the chat."
        ),
    )
    def beebreak_leave_queue() -> LeaveQueueResult:
        result = leave_provider()
        return {"result": "companion_not_running" if result is None else result}

    return server


def query_companion(timeout_s: float) -> StatusReply | LegacyStatusReply | None:
    """Ask the companion for its status; None (not running) on any failure."""
    try:
        path = socket_path()
    except (UnsafeRuntimeDir, OSError):
        return None
    return request_status(socket_path=path, timeout=timeout_s)


def leave_queue_companion(timeout_s: float) -> LeaveResult | None:
    """Ask the companion to cancel a search; None when it is not running."""
    try:
        path = socket_path()
    except (UnsafeRuntimeDir, OSError):
        return None
    return request_leave_queue(socket_path=path, timeout=timeout_s)


def main() -> None:
    timeout_s = McpSettings.from_env().status_timeout_s
    build_server(
        lambda: query_companion(timeout_s),
        lambda: leave_queue_companion(timeout_s),
    ).run("stdio")


if __name__ == "__main__":
    main()

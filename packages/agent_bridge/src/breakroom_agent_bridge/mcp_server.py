"""Breakroom MCP server: one read-only tool, ``breakroom_status``.

Everything returned here enters Claude's context, so the result has a fixed,
tiny shape (THREAT_MODEL T9). No tool changes consent or accepts invitations
(T10); accepting is a GUI click only.
"""

from collections.abc import Callable
from typing import Literal, TypedDict

from mcp.server import MCPServer

from breakroom_agent_bridge.config import McpSettings
from breakroom_agent_bridge.ipc_client import request_status
from breakroom_shared.ipc import ActivityState, InvitationState, StatusReply
from breakroom_shared.paths import UnsafeRuntimeDir, socket_path
from breakroom_shared.presence import RemainingTimeBucket


class BreakroomStatus(TypedDict):
    companion: Literal["running", "not_running"]
    invitation: InvitationState | None
    last_event_at: str | None
    activity: ActivityState | None
    estimate: RemainingTimeBucket | None


def build_server(status_provider: Callable[[], StatusReply | None]) -> MCPServer:
    server = MCPServer("breakroom")

    @server.tool(
        name="breakroom_status",
        description=(
            "Report local Breakroom status: whether the companion app is running, "
            "its invitation state, the aggregate agent activity state and, while "
            "an invitation is showing, a coarse remaining-time bucket. "
            "Read-only, no side effects."
        ),
    )
    def breakroom_status() -> BreakroomStatus:
        reply = status_provider()
        if reply is None:
            return {
                "companion": "not_running",
                "invitation": None,
                "last_event_at": None,
                "activity": None,
                "estimate": None,
            }
        return {
            "companion": "running",
            "invitation": reply.invitation,
            "last_event_at": (
                reply.last_event_at.isoformat() if reply.last_event_at else None
            ),
            "activity": reply.activity,
            "estimate": reply.estimate,
        }

    return server


def query_companion(timeout_s: float) -> StatusReply | None:
    """Ask the companion for its status; None (not running) on any failure."""
    try:
        path = socket_path()
    except (UnsafeRuntimeDir, OSError):
        return None
    return request_status(socket_path=path, timeout=timeout_s)


def main() -> None:
    timeout_s = McpSettings.from_env().status_timeout_s
    build_server(lambda: query_companion(timeout_s)).run("stdio")


if __name__ == "__main__":
    main()

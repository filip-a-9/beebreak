"""M1-01: the breakroom_status MCP tool (THREAT_MODEL T9, T10)."""

import ast
import asyncio
import json
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from breakroom_agent_bridge import ipc_client, mcp_server
from breakroom_agent_bridge.config import McpSettings
from breakroom_agent_bridge.ipc_client import LegacyStatusReply
from breakroom_agent_bridge.mcp_server import build_server, query_companion
from breakroom_shared.ipc import LeaveResult, StatusReply

STATUS_KEYS = {
    "companion",
    "invitation",
    "last_event_at",
    "activity",
    "estimate",
    "queue",
}
TOOLS = ["breakroom_status", "breakroom_leave_queue"]


def _call(reply: StatusReply | LegacyStatusReply | None) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        async with Client(build_server(lambda: reply)) as client:
            result = await client.call_tool("breakroom_status", {})
            assert not result.is_error
            assert result.structured_content is not None
            return dict(result.structured_content)

    return asyncio.run(run())


def test_exactly_the_two_tools_are_registered_and_take_no_input() -> None:
    async def run() -> list[Any]:
        async with Client(build_server(lambda: None)) as client:
            return (await client.list_tools()).tools

    tools = asyncio.run(run())
    assert sorted(t.name for t in tools) == sorted(TOOLS)
    for tool in tools:
        assert not tool.input_schema.get("properties")
        assert tool.description


def test_no_reply_reports_not_running_with_nulls() -> None:
    assert _call(None) == {
        "companion": "not_running",
        "invitation": None,
        "last_event_at": None,
        "activity": None,
        "estimate": None,
        "queue": None,
    }


def test_reply_fields_are_passed_through_and_nothing_else() -> None:
    when = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    out = _call(
        StatusReply(
            kind="status", version=3, invitation="suggested", last_event_at=when
        )
    )
    assert set(out) == STATUS_KEYS
    assert out["companion"] == "running"
    assert out["invitation"] == "suggested"
    assert datetime.fromisoformat(out["last_event_at"]) == when


def test_reply_without_last_event_reports_null_timestamp() -> None:
    out = _call(StatusReply(kind="status", version=3, invitation="idle"))
    assert out["companion"] == "running"
    assert out["invitation"] == "idle"
    assert out["last_event_at"] is None


def test_activity_and_estimate_come_from_a_v2_reply() -> None:
    out = _call(
        StatusReply(
            kind="status",
            version=3,
            invitation="suggested",
            activity="active",
            estimate="10_20m",
        )
    )
    assert out["activity"] == "active"
    assert out["estimate"] == "10_20m"


def test_reply_defaults_report_unknown_activity_and_no_estimate() -> None:
    out = _call(StatusReply(kind="status", version=3, invitation="idle"))
    assert out["activity"] == "unknown"
    assert out["estimate"] is None


def test_description_mentions_activity() -> None:
    async def run() -> str:
        async with Client(build_server(lambda: None)) as client:
            tools = (await client.list_tools()).tools
            return (
                next(t for t in tools if t.name == "breakroom_status").description or ""
            )

    assert "activity" in asyncio.run(run()).lower()


def _rpc(id_: int | None, method: str, params: dict[str, Any] | None = None) -> str:
    msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if id_ is not None:
        msg["id"] = id_
    if params is not None:
        msg["params"] = params
    return json.dumps(msg) + "\n"


def test_subprocess_speaks_mcp_over_stdio() -> None:
    init = {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    }
    stdin = (
        _rpc(1, "initialize", init)
        + _rpc(None, "notifications/initialized")
        + _rpc(2, "tools/list", {})
        + _rpc(3, "tools/call", {"name": "breakroom_status", "arguments": {}})
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "breakroom_agent_bridge.mcp_server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    # Keep stdin open until the last reply arrives: EOF may end the server early.
    timer = threading.Timer(20, proc.kill)
    timer.start()
    replies: dict[int, Any] = {}
    try:
        assert proc.stdin is not None
        assert proc.stdout is not None
        proc.stdin.write(stdin)
        proc.stdin.flush()
        for line in proc.stdout:
            msg = json.loads(line)
            if "id" in msg:
                replies[msg["id"]] = msg
            if 3 in replies:
                break
    finally:
        timer.cancel()
        proc.kill()
        proc.communicate()  # reaps it and closes the pipes
    assert sorted(t["name"] for t in replies[2]["result"]["tools"]) == sorted(TOOLS)
    assert replies[3]["result"]["structuredContent"]["companion"] == "not_running"


def _unsafe_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    xdg = tmp_path / "x"
    (xdg / "breakroom").mkdir(parents=True, mode=0o700)
    (xdg / "breakroom").chmod(0o770)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(xdg))


def test_status_provider_reports_not_running_for_unsafe_runtime_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _unsafe_xdg(tmp_path, monkeypatch)
    assert query_companion(0.5) is None


def test_status_timeout_setting_defaults_and_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert McpSettings.from_env().status_timeout_s == 0.5
    monkeypatch.setenv("BREAKROOM_MCP_STATUS_TIMEOUT_S", "0.1")
    assert McpSettings.from_env().status_timeout_s == 0.1
    for bad in ("0", "-2", "x"):
        monkeypatch.setenv("BREAKROOM_MCP_STATUS_TIMEOUT_S", bad)
        assert McpSettings.from_env().status_timeout_s == 0.5


def test_status_provider_honours_the_timeout_it_is_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory(dir="/tmp") as d:
        sock = Path(d) / "breakroom" / "companion.sock"
        sock.parent.mkdir(mode=0o700)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(sock))
        server.listen(1)  # accepts at kernel level, never replies
        monkeypatch.setenv("XDG_RUNTIME_DIR", d)
        try:
            start = time.monotonic()
            assert query_companion(0.1) is None
            assert time.monotonic() - start < 0.4
        finally:
            server.close()


# --- M4-07: queue status and breakroom_leave_queue ---------------------------------


@pytest.mark.parametrize("queue", ["none", "searching", "matched"])
def test_status_reports_the_fixed_queue_enum(queue: str) -> None:
    out = _call(
        StatusReply.model_validate(
            {"kind": "status", "version": 3, "invitation": "idle", "queue": queue}
        )
    )
    assert out["queue"] == queue
    assert set(out) == STATUS_KEYS


def test_status_from_an_old_v2_companion_reports_queue_none_value() -> None:
    out = _call(LegacyStatusReply(kind="status", version=2, invitation="idle"))
    assert out["companion"] == "running"
    assert out["queue"] is None
    assert set(out) == STATUS_KEYS


def _leave(provider: Callable[[], LeaveResult | None] | None) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        server = (
            build_server(lambda: None)
            if provider is None
            else build_server(lambda: None, provider)
        )
        async with Client(server) as client:
            result = await client.call_tool("breakroom_leave_queue", {})
            assert not result.is_error
            assert result.structured_content is not None
            return dict(result.structured_content)

    return asyncio.run(run())


def test_leave_queue_without_a_companion_reports_companion_not_running() -> None:
    assert _leave(None) == {"result": "companion_not_running"}
    assert _leave(lambda: None) == {"result": "companion_not_running"}


@pytest.mark.parametrize("result", ["cancelled", "not_searching", "unavailable"])
def test_leave_queue_returns_exactly_the_companion_enum(result: LeaveResult) -> None:
    assert _leave(lambda: result) == {"result": result}


def test_leave_queue_provider_is_called_once_per_tool_call() -> None:
    calls: list[int] = []

    def provider() -> LeaveResult:
        calls.append(1)
        return "cancelled"

    _leave(provider)
    assert calls == [1]
    _call(None)  # a status call never cancels anything
    assert calls == [1]


def test_leave_queue_description_says_cancel_only() -> None:
    async def run() -> str:
        async with Client(build_server(lambda: None)) as client:
            tools = (await client.list_tools()).tools
            tool = next(t for t in tools if t.name == "breakroom_leave_queue")
            return tool.description or ""

    text = asyncio.run(run()).lower()
    assert "cancel" in text and "cannot start" in text


def test_leave_queue_end_to_end_against_a_real_ipc_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio as aio

    from breakroom_companion.ipc_server import IpcServer

    with tempfile.TemporaryDirectory(dir="/tmp") as d:
        sock = Path(d) / "breakroom" / "companion.sock"
        sock.parent.mkdir(mode=0o700)
        monkeypatch.setenv("XDG_RUNTIME_DIR", d)
        status = StatusReply(
            kind="status", version=3, invitation="idle", queue="searching"
        )

        async def scenario() -> tuple[object, object]:
            server = IpcServer(
                sock,
                on_event=lambda _e: None,
                status=lambda: status,
                leave_queue=lambda: "cancelled",
            )
            await server.start()
            try:
                got_status = await aio.to_thread(query_companion, 1.0)
                got_leave = await aio.to_thread(mcp_server.leave_queue_companion, 1.0)
                return got_status, got_leave
            finally:
                await server.close()

        got_status, got_leave = aio.run(scenario())
    assert got_status == status
    assert got_leave == "cancelled"


def test_leave_queue_companion_is_none_for_unsafe_runtime_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _unsafe_xdg(tmp_path, monkeypatch)
    assert mcp_server.leave_queue_companion(0.5) is None


def test_leave_queue_companion_honours_the_timeout_it_is_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory(dir="/tmp") as d:
        sock = Path(d) / "breakroom" / "companion.sock"
        sock.parent.mkdir(mode=0o700)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(sock))
        server.listen(1)  # accepts at kernel level, never replies
        monkeypatch.setenv("XDG_RUNTIME_DIR", d)
        try:
            start = time.monotonic()
            assert mcp_server.leave_queue_companion(0.1) == "unavailable"
            assert time.monotonic() - start < 0.4
        finally:
            server.close()


def test_bridge_never_references_the_queue_start_path() -> None:
    """Rule 14: nothing in the agent-facing code can start a search."""
    forbidden = {"join_queue", "QueueJoinRequest", "build_presence"}
    for module in (mcp_server, ipc_client):
        assert module.__file__ is not None
        tree = ast.parse(Path(module.__file__).read_text())
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
            elif isinstance(node, ast.alias):
                names.add(node.name.split(".")[-1])
            elif isinstance(node, ast.FunctionDef | ast.ClassDef):
                names.add(node.name)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                names.update(node.value.replace("(", " ").split())
        assert not names & forbidden, module.__name__

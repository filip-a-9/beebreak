"""M1-01: the breakroom_status MCP tool (THREAT_MODEL T9, T10)."""

import asyncio
import json
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from breakroom_agent_bridge.config import McpSettings
from breakroom_agent_bridge.mcp_server import build_server, query_companion
from breakroom_shared.ipc import StatusReply

STATUS_KEYS = {"companion", "invitation", "last_event_at"}


def _call(reply: StatusReply | None) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        async with Client(build_server(lambda: reply)) as client:
            result = await client.call_tool("breakroom_status", {})
            assert not result.is_error
            assert result.structured_content is not None
            return dict(result.structured_content)

    return asyncio.run(run())


def test_only_status_tool_is_registered_and_takes_no_input() -> None:
    async def run() -> list[Any]:
        async with Client(build_server(lambda: None)) as client:
            return (await client.list_tools()).tools

    tools = asyncio.run(run())
    assert [t.name for t in tools] == ["breakroom_status"]
    assert not tools[0].input_schema.get("properties")
    assert tools[0].description


def test_no_reply_reports_not_running_with_nulls() -> None:
    assert _call(None) == {
        "companion": "not_running",
        "invitation": None,
        "last_event_at": None,
    }


def test_reply_fields_are_passed_through_and_nothing_else() -> None:
    when = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    out = _call(
        StatusReply(
            kind="status", version=1, invitation="suggested", last_event_at=when
        )
    )
    assert set(out) == STATUS_KEYS
    assert out["companion"] == "running"
    assert out["invitation"] == "suggested"
    assert datetime.fromisoformat(out["last_event_at"]) == when


def test_reply_without_last_event_reports_null_timestamp() -> None:
    out = _call(StatusReply(kind="status", version=1, invitation="idle"))
    assert out["companion"] == "running"
    assert out["invitation"] == "idle"
    assert out["last_event_at"] is None


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
        proc.wait()
    assert [t["name"] for t in replies[2]["result"]["tools"]] == ["breakroom_status"]
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

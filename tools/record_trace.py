"""Dev-only stand-in for the companion: records normalized events to a file.

Usage: python tools/record_trace.py TRACE.jsonl

Binds the companion socket, appends every received NormalizedEvent as one JSON
line, and answers status requests with "idle". Uses only the public protocol in
breakroom_shared. Refuses to start if a companion is already listening.
Stop with Ctrl-C.
"""

import asyncio
import contextlib
import signal
import socket
import sys
from pathlib import Path

from pydantic import ValidationError

from breakroom_shared.ipc import (
    MAX_FRAME_BYTES,
    ClientFrame,
    EventFrame,
    StatusReply,
)
from breakroom_shared.paths import socket_path


def _already_running(path: Path) -> bool:
    if not path.exists():
        return False
    with socket.socket(socket.AF_UNIX) as s:
        s.settimeout(1)
        try:
            s.connect(str(path))
        except OSError:
            return False  # stale socket file
    return True


async def _serve(path: Path, out_path: Path) -> None:
    with out_path.open("a", encoding="utf-8") as out:

        async def handle(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            try:
                line = await asyncio.wait_for(reader.readline(), timeout=2)
                if not line or len(line) > MAX_FRAME_BYTES:
                    return
                frame = ClientFrame.model_validate_json(line).root
                if isinstance(frame, EventFrame):
                    out.write(frame.event.model_dump_json() + "\n")
                    out.flush()
                else:
                    reply = StatusReply(kind="status", version=4, invitation="idle")
                    writer.write(reply.model_dump_json().encode() + b"\n")
                    await writer.drain()
            except (ValidationError, TimeoutError, ConnectionError):
                pass  # drop malformed frames silently; never echo content
            finally:
                writer.close()

        path.unlink(missing_ok=True)
        server = await asyncio.start_unix_server(handle, path=str(path))
        path.chmod(0o600)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        try:
            await stop.wait()
        finally:
            server.close()
            await server.wait_closed()
            with contextlib.suppress(FileNotFoundError):
                path.unlink()


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: record_trace.py TRACE.jsonl", file=sys.stderr)
        return 2
    path = socket_path()
    if _already_running(path):
        print("a companion is already running; refusing to start", file=sys.stderr)
        return 1
    asyncio.run(_serve(path, Path(argv[1])))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

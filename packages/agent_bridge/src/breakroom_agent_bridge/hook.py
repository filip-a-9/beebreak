"""Claude Code hook entry point.

Claude Code interprets hook stdout/stderr and exit codes, so this process always
exits 0 and writes nothing. Payload content is never logged (spec §4 rule 4).
"""

import contextlib
import json
import sys
from datetime import UTC, datetime
from uuid import uuid4

from breakroom_agent_bridge.config import HookSettings
from breakroom_agent_bridge.ipc_client import send_event
from breakroom_agent_bridge.reducer import reduce
from breakroom_agent_bridge.session_key import load_or_create_salt
from breakroom_shared.paths import salt_path, socket_path


def _run() -> None:
    settings = HookSettings.from_env()
    raw = sys.stdin.buffer.read(settings.max_stdin_bytes + 1)
    if len(raw) > settings.max_stdin_bytes:
        return
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        return
    event = reduce(
        payload,
        salt=load_or_create_salt(salt_path()),
        now=datetime.now(UTC),
        event_id=uuid4(),
    )
    if event is not None:
        send_event(event, socket_path=socket_path(), timeout=settings.send_timeout_s)


def main() -> None:
    # Fail open and never leak content: swallow everything, including SystemExit.
    with contextlib.suppress(BaseException):
        _run()
    sys.exit(0)


if __name__ == "__main__":
    main()

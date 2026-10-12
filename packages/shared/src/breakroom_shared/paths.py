"""Per-user runtime locations shared by the hook, MCP server, and companion."""

import os
from pathlib import Path


def runtime_dir() -> Path:
    """Return the private runtime directory, creating it with mode 0700."""
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/breakroom-{os.getuid()}"
    path = Path(base) / "breakroom"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def socket_path() -> Path:
    return runtime_dir() / "companion.sock"


def salt_path() -> Path:
    return runtime_dir() / "salt"

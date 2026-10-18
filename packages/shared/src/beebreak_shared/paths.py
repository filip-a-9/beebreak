"""Per-user runtime locations shared by the hook, MCP server, and companion."""

import os
import stat
from pathlib import Path

# Parent of the per-user fallback dir; a seam for tests only.
_FALLBACK_PARENT = Path("/tmp")


class UnsafeRuntimeDir(RuntimeError):
    """The runtime directory is not a private directory owned by this user."""


def _check_private_dir(path: Path) -> None:
    """Refuse symlinks and directories not owned by us or not mode 0700."""
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise UnsafeRuntimeDir("runtime directory is not private")


def runtime_dir() -> Path:
    """Return the private runtime directory, creating it with mode 0700.

    Raises UnsafeRuntimeDir if the /tmp fallback or the ``beebreak`` directory
    exists but is a symlink, owned by someone else, or not mode 0700.
    """
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        base = Path(xdg)
    else:
        base = _FALLBACK_PARENT / f"beebreak-{os.getuid()}"
        base.mkdir(mode=0o700, parents=True, exist_ok=True)
        _check_private_dir(base)
    path = base / "beebreak"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    _check_private_dir(path)
    return path


def socket_path() -> Path:
    return runtime_dir() / "companion.sock"


def salt_path() -> Path:
    return runtime_dir() / "salt"


def state_dir() -> Path:
    """Private persistent directory ($XDG_STATE_HOME or ~/.local/state)/beebreak.

    Mode 0700; raises UnsafeRuntimeDir like runtime_dir.
    """
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "state"
    path = base / "beebreak"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    _check_private_dir(path)
    return path


def state_path() -> Path:
    return state_dir() / "companion-state.json"


def credentials_path() -> Path:
    """File credential store, used only when no OS keyring backend works."""
    return state_dir() / "credentials.json"

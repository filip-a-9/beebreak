"""Install and uninstall of the companion's per-user files. Owned by M6-02.

Install writes a small record (server URL, optional CA file) and, on request, an
XDG autostart entry. Uninstall removes every per-user file Breakroom created and
revokes the device on the server. Every step tolerates files that are already
gone, so a partial install uninstalls cleanly. Output lines are fixed strings.
"""

import asyncio
import contextlib
import dataclasses
import json
import os
import select
import shutil
import signal
import socket
import ssl
import stat
import struct
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from breakroom_companion.api_client import ApiClient, ApiError
from breakroom_companion.credentials import (
    CredentialStore,
    FileCredentialStore,
    default_credential_store,
)
from breakroom_companion.endpoint import EndpointRefused, validate_server_url
from breakroom_companion.remote_config import (
    DEFAULT_SERVER_URL,
    SERVER_URL_ENV,
    RemoteConfig,
)
from breakroom_shared.paths import (
    UnsafeRuntimeDir,
    credentials_path,
    runtime_dir,
    socket_path,
    state_dir,
)

INSTALL_RECORD = "install.json"
AUTOSTART_NAME = "breakroom.desktop"
STOP_WAIT_S = 5.0  # how long uninstall waits for a running companion to exit
CONNECT_TIMEOUT_S = 1.0
_RECORD_VERSION = 1
_SO_PEERCRED_FMT = "3i"  # struct ucred: pid, uid, gid

StoreFactory = Callable[[str], CredentialStore]


class CaFileRefused(ValueError):
    """The CA file is unusable. The message never echoes the path or contents."""


def autostart_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "autostart" / AUTOSTART_NAME


def validate_ca_file(path: str) -> str:
    """Absolute path of a readable PEM CA file that only its owner can modify."""
    try:
        resolved = Path(path).expanduser().resolve(strict=True)
        info = resolved.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022:
            raise CaFileRefused("CA file is not allowed")
        ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT).load_verify_locations(str(resolved))
    except (OSError, ssl.SSLError, ValueError):
        raise CaFileRefused("CA file is not allowed") from None
    return str(resolved)


def _read_record() -> dict[str, object]:
    try:
        data = json.loads((state_dir() / INSTALL_RECORD).read_text("utf-8"))
    except (OSError, ValueError, UnsafeRuntimeDir):
        return {}
    return data if isinstance(data, dict) else {}


def read_saved_server_url() -> str | None:
    """The server URL recorded by `install`, or None (missing, damaged, unsafe)."""
    value = _read_record().get("server_url")
    if not isinstance(value, str):
        return None
    try:
        return validate_server_url(value)
    except EndpointRefused:
        return None


def read_saved_ca_file() -> str | None:
    """The CA file recorded by `install`, if it is still acceptable."""
    value = _read_record().get("ca_file")
    if not isinstance(value, str):
        return None
    try:
        return validate_ca_file(value)
    except CaFileRefused:
        return None


def _desktop_exec_arg(arg: str) -> str:
    """One quoted Exec argument per the desktop-entry spec."""
    for char in '\\"`$':
        arg = arg.replace(char, "\\" + char)
    arg = arg.replace("%", "%%")
    # The string-value rule applies on top: a backslash is written doubled.
    return '"' + arg.replace("\\", "\\\\") + '"'


def _desktop_entry(exec_path: str) -> str:
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Breakroom\n"
        "Comment=Opt-in chat break while coding agents run\n"
        f"Exec={_desktop_exec_arg(exec_path)}\n"
        "Terminal=false\n"
        "X-GNOME-Autostart-enabled=true\n"
    )


def install(
    server_url: str,
    *,
    autostart: bool,
    exec_path: str,
    ca_file: str | None = None,
) -> list[str]:
    """Record the validated server URL (and CA file); optionally add autostart.

    Raises EndpointRefused or CaFileRefused (nothing is written)."""
    url = validate_server_url(server_url)
    record: dict[str, object] = {"version": _RECORD_VERSION, "server_url": url}
    done = ["saved server URL"]
    if ca_file is not None:
        record["ca_file"] = validate_ca_file(ca_file)
        done.append("saved CA file path")
    path = state_dir() / INSTALL_RECORD
    path.write_text(json.dumps(record), "utf-8")
    path.chmod(0o600)
    if autostart:
        entry = autostart_path()
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.write_text(_desktop_entry(exec_path), "utf-8")
        done.append("added autostart entry")
    return done


# --- uninstall ---------------------------------------------------------------


def _wait_exit(pidfd: int, timeout_s: float) -> bool:
    poller = select.poll()
    poller.register(pidfd, select.POLLIN)
    return bool(poller.poll(timeout_s * 1000))


def stop_companion() -> str:
    """Ask a running companion to quit. Returns "stopped", "not_running"
    or "still_running".

    The IPC socket identifies the process: while connected, the listener's pid
    is read from SO_PEERCRED and pinned with a pidfd, so the signal cannot reach
    a recycled pid. SIGTERM is the companion's normal clean shutdown."""
    try:
        sock = socket_path()
    except (UnsafeRuntimeDir, OSError):
        return "not_running"
    pidfd: int | None = None
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(CONNECT_TIMEOUT_S)
        try:
            s.connect(str(sock))
        except (FileNotFoundError, ConnectionRefusedError):
            return "not_running"
        except OSError:
            return "still_running"  # something is there but does not answer
        raw = s.getsockopt(
            socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize(_SO_PEERCRED_FMT)
        )
        pid, uid, _gid = struct.unpack(_SO_PEERCRED_FMT, raw)
        if uid != os.getuid():
            return "not_running"  # not our process; never signal it
        try:
            pidfd = os.pidfd_open(pid)
        except OSError:
            return "still_running"
    try:
        with contextlib.suppress(ProcessLookupError):
            signal.pidfd_send_signal(pidfd, signal.SIGTERM)
        if _wait_exit(pidfd, STOP_WAIT_S):
            return "stopped"
        return "still_running"
    finally:
        os.close(pidfd)


def _load_refresh_token(
    origins: list[str], make_store: StoreFactory
) -> tuple[str, str] | None:
    """(origin, refresh token) from the first store holding a usable login."""
    for origin in origins:
        stores: list[CredentialStore] = [make_store(origin)]
        with contextlib.suppress(UnsafeRuntimeDir, OSError):
            stores.append(FileCredentialStore(credentials_path(), origin))
        for store in stores:
            creds = store.load()
            if creds is not None:
                return origin, creds.refresh_token
    return None


def revoke_config(origin: str) -> RemoteConfig:
    """Environment settings (timeouts) applied to `origin`. May raise
    ValueError or EndpointRefused when a BREAKROOM_* setting is invalid."""
    return dataclasses.replace(RemoteConfig.from_env(), server_url=origin)


def revoke(config: RemoteConfig, refresh_token: str) -> str:
    """Revoke the device behind `refresh_token`: "revoked", "offline" (server
    unreachable) or "rejected"."""

    async def go() -> str:
        api = ApiClient(
            config.server_url, config.http_timeout_s, ssl_context=config.ssl_context()
        )
        try:
            pair = await api.refresh(refresh_token)
            await api.logout(pair.access_token)
        except ApiError as e:
            return "offline" if e.code == "network" else "rejected"
        finally:
            await api.aclose()
        return "revoked"

    return asyncio.run(go())


_REVOKE_LINES = {
    "revoked": "revoked this device on the server",
    "offline": "server unreachable: this device was NOT revoked on the server",
    "rejected": "server refused the revoke: login may already be invalid",
    "config": "invalid BREAKROOM_* setting: this device was NOT revoked",
}


def _remove_dir(path_fn: Callable[[], Path]) -> Path | None:
    """Remove a Breakroom directory; its path if removed, else None."""
    try:
        path = path_fn()
    except (UnsafeRuntimeDir, OSError):
        return None
    if path.is_symlink() or not path.is_dir():
        return None
    shutil.rmtree(path, ignore_errors=True)
    return None if path.exists() else path


def _remove_empty_fallback_parent(removed: Path) -> None:
    """Without XDG_RUNTIME_DIR the runtime dir sits in /tmp/breakroom-<uid>,
    which the companion created; remove it too once empty."""
    if os.environ.get("XDG_RUNTIME_DIR"):
        return
    parent = removed.parent
    if parent.name != f"breakroom-{os.getuid()}" or parent.is_symlink():
        return
    with contextlib.suppress(OSError):
        if parent.lstat().st_uid == os.getuid():
            parent.rmdir()  # fails (and stays) if not empty


@dataclass
class UninstallReport:
    lines: list[str] = field(default_factory=list)
    completed: bool = True

    def add(self, line: str) -> None:
        self.lines.append(line)


def _origins() -> list[str]:
    """Server origins a login may be stored under: recorded, env, default."""
    origins: list[str] = []
    candidates = [read_saved_server_url(), os.environ.get(SERVER_URL_ENV)]
    for raw in candidates:
        if raw:
            with contextlib.suppress(EndpointRefused):
                origin = validate_server_url(raw)
                if origin not in origins:
                    origins.append(origin)
    if DEFAULT_SERVER_URL not in origins:
        origins.append(DEFAULT_SERVER_URL)
    return origins


def uninstall(
    *, keep_account: bool, make_store: StoreFactory = default_credential_store
) -> UninstallReport:
    report = UninstallReport()
    stopped = stop_companion()
    if stopped == "still_running":
        report.add("the companion did not stop; nothing was removed")
        report.add("quit the companion, then run uninstall again")
        report.completed = False
        return report
    if stopped == "stopped":
        report.add("stopped the running companion")

    origins = _origins()
    if keep_account:
        report.add("kept the account on the server (--keep-account)")
    else:
        login = _load_refresh_token(origins, make_store)
        if login is None:
            report.add("no stored login; nothing to revoke on the server")
        else:
            origin, token = login
            try:
                outcome = revoke(revoke_config(origin), token)
            except (ValueError, EndpointRefused):
                outcome = "config"
            report.add(_REVOKE_LINES[outcome])

    cleared = True
    for origin in origins:
        try:
            cleared = make_store(origin).clear() and cleared
        except (UnsafeRuntimeDir, OSError):
            cleared = False
    report.add("cleared stored login" if cleared else "could not clear the login")
    if _remove_dir(state_dir):
        report.add("removed state directory (state, credentials, install record)")
    removed = _remove_dir(runtime_dir)
    if removed is not None:
        report.add("removed socket directory (socket, salt)")
        _remove_empty_fallback_parent(removed)
    entry = autostart_path()
    if entry.is_file() or entry.is_symlink():
        entry.unlink()
        report.add("removed autostart entry")
    return report

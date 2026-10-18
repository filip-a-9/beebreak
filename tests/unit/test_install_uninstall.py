"""Companion install/uninstall against a temp HOME/XDG tree (M6-02)."""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from beebreak_agent_bridge.ipc_client import request_status
from beebreak_companion import cli, install
from beebreak_companion.api_client import ApiClient, ApiError
from beebreak_companion.credentials import (
    CredentialStore,
    FileCredentialStore,
    StoredCredentials,
)
from beebreak_companion.endpoint import EndpointRefused
from beebreak_companion.remote_config import CA_FILE_ENV, SERVER_URL_ENV, RemoteConfig
from beebreak_server.models import Device
from beebreak_shared import paths
from beebreak_shared.paths import credentials_path, runtime_dir, state_dir

if TYPE_CHECKING:
    from tests.e2e_support.server import LiveServer

pytest_plugins = ["tests.e2e_support.server"]  # live_server fixtures

ORIGIN = "http://127.0.0.1:9"  # nothing listens here


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    with tempfile.TemporaryDirectory(dir="/tmp") as d:
        root = Path(d)
        monkeypatch.setenv("HOME", str(root))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(root / "config"))
        monkeypatch.setenv("XDG_STATE_HOME", str(root / "state"))
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(root / "run"))
        (root / "run").mkdir(mode=0o700)
        yield root


def file_store(origin: str) -> CredentialStore:
    return FileCredentialStore(credentials_path(), origin)


def mentions_beebreak(root: Path) -> list[Path]:
    """Every path under root whose name or contents mention beebreak."""
    found = []
    for path in root.rglob("*"):
        if "beebreak" in path.name.lower() or (
            path.is_file() and b"beebreak" in path.read_bytes().lower()
        ):
            found.append(path)
    return found


def plant_old_autostart_entry() -> None:
    """An entry written by a release that still had `install --autostart`."""
    install.autostart_path().parent.mkdir(parents=True, exist_ok=True)
    install.autostart_path().write_text("[Desktop Entry]\nName=Beebreak\n")


def save_login(origin: str, token: str, expires_in: timedelta) -> None:
    file_store(origin).save(StoredCredentials(token, datetime.now(UTC) + expires_in))


def test_install_then_uninstall_leaves_nothing(home: Path) -> None:
    install.install(ORIGIN)
    plant_old_autostart_entry()
    save_login(ORIGIN, "t", timedelta(days=1))
    (runtime_dir() / "salt").write_bytes(b"x")
    assert mentions_beebreak(home)  # install really wrote something

    install.uninstall(keep_account=True, make_store=file_store)

    assert mentions_beebreak(home) == []


def test_install_rejects_a_disallowed_url_and_writes_nothing(home: Path) -> None:
    with pytest.raises(EndpointRefused):
        install.install("http://example.com")
    assert mentions_beebreak(home) == []


def test_saved_url_round_trips(home: Path) -> None:
    assert install.read_saved_server_url() is None
    install.install(ORIGIN)
    assert install.read_saved_server_url() == ORIGIN


def test_uninstall_on_a_machine_that_never_installed(home: Path) -> None:
    install.uninstall(keep_account=False, make_store=file_store)
    install.uninstall(keep_account=True, make_store=file_store)
    assert mentions_beebreak(home) == []


@pytest.mark.parametrize("stage", ["record", "autostart", "login", "socket_dir"])
def test_partial_install_uninstalls_cleanly(home: Path, stage: str) -> None:
    if stage == "record":
        install.install(ORIGIN)
    elif stage == "autostart":
        plant_old_autostart_entry()
    elif stage == "login":
        save_login(ORIGIN, "t", timedelta(days=1))
    else:
        runtime_dir()
    install.uninstall(keep_account=True, make_store=file_store)
    assert mentions_beebreak(home) == []


def test_corrupt_install_record_is_ignored_and_removed(home: Path) -> None:
    (state_dir() / install.INSTALL_RECORD).write_text("{not json")
    assert install.read_saved_server_url() is None
    install.uninstall(keep_account=True, make_store=file_store)
    assert mentions_beebreak(home) == []


def test_symlinked_state_dir_is_not_followed(home: Path) -> None:
    victim = home / "victim"
    victim.mkdir()
    (victim / "keep.txt").write_text("x")
    (home / "state").mkdir()
    (home / "state" / "beebreak").symlink_to(victim)
    install.uninstall(keep_account=True, make_store=file_store)
    assert (victim / "keep.txt").exists()


# --- device revoke ---------------------------------------------------------


def sign_in(server: LiveServer, email: str) -> StoredCredentials:
    async def go() -> StoredCredentials:
        api = ApiClient(server.base_url, 5)
        try:
            started = await api.start_auth(email, server.invite(email))
            reply = await api.verify(started.challenge_id, server.last_code(), "box")
        finally:
            await api.aclose()
        t = reply.tokens
        return StoredCredentials(t.refresh_token, t.refresh_expires_at)

    return asyncio.run(go())


def refresh_fails(server: LiveServer, token: str) -> bool:
    async def go() -> bool:
        api = ApiClient(server.base_url, 5)
        try:
            await api.refresh(token)
        except ApiError:
            return True
        finally:
            await api.aclose()
        return False

    return asyncio.run(go())


def test_uninstall_revokes_the_device_on_a_real_server(
    home: Path, live_server: LiveServer
) -> None:
    creds = sign_in(live_server, "ada@example.com")
    install.install(live_server.base_url)
    file_store(live_server.base_url).save(creds)

    install.uninstall(keep_account=False, make_store=file_store)

    # The refresh inside uninstall rotates the token, so check the device row.
    with live_server.session_factory() as session:
        devices = session.scalars(select(Device)).all()
    assert [d.revoked_at is not None for d in devices] == [True]
    assert mentions_beebreak(home) == []


def test_keep_account_leaves_the_device_valid(
    home: Path, live_server: LiveServer
) -> None:
    creds = sign_in(live_server, "bob@example.com")
    install.install(live_server.base_url)
    file_store(live_server.base_url).save(creds)

    install.uninstall(keep_account=True, make_store=file_store)

    assert not refresh_fails(live_server, creds.refresh_token)
    assert mentions_beebreak(home) == []


def test_offline_server_still_removes_everything(home: Path) -> None:
    install.install(ORIGIN)
    plant_old_autostart_entry()
    save_login(ORIGIN, "t", timedelta(days=1))
    report = install.uninstall(keep_account=False, make_store=file_store)
    assert report.lines
    assert mentions_beebreak(home) == []


def test_revoke_outcome_offline_vs_rejected(live_server: LiveServer) -> None:
    offline = RemoteConfig(server_url=ORIGIN, http_timeout_s=1.0)
    assert install.revoke(offline, "t") == "offline"
    live = RemoteConfig(server_url=live_server.base_url, http_timeout_s=5.0)
    assert install.revoke(live, "bogus") == "rejected"


# --- stopping a running companion ------------------------------------------


def test_uninstall_stops_a_running_companion(home: Path) -> None:
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "beebreak_companion.app"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    sock = runtime_dir() / "companion.sock"
    try:
        deadline = time.monotonic() + 10
        while request_status(socket_path=sock) is None:
            assert time.monotonic() < deadline, "companion did not start"
            time.sleep(0.05)
        install.uninstall(keep_account=True, make_store=file_store)
        proc.wait(timeout=10)
        assert proc.returncode is not None
        assert mentions_beebreak(home) == []
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGKILL)
            proc.wait()
        for pipe in (proc.stdout, proc.stderr):
            if pipe is not None:
                pipe.close()


# --- command line -----------------------------------------------------------


def test_cli_install_and_uninstall(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli.main(["install", "--server-url", ORIGIN])
    assert code == 0
    plant_old_autostart_entry()
    assert "claude plugin" in capsys.readouterr().out  # the next command
    assert cli.main(["uninstall", "--keep-account"]) == 0
    assert mentions_beebreak(home) == []


def test_cli_install_prompts_and_reasks_on_a_bad_url(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    answers = iter(["http://example.com", ORIGIN])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    assert cli.main(["install"]) == 0
    assert install.read_saved_server_url() == ORIGIN


def test_cli_install_no_longer_offers_autostart(home: Path) -> None:
    # WSLg has no desktop session, so nothing would ever run the entry.
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["install", "--server-url", ORIGIN, "--autostart"])
    assert exit_info.value.code == cli.EXIT_USAGE
    assert mentions_beebreak(home) == []


def test_cli_install_refuses_a_bad_flag_url(home: Path) -> None:
    assert cli.main(["install", "--server-url", "ftp://x"]) != 0
    assert mentions_beebreak(home) == []


# --- review fixes: CA file, env precedence, stop safety, cleanup -------------

CA_PEM = """-----BEGIN CERTIFICATE-----
MIIBezCCASGgAwIBAgIURXpBU435/GXUYmCFq5HE/Q45cWswCgYIKoZIzj0EAwIw
EjEQMA4GA1UEAwwHdGVzdC1jYTAgFw0yNjEwMTYxMjQ1MjNaGA8yMTI2MDkyMjEy
NDUyM1owEjEQMA4GA1UEAwwHdGVzdC1jYTBZMBMGByqGSM49AgEGCCqGSM49AwEH
A0IABN0Ia7+qm0u1S7WR9rNFg+aSqZprErNsX8NV4KFWQ5t02MjAAWuAN387OQyH
tFXI8JNIkjQZ7RrVf1E5gN4lLrejUzBRMB0GA1UdDgQWBBSKMnCym+JBAY0Y8ZGQ
vImViVCULTAfBgNVHSMEGDAWgBSKMnCym+JBAY0Y8ZGQvImViVCULTAPBgNVHRMB
Af8EBTADAQH/MAoGCCqGSM49BAMCA0gAMEUCIBfmDor+gHPYt6q3Jpg7izqVqy0B
75woG3PS0QvO6KOEAiEAoNt3bTf4ZL25yWUpkHoD/M8RrH2r/IlGeqf46N2BijQ=
-----END CERTIFICATE-----
"""


def write_ca(home: Path, text: str = CA_PEM, mode: int = 0o644) -> Path:
    path = home / "ca.pem"
    path.write_text(text)
    path.chmod(mode)
    return path


def test_ca_file_is_validated_at_install(home: Path) -> None:
    good = write_ca(home)
    assert install.validate_ca_file(str(good)) == str(good)
    bad_inputs = [
        home / "missing.pem",
        home,  # a directory
        write_ca(home, "not a certificate\n"),
        write_ca(home, mode=0o666),  # world-writable
        write_ca(home, mode=0o664),  # group-writable
    ]
    for bad in bad_inputs:
        with pytest.raises(install.CaFileRefused):
            install.validate_ca_file(str(bad))


def test_ca_file_owned_by_another_user_is_refused_at_install(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Install must refuse what the companion would refuse at start. Cannot chown
    # without root, so the real file (ours) is judged by a pretend current user.
    ca = write_ca(home)
    monkeypatch.setattr(os, "getuid", lambda: ca.stat().st_uid + 1)
    with pytest.raises(install.CaFileRefused):
        install.validate_ca_file(str(ca))


def test_install_with_a_bad_ca_file_writes_nothing(home: Path) -> None:
    with pytest.raises(install.CaFileRefused):
        install.install(ORIGIN, ca_file=str(home / "no"))
    assert mentions_beebreak(home) == []


def test_cli_install_records_the_ca_file_and_refuses_a_bad_one(home: Path) -> None:
    ca = write_ca(home)
    assert cli.main(["install", "--server-url", ORIGIN, "--ca-file", str(ca)]) == 0
    assert install.read_saved_ca_file() == str(ca)
    assert cli.main(["install", "--server-url", ORIGIN, "--ca-file", "/nope"]) != 0


class Recorder:
    def __init__(self) -> None:
        self.env: dict[str, str | None] = {}

    def __call__(self) -> int:
        for name in (SERVER_URL_ENV, CA_FILE_ENV):
            self.env[name] = os.environ.get(name)
        return 0


@pytest.fixture
def run_recorder(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    import beebreak_companion.app as app

    recorder = Recorder()
    monkeypatch.setattr(app, "main", recorder)
    # Register both variables so the test's own changes are undone.
    for name in (SERVER_URL_ENV, CA_FILE_ENV):
        monkeypatch.setenv(name, "x")
        monkeypatch.delenv(name)
    return recorder


def test_run_applies_the_recorded_url_and_ca_when_env_is_unset(
    home: Path, run_recorder: Recorder
) -> None:
    ca = write_ca(home)
    install.install(ORIGIN, ca_file=str(ca))
    assert cli.main([]) == 0
    assert run_recorder.env == {SERVER_URL_ENV: ORIGIN, CA_FILE_ENV: str(ca)}


def test_run_lets_the_environment_override_the_recorded_settings(
    home: Path, run_recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    ca = write_ca(home)
    install.install(ORIGIN, ca_file=str(ca))
    monkeypatch.setenv(SERVER_URL_ENV, "https://example.org")
    monkeypatch.setenv(CA_FILE_ENV, "/elsewhere.pem")
    assert cli.main(["run"]) == 0
    assert run_recorder.env == {
        SERVER_URL_ENV: "https://example.org",
        CA_FILE_ENV: "/elsewhere.pem",
    }


def test_run_without_a_record_sets_nothing(home: Path, run_recorder: Recorder) -> None:
    assert cli.main([]) == 0
    assert run_recorder.env == {SERVER_URL_ENV: None, CA_FILE_ENV: None}


def test_uninstall_aborts_untouched_when_the_companion_will_not_stop(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BEEBREAK_STOP_WAIT_S", "0.5")
    install.install(ORIGIN)
    plant_old_autostart_entry()
    sock = runtime_dir() / "companion.sock"
    code = (
        "import signal, socket, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "s = socket.socket(socket.AF_UNIX)\n"
        f"s.bind({str(sock)!r})\n"
        "s.listen(4)\n"
        "print('up', flush=True)\n"
        "time.sleep(60)\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE)
    try:
        assert proc.stdout is not None
        proc.stdout.readline()
        started = time.monotonic()
        report = install.uninstall(keep_account=False, make_store=file_store)
        # The 0.5 s wait from env, not the 5 s default.
        assert time.monotonic() - started < 3
        assert report.completed is False
        assert install.read_saved_server_url() == ORIGIN
        assert install.autostart_path().is_file()
    finally:
        proc.kill()
        proc.wait()
        if proc.stdout is not None:
            proc.stdout.close()


def test_stop_ignores_a_missing_or_dead_socket(home: Path) -> None:
    assert install.stop_companion() == "not_running"  # no socket at all
    dead = runtime_dir() / "companion.sock"
    with socket.socket(socket.AF_UNIX) as s:
        s.bind(str(dead))  # bound, never listening: connection refused
    assert install.stop_companion() == "not_running"


def test_fallback_runtime_parent_is_removed_when_empty(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    monkeypatch.setattr(paths, "_FALLBACK_PARENT", home / "tmp")
    (home / "tmp").mkdir()
    (runtime_dir() / "salt").write_bytes(b"x")
    parent = runtime_dir().parent
    assert parent.is_dir()
    install.uninstall(keep_account=True, make_store=file_store)
    assert not parent.exists()
    assert (home / "tmp").is_dir()


def test_fallback_runtime_parent_with_other_content_is_kept(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    monkeypatch.setattr(paths, "_FALLBACK_PARENT", home / "tmp")
    (home / "tmp").mkdir()
    parent = runtime_dir().parent
    (parent / "other").write_text("x")
    install.uninstall(keep_account=True, make_store=file_store)
    assert (parent / "other").is_file()


def test_clearing_does_not_depend_on_the_environment_being_valid(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_origin = "https://example.org"
    monkeypatch.setenv(SERVER_URL_ENV, env_origin)
    monkeypatch.setenv("BEEBREAK_HTTP_TIMEOUT_S", "not-a-number")
    cleared: list[str] = []

    def recording_store(origin: str) -> CredentialStore:
        cleared.append(origin)
        return file_store(origin)

    report = install.uninstall(keep_account=True, make_store=recording_store)
    assert env_origin in cleared
    assert report.completed
    assert mentions_beebreak(home) == []


def test_invalid_environment_skips_the_revoke_but_still_removes_everything(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install.install(ORIGIN)
    save_login(ORIGIN, "t", timedelta(days=1))
    monkeypatch.setenv("BEEBREAK_HTTP_TIMEOUT_S", "not-a-number")
    install.uninstall(keep_account=False, make_store=file_store)
    assert mentions_beebreak(home) == []


def test_stop_wait_env_reaches_the_wait_and_bad_values_fall_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from beebreak_companion import config

    assert config.stop_wait_s() == 5.0
    assert config.connect_timeout_s() == 1.0
    monkeypatch.setenv("BEEBREAK_STOP_WAIT_S", "0.25")
    monkeypatch.setenv("BEEBREAK_CONNECT_TIMEOUT_S", "0.5")
    assert (config.stop_wait_s(), config.connect_timeout_s()) == (0.25, 0.5)
    for bad in ("0", "-3", "abc", "inf", "nan"):
        monkeypatch.setenv("BEEBREAK_STOP_WAIT_S", bad)
        monkeypatch.setenv("BEEBREAK_CONNECT_TIMEOUT_S", bad)
        assert (config.stop_wait_s(), config.connect_timeout_s()) == (5.0, 1.0)

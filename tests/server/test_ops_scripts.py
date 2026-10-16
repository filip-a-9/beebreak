"""M6-08: scripts/backup.sh and scripts/restore.sh, and the compose logging switch.

The scripts are run for real. Only `docker` is replaced, by a stub first on PATH
that records its arguments and plays a database that answers like Postgres
would; the real round trip is the restore drill in docs/RUNBOOK.md.
"""

import hashlib
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BACKUP = ROOT / "scripts" / "backup.sh"
RESTORE = ROOT / "scripts" / "restore.sh"
DUMP_BYTES = b"PGDMP\x00fake-custom-format-bytes\xff" * 20

# Stand-in for `docker compose ... exec/run/up/stop`. FAKE_TABLES is what the
# "database" answers to the table count; FAKE_DUMP is what pg_dump prints.
FAKE_DOCKER = r"""#!/usr/bin/env bash
echo "$*" >>"$FAKE_LOG"
case "$*" in
  *pg_dump*) printf '%s' "$FAKE_DUMP" | base64 -d ;;
  *"pg_restore --list"*) cat >/dev/null ;;
  *pg_restore*) cat >"$FAKE_RESTORED"; [[ -z "$FAKE_FAIL_RESTORE" ]] || exit 1 ;;
  *"count(*)"*) echo "$FAKE_TABLES" ;;
esac
exit 0
"""


@pytest.fixture
def fake_docker(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(0o755)
    return bin_dir


def _run(
    script: Path,
    args: list[str],
    tmp_path: Path,
    bin_dir: Path | None = None,
    **extra: str,
) -> subprocess.CompletedProcess[str]:
    import base64

    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}" if bin_dir else os.environ["PATH"],
        "FAKE_LOG": str(tmp_path / "docker.log"),
        "FAKE_RESTORED": str(tmp_path / "restored.bin"),
        "FAKE_DUMP": base64.b64encode(DUMP_BYTES).decode(),
        "FAKE_TABLES": "0",
        "FAKE_FAIL_RESTORE": "",
        **extra,
    }
    return subprocess.run(
        [str(script), *args], env=env, capture_output=True, text=True, timeout=60
    )


def _calls(tmp_path: Path) -> list[str]:
    log = tmp_path / "docker.log"
    return log.read_text().splitlines() if log.exists() else []


@pytest.mark.parametrize("script", [BACKUP, RESTORE])
def test_scripts_are_valid_bash_and_executable(script: Path) -> None:
    assert script.stat().st_mode & stat.S_IXUSR
    result = subprocess.run(["bash", "-n", str(script)], capture_output=True)
    assert result.returncode == 0, result.stderr


# --- backup ------------------------------------------------------------------


def test_backup_writes_a_dated_0600_file_and_reports_size_and_checksum(
    tmp_path: Path, fake_docker: Path
) -> None:
    out = tmp_path / "out"
    result = _run(BACKUP, ["--out-dir", str(out)], tmp_path, fake_docker)
    assert result.returncode == 0, result.stderr
    (dump,) = list(out.iterdir())
    assert dump.name.startswith("breakroom-") and dump.suffix == ".dump"
    assert stat.S_IMODE(dump.stat().st_mode) == 0o600
    assert dump.read_bytes() == DUMP_BYTES
    assert hashlib.sha256(DUMP_BYTES).hexdigest() in result.stdout
    assert str(len(DUMP_BYTES)) in result.stdout
    assert any("--format=custom" in call for call in _calls(tmp_path))


def test_backup_leaves_no_file_when_the_dump_is_empty(
    tmp_path: Path, fake_docker: Path
) -> None:
    out = tmp_path / "out"
    result = _run(BACKUP, ["--out-dir", str(out)], tmp_path, fake_docker, FAKE_DUMP="")
    assert result.returncode != 0
    assert list(out.iterdir()) == []


def test_backup_refuses_two_encryption_choices(tmp_path: Path) -> None:
    result = _run(BACKUP, ["--age-recipient", "k", "--gpg-recipient", "g"], tmp_path)
    assert result.returncode == 2


def test_backup_rejects_an_unknown_flag(tmp_path: Path) -> None:
    assert _run(BACKUP, ["--nope"], tmp_path).returncode == 2


# --- restore -----------------------------------------------------------------


def _backup_file(tmp_path: Path) -> Path:
    path = tmp_path / "breakroom-test.dump"
    path.write_bytes(DUMP_BYTES)
    return path


def test_restore_needs_an_existing_file(tmp_path: Path) -> None:
    assert _run(RESTORE, [], tmp_path).returncode == 2
    assert _run(RESTORE, [str(tmp_path / "missing.dump")], tmp_path).returncode == 2
    assert _calls(tmp_path) == []


def test_restore_refuses_a_non_empty_database_and_changes_nothing(
    tmp_path: Path, fake_docker: Path
) -> None:
    result = _run(
        RESTORE, [str(_backup_file(tmp_path))], tmp_path, fake_docker, FAKE_TABLES="14"
    )
    assert result.returncode == 1
    calls = " ".join(_calls(tmp_path))
    assert "pg_restore" not in calls and " stop " not in calls
    assert "drop schema" not in calls
    assert not (tmp_path / "restored.bin").exists()


def test_restore_into_an_empty_database_restores_migrates_then_purges(
    tmp_path: Path, fake_docker: Path
) -> None:
    result = _run(RESTORE, [str(_backup_file(tmp_path))], tmp_path, fake_docker)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "restored.bin").read_bytes() == DUMP_BYTES
    calls = _calls(tmp_path)

    def at(needle: str) -> int:
        return next(i for i, call in enumerate(calls) if needle in call)

    assert (
        at(" stop ") < at("pg_restore") < at(" migrate") < at("breakroom-server purge")
    )
    assert "drop schema" not in " ".join(calls)  # nothing to drop in a fresh one
    assert "purge" in result.stdout.lower()


def test_restore_with_force_replaces_a_non_empty_database(
    tmp_path: Path, fake_docker: Path
) -> None:
    result = _run(
        RESTORE,
        [str(_backup_file(tmp_path)), "--force"],
        tmp_path,
        fake_docker,
        FAKE_TABLES="14",
    )
    assert result.returncode == 0, result.stderr
    calls = _calls(tmp_path)
    drop = next(i for i, c in enumerate(calls) if "drop schema" in c)
    restore = next(i for i, c in enumerate(calls) if "pg_restore" in c)
    assert drop < restore
    assert (tmp_path / "restored.bin").read_bytes() == DUMP_BYTES


def test_a_failed_restore_stops_before_migrate_and_purge_and_says_so(
    tmp_path: Path, fake_docker: Path
) -> None:
    result = _run(
        RESTORE,
        [str(_backup_file(tmp_path)), "--force"],
        tmp_path,
        fake_docker,
        FAKE_TABLES="14",
        FAKE_FAIL_RESTORE="1",
    )
    assert result.returncode != 0
    calls = " ".join(_calls(tmp_path))
    assert " migrate" not in calls and "purge" not in calls
    assert "--force" in result.stderr  # tells how to retry


def test_restore_of_an_age_file_needs_an_identity(tmp_path: Path) -> None:
    encrypted = tmp_path / "x.dump.age"
    encrypted.write_bytes(b"not really age")
    assert _run(RESTORE, [str(encrypted)], tmp_path).returncode == 2


# --- runbook -----------------------------------------------------------------


def test_runbook_keeps_the_undecided_placeholders_and_the_appeal_time() -> None:
    text = (ROOT / "docs" / "RUNBOOK.md").read_text()
    assert "<abuse report owner and response time — to be decided>" in text
    assert "<contact address>" in text
    assert "14 days" in text


# --- compose -----------------------------------------------------------------


def test_compose_prod_logs_json_and_checks_health() -> None:
    text = (ROOT / "compose.prod.yaml").read_text()
    server = text.split("\n  server:\n")[1].split("\n  caddy:\n")[0]
    assert "BREAKROOM_LOG_FORMAT: json" in server
    health = server.split("healthcheck:")[1]
    assert "/v1/health" in health


@pytest.mark.skipif(shutil.which("docker") is None, reason="needs docker compose")
def test_every_compose_service_rotates_its_logs() -> None:
    env = {
        **os.environ,
        **{
            name: "x"
            for name in (
                "POSTGRES_PASSWORD",
                "BREAKROOM_SERVER_SECRET",
                "BREAKROOM_SMTP_HOST",
                "BREAKROOM_SMTP_PORT",
                "BREAKROOM_SMTP_USERNAME",
                "BREAKROOM_SMTP_PASSWORD",
                "BREAKROOM_MAIL_FROM",
                "BREAKROOM_PUBLIC_HOST",
            )
        },
    }
    out = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(ROOT / "compose.prod.yaml"),
            "config",
            "--format",
            "json",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    services = json.loads(out.stdout)["services"]
    assert set(services) >= {"postgres", "migrate", "server", "caddy"}
    for name, service in services.items():
        logging = service["logging"]
        assert logging["driver"] == "json-file", name
        assert logging["options"]["max-size"] and logging["options"]["max-file"], name

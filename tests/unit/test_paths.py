import os
import stat
from pathlib import Path

import pytest

from breakroom_shared import paths
from breakroom_shared.paths import UnsafeRuntimeDir, runtime_dir


@pytest.fixture
def fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """XDG_RUNTIME_DIR unset; the /tmp fallback parent redirected to tmp_path."""
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(paths, "_FALLBACK_PARENT", tmp_path)
    return tmp_path / f"breakroom-{os.getuid()}"


def test_fallback_dir_is_created_private(fallback: Path) -> None:
    path = runtime_dir()
    assert path == fallback / "breakroom"
    assert stat.S_IMODE(fallback.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o700


def test_existing_private_fallback_dir_is_accepted(fallback: Path) -> None:
    runtime_dir()
    assert runtime_dir() == fallback / "breakroom"


def test_fallback_with_wrong_mode_is_refused(fallback: Path) -> None:
    fallback.mkdir(mode=0o755)
    fallback.chmod(0o755)
    with pytest.raises(UnsafeRuntimeDir):
        runtime_dir()
    assert stat.S_IMODE(fallback.stat().st_mode) == 0o755  # not chmodded


def test_fallback_symlink_is_refused(fallback: Path, tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.mkdir(mode=0o700)
    fallback.symlink_to(target)
    with pytest.raises(UnsafeRuntimeDir):
        runtime_dir()
    assert not (target / "breakroom").exists()


def test_fallback_owned_by_someone_else_is_refused(
    fallback: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fallback.mkdir(mode=0o700)
    monkeypatch.setattr(os, "getuid", lambda: os.stat(fallback).st_uid + 1)
    with pytest.raises(UnsafeRuntimeDir):
        paths._check_private_dir(fallback)


def test_xdg_subdir_with_wrong_mode_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    sub = tmp_path / "breakroom"
    sub.mkdir(mode=0o700)
    sub.chmod(0o770)
    with pytest.raises(UnsafeRuntimeDir):
        runtime_dir()


def test_xdg_subdir_symlink_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    xdg = tmp_path / "xdg"
    xdg.mkdir()
    target = tmp_path / "elsewhere"
    target.mkdir(mode=0o700)
    (xdg / "breakroom").symlink_to(target)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(xdg))
    with pytest.raises(UnsafeRuntimeDir):
        runtime_dir()


def test_xdg_subdir_is_created_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert stat.S_IMODE(runtime_dir().stat().st_mode) == 0o700


def test_state_dir_honours_xdg_state_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    d = paths.state_dir()
    assert d == tmp_path / "breakroom"
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
    assert paths.state_path() == d / "companion-state.json"


def test_state_dir_defaults_under_home_local_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert paths.state_dir() == tmp_path / ".local" / "state" / "breakroom"


def test_state_dir_with_wrong_mode_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    sub = tmp_path / "breakroom"
    sub.mkdir(mode=0o700)
    sub.chmod(0o770)
    with pytest.raises(UnsafeRuntimeDir):
        paths.state_dir()


def test_state_dir_symlink_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    xdg = tmp_path / "xdg"
    xdg.mkdir()
    target = tmp_path / "elsewhere"
    target.mkdir(mode=0o700)
    (xdg / "breakroom").symlink_to(target)
    monkeypatch.setenv("XDG_STATE_HOME", str(xdg))
    with pytest.raises(UnsafeRuntimeDir):
        paths.state_dir()

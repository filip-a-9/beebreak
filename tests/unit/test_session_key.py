import multiprocessing
import stat
from pathlib import Path

import pytest

from breakroom_agent_bridge.session_key import derive_key, load_or_create_salt


def test_same_session_and_salt_give_same_key() -> None:
    salt = b"s" * 32
    assert derive_key("abc", salt) == derive_key("abc", salt)


def test_different_salt_gives_different_key() -> None:
    assert derive_key("abc", b"a" * 32) != derive_key("abc", b"b" * 32)


def test_different_session_gives_different_key() -> None:
    salt = b"s" * 32
    assert derive_key("abc", salt) != derive_key("abd", salt)


def test_key_is_16_hex_chars_and_hides_session_id() -> None:
    key = derive_key("raw-session-id", b"s" * 32)
    assert len(key) == 16
    int(key, 16)
    assert "raw-session-id" not in key


def test_salt_created_with_mode_0600(tmp_path: Path) -> None:
    path = tmp_path / "salt"
    salt = load_or_create_salt(path)
    assert len(salt) == 32
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_existing_salt_is_reused(tmp_path: Path) -> None:
    path = tmp_path / "salt"
    assert load_or_create_salt(path) == load_or_create_salt(path)


def _create(path: str, out: "multiprocessing.Queue[bytes]") -> None:
    out.put(load_or_create_salt(Path(path)))


def test_concurrent_creation_yields_one_salt(tmp_path: Path) -> None:
    ctx = multiprocessing.get_context("spawn")
    out: multiprocessing.Queue[bytes] = ctx.Queue()
    path = str(tmp_path / "salt")
    procs = [ctx.Process(target=_create, args=(path, out)) for _ in range(8)]
    for p in procs:
        p.start()
    salts = {out.get(timeout=10) for _ in procs}
    for p in procs:
        p.join()
    assert len(salts) == 1
    assert salts == {Path(path).read_bytes()}


@pytest.mark.parametrize("stale", [b"", b"short", b"x" * 31])
def test_short_existing_salt_is_replaced(tmp_path: Path, stale: bytes) -> None:
    path = tmp_path / "salt"
    path.write_bytes(stale)
    salt = load_or_create_salt(path)
    assert len(salt) == 32
    assert path.read_bytes() == salt
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert load_or_create_salt(path) == salt


def test_full_size_existing_salt_is_reused_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "salt"
    path.write_bytes(b"k" * 32)
    assert load_or_create_salt(path) == b"k" * 32
    assert path.read_bytes() == b"k" * 32


def test_no_temp_files_left_behind(tmp_path: Path) -> None:
    load_or_create_salt(tmp_path / "salt")
    assert [p.name for p in tmp_path.iterdir()] == ["salt"]

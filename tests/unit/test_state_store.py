import json
import os
import signal
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from breakroom_companion.state_store import FileStateStore, PersistedState

WHEN = datetime(2026, 10, 12, 12, 0, 0, tzinfo=UTC)


def test_round_trip(tmp_path: Path) -> None:
    store = FileStateStore(tmp_path / "s.json")
    state = PersistedState(enabled=False, cooldown_until=WHEN)
    assert store.save(state) is True
    assert FileStateStore(tmp_path / "s.json").load() == state


def test_round_trip_without_cooldown(tmp_path: Path) -> None:
    store = FileStateStore(tmp_path / "s.json")
    store.save(PersistedState())
    assert store.load() == PersistedState(enabled=True, cooldown_until=None)


def test_missing_file_gives_defaults(tmp_path: Path) -> None:
    assert FileStateStore(tmp_path / "nope.json").load() == PersistedState()


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        "",
        "[]",
        '{"version": 2, "enabled": true, "cooldown_until": null}',
        '{"enabled": true, "cooldown_until": null}',
        '{"version": 1, "enabled": "yes", "cooldown_until": null}',
        '{"version": 1, "enabled": true, "cooldown_until": "2026-10-12T12:00:00"}',
        '{"version": 1, "enabled": true, "cooldown_until": "garbage"}',
        '{"version": 1, "enabled": true, "cooldown_until": 5}',
        '{"version": true, "enabled": true, "cooldown_until": null}',
        '{"version": 1.0, "enabled": true, "cooldown_until": null}',
        # Out of datetime range once converted to UTC (OverflowError).
        '{"version": 1, "enabled": true,'
        ' "cooldown_until": "0001-01-01T00:00:00+05:00"}',
        # Nested deeper than the parser's recursion limit (RecursionError).
        pytest.param("[" * 100_000, id="deeply-nested"),
    ],
)
def test_unusable_file_turns_suggestions_off(tmp_path: Path, content: str) -> None:
    path = tmp_path / "s.json"
    path.write_text(content)
    assert FileStateStore(path).load() == PersistedState(enabled=False)


def test_unreadable_path_turns_suggestions_off(tmp_path: Path) -> None:
    (tmp_path / "s.json").mkdir()  # a directory where the file should be
    assert FileStateStore(tmp_path / "s.json").load() == PersistedState(enabled=False)


def test_symlinked_file_is_not_followed(tmp_path: Path) -> None:
    real = tmp_path / "real.json"
    real.write_text('{"version": 1, "enabled": true, "cooldown_until": null}')
    link = tmp_path / "s.json"
    link.symlink_to(real)
    assert FileStateStore(link).load() == PersistedState(enabled=False)


def test_saved_file_is_mode_0600_json(tmp_path: Path) -> None:
    path = tmp_path / "s.json"
    FileStateStore(path).save(PersistedState(cooldown_until=WHEN))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    data = json.loads(path.read_text())
    assert data["version"] == 1 and data["enabled"] is True
    assert datetime.fromisoformat(data["cooldown_until"]) == WHEN
    assert [p.name for p in tmp_path.iterdir()] == ["s.json"]  # no temp left over


def test_save_replaces_an_existing_file(tmp_path: Path) -> None:
    store = FileStateStore(tmp_path / "s.json")
    store.save(PersistedState(enabled=False))
    store.save(PersistedState(enabled=True))
    assert store.load().enabled is True


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory modes")
def test_failed_save_returns_false(tmp_path: Path) -> None:
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    try:
        assert FileStateStore(ro / "s.json").save(PersistedState()) is False
    finally:
        ro.chmod(0o700)


def test_save_into_missing_directory_returns_false(tmp_path: Path) -> None:
    assert FileStateStore(tmp_path / "gone" / "s.json").save(PersistedState()) is False


def test_fifo_at_state_path_turns_suggestions_off_without_blocking(
    tmp_path: Path,
) -> None:
    path = tmp_path / "s.json"
    os.mkfifo(path)

    class Hung(BaseException):
        # Not an OSError: load() must not be able to swallow it.
        pass

    def _hung(*_: object) -> None:
        raise Hung

    old = signal.signal(signal.SIGALRM, _hung)
    signal.alarm(2)
    try:
        assert FileStateStore(path).load() == PersistedState(enabled=False)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


def test_failed_replace_leaves_no_temp_file(tmp_path: Path) -> None:
    # A non-empty directory at the target makes os.replace fail after the temp
    # file was written.
    target = tmp_path / "s.json"
    target.mkdir()
    (target / "keep").write_text("x")
    assert FileStateStore(target).save(PersistedState()) is False
    assert [p.name for p in tmp_path.iterdir()] == ["s.json"]


def test_stack_tags_round_trip_in_vocabulary_order(tmp_path: Path) -> None:
    store = FileStateStore(tmp_path / "s.json")
    state = PersistedState(stack_tags=("python", "docker"))
    assert store.save(state) is True
    assert FileStateStore(tmp_path / "s.json").load().stack_tags == (
        "python",
        "docker",
    )


def _load_with_tags(tmp_path: Path, tags: object) -> PersistedState:
    path = tmp_path / "s.json"
    path.write_text(
        json.dumps(
            {"version": 1, "enabled": True, "cooldown_until": None, "stack_tags": tags}
        )
    )
    return FileStateStore(path).load()


def test_unknown_duplicate_and_excess_tags_are_dropped_leniently(
    tmp_path: Path,
) -> None:
    loaded = _load_with_tags(
        tmp_path, ["docker", "nope", 7, {"a": 1}, "python", "python", "go"]
    )
    assert loaded.enabled is True  # the rest of the file still counts
    assert loaded.stack_tags == ("python", "go", "docker")
    many = _load_with_tags(
        tmp_path, ["python", "go", "rust", "java", "ruby", "php", "swift"]
    )
    assert many.stack_tags == ("python", "go", "rust", "java", "ruby")


@pytest.mark.parametrize("bad", ["python", 5, None, {"python": 1}])
def test_a_malformed_tag_list_means_no_tags(tmp_path: Path, bad: object) -> None:
    loaded = _load_with_tags(tmp_path, bad)
    assert loaded.stack_tags == () and loaded.enabled is True


def test_a_file_without_tags_loads_with_none(tmp_path: Path) -> None:
    path = tmp_path / "s.json"
    path.write_text('{"version": 1, "enabled": false, "cooldown_until": null}')
    assert FileStateStore(path).load() == PersistedState(enabled=False)

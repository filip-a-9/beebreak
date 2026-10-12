from collections.abc import Callable

import pytest

from breakroom_companion import config

CASES: list[tuple[Callable[[], float], str, float]] = [
    (config.ipc_read_timeout_s, "BREAKROOM_IPC_READ_TIMEOUT_S", 1.0),
    (config.worker_start_timeout_s, "BREAKROOM_WORKER_START_TIMEOUT_S", 5.0),
    (config.worker_stop_timeout_s, "BREAKROOM_WORKER_STOP_TIMEOUT_S", 5.0),
    (config.probe_timeout_s, "BREAKROOM_PROBE_TIMEOUT_S", 1.0),
]


@pytest.mark.parametrize(("getter", "name", "default"), CASES)
def test_default_when_unset(
    getter: Callable[[], float], name: str, default: float
) -> None:
    assert getter() == default


@pytest.mark.parametrize(("getter", "name", "default"), CASES)
def test_env_overrides(
    monkeypatch: pytest.MonkeyPatch,
    getter: Callable[[], float],
    name: str,
    default: float,
) -> None:
    monkeypatch.setenv(name, "0.25")
    assert getter() == 0.25


@pytest.mark.parametrize(("getter", "name", "default"), CASES)
@pytest.mark.parametrize("bad", ["0", "-1", "abc", ""])
def test_bad_values_fall_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
    getter: Callable[[], float],
    name: str,
    default: float,
    bad: str,
) -> None:
    monkeypatch.setenv(name, bad)
    assert getter() == default


def test_trigger_config_lives_in_the_config_module() -> None:
    assert config.TriggerConfig().cooldown_s == 900

from collections.abc import Callable

import pytest

from breakroom_companion import config

CASES: list[tuple[Callable[[], float], str, float]] = [
    (config.ipc_read_timeout_s, "BREAKROOM_IPC_READ_TIMEOUT_S", 1.0),
    (config.worker_start_timeout_s, "BREAKROOM_WORKER_START_TIMEOUT_S", 5.0),
    (config.worker_stop_timeout_s, "BREAKROOM_WORKER_STOP_TIMEOUT_S", 5.0),
    (config.probe_timeout_s, "BREAKROOM_PROBE_TIMEOUT_S", 1.0),
    (config.notify_interval_s, "BREAKROOM_NOTIFY_INTERVAL_S", 10.0),
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


def test_trigger_config_has_no_quiet_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    assert not hasattr(config.TriggerConfig(), "quiet_gap_s")
    monkeypatch.setenv("BREAKROOM_QUIET_GAP_S", "0")  # ignored now
    assert config.TriggerConfig.from_env().cooldown_s == 900


def test_trigger_config_env_and_rejection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BREAKROOM_COOLDOWN_S", "5")
    assert config.TriggerConfig.from_env().cooldown_s == 5
    for bad in ("0", "-3", "abc"):
        monkeypatch.setenv("BREAKROOM_COOLDOWN_S", bad)
        with pytest.raises(ValueError):
            config.TriggerConfig.from_env()


def test_task_evidence_wait_default_and_env(monkeypatch: pytest.MonkeyPatch) -> None:
    assert config.TriggerConfig().task_evidence_wait_s == 300
    monkeypatch.setenv("BREAKROOM_TASK_EVIDENCE_WAIT_S", "40")
    assert config.TriggerConfig.from_env().task_evidence_wait_s == 40
    with pytest.raises(ValueError):
        config.TriggerConfig(task_evidence_wait_s=0)

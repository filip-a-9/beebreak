import pytest

from beebreak_companion.endpoint import EndpointRefused
from beebreak_companion.remote_config import RemoteConfig


def test_defaults() -> None:
    config = RemoteConfig.from_env()

    assert config.server_url == "http://127.0.0.1:8000"
    assert config.http_timeout_s == 10
    assert config.ws_ping_interval_s == 25
    assert config.reconnect_min_s == 1
    assert config.reconnect_max_s == 60


def test_server_url_is_canonicalised(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BEEBREAK_SERVER_URL", "HTTPS://Chat.Example/")

    assert RemoteConfig.from_env().server_url == "https://chat.example"


def test_disallowed_server_url_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BEEBREAK_SERVER_URL", "http://example.com")

    with pytest.raises(EndpointRefused):
        RemoteConfig.from_env()


def test_timings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BEEBREAK_HTTP_TIMEOUT_S", "2.5")
    monkeypatch.setenv("BEEBREAK_WS_PING_INTERVAL_S", "0.1")
    monkeypatch.setenv("BEEBREAK_RECONNECT_MIN_S", "0.05")
    monkeypatch.setenv("BEEBREAK_RECONNECT_MAX_S", "3")

    config = RemoteConfig.from_env()

    assert (config.http_timeout_s, config.ws_ping_interval_s) == (2.5, 0.1)
    assert (config.reconnect_min_s, config.reconnect_max_s) == (0.05, 3)


@pytest.mark.parametrize("value", ["0", "-1", "abc", "nan", "inf", ""])
@pytest.mark.parametrize(
    "name",
    [
        "BEEBREAK_HTTP_TIMEOUT_S",
        "BEEBREAK_WS_PING_INTERVAL_S",
        "BEEBREAK_RECONNECT_MIN_S",
        "BEEBREAK_RECONNECT_MAX_S",
    ],
)
def test_invalid_timing_is_rejected(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError):
        RemoteConfig.from_env()


def test_reconnect_min_above_max_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BEEBREAK_RECONNECT_MIN_S", "90")

    with pytest.raises(ValueError):
        RemoteConfig.from_env()


_NEW_FLOAT_ENVS = [
    "BEEBREAK_REFRESH_LEAD_S",
    "BEEBREAK_NOMINAL_ACCESS_TTL_S",
    "BEEBREAK_MIN_REFRESH_DELAY_S",
    "BEEBREAK_GITHUB_MIN_POLL_INTERVAL_S",
]


def test_refresh_settings_from_env_reach_the_refresh_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from beebreak_companion.remote import refresh_delay_s

    monkeypatch.setenv("BEEBREAK_REFRESH_LEAD_S", "10")
    monkeypatch.setenv("BEEBREAK_NOMINAL_ACCESS_TTL_S", "100")
    monkeypatch.setenv("BEEBREAK_MIN_REFRESH_DELAY_S", "0.5")
    monkeypatch.setenv("BEEBREAK_GITHUB_POLL_RETRIES", "7")
    monkeypatch.setenv("BEEBREAK_GITHUB_MIN_POLL_INTERVAL_S", "0.25")

    config = RemoteConfig.from_env()

    assert config.github_poll_retries == 7
    assert config.github_min_poll_interval_s == 0.25
    assert refresh_delay_s(80, config) == 70  # ttl 80 minus a 10 s lead
    assert refresh_delay_s(5000, config) == 90  # beyond 100 s: nominal 100 - 10
    assert refresh_delay_s(0.2, config) == 0.5  # the floor


@pytest.mark.parametrize("value", ["0", "-1", "abc", "nan", "inf", ""])
@pytest.mark.parametrize("name", [*_NEW_FLOAT_ENVS, "BEEBREAK_GITHUB_POLL_RETRIES"])
def test_invalid_refresh_setting_is_rejected(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError):
        RemoteConfig.from_env()


def test_fractional_poll_retries_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BEEBREAK_GITHUB_POLL_RETRIES", "2.5")

    with pytest.raises(ValueError):
        RemoteConfig.from_env()

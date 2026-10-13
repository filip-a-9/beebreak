import pytest

from breakroom_companion.endpoint import EndpointRefused
from breakroom_companion.remote_config import RemoteConfig


def test_defaults() -> None:
    config = RemoteConfig.from_env()

    assert config.server_url == "http://127.0.0.1:8000"
    assert config.http_timeout_s == 10
    assert config.ws_ping_interval_s == 25
    assert config.reconnect_min_s == 1
    assert config.reconnect_max_s == 60


def test_server_url_is_canonicalised(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BREAKROOM_SERVER_URL", "HTTPS://Chat.Example/")

    assert RemoteConfig.from_env().server_url == "https://chat.example"


def test_disallowed_server_url_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BREAKROOM_SERVER_URL", "http://example.com")

    with pytest.raises(EndpointRefused):
        RemoteConfig.from_env()


def test_timings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BREAKROOM_HTTP_TIMEOUT_S", "2.5")
    monkeypatch.setenv("BREAKROOM_WS_PING_INTERVAL_S", "0.1")
    monkeypatch.setenv("BREAKROOM_RECONNECT_MIN_S", "0.05")
    monkeypatch.setenv("BREAKROOM_RECONNECT_MAX_S", "3")

    config = RemoteConfig.from_env()

    assert (config.http_timeout_s, config.ws_ping_interval_s) == (2.5, 0.1)
    assert (config.reconnect_min_s, config.reconnect_max_s) == (0.05, 3)


@pytest.mark.parametrize("value", ["0", "-1", "abc", "nan", "inf", ""])
@pytest.mark.parametrize(
    "name",
    [
        "BREAKROOM_HTTP_TIMEOUT_S",
        "BREAKROOM_WS_PING_INTERVAL_S",
        "BREAKROOM_RECONNECT_MIN_S",
        "BREAKROOM_RECONNECT_MAX_S",
    ],
)
def test_invalid_timing_is_rejected(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError):
        RemoteConfig.from_env()


def test_reconnect_min_above_max_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BREAKROOM_RECONNECT_MIN_S", "90")

    with pytest.raises(ValueError):
        RemoteConfig.from_env()

import base64

import pytest

from breakroom_server.config import ServerConfig, decode_secret

SECRET = base64.urlsafe_b64encode(b"s" * 48).decode().rstrip("=")


def test_from_env_requires_database_url_and_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError):
        ServerConfig.from_env()
    monkeypatch.setenv("BREAKROOM_DATABASE_URL", "sqlite://")
    with pytest.raises(ValueError):
        ServerConfig.from_env()
    monkeypatch.setenv("BREAKROOM_SERVER_SECRET", SECRET)
    config = ServerConfig.from_env()
    assert config.secret == b"s" * 48
    assert config.access_ttl_s == 900


def test_short_secret_rejected() -> None:
    with pytest.raises(ValueError):
        decode_secret(base64.urlsafe_b64encode(b"s" * 31).decode())


def test_secret_error_does_not_echo_value() -> None:
    with pytest.raises(ValueError) as info:
        decode_secret("not*base64*!!")
    assert "not*base64" not in str(info.value)


@pytest.mark.parametrize("value", ["0", "-5", "x"])
def test_non_positive_or_bad_numbers_rejected(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("BREAKROOM_DATABASE_URL", "sqlite://")
    monkeypatch.setenv("BREAKROOM_SERVER_SECRET", SECRET)
    monkeypatch.setenv("BREAKROOM_ACCESS_TTL_S", value)
    with pytest.raises(ValueError):
        ServerConfig.from_env()


CHAT_DEFAULTS = {
    "chat_grace_s": 120,
    "chat_extension_s": 600,
    "chat_max_extensions": 2,
    "chat_disconnect_s": 60,
    "message_retention_s": 86_400,
    "report_window_s": 86_400,
    "report_retention_s": 7_776_000,
    "rate_message_user": 20,
    "rate_message_window_s": 10,
    "rate_report_user": 5,
}


def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BREAKROOM_DATABASE_URL", "sqlite://")
    monkeypatch.setenv("BREAKROOM_SERVER_SECRET", SECRET)


def test_chat_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch)
    config = ServerConfig.from_env()
    for name, value in CHAT_DEFAULTS.items():
        assert getattr(config, name) == value


@pytest.mark.parametrize("name", sorted(CHAT_DEFAULTS))
def test_chat_values_read_from_env(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    _env(monkeypatch)
    monkeypatch.setenv(f"BREAKROOM_{name.upper()}", "7")
    assert getattr(ServerConfig.from_env(), name) == 7


@pytest.mark.parametrize("name", sorted(set(CHAT_DEFAULTS) - {"chat_max_extensions"}))
def test_chat_values_must_be_positive(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    _env(monkeypatch)
    monkeypatch.setenv(f"BREAKROOM_{name.upper()}", "0")
    with pytest.raises(ValueError):
        ServerConfig.from_env()


def test_zero_extensions_allowed_negative_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch)
    monkeypatch.setenv("BREAKROOM_CHAT_MAX_EXTENSIONS", "0")
    assert ServerConfig.from_env().chat_max_extensions == 0
    for bad in ("-1", "x"):
        monkeypatch.setenv("BREAKROOM_CHAT_MAX_EXTENSIONS", bad)
        with pytest.raises(ValueError):
            ServerConfig.from_env()
    with pytest.raises(ValueError):
        ServerConfig(database_url="sqlite://", secret=b"s" * 32, chat_max_extensions=-1)

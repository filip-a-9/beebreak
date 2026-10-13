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

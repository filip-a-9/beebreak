"""Which sign-in methods a server offers is decided by its configuration."""

import base64
import dataclasses

import pytest

from breakroom_server.config import ServerConfig

SECRET = base64.urlsafe_b64encode(b"s" * 48).decode().rstrip("=")


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BREAKROOM_DATABASE_URL", "sqlite://")
    monkeypatch.setenv("BREAKROOM_SERVER_SECRET", SECRET)


def test_default_is_email_only() -> None:
    config = ServerConfig.from_env()
    assert config.email_enabled is True
    assert config.github_enabled is False
    assert config.github_oauth_url == "https://github.com"
    assert config.github_api_url == "https://api.github.com"


def test_client_id_turns_github_on_next_to_email(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BREAKROOM_GITHUB_CLIENT_ID", "Iv1.abc123")
    config = ServerConfig.from_env()
    assert (config.email_enabled, config.github_enabled) == (True, True)
    assert config.github_client_id == "Iv1.abc123"


def test_empty_smtp_host_with_github_is_a_github_only_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BREAKROOM_SMTP_HOST", "")
    monkeypatch.setenv("BREAKROOM_GITHUB_CLIENT_ID", "Iv1.abc123")
    config = ServerConfig.from_env()
    assert (config.email_enabled, config.github_enabled) == (False, True)


def test_no_method_at_all_refuses_to_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BREAKROOM_SMTP_HOST", "")
    with pytest.raises(ValueError, match="sign-in method"):
        ServerConfig.from_env()


def test_the_recording_mailer_counts_as_email(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BREAKROOM_SMTP_HOST", "")
    monkeypatch.setenv("BREAKROOM_MAILER", "record")
    assert ServerConfig.from_env().email_enabled is True


@pytest.mark.parametrize("client_id", ["has space", "a/b", "x" * 101, "ünï"])
def test_a_malformed_client_id_is_rejected_without_echoing_it(
    monkeypatch: pytest.MonkeyPatch, client_id: str
) -> None:
    monkeypatch.setenv("BREAKROOM_GITHUB_CLIENT_ID", client_id)
    with pytest.raises(ValueError) as info:
        ServerConfig.from_env()
    assert client_id not in str(info.value)


def test_github_urls_and_timeout_come_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BREAKROOM_GITHUB_CLIENT_ID", "Iv1.abc123")
    monkeypatch.setenv("BREAKROOM_GITHUB_OAUTH_URL", "http://127.0.0.1:9/")
    monkeypatch.setenv("BREAKROOM_GITHUB_API_URL", "http://127.0.0.1:8")
    monkeypatch.setenv("BREAKROOM_GITHUB_TIMEOUT_S", "3")
    config = ServerConfig.from_env()
    assert config.github_oauth_url == "http://127.0.0.1:9"  # no trailing slash
    assert config.github_api_url == "http://127.0.0.1:8"
    assert config.github_timeout_s == 3


@pytest.mark.parametrize("url", ["ftp://x", "github.com", "http://", ""])
def test_a_bad_base_url_is_rejected(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    monkeypatch.setenv("BREAKROOM_GITHUB_API_URL", url)
    with pytest.raises(ValueError):
        ServerConfig.from_env()


def test_timeout_must_be_positive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BREAKROOM_GITHUB_TIMEOUT_S", "0")
    with pytest.raises(ValueError):
        ServerConfig.from_env()


def test_direct_construction_validates_too() -> None:
    config = ServerConfig.from_env()
    with pytest.raises(ValueError):
        dataclasses.replace(config, smtp_host="", github_client_id="")

"""M6-03 config: trusted proxies, weak database password, SMTP TLS and auth."""

import base64

import pytest

from beebreak_server.config import ServerConfig, reject_weak_database_url
from tests.server.conftest import TEST_SECRET

SECRET = base64.urlsafe_b64encode(TEST_SECRET).decode()
GOOD_URL = "postgresql+psycopg://beebreak:s3cret-value@postgres:5432/beebreak"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BEEBREAK_DATABASE_URL", GOOD_URL)
    monkeypatch.setenv("BEEBREAK_SERVER_SECRET", SECRET)


def test_trusted_proxies_default_to_none() -> None:
    assert ServerConfig.from_env().trusted_proxies == ()


def test_trusted_proxies_parse_cidrs_and_addresses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BEEBREAK_TRUSTED_PROXIES", "172.18.0.0/16, 10.0.0.5 ,fd00::/8")
    config = ServerConfig.from_env()
    assert config.trusted_proxies == ("172.18.0.0/16", "10.0.0.5", "fd00::/8")


@pytest.mark.parametrize(
    "value",
    ["*", "not-an-ip", "10.0.0.0/33", "10.0.0.1,,", "10.0.0.1/24", "172.29.7.1/24"],
)
def test_trusted_proxies_reject_wildcards_and_garbage(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("BEEBREAK_TRUSTED_PROXIES", value)
    with pytest.raises(ValueError):
        ServerConfig.from_env()


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+psycopg://beebreak:change-me@postgres:5432/beebreak",
        "postgresql://u:change-me@h/db",
    ],
)
def test_example_database_password_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    monkeypatch.setenv("BEEBREAK_DATABASE_URL", url)
    with pytest.raises(ValueError) as info:
        ServerConfig.from_env()
    assert "change-me" not in str(info.value)
    with pytest.raises(ValueError):
        reject_weak_database_url(url)


def test_other_passwords_and_sqlite_are_accepted() -> None:
    reject_weak_database_url(GOOD_URL)
    reject_weak_database_url("sqlite:////tmp/x.db")
    reject_weak_database_url("postgresql://u@h/db")


def test_smtp_tls_defaults_to_none_without_credentials() -> None:
    config = ServerConfig.from_env()
    assert config.smtp_tls == "none"
    assert config.smtp_username is None and config.smtp_password is None


def test_smtp_tls_modes(monkeypatch: pytest.MonkeyPatch) -> None:
    for mode in ("none", "starttls", "implicit"):
        monkeypatch.setenv("BEEBREAK_SMTP_TLS", mode)
        assert ServerConfig.from_env().smtp_tls == mode
    monkeypatch.setenv("BEEBREAK_SMTP_TLS", "ssl")
    with pytest.raises(ValueError):
        ServerConfig.from_env()


def test_old_starttls_flag_is_an_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BEEBREAK_SMTP_STARTTLS", "1")
    assert ServerConfig.from_env().smtp_tls == "starttls"
    monkeypatch.setenv("BEEBREAK_SMTP_TLS", "implicit")  # the new name wins
    assert ServerConfig.from_env().smtp_tls == "implicit"


def test_smtp_credentials_need_tls_and_both_halves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BEEBREAK_SMTP_USERNAME", "mailer")
    monkeypatch.setenv("BEEBREAK_SMTP_PASSWORD", "hunter2-hunter2")
    with pytest.raises(ValueError) as info:  # no TLS: credentials would be sent clear
        ServerConfig.from_env()
    assert "hunter2" not in str(info.value)
    monkeypatch.setenv("BEEBREAK_SMTP_TLS", "starttls")
    config = ServerConfig.from_env()
    assert config.smtp_username == "mailer"
    assert "hunter2" not in repr(config)
    monkeypatch.delenv("BEEBREAK_SMTP_PASSWORD")
    with pytest.raises(ValueError):
        ServerConfig.from_env()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("BEEBREAK_SMTP_USERNAME", "change-me-user"),
        ("BEEBREAK_SMTP_PASSWORD", "change-me-too"),
        ("BEEBREAK_SMTP_PASSWORD", "change-me"),
    ],
)
def test_example_smtp_placeholders_refuse_to_start(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv("BEEBREAK_SMTP_TLS", "starttls")
    monkeypatch.setenv("BEEBREAK_SMTP_USERNAME", "real-user")
    monkeypatch.setenv("BEEBREAK_SMTP_PASSWORD", "real-password-123")
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError) as info:
        ServerConfig.from_env()
    assert value not in str(info.value)


def test_example_server_secret_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BEEBREAK_SERVER_SECRET", "change-me")
    with pytest.raises(ValueError):
        ServerConfig.from_env()

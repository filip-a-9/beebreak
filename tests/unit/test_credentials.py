import json
import os
import stat
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import keyring
import keyring.backend
import keyring.errors
import pytest
from keyring.backends import fail

from beebreak_companion.credentials import (
    FileCredentialStore,
    KeyringCredentialStore,
    StoredCredentials,
    default_credential_store,
)

ORIGIN = "http://127.0.0.1:8000"
FUTURE = datetime(2099, 1, 1, tzinfo=UTC)
PAST = datetime(2020, 1, 1, tzinfo=UTC)
CREDS = StoredCredentials(
    refresh_token="brr_test-token-value", refresh_expires_at=FUTURE
)


class MemoryKeyring(keyring.backend.KeyringBackend):
    priority = 1

    def __init__(self) -> None:
        super().__init__()  # type: ignore[no-untyped-call]
        self.items: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.items.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.items[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if (service, username) not in self.items:
            raise keyring.errors.PasswordDeleteError("missing")
        del self.items[(service, username)]


@pytest.fixture
def memory_keyring() -> Iterator[MemoryKeyring]:
    previous = keyring.get_keyring()
    backend = MemoryKeyring()
    keyring.set_keyring(backend)
    yield backend
    keyring.set_keyring(previous)


@pytest.fixture
def state_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    return tmp_path / "state"


@pytest.fixture
def private_dir(tmp_path: Path) -> Path:
    d = tmp_path / "priv"
    d.mkdir(mode=0o700)
    return d


# File store


def test_file_round_trip(private_dir: Path) -> None:
    store = FileCredentialStore(private_dir / "c.json", ORIGIN)

    assert store.save(CREDS) is True
    assert FileCredentialStore(private_dir / "c.json", ORIGIN).load() == CREDS


def test_file_is_mode_0600(private_dir: Path) -> None:
    path = private_dir / "c.json"
    FileCredentialStore(path, ORIGIN).save(CREDS)

    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_file_store_is_not_encrypted(private_dir: Path) -> None:
    assert FileCredentialStore(private_dir / "c.json", ORIGIN).encrypted is False


def test_file_for_other_origin_loads_as_none(private_dir: Path) -> None:
    path = private_dir / "c.json"
    FileCredentialStore(path, ORIGIN).save(CREDS)

    assert FileCredentialStore(path, "https://other.example").load() is None


def test_file_missing_loads_as_none(private_dir: Path) -> None:
    assert FileCredentialStore(private_dir / "none.json", ORIGIN).load() is None


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "[]",
        "{}",
        json.dumps({"version": 2}),
        json.dumps({"version": True}),
        json.dumps({"version": 1, "origin": ORIGIN, "refresh_token": "t"}),
        json.dumps(
            {
                "version": 1,
                "origin": ORIGIN,
                "refresh_token": "t",
                "refresh_expires_at": "2099-01-01T00:00:00",  # naive
            }
        ),
        json.dumps(
            {
                "version": 1,
                "origin": ORIGIN,
                "refresh_token": "t",
                "refresh_expires_at": "garbage",
            }
        ),
        json.dumps(
            {
                "version": 1,
                "origin": ORIGIN,
                "refresh_token": "",
                "refresh_expires_at": "2099-01-01T00:00:00+00:00",
            }
        ),
    ],
)
def test_file_corrupt_loads_as_none(private_dir: Path, content: str) -> None:
    path = private_dir / "c.json"
    path.write_text(content, encoding="utf-8")

    assert FileCredentialStore(path, ORIGIN).load() is None


def test_file_expired_loads_as_none(private_dir: Path) -> None:
    path = private_dir / "c.json"
    store = FileCredentialStore(path, ORIGIN)
    store.save(StoredCredentials(refresh_token="t", refresh_expires_at=PAST))

    assert store.load() is None


def test_file_symlink_is_not_followed(private_dir: Path) -> None:
    real = private_dir / "real.json"
    FileCredentialStore(real, ORIGIN).save(CREDS)
    link = private_dir / "link.json"
    link.symlink_to(real)

    assert FileCredentialStore(link, ORIGIN).load() is None


def test_file_clear(private_dir: Path) -> None:
    path = private_dir / "c.json"
    store = FileCredentialStore(path, ORIGIN)
    store.save(CREDS)

    assert store.clear() is True
    assert not path.exists()
    assert store.clear() is True  # already gone


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory modes")
def test_file_save_in_read_only_dir_returns_false(private_dir: Path) -> None:
    private_dir.chmod(0o500)
    try:
        assert FileCredentialStore(private_dir / "c.json", ORIGIN).save(CREDS) is False
    finally:
        private_dir.chmod(0o700)


def test_file_save_into_missing_dir_returns_false(tmp_path: Path) -> None:
    store = FileCredentialStore(tmp_path / "no" / "such" / "c.json", ORIGIN)

    assert store.save(CREDS) is False


# Keyring store


def test_keyring_round_trip_and_clear(
    memory_keyring: MemoryKeyring, state_home: Path
) -> None:
    store = KeyringCredentialStore(ORIGIN)

    assert store.encrypted is True
    assert store.load() is None
    assert store.save(CREDS) is True
    assert KeyringCredentialStore(ORIGIN).load() == CREDS
    assert store.clear() is True
    assert store.load() is None
    assert store.clear() is True  # clearing nothing is fine


def test_keyring_value_is_versioned_json(
    memory_keyring: MemoryKeyring, state_home: Path
) -> None:
    KeyringCredentialStore(ORIGIN).save(CREDS)

    ((service, _), value) = next(iter(memory_keyring.items.items()))
    assert service == "beebreak"
    assert json.loads(value)["version"] == 1


def test_keyring_two_state_dirs_do_not_collide(
    memory_keyring: MemoryKeyring,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "a"))
    first = KeyringCredentialStore(ORIGIN)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "b"))
    second = KeyringCredentialStore(ORIGIN)

    first.save(CREDS)

    assert second.load() is None
    assert first.load() == CREDS


def test_keyring_two_origins_do_not_collide(
    memory_keyring: MemoryKeyring, state_home: Path
) -> None:
    KeyringCredentialStore(ORIGIN).save(CREDS)

    assert KeyringCredentialStore("https://other.example").load() is None


def test_keyring_expired_and_corrupt_load_as_none(
    memory_keyring: MemoryKeyring, state_home: Path
) -> None:
    store = KeyringCredentialStore(ORIGIN)
    store.save(StoredCredentials(refresh_token="t", refresh_expires_at=PAST))
    assert store.load() is None

    key = next(iter(memory_keyring.items))
    memory_keyring.items[key] = "{not json"
    assert store.load() is None


def test_keyring_failures_return_false_or_none(state_home: Path) -> None:
    previous = keyring.get_keyring()
    keyring.set_keyring(_fail_backend())
    try:
        store = KeyringCredentialStore(ORIGIN)
        assert store.load() is None
        assert store.save(CREDS) is False
        assert store.clear() is False
    finally:
        keyring.set_keyring(previous)


# Default selection


def test_default_uses_file_store_when_fail_backend_active(state_home: Path) -> None:
    previous = keyring.get_keyring()
    keyring.set_keyring(_fail_backend())
    try:
        store = default_credential_store(ORIGIN)
    finally:
        keyring.set_keyring(previous)

    assert isinstance(store, FileCredentialStore)
    assert store.encrypted is False
    assert store.save(CREDS) is True
    assert (state_home / "beebreak" / "credentials.json").is_file()


def test_default_uses_keyring_with_working_backend(
    memory_keyring: MemoryKeyring, state_home: Path
) -> None:
    store = default_credential_store(ORIGIN)

    assert isinstance(store, KeyringCredentialStore)
    assert store.encrypted is True


def test_default_falls_back_when_probe_raises(state_home: Path) -> None:
    class Broken(MemoryKeyring):
        def get_password(self, service: str, username: str) -> str | None:
            raise keyring.errors.KeyringError("locked")

    previous = keyring.get_keyring()
    keyring.set_keyring(Broken())
    try:
        store = default_credential_store(ORIGIN)
    finally:
        keyring.set_keyring(previous)

    assert isinstance(store, FileCredentialStore)


def _fail_backend() -> keyring.backend.KeyringBackend:
    return fail.Keyring()  # type: ignore[no-untyped-call]

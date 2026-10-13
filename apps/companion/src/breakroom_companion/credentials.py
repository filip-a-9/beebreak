"""Refresh-token storage: OS keyring, or a 0600 file fallback. Owned by M3-04."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class StoredCredentials:
    refresh_token: str
    refresh_expires_at: datetime  # aware UTC


class CredentialStore(Protocol):
    def load(self) -> StoredCredentials | None: ...
    def save(self, creds: StoredCredentials) -> bool: ...
    def clear(self) -> bool: ...
    @property
    def encrypted(self) -> bool: ...


def default_credential_store(origin: str) -> CredentialStore:
    raise NotImplementedError

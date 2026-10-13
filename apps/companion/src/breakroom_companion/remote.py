"""Background worker for the server connection. Owned by M3-05a."""

from typing import Literal

from PySide6.QtCore import QObject, Signal

from breakroom_companion.credentials import CredentialStore
from breakroom_companion.remote_config import RemoteConfig
from breakroom_shared.api import Consent

ConnectionState = Literal[
    "signed_out", "signing_in", "connecting", "connected", "offline"
]


class RemoteWorker(QObject):
    """Create on the Qt main thread; signals are delivered there (queued)."""

    state_changed = Signal(str)  # ConnectionState
    me_changed = Signal(object)  # Me | None
    error = Signal(str)  # ErrorCode, "network" or "replaced"

    def __init__(self, config: RemoteConfig, store: CredentialStore) -> None:
        super().__init__()
        self.config = config
        self.store = store

    def start(self) -> None:
        raise NotImplementedError

    def begin_sign_in(self, email: str, invite_code: str | None) -> None:
        raise NotImplementedError

    def verify(self, code: str) -> None:
        raise NotImplementedError

    def set_nickname(self, nickname: str) -> None:
        raise NotImplementedError

    def set_consent(self, consent: Consent) -> None:
        raise NotImplementedError

    def sign_out(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

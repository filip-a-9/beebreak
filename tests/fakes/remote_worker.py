"""A RemoteWorker that never touches the network, for UI and wiring tests (M4).

It has the real signals (it subclasses RemoteWorker) but records every public
call instead of scheduling work. Tests drive the UI by emitting the signals
through the `push_*` helpers.
"""

from dataclasses import dataclass, field

from breakroom_companion.break_state import BreakState
from breakroom_companion.credentials import StoredCredentials
from breakroom_companion.remote import ConnectionState, RemoteWorker
from breakroom_companion.remote_config import RemoteConfig
from breakroom_shared.api import Consent, Me
from breakroom_shared.presence import OutboundPresence


class _NoCredentials:
    def load(self) -> StoredCredentials | None:
        return None

    def save(self, creds: StoredCredentials) -> bool:
        return False

    def clear(self) -> bool:
        return True

    @property
    def encrypted(self) -> bool:
        return True


@dataclass
class Calls:
    joins: list[OutboundPresence] = field(default_factory=list)
    leaves: int = 0
    ends: int = 0
    blocks: int = 0
    nicknames: list[str] = field(default_factory=list)
    consents: list[Consent] = field(default_factory=list)
    sign_ins: list[tuple[str, str | None]] = field(default_factory=list)
    codes: list[str] = field(default_factory=list)
    starts: int = 0
    sign_outs: int = 0
    stops: int = 0


class FakeRemoteWorker(RemoteWorker):
    def __init__(self) -> None:
        super().__init__(
            RemoteConfig(server_url="http://127.0.0.1:9"), _NoCredentials()
        )
        self.calls = Calls()

    # --- recorded public methods ---

    def start(self) -> None:
        self.calls.starts += 1

    def begin_sign_in(self, email: str, invite_code: str | None) -> None:
        self.calls.sign_ins.append((email, invite_code))

    def verify(self, code: str) -> None:
        self.calls.codes.append(code)

    def set_nickname(self, nickname: str) -> None:
        self.calls.nicknames.append(nickname)

    def set_consent(self, consent: Consent) -> None:
        self.calls.consents.append(consent)

    def sign_out(self) -> None:
        self.calls.sign_outs += 1

    def stop(self) -> None:
        self.calls.stops += 1

    def join_queue(self, presence: OutboundPresence) -> None:
        self.calls.joins.append(presence)

    def leave_queue(self) -> None:
        self.calls.leaves += 1

    def end_match(self) -> None:
        self.calls.ends += 1

    def block_peer(self) -> None:
        self.calls.blocks += 1

    # --- drive the signals as the real worker would ---

    def push_state(self, state: ConnectionState) -> None:
        self.state = state
        self.state_changed.emit(state)

    def push_me(self, me: Me | None) -> None:
        self.me_changed.emit(me)

    def push_error(self, code: str) -> None:
        self.error.emit(code)

    def push_break(self, state: BreakState) -> None:
        self.break_changed.emit(state)

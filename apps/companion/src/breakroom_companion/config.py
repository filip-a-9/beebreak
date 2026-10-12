"""Typed companion configuration read from environment variables.

IPC section owned by M1-03; M1-05 adds trigger settings in its own module.
Values are read at call time so tests and launchers can override them.
"""

import os

IPC_READ_TIMEOUT_ENV = "BREAKROOM_IPC_READ_TIMEOUT_S"
_DEFAULT_IPC_READ_TIMEOUT_S = 1.0


def ipc_read_timeout_s() -> float:
    """Seconds a client connection may take to deliver its one frame line."""
    raw = os.environ.get(IPC_READ_TIMEOUT_ENV)
    if raw is None:
        return _DEFAULT_IPC_READ_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_IPC_READ_TIMEOUT_S
    return value if value > 0 else _DEFAULT_IPC_READ_TIMEOUT_S

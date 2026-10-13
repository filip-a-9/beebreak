"""FastAPI application factory. Owned by M3-02 (routes) and M3-03 (realtime)."""

from collections.abc import Callable
from datetime import datetime

from fastapi import FastAPI

from breakroom_server.config import ServerConfig
from breakroom_server.mailer import Mailer


def create_app(
    config: ServerConfig,
    mailer: Mailer | None = None,
    clock: Callable[[], datetime] | None = None,
) -> FastAPI:
    raise NotImplementedError


def app_from_env() -> FastAPI:
    """Uvicorn factory: `uvicorn --factory breakroom_server.app:app_from_env`."""
    return create_app(ServerConfig.from_env())

"""Database engine, session factory and Alembic helpers. Owned by M3-01."""

from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

SessionFactory = sessionmaker[Session]

# apps/server, which holds alembic.ini and migrations/ (an editable install).
SERVER_DIR = Path(__file__).resolve().parents[2]


def make_engine(url: str) -> Engine:
    """Sync SQLAlchemy 2 engine. `postgresql://` means the psycopg 3 driver.

    SQLite gets foreign keys switched on; an in-memory SQLite database is shared
    by every thread through one connection.
    """
    parsed = make_url(url)
    if parsed.drivername == "postgresql":
        parsed = parsed.set(drivername="postgresql+psycopg")
    if parsed.get_backend_name() != "sqlite":
        return create_engine(parsed, pool_pre_ping=True, hide_parameters=True)

    # hide_parameters keeps emails and tokens out of DBAPI error messages.
    kwargs: dict[str, Any] = {
        "connect_args": {"check_same_thread": False},
        "hide_parameters": True,
    }
    if parsed.database in (None, "", ":memory:"):
        kwargs["poolclass"] = StaticPool
    engine = create_engine(parsed, **kwargs)

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def alembic_config(url: str) -> Config:
    config = Config(str(SERVER_DIR / "alembic.ini"))
    # configparser interpolation: a literal % in the URL must be doubled.
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


def upgrade_head(url: str) -> None:
    command.upgrade(alembic_config(url), "head")

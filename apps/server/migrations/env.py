"""Alembic environment: URL comes from the config, metadata from the models."""

from alembic import context
from sqlalchemy import Connection

from beebreak_server.db import make_engine
from beebreak_server.models import Base

config = context.config
target_metadata = Base.metadata


def _set_sqlite_foreign_keys(connection: Connection, on: bool) -> None:
    # The pragma is a no-op inside a transaction, so it is set between them.
    connection.exec_driver_sql(f"PRAGMA foreign_keys={'ON' if on else 'OFF'}")
    connection.commit()


def run_migrations_online() -> None:
    engine = make_engine(config.get_main_option("sqlalchemy.url") or "")
    try:
        with engine.connect() as connection:
            # SQLite batch mode rebuilds a table by dropping it; with foreign
            # keys on, that fails while other rows reference it. Switch them off
            # for the run and check every reference afterwards instead.
            sqlite = connection.dialect.name == "sqlite"
            if sqlite:
                _set_sqlite_foreign_keys(connection, on=False)
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                compare_type=True,
            )
            with context.begin_transaction():
                context.run_migrations()
                if sqlite:
                    broken = connection.exec_driver_sql(
                        "PRAGMA foreign_key_check"
                    ).first()
                    if broken is not None:
                        raise RuntimeError("migration left a dangling reference")
            if sqlite:
                _set_sqlite_foreign_keys(connection, on=True)
    finally:
        engine.dispose()


run_migrations_online()

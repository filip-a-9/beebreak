"""Alembic environment: URL comes from the config, metadata from the models."""

from alembic import context

from breakroom_server.db import make_engine
from breakroom_server.models import Base

config = context.config
target_metadata = Base.metadata


def run_migrations_online() -> None:
    engine = make_engine(config.get_main_option("sqlalchemy.url") or "")
    try:
        with engine.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                compare_type=True,
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


run_migrations_online()

"""Database engine and session factory. Owned by M3-01."""

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

SessionFactory = sessionmaker[Session]


def make_engine(url: str) -> Engine:
    raise NotImplementedError

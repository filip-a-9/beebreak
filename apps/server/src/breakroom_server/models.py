"""ORM models for accounts, invites, sign-in challenges and devices. M3-01.

Primary keys are UUIDs generated in Python. Every timestamp is an aware UTC
datetime: `timestamptz` on PostgreSQL, and the `UtcDateTime` decorator keeps
SQLite (which stores naive text) honest. No model reads the wall clock; callers
pass the injected clock's time explicitly.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    MetaData,
    String,
    Uuid,
)
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

MAX_EMAIL_LENGTH = 254


def email_canonical(raw: str) -> str | None:
    """Lower-cased, trimmed address, or None if it cannot be an address.

    Deliberately shallow (no DNS, no RFC 5322 parser): exactly one `@`, a
    non-empty local part, a domain with an inner dot, no whitespace, and at most
    254 characters.
    """
    email = raw.strip().lower()
    if not email or len(email) > MAX_EMAIL_LENGTH:
        return None
    if any(c.isspace() or not c.isprintable() for c in email):
        return None
    if email.count("@") != 1:
        return None
    local, domain = email.split("@")
    if not local or "." not in domain:
        return None
    if domain.startswith(".") or domain.endswith("."):
        return None
    return email


class UtcDateTime(TypeDecorator[datetime]):
    """`timestamptz` that only accepts and only returns aware UTC datetimes."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> Any:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("naive datetime; timestamps must be timezone-aware")
        return value.astimezone(UTC)

    def process_result_value(
        self, value: datetime | None, dialect: Dialect
    ) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class Base(DeclarativeBase):
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(column_0_label)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )
    type_annotation_map = {  # noqa: RUF012
        datetime: UtcDateTime,
        uuid.UUID: Uuid,
        bytes: LargeBinary,
    }


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(Uuid, primary_key=True, default=uuid.uuid4)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'disabled')", name="status"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    email_canonical: Mapped[str] = mapped_column(String(MAX_EMAIL_LENGTH), unique=True)
    nickname: Mapped[str | None] = mapped_column(String(24), default=None)
    nickname_skeleton: Mapped[str | None] = mapped_column(
        String(24), unique=True, default=None
    )
    status: Mapped[str] = mapped_column(String(16), default="active")
    created_at: Mapped[datetime]


class BetaInvite(Base):
    __tablename__ = "beta_invites"

    id: Mapped[uuid.UUID] = _uuid_pk()
    email_canonical: Mapped[str] = mapped_column(String(MAX_EMAIL_LENGTH))
    code_hmac: Mapped[bytes] = mapped_column(LargeBinary(32), unique=True)
    expires_at: Mapped[datetime]
    redeemed_at: Mapped[datetime | None] = mapped_column(default=None)
    redeemed_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id"), default=None
    )
    revoked_at: Mapped[datetime | None] = mapped_column(default=None)
    created_at: Mapped[datetime]


class AuthChallenge(Base):
    __tablename__ = "auth_challenges"
    __table_args__ = (
        CheckConstraint("attempts >= 0", name="attempts_nonneg"),
        Index("ix_auth_challenges_email_consumed", "email_canonical", "consumed_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    email_canonical: Mapped[str] = mapped_column(String(MAX_EMAIL_LENGTH))
    code_hmac: Mapped[bytes] = mapped_column(LargeBinary(32))
    invite_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("beta_invites.id"), default=None
    )
    expires_at: Mapped[datetime]
    attempts: Mapped[int] = mapped_column(default=0)
    consumed_at: Mapped[datetime | None] = mapped_column(default=None)
    created_at: Mapped[datetime]


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(40))
    access_hash: Mapped[bytes | None] = mapped_column(
        LargeBinary(32), unique=True, default=None
    )
    access_expires_at: Mapped[datetime | None] = mapped_column(default=None)
    revoked_at: Mapped[datetime | None] = mapped_column(default=None)
    created_at: Mapped[datetime]
    last_seen_at: Mapped[datetime | None] = mapped_column(default=None)
    connected_at: Mapped[datetime | None] = mapped_column(default=None)


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    token_hash: Mapped[bytes] = mapped_column(LargeBinary(32), primary_key=True)
    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id"), index=True)
    expires_at: Mapped[datetime]
    used_at: Mapped[datetime | None] = mapped_column(default=None)


class UserPreference(Base):
    __tablename__ = "user_preferences"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    consent_version: Mapped[int]
    share_task_category: Mapped[bool] = mapped_column(default=False)
    share_stack_tags: Mapped[bool] = mapped_column(default=False)
    share_time_bucket: Mapped[bool] = mapped_column(default=False)
    consented_at: Mapped[datetime]

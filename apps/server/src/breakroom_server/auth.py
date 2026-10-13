"""Sign-in, token issue/refresh/logout and access-token checks. Owned by M3-02."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy.orm import Session


@dataclass(frozen=True)
class Principal:
    user_id: UUID
    device_id: UUID
    access_expires_at: datetime


def authenticate_access(
    session: Session, token: str, now: datetime
) -> Principal | None:
    """The caller of a valid, unexpired access token on an active device/user."""
    raise NotImplementedError

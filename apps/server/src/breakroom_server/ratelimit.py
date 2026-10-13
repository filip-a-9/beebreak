"""In-memory sliding-window rate limiter (single server instance). Owned by M3-07."""

from collections.abc import Callable
from datetime import datetime


class RateLimiter:
    def __init__(
        self, limit: int, window_s: float, clock: Callable[[], datetime]
    ) -> None:
        raise NotImplementedError

    def hit(self, key: str) -> float | None:
        """None if allowed (and recorded), else seconds until a retry can pass."""
        raise NotImplementedError

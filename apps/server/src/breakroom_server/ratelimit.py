"""In-memory sliding-window rate limiter (single server instance). Owned by M3-07."""

import math
import threading
from collections import deque
from collections.abc import Callable
from datetime import datetime


class RateLimiter:
    def __init__(
        self, limit: int, window_s: float, clock: Callable[[], datetime]
    ) -> None:
        if limit <= 0 or window_s <= 0:
            raise ValueError("limit and window_s must be positive")
        self._limit = limit
        self._window_s = window_s
        self._clock = clock
        self._lock = threading.Lock()
        self._hits: dict[str, deque[float]] = {}

    def hit(self, key: str) -> float | None:
        """None if allowed (and recorded), else seconds until a retry can pass."""
        now = self._clock().timestamp()
        cutoff = now - self._window_s
        with self._lock:
            self._prune(cutoff)
            times = self._hits.get(key)
            if times is not None and len(times) >= self._limit:
                return float(max(1, math.ceil(times[0] + self._window_s - now)))
            if times is None:
                times = self._hits[key] = deque()
            times.append(now)
            return None

    def _prune(self, cutoff: float) -> None:
        """Drop expired hits of every key and forget keys with none left."""
        for key in list(self._hits):
            times = self._hits[key]
            while times and times[0] <= cutoff:
                times.popleft()
            if not times:
                del self._hits[key]

from datetime import UTC, datetime, timedelta

from breakroom_server.ratelimit import RateLimiter


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 13, 12, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def test_blocks_after_limit_with_retry_after() -> None:
    clock = Clock()
    limiter = RateLimiter(3, 60, clock)
    for _ in range(3):
        assert limiter.hit("a") is None
        clock.advance(10)
    # first hit was 30 s ago -> leaves the window in 30 s
    assert limiter.hit("a") == 30


def test_retry_after_rounds_up_and_is_at_least_one() -> None:
    clock = Clock()
    limiter = RateLimiter(1, 60, clock)
    assert limiter.hit("a") is None
    clock.advance(10.2)
    assert limiter.hit("a") == 50
    clock.advance(49.5)
    assert limiter.hit("a") == 1


def test_window_slides() -> None:
    clock = Clock()
    limiter = RateLimiter(2, 60, clock)
    assert limiter.hit("a") is None
    clock.advance(30)
    assert limiter.hit("a") is None
    assert limiter.hit("a") == 30
    clock.advance(30)  # the first hit leaves exactly now
    assert limiter.hit("a") is None
    assert limiter.hit("a") == 30


def test_keys_are_independent() -> None:
    limiter = RateLimiter(1, 60, Clock())
    assert limiter.hit("a") is None
    assert limiter.hit("b") is None
    assert limiter.hit("a") is not None


def test_rejected_hits_are_not_recorded() -> None:
    clock = Clock()
    limiter = RateLimiter(1, 60, clock)
    assert limiter.hit("a") is None
    clock.advance(30)
    for _ in range(5):
        assert limiter.hit("a") == 30
    clock.advance(30)
    assert limiter.hit("a") is None


def test_empty_keys_are_pruned() -> None:
    clock = Clock()
    limiter = RateLimiter(2, 60, clock)
    for i in range(50):
        assert limiter.hit(f"k{i}") is None
    clock.advance(61)
    assert limiter.hit("fresh") is None
    assert set(limiter._hits) == {"fresh"}

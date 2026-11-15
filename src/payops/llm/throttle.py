"""Client-side token-bucket throttle so free-tier limits are respected before the
provider has to reject us with a 429.

Two buckets: requests/minute and tokens/minute. Token cost is estimated before the
call (prompt size) and corrected after it with the real usage, so the bucket can go
into debt and the *next* call waits.
"""

import threading
import time
from collections.abc import Callable

Clock = Callable[[], float]
Sleep = Callable[[float], None]


class TokenBucket:
    """Holds up to ``capacity`` units and refills at ``capacity / 60`` per second."""

    def __init__(
        self, per_minute: int, *, clock: Clock = time.monotonic, sleep: Sleep = time.sleep
    ) -> None:
        if per_minute <= 0:
            raise ValueError("per_minute must be positive")
        self.capacity = float(per_minute)
        self.rate = per_minute / 60.0
        self._level = self.capacity
        self._clock = clock
        self._sleep = sleep
        self._updated = clock()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = self._clock()
        self._level = min(self.capacity, self._level + (now - self._updated) * self.rate)
        self._updated = now

    @property
    def level(self) -> float:
        with self._lock:
            self._refill()
            return self._level

    def wait_time(self, amount: float) -> float:
        """Seconds until ``amount`` units are available (0 if available now)."""
        with self._lock:
            self._refill()
            # A request larger than the bucket can never fit; let it through once full.
            need = min(amount, self.capacity) - self._level
            return max(0.0, need / self.rate)

    def acquire(self, amount: float = 1.0) -> float:
        """Block until ``amount`` units are available, then take them. Returns seconds waited."""
        waited = 0.0
        while (delay := self.wait_time(amount)) > 0:
            self._sleep(delay)
            waited += delay
        with self._lock:
            self._refill()
            self._level -= amount
        return waited

    def adjust(self, delta: float) -> None:
        """Take (positive) or give back (negative) units after the real cost is known."""
        with self._lock:
            self._refill()
            self._level = min(self.capacity, self._level - delta)


class Throttle:
    """Requests/minute and tokens/minute limits. A limit of 0 disables that bucket."""

    def __init__(
        self,
        requests_per_minute: int,
        tokens_per_minute: int,
        *,
        clock: Clock = time.monotonic,
        sleep: Sleep = time.sleep,
    ) -> None:
        self.requests = (
            TokenBucket(requests_per_minute, clock=clock, sleep=sleep)
            if requests_per_minute > 0
            else None
        )
        self.tokens = (
            TokenBucket(tokens_per_minute, clock=clock, sleep=sleep)
            if tokens_per_minute > 0
            else None
        )

    def acquire(self, estimated_tokens: int) -> float:
        waited = 0.0
        if self.requests:
            waited += self.requests.acquire(1)
        if self.tokens:
            waited += self.tokens.acquire(estimated_tokens)
        return waited

    def record_usage(self, estimated_tokens: int, actual_tokens: int) -> None:
        if self.tokens and actual_tokens:
            self.tokens.adjust(actual_tokens - estimated_tokens)


def estimate_tokens(text: str) -> int:
    """Rough prompt size: ~4 characters per token for English text and JSON."""
    return max(1, len(text) // 4)

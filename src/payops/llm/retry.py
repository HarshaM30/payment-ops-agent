"""Retries with exponential backoff and full jitter for transient LLM failures.

Retried: 429 (rate limited), 5xx, timeouts, and connection errors. ``Retry-After``
is honored when the provider sends it. Everything else (bad request, auth) fails
immediately: retrying won't fix it.
"""

import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Any

from payops import clock

RETRYABLE_STATUS = frozenset({408, 429})  # plus every 5xx
_RETRYABLE_NAMES = frozenset(
    {
        "Timeout",
        "TimeoutError",
        "APIConnectionError",
        "RateLimitError",
        "InternalServerError",
        "ServiceUnavailableError",
        "BadGatewayError",
        "ConnectionError",
    }
)


class LLMError(Exception):
    """Base class for LLM-layer failures the agent handles cleanly."""


class LLMUnavailableError(LLMError):
    """The provider kept failing after every retry, or failed in a non-retryable way."""

    def __init__(self, message: str, *, attempts: int, last_error: BaseException | None) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.last_error = last_error


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = 5
    base_seconds: float = 1.0
    max_seconds: float = 60.0

    def backoff(self, attempt: int, rng: random.Random) -> float:
        """Full jitter: uniform in [0, min(max, base * 2^attempt)]."""
        ceiling = min(self.max_seconds, self.base_seconds * (2**attempt))
        return rng.uniform(0, ceiling)


def status_code(exc: BaseException) -> int | None:
    for attr in ("status_code", "status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    value = getattr(response, "status_code", None)
    return value if isinstance(value, int) else None


def is_retryable(exc: BaseException) -> bool:
    code = status_code(exc)
    if code is not None:
        return code in RETRYABLE_STATUS or code >= 500
    return isinstance(exc, TimeoutError | ConnectionError) or type(exc).__name__ in _RETRYABLE_NAMES


def _headers(exc: BaseException) -> dict[str, Any]:
    headers = getattr(exc, "headers", None)
    if headers is None:
        headers = getattr(getattr(exc, "response", None), "headers", None)
    try:
        return {str(k).lower(): v for k, v in dict(headers or {}).items()}
    except (TypeError, ValueError):
        return {}


def retry_after_seconds(exc: BaseException) -> float | None:
    """``Retry-After`` as seconds: an explicit attribute, or the header (delta or HTTP date)."""
    explicit = getattr(exc, "retry_after", None)
    if isinstance(explicit, int | float) and explicit >= 0:
        return float(explicit)
    value = _headers(exc).get("retry-after")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        pass
    try:
        when = parsedate_to_datetime(str(value))
    except (TypeError, ValueError):
        return None
    return max(0.0, (when - clock.now()).total_seconds())


@dataclass
class RetryStats:
    attempts: int = 0
    waits: list[float] = field(default_factory=list)


def call_with_retry[T](
    fn: Callable[[], T],
    policy: RetryPolicy,
    *,
    sleep: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
    stats: RetryStats | None = None,
) -> T:
    rng = rng or random.Random()
    stats = stats if stats is not None else RetryStats()
    last: BaseException | None = None
    for attempt in range(policy.max_retries + 1):
        stats.attempts = attempt + 1
        try:
            return fn()
        except Exception as exc:
            last = exc
            if not is_retryable(exc):
                raise LLMUnavailableError(
                    f"LLM call failed (not retryable): {type(exc).__name__}: {exc}",
                    attempts=stats.attempts,
                    last_error=exc,
                ) from exc
            if attempt == policy.max_retries:
                break
            hinted = retry_after_seconds(exc)
            delay = (
                min(hinted, policy.max_seconds)
                if hinted is not None
                else policy.backoff(attempt, rng)
            )
            stats.waits.append(delay)
            sleep(delay)
    raise LLMUnavailableError(
        f"LLM call failed after {stats.attempts} attempts: {type(last).__name__}: {last}",
        attempts=stats.attempts,
        last_error=last,
    ) from last

import random
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import pytest

from payops.llm import retry
from payops.llm.retry import (
    LLMUnavailableError,
    RetryPolicy,
    RetryStats,
    call_with_retry,
    is_retryable,
    retry_after_seconds,
)
from payops.llm.throttle import Throttle, TokenBucket, estimate_tokens


class HTTPError(Exception):
    def __init__(self, status: int, headers: dict | None = None) -> None:
        super().__init__(f"HTTP {status}")
        self.status_code = status
        self.headers = headers or {}


class Flaky:
    """Fails with the given errors in order, then returns 'ok'."""

    def __init__(self, *errors: Exception) -> None:
        self.errors = list(errors)
        self.calls = 0

    def __call__(self) -> str:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return "ok"


POLICY = RetryPolicy(max_retries=3, base_seconds=1.0, max_seconds=8.0)


def test_429_backoff_then_success() -> None:
    sleeps: list[float] = []
    fn = Flaky(HTTPError(429), HTTPError(429))
    stats = RetryStats()
    result = call_with_retry(fn, POLICY, sleep=sleeps.append, rng=random.Random(1), stats=stats)
    assert result == "ok"
    assert fn.calls == 3
    assert stats.attempts == 3
    assert len(sleeps) == 2
    assert 0 <= sleeps[0] <= 1.0  # full jitter within base * 2^0
    assert 0 <= sleeps[1] <= 2.0  # ... and base * 2^1


def test_429_exhausted_fails_cleanly() -> None:
    sleeps: list[float] = []
    fn = Flaky(*[HTTPError(429) for _ in range(10)])
    with pytest.raises(LLMUnavailableError) as info:
        call_with_retry(fn, POLICY, sleep=sleeps.append, rng=random.Random(1))
    assert fn.calls == 4  # 1 try + 3 retries
    assert len(sleeps) == 3
    assert info.value.attempts == 4
    assert isinstance(info.value.last_error, HTTPError)
    assert "after 4 attempts" in str(info.value)


def test_retry_after_header_is_honored() -> None:
    sleeps: list[float] = []
    fn = Flaky(HTTPError(429, {"Retry-After": "7"}))
    call_with_retry(fn, POLICY, sleep=sleeps.append)
    assert sleeps == [7.0]


def test_retry_after_is_capped_by_max_backoff() -> None:
    sleeps: list[float] = []
    call_with_retry(Flaky(HTTPError(429, {"retry-after": "3600"})), POLICY, sleep=sleeps.append)
    assert sleeps == [8.0]


def test_retry_after_http_date(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(retry.clock, "now", lambda: now)
    exc = HTTPError(503, {"Retry-After": format_datetime(now + timedelta(seconds=5), usegmt=True)})
    assert retry_after_seconds(exc) == pytest.approx(5.0)


def test_retry_after_attribute_and_garbage() -> None:
    exc = HTTPError(429)
    exc.retry_after = 2  # type: ignore[attr-defined]
    assert retry_after_seconds(exc) == 2.0
    assert retry_after_seconds(HTTPError(429, {"Retry-After": "soon"})) is None
    assert retry_after_seconds(HTTPError(429)) is None


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (HTTPError(429), True),
        (HTTPError(500), True),
        (HTTPError(503), True),
        (HTTPError(408), True),
        (HTTPError(400), False),
        (HTTPError(401), False),
        (HTTPError(404), False),
        (TimeoutError("slow"), True),
        (ConnectionError("reset"), True),
        (type("Timeout", (Exception,), {})("litellm timeout"), True),
        (ValueError("bad"), False),
    ],
)
def test_is_retryable(exc: Exception, expected: bool) -> None:
    assert is_retryable(exc) is expected


def test_non_retryable_fails_immediately() -> None:
    sleeps: list[float] = []
    fn = Flaky(HTTPError(401))
    with pytest.raises(LLMUnavailableError, match="not retryable"):
        call_with_retry(fn, POLICY, sleep=sleeps.append)
    assert fn.calls == 1
    assert sleeps == []


def test_timeouts_and_5xx_are_retried() -> None:
    fn = Flaky(TimeoutError(), HTTPError(502))
    assert call_with_retry(fn, POLICY, sleep=lambda _: None) == "ok"
    assert fn.calls == 3


def test_backoff_is_capped() -> None:
    policy = RetryPolicy(max_retries=10, base_seconds=1, max_seconds=5)
    rng = random.Random(0)
    assert all(policy.backoff(attempt, rng) <= 5 for attempt in range(10))


# ------------------------------------------------------------------ throttle


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


def test_bucket_allows_burst_then_waits() -> None:
    clk = FakeClock()
    bucket = TokenBucket(60, clock=clk, sleep=clk.sleep)  # 1 per second
    for _ in range(60):
        assert bucket.acquire() == 0
    waited = bucket.acquire()
    assert waited == pytest.approx(1.0)
    assert clk.t == pytest.approx(1.0)


def test_bucket_refills_over_time() -> None:
    clk = FakeClock()
    bucket = TokenBucket(60, clock=clk, sleep=clk.sleep)
    bucket.acquire(60)
    clk.t += 30
    assert bucket.level == pytest.approx(30)
    clk.t += 1000
    assert bucket.level == pytest.approx(60)  # never above capacity


def test_oversized_request_waits_for_full_bucket_only() -> None:
    clk = FakeClock()
    bucket = TokenBucket(100, clock=clk, sleep=clk.sleep)
    bucket.acquire(100)
    assert bucket.acquire(500) == pytest.approx(60.0)


def test_throttle_tracks_requests_and_tokens() -> None:
    clk = FakeClock()
    throttle = Throttle(2, 1000, clock=clk, sleep=clk.sleep)
    assert throttle.acquire(100) == 0
    assert throttle.acquire(100) == 0
    assert throttle.acquire(100) == pytest.approx(30.0)  # third request in a minute waits


def test_throttle_corrects_with_actual_usage() -> None:
    clk = FakeClock()
    throttle = Throttle(0, 600, clock=clk, sleep=clk.sleep)  # 10 tokens/s, no request limit
    assert throttle.requests is None
    throttle.acquire(100)
    throttle.record_usage(estimated_tokens=100, actual_tokens=600)  # real call was bigger
    assert throttle.tokens is not None
    assert throttle.tokens.level == pytest.approx(0)
    assert throttle.acquire(100) == pytest.approx(10.0)


def test_throttle_disabled() -> None:
    throttle = Throttle(0, 0)
    assert throttle.acquire(10**9) == 0
    throttle.record_usage(1, 10**9)


def test_bucket_rejects_zero_rate() -> None:
    with pytest.raises(ValueError, match="positive"):
        TokenBucket(0)


def test_estimate_tokens() -> None:
    assert estimate_tokens("") == 1
    assert estimate_tokens("x" * 400) == 100

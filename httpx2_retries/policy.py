# !/usr/bin/python3
# @Author   : Mr.Nobody
# @Software : PyCharm
# @Time     : 2026/09/17 18:23:50


from __future__ import annotations

import inspect
import math
import random
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import ClassVar, Self, TypedDict, Unpack, cast

import anyio
import httpx2

Validator = Callable[[httpx2.Response], None | Awaitable[None]]


class RetryChanges(TypedDict, total=False):
    total: int
    backoff_factor: float
    max_backoff_wait: float
    backoff_jitter: float
    respect_retry_after_header: bool
    allowed_methods: Iterable[str] | None
    status_forcelist: Iterable[int] | None
    retry_on_exceptions: Iterable[type[Exception]] | None
    total_timeout: float | None
    max_total_sleep: float | None
    max_elapsed_time: float | None
    validate_response: Validator | None
    attempts_made: int
    elapsed_sleep: float


@dataclass(frozen=True)
class RetryState:
    """Per-request snapshot. attempts_made excludes the initial request."""

    attempts_made: int = 0
    requests_made: int = 0
    elapsed_sleep: float = 0.0
    elapsed_time: float = 0.0
    stop_reason: str | None = None
    last_status_code: int | None = None
    last_exception: str | None = None


class RetryDeadlineExceeded(httpx2.TimeoutException):
    """The overall retry deadline expired. This error is never retried."""


def _number(name: str, value: float, *, positive: bool = False) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or (value <= 0 if positive else value < 0)
    ):
        qualifier = "positive" if positive else "non-negative"
        raise ValueError(f"{name} must be finite and {qualifier}")


@dataclass(frozen=True)
class Retry:
    """Retry policy. total excludes the initial attempt.

    None collections select defaults; empty collections disable conditions.
    Runtime counters never mutate shared policies. Counter fields and
    increment() provide immutable compatibility snapshots for manual loops.
    """

    RETRYABLE_METHODS: ClassVar[frozenset[str]] = frozenset(
        {"HEAD", "GET", "PUT", "DELETE", "OPTIONS", "TRACE"}
    )
    RETRYABLE_STATUS_CODES: ClassVar[frozenset[int]] = frozenset(
        {429, 500, 502, 503, 504}
    )
    RETRYABLE_EXCEPTIONS: ClassVar[tuple[type[Exception], ...]] = (
        httpx2.TimeoutException,
        httpx2.NetworkError,
        httpx2.ProxyError,
        httpx2.RemoteProtocolError,
    )

    # Preserve this project's positional order; migration should use keywords.
    total: int = 5
    backoff_factor: float = 1.0
    max_backoff_wait: float = 60.0
    backoff_jitter: float = 1.0
    respect_retry_after_header: bool = True
    allowed_methods: Iterable[str] | None = None
    status_forcelist: Iterable[int] | None = None
    retry_on_exceptions: Iterable[type[Exception]] | None = None
    total_timeout: float | None = None
    max_total_sleep: float | None = None
    max_elapsed_time: float | None = None
    validate_response: Validator | None = None
    attempts_made: int = 0
    elapsed_sleep: float = 0.0

    def __post_init__(self) -> None:
        for name in ("total", "attempts_made"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        for name in (
            "backoff_factor",
            "max_backoff_wait",
            "backoff_jitter",
            "elapsed_sleep",
        ):
            _number(name, getattr(self, name))
        for name in ("total_timeout", "max_total_sleep", "max_elapsed_time"):
            value = getattr(self, name)
            if value is not None:
                _number(name, value, positive=True)
        if self.backoff_jitter > 1:
            raise ValueError("backoff_jitter must be between 0 and 1")
        if type(self.respect_retry_after_header) is not bool:
            raise TypeError("respect_retry_after_header must be a bool")
        if (
            self.total_timeout is not None
            and self.max_total_sleep is not None
            and self.total_timeout != self.max_total_sleep
        ):
            raise ValueError("total_timeout and max_total_sleep are aliases; use one")
        if self.validate_response is not None and not callable(self.validate_response):
            raise TypeError("validate_response must be callable")
        methods = (
            self.RETRYABLE_METHODS
            if self.allowed_methods is None
            else self.allowed_methods
        )
        if isinstance(methods, (str, bytes)):
            raise TypeError("allowed_methods must be a collection, e.g. {'GET'}")
        methods = tuple(methods)
        token_chars = "!#$%&'*+-.^_`|~0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
        if any(
            not isinstance(m, str) or not m or any(c not in token_chars for c in m)
            for m in methods
        ):
            raise ValueError("allowed_methods must contain HTTP method tokens")
        statuses = frozenset(
            self.RETRYABLE_STATUS_CODES
            if self.status_forcelist is None
            else self.status_forcelist
        )
        errors = tuple(
            self.RETRYABLE_EXCEPTIONS
            if self.retry_on_exceptions is None
            else self.retry_on_exceptions
        )
        if any(
            isinstance(s, bool) or not isinstance(s, int) or not 100 <= s <= 599
            for s in statuses
        ):
            raise ValueError("status_forcelist must contain HTTP status codes")
        if any(not isinstance(e, type) or not issubclass(e, Exception) for e in errors):
            raise ValueError("retry_on_exceptions must contain Exception subclasses")
        object.__setattr__(
            self, "allowed_methods", frozenset(m.upper() for m in methods)
        )
        object.__setattr__(self, "status_forcelist", statuses)
        object.__setattr__(self, "retry_on_exceptions", errors)

    @property
    def retryable_exceptions(self) -> tuple[type[Exception], ...]:
        return cast(tuple[type[Exception], ...], self.retry_on_exceptions)

    @property
    def sleep_budget(self) -> float | None:
        return (
            self.max_total_sleep
            if self.max_total_sleep is not None
            else self.total_timeout
        )

    def copy_with(self, **changes: Unpack[RetryChanges]) -> Self:
        """Return a new policy, preserving subclasses and normalizing aliases."""
        if "max_total_sleep" in changes and "total_timeout" not in changes:
            changes["total_timeout"] = None
        elif "total_timeout" in changes and "max_total_sleep" not in changes:
            changes["max_total_sleep"] = None
        return replace(self, **changes)

    def increment(self, *, elapsed_sleep: float = 0.0) -> Self:
        """Return a new snapshot; elapsed_sleep is additional completed wait."""
        _number("elapsed_sleep", elapsed_sleep)
        return self.copy_with(
            attempts_made=self.attempts_made + 1,
            elapsed_sleep=self.elapsed_sleep + elapsed_sleep,
        )

    def is_retryable_method(self, method: str) -> bool:
        return method.upper() in cast(frozenset[str], self.allowed_methods)

    def is_retryable_status_code(self, status_code: int) -> bool:
        return status_code in cast(frozenset[int], self.status_forcelist)

    def is_retryable_exception(self, exception: Exception) -> bool:
        return not isinstance(exception, RetryDeadlineExceeded) and isinstance(
            exception, self.retryable_exceptions
        )

    def is_exhausted(self) -> bool:
        return self.attempts_made >= self.total or (
            self.sleep_budget is not None and self.elapsed_sleep >= self.sleep_budget
        )

    def is_retry(
        self, method: str, status_code: int, has_retry_after: bool = False
    ) -> bool:
        """Retry-After changes timing, not eligibility; status must be allowed."""
        return (
            not self.is_exhausted()
            and self.is_retryable_method(method)
            and self.is_retryable_status_code(status_code)
        )

    def parse_retry_after(self, retry_after: str) -> float:
        try:
            value = retry_after.strip()
            if value.isascii() and value.isdigit():
                return float(value)
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=UTC)
            return max(0.0, (date - datetime.now(UTC)).total_seconds())
        except (ValueError, TypeError, OverflowError, AttributeError) as exc:
            raise ValueError("Invalid Retry-After header") from exc

    def backoff_strategy(self) -> float:
        """Override for custom backoff; attempts_made starts at 1."""
        if self.backoff_factor == 0 or self.attempts_made == 0:
            return 0.0
        try:
            ceiling = min(
                self.max_backoff_wait,
                math.ldexp(self.backoff_factor, self.attempts_made),
            )
        except OverflowError:
            ceiling = self.max_backoff_wait
        return ceiling * random.uniform(1.0 - self.backoff_jitter, 1.0)

    def delay(
        self, retry_number: int, headers: Mapping[str, str] | None = None
    ) -> float | None:
        """None means Retry-After exceeds the cap; never shorten server waits."""
        if type(retry_number) is not int or retry_number < 1:
            raise ValueError("retry_number must be a positive integer")
        value = (
            httpx2.Headers(headers).get("Retry-After") if headers is not None else None
        )
        if self.respect_retry_after_header and value:
            try:
                seconds = self.parse_retry_after(value)
            except ValueError:
                pass
            else:
                return seconds if seconds <= self.max_backoff_wait else None
        delay = self.copy_with(attempts_made=retry_number).backoff_strategy()
        _number("backoff_strategy result", delay)
        return min(delay, self.max_backoff_wait)

    def sleep(
        self, response: httpx2.Response | Exception, *, delay: float | None = None
    ) -> float | None:
        """Wait and return scheduled seconds, without mutating this policy."""
        if delay is None:
            headers = None if isinstance(response, Exception) else response.headers
            delay = self.delay(
                max(1, self.attempts_made),
                headers,
            )
        if delay is not None:
            _number("delay", delay)
            if self.sleep_budget is not None and (
                self.elapsed_sleep >= self.sleep_budget
                or self.elapsed_sleep + delay > self.sleep_budget
            ):
                return None
            time.sleep(delay)
        return delay

    async def asleep(
        self, response: httpx2.Response | Exception, *, delay: float | None = None
    ) -> float | None:
        """Asyncio/Trio counterpart to sleep()."""
        if delay is None:
            delay = self.delay(
                max(1, self.attempts_made),
                response.headers if isinstance(response, httpx2.Response) else None,
            )
        if delay is not None:
            _number("delay", delay)
            if self.sleep_budget is not None and (
                self.elapsed_sleep >= self.sleep_budget
                or self.elapsed_sleep + delay > self.sleep_budget
            ):
                return None
            await anyio.sleep(delay)
        return delay


def is_async_callable(callback: Validator) -> bool:
    call_method = getattr(callback, "__call__", None)  # noqa: B004 - inspect whether the callable method is async.
    return inspect.iscoroutinefunction(callback) or inspect.iscoroutinefunction(
        call_method
    )

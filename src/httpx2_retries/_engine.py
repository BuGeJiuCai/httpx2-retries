from __future__ import annotations

import inspect
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any

import anyio
import httpx2

from .policy import Retry, RetryDeadlineExceeded, RetryState, _number, is_async_callable

logger = logging.getLogger("httpx2_retries.transport")
_MISSING = object()


def resolve_policy(request: httpx2.Request, default: Retry) -> Retry:
    policy = request.extensions.get("retry", default)
    if policy is False:
        return default.copy_with(total=0)
    if not isinstance(policy, Retry):
        raise TypeError(
            "request.extensions['retry'] must be httpx2_retries.Retry or False"
        )
    return policy


class _Run:
    def __init__(self, request: httpx2.Request, policy: Retry) -> None:
        self.request, self.policy = request, policy
        self.started = time.monotonic()
        self.replayable = isinstance(request.stream, httpx2.ByteStream)
        self.state = RetryState(
            attempts_made=policy.attempts_made, elapsed_sleep=policy.elapsed_sleep
        )
        self.original_timeout = request.extensions.get("timeout", _MISSING)
        self.publish()

    def publish(self, **changes: Any) -> RetryState:
        self.state = replace(
            self.state, elapsed_time=time.monotonic() - self.started, **changes
        )
        self.request.extensions["retry_state"] = self.state
        return self.state

    def remaining(self) -> float | None:
        if self.policy.max_elapsed_time is None:
            return None
        remaining = self.policy.max_elapsed_time - (time.monotonic() - self.started)
        if remaining <= 0:
            raise RetryDeadlineExceeded("Retry deadline exceeded", request=self.request)
        return remaining

    def begin(self) -> None:
        remaining = self.remaining()
        if remaining is not None:
            original = (
                self.original_timeout if isinstance(self.original_timeout, dict) else {}
            )
            self.request.extensions["timeout"] = {
                phase: min(value, remaining) if value is not None else remaining
                for phase in ("connect", "read", "write", "pool")
                for value in (original.get(phase),)
            }
        self.publish(
            requests_made=self.state.requests_made + 1,
            last_status_code=None,
            last_exception=None,
        )

    def restore(self) -> None:
        if self.original_timeout is _MISSING:
            self.request.extensions.pop("timeout", None)
        else:
            self.request.extensions["timeout"] = self.original_timeout

    def next_delay(
        self, response: httpx2.Response | None, error: Exception | None = None
    ) -> float | None:
        policy = self.policy
        reason = None
        if error is not None and not policy.is_retryable_exception(error):
            reason = "exception_not_retryable"
        elif (
            error is None
            and response is not None
            and not policy.is_retryable_status_code(response.status_code)
        ):
            reason = "success"
        elif not policy.is_retryable_method(self.request.method):
            reason = "method_not_allowed"
        elif not self.replayable:
            reason = "body_not_replayable"
        elif self.state.attempts_made >= policy.total:
            reason = "retries_exhausted"
        if reason is not None:
            self.publish(stop_reason=reason)
            return None
        delay = policy.delay(
            self.state.attempts_made + 1,
            response.headers if response is not None else None,
        )
        if delay is None:
            self.publish(stop_reason="retry_after_exceeds_limit")
            return None
        _number("delay", delay)
        if policy.sleep_budget is not None and (
            self.state.elapsed_sleep >= policy.sleep_budget
            or self.state.elapsed_sleep + delay > policy.sleep_budget
        ):
            self.publish(stop_reason="sleep_budget_exhausted")
            return None
        remaining = self.remaining()
        if remaining is not None and delay >= remaining:
            raise RetryDeadlineExceeded(
                "Next retry would exceed the deadline", request=self.request
            )
        logger.debug(
            "HTTP retry method=%s retry=%s delay=%.3f status=%s error=%s",
            self.request.method,
            self.state.attempts_made + 1,
            delay,
            self.state.last_status_code,
            self.state.last_exception,
        )
        return delay

    def slept(self, delay: float, started: float) -> None:
        self.publish(
            elapsed_sleep=self.state.elapsed_sleep
            + max(delay, time.monotonic() - started)
        )
        self.remaining()
        self.publish(attempts_made=self.state.attempts_made + 1)

    def result(self, response: httpx2.Response) -> httpx2.Response:
        state = self.publish()
        response.request.extensions["retry_state"] = state
        response.extensions.update(
            retry_attempts=state.attempts_made,
            retry_state=state,
            retry=self.policy.copy_with(
                attempts_made=state.attempts_made, elapsed_sleep=state.elapsed_sleep
            ),
        )
        return response

    def failed(self, error: BaseException) -> None:
        reason = self.state.stop_reason
        if isinstance(error, RetryDeadlineExceeded):
            reason = "deadline_exceeded"
        elif not isinstance(error, Exception):
            reason = "cancelled"
        elif reason is None:
            reason = "internal_error"
        state = self.publish(stop_reason=reason, last_exception=type(error).__name__)
        # Preserve the original exception identity/type. The request extension
        # remains usable even for exceptions that reject custom attributes.
        try:
            error.retry_state = state  # type: ignore[attr-defined]
        except (AttributeError, TypeError):
            pass


def run_sync(
    request: httpx2.Request,
    send: Callable[[httpx2.Request], httpx2.Response],
    policy: Retry,
) -> httpx2.Response:
    if policy.validate_response is not None and is_async_callable(
        policy.validate_response
    ):
        raise TypeError("An async validate_response requires an async client")
    run = _Run(request, policy)
    response = None
    try:
        while True:
            run.begin()
            outcome: httpx2.Response | Exception
            try:
                response = send(request)
                try:
                    _ = response.request
                except RuntimeError:
                    response.request = request
                run.publish(last_status_code=response.status_code)
                run.remaining()
                if (
                    policy.validate_response is not None
                    and not policy.is_retryable_status_code(response.status_code)
                ):
                    result = policy.validate_response(response)
                    if inspect.isawaitable(result):
                        if inspect.iscoroutine(result):
                            result.close()
                        raise TypeError(
                            "An awaitable validate_response requires an async client"
                        )
                run.remaining()
            except Exception as error:
                run.publish(last_exception=type(error).__name__)
                if isinstance(error, RetryDeadlineExceeded):
                    raise
                run.remaining()
                delay = run.next_delay(response, error)
                if delay is None:
                    raise
                outcome = error
            else:
                delay = run.next_delay(response)
                if delay is None:
                    final, response = response, None
                    return run.result(final)
                outcome = response
            if response is not None:
                response.close()
                response = None
            started = time.monotonic()
            policy.sleep(outcome, delay=delay)
            run.slept(delay, started)
    except BaseException as error:
        run.failed(error)
        raise
    finally:
        run.restore()
        if response is not None:
            response.close()


async def run_async(
    request: httpx2.Request,
    send: Callable[[httpx2.Request], Awaitable[httpx2.Response]],
    policy: Retry,
) -> httpx2.Response:
    run = _Run(request, policy)
    response: httpx2.Response | None = None

    async def loop() -> httpx2.Response:
        nonlocal response
        while True:
            run.begin()
            outcome: httpx2.Response | Exception
            try:
                current_response = await send(request)
                response = current_response
                try:
                    _ = current_response.request
                except RuntimeError:
                    current_response.request = request

                run.publish(last_status_code=current_response.status_code)
                run.remaining()
                if (
                    policy.validate_response is not None
                    and not policy.is_retryable_status_code(
                        current_response.status_code
                    )
                ):
                    result = policy.validate_response(current_response)
                    if inspect.isawaitable(result):
                        await result
                run.remaining()
            except Exception as error:
                run.publish(last_exception=type(error).__name__)
                if isinstance(error, RetryDeadlineExceeded):
                    raise
                run.remaining()
                delay = run.next_delay(response, error)
                if delay is None:
                    raise
                outcome = error
            else:
                delay = run.next_delay(response)
                if delay is None:
                    final, response = response, None
                    return run.result(final)
                outcome = response
            if response is not None:
                await response.aclose()
                response = None
            started = time.monotonic()
            await policy.asleep(outcome, delay=delay)
            run.slept(delay, started)

    try:
        if policy.max_elapsed_time is None:
            return await loop()
        scope = None
        try:
            with anyio.fail_after(run.remaining()) as scope:
                return await loop()
        except TimeoutError as error:
            if scope is not None and scope.cancel_called:
                raise RetryDeadlineExceeded(
                    "Retry deadline exceeded", request=request
                ) from error
            raise
    except BaseException as error:
        run.failed(error)
        raise
    finally:
        run.restore()
        if response is not None:
            with anyio.CancelScope(shield=True):
                await response.aclose()

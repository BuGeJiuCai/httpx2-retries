# !/usr/bin/python3
# @Author   : Mr.Nobody
# @Software : PyCharm
# @Time     : 2026/09/17 18:23:58

from __future__ import annotations

import ssl
from collections.abc import Iterable
from types import TracebackType
from typing import Self, TypedDict, Unpack

import httpx2

from ._engine import resolve_policy, run_async, run_sync
from .policy import Retry

SocketOption = (
    tuple[int, int, int]
    | tuple[int, int, bytes | bytearray]
    | tuple[int, int, None, int]
)


class TransportOptions(TypedDict, total=False):
    verify: ssl.SSLContext | str | bool
    cert: str | tuple[str, str] | tuple[str, str, str] | None
    trust_env: bool
    http1: bool
    http2: bool
    limits: httpx2.Limits
    proxy: httpx2.Proxy | httpx2.URL | str | None
    uds: str | None
    local_address: str | None
    retries: int
    socket_options: Iterable[SocketOption] | None


def has_retries(transport: object, seen: set[int] | None = None) -> bool:
    """Inspect known HTTPX2 pools and conventional transparent wrappers."""
    if isinstance(transport, RetryTransport):
        return True
    seen = set() if seen is None else seen
    if id(transport) in seen:
        return False
    seen.add(id(transport))
    if isinstance(transport, (httpx2.HTTPTransport, httpx2.AsyncHTTPTransport)):
        # HTTPX2 does not expose a public getter for connection retries.
        return bool(getattr(getattr(transport, "_pool", None), "_retries", 0))
    inner = getattr(transport, "transport", None)
    return inner is not None and has_retries(inner, seen)


class RetryTransport(httpx2.BaseTransport, httpx2.AsyncBaseTransport):
    """Sync/async wrapper. Owns the wrapped transport; creates pools lazily.

    A supplied sync-only/async-only transport can only serve that mode.
    Use separate instances for clients with independent lifetimes.
    """

    def __init__(
        self,
        transport: httpx2.BaseTransport | httpx2.AsyncBaseTransport | None = None,
        retry: Retry | None = None,
        *,
        allow_nested_retries: bool = False,
        **transport_options: Unpack[TransportOptions],
    ) -> None:
        if transport is not None and transport_options:
            raise ValueError("pass either transport or transport options, not both")
        if transport is not None and not isinstance(
            transport, (httpx2.BaseTransport, httpx2.AsyncBaseTransport)
        ):
            raise TypeError("transport must be an HTTPX2 transport")
        if retry is not None and not isinstance(retry, Retry):
            raise TypeError("retry must be httpx2_retries.Retry")
        if not allow_nested_retries and (
            has_retries(transport) or transport_options.get("retries", 0)
        ):
            raise ValueError(
                "Nested retries are disabled; set underlying retries=0 or explicitly allow_nested_retries=True"
            )
        self.retry = retry if retry is not None else Retry()
        self._provided = transport is not None
        self._options = transport_options
        self._sync_transport = (
            transport if isinstance(transport, httpx2.BaseTransport) else None
        )
        self._async_transport = (
            transport if isinstance(transport, httpx2.AsyncBaseTransport) else None
        )

    def _sync(self) -> httpx2.BaseTransport:
        if self._sync_transport is None:
            if self._provided:
                raise TypeError(
                    "A synchronous client requires a synchronous inner transport"
                )
            self._sync_transport = httpx2.HTTPTransport(**self._options)
        return self._sync_transport

    def _async(self) -> httpx2.AsyncBaseTransport:
        if self._async_transport is None:
            if self._provided:
                raise TypeError(
                    "An asynchronous client requires an asynchronous inner transport"
                )
            self._async_transport = httpx2.AsyncHTTPTransport(**self._options)
        return self._async_transport

    @property
    def transport(self) -> httpx2.BaseTransport | httpx2.AsyncBaseTransport:
        """The supplied/initialized transport, defaulting to the sync pool."""
        if self._sync_transport is not None:
            return self._sync_transport
        if self._async_transport is not None:
            return self._async_transport
        return self._sync()

    def handle_request(self, request: httpx2.Request) -> httpx2.Response:
        return run_sync(
            request, self._sync().handle_request, resolve_policy(request, self.retry)
        )

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        return await run_async(
            request,
            self._async().handle_async_request,
            resolve_policy(request, self.retry),
        )

    def __enter__(self) -> Self:
        self._sync().__enter__()
        return self

    def close(self) -> None:
        if self._sync_transport is not None:
            self._sync_transport.close()

    def __exit__(
        self,
        exc_type: type[BaseException] | None = None,
        exc_value: BaseException | None = None,
        traceback: TracebackType | None = None,
    ) -> None:
        if self._sync_transport is not None:
            self._sync_transport.__exit__(exc_type, exc_value, traceback)

    async def __aenter__(self) -> Self:
        await self._async().__aenter__()
        return self

    async def aclose(self) -> None:
        if self._async_transport is not None:
            await self._async_transport.aclose()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None = None,
        exc_value: BaseException | None = None,
        traceback: TracebackType | None = None,
    ) -> None:
        if self._async_transport is not None:
            await self._async_transport.__aexit__(exc_type, exc_value, traceback)


class AsyncRetryTransport(RetryTransport):
    """Explicit async entry point retained for existing users."""

    def __init__(
        self,
        transport: httpx2.AsyncBaseTransport | None = None,
        retry: Retry | None = None,
        *,
        allow_nested_retries: bool = False,
        **transport_options: Unpack[TransportOptions],
    ) -> None:
        if transport is not None and not isinstance(
            transport, httpx2.AsyncBaseTransport
        ):
            raise TypeError("transport must be an HTTPX2 asynchronous transport")
        super().__init__(
            transport,
            retry,
            allow_nested_retries=allow_nested_retries,
            **transport_options,
        )

    @property
    def transport(self) -> httpx2.AsyncBaseTransport:
        return self._async()

    def _sync(self) -> httpx2.BaseTransport:
        raise TypeError("AsyncRetryTransport requires an asynchronous client")

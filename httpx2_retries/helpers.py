# !/usr/bin/python3
# @Author   : Mr.Nobody
# @Software : PyCharm
# @Time     : 2026/09/17 18:23:44

from __future__ import annotations

from collections.abc import AsyncIterable, Callable, Iterable, Mapping, Sequence
from typing import Any, Literal, TypedDict, Unpack

import httpx2

from ._engine import resolve_policy, run_async, run_sync
from .policy import Retry
from .transport import has_retries


class RequestOptions(TypedDict, total=False):
    content: str | bytes | Iterable[bytes] | AsyncIterable[bytes] | None
    data: Mapping[str, Any] | None
    files: Mapping[str, Any] | Sequence[tuple[str, Any]] | None
    json: Any
    params: (
        httpx2.QueryParams
        | Mapping[str, Any]
        | Sequence[tuple[str, Any]]
        | str
        | bytes
        | None
    )
    headers: httpx2.Headers | Mapping[str, str] | Sequence[tuple[str, str]] | None
    cookies: httpx2.Cookies | Mapping[str, str] | None
    timeout: (
        httpx2.Timeout
        | float
        | tuple[float | None, float | None, float | None, float | None]
        | None
    )
    extensions: Mapping[str, Any] | None
    auth: (
        httpx2.Auth
        | tuple[str, str]
        | Callable[[httpx2.Request], httpx2.Request]
        | None
    )
    follow_redirects: bool
    stream: Literal[False]


def _prepare(
    client: httpx2.Client | httpx2.AsyncClient,
    method: str,
    url: httpx2.URL | str,
    retry: Retry | None,
    options: dict[str, Any],
) -> tuple[httpx2.Request, Retry, dict[str, Any]]:
    if retry is not None and not isinstance(retry, Retry):
        raise TypeError("retry must be httpx2_retries.Retry")
    if options.pop("stream", False):
        raise ValueError(
            "Retry helpers buffer responses; use client.stream with RetryTransport for streaming"
        )
    # HTTPX2 exposes no public transport inventory. Keep private inspection in
    # one place, including proxy mounts so they cannot silently double retry.
    transports = [
        getattr(client, "_transport", None),
        *getattr(client, "_mounts", {}).values(),
    ]
    if any(has_retries(transport) for transport in transports):
        raise ValueError(
            "Retry helpers require a client without RetryTransport or connection retries"
        )
    send_options: dict[str, Any] = {
        key: options.pop(key) for key in ("auth", "follow_redirects") if key in options
    }
    # Do not mutate a caller-owned extensions dict when publishing state.
    if options.get("extensions") is not None:
        options["extensions"] = dict(options["extensions"])
    request = client.build_request(method, url, **options)
    return (
        request,
        resolve_policy(request, retry if retry is not None else Retry()),
        send_options,
    )


def retry_request(
    client: httpx2.Client,
    method: str,
    url: httpx2.URL | str,
    *,
    retry: Retry | None = None,
    **kwargs: Unpack[RequestOptions],
) -> httpx2.Response:
    """Send and read the complete response. Single-use uploads are not replayed."""
    if not isinstance(client, httpx2.Client):
        raise TypeError("client must be httpx2.Client")
    request, policy, options = _prepare(client, method, url, retry, dict(kwargs))
    return run_sync(request, lambda request: client.send(request, **options), policy)


async def aretry_request(
    client: httpx2.AsyncClient,
    method: str,
    url: httpx2.URL | str,
    *,
    retry: Retry | None = None,
    **kwargs: Unpack[RequestOptions],
) -> httpx2.Response:
    """Asyncio/Trio counterpart to retry_request; not a streaming interface."""
    if not isinstance(client, httpx2.AsyncClient):
        raise TypeError("client must be httpx2.AsyncClient")
    request, policy, options = _prepare(client, method, url, retry, dict(kwargs))
    return await run_async(
        request, lambda request: client.send(request, **options), policy
    )

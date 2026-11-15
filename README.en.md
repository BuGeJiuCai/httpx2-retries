# httpx2_retries

[English](README.en.md) | [简体中文](README.md)

A sync and async retry layer for HTTPX2, supporting asyncio / uvloop / Trio.
Python >= 3.11. Runtime dependencies are `httpx2>=2.13,<3` and `anyio>=4,<5`.

The simplest usage: attach a retry transport to the client and call the client as usual.

## Installation

Run from the repository root:

```bash
uv sync
# Install the optional Trio runtime when needed:
uv sync --extra trio
```

You can also use `uv pip install -e .` or `python -m pip install .`.

`uv sync` installs the `dev` dependency group by default. The `src/` directory holds the
flat `httpx2_retries` package sources and type marker (mapped through setuptools
package-dir); tests live in `tests/`.

## Quick start

Sync:

```python
import httpx2
from httpx2_retries import RetryTransport

with httpx2.Client(transport=RetryTransport()) as client:
    response = client.get("https://example.com")
    print(response.extensions["retry_attempts"])
```

Async:

```python
import httpx2
from httpx2_retries import AsyncRetryTransport


async def fetch(url: str):
    async with httpx2.AsyncClient(transport=AsyncRetryTransport()) as client:
        return await client.get(url)
```

The default policy retries GET / HEAD / PUT / DELETE / OPTIONS / TRACE on
429, 500, 502, 503, and 504, plus common network errors such as timeouts and connection
failures, up to 5 times.

To customize it, pass the same `Retry` to the transport:

```python
import httpx2
from httpx2_retries import Retry, RetryTransport

policy = Retry(total=3, max_total_sleep=20, max_elapsed_time=45)

with httpx2.Client(transport=RetryTransport(retry=policy), timeout=10) as client:
    response = client.get("https://example.com")
```

For async use `AsyncRetryTransport(retry=policy)`; the same `RetryTransport` class works
for both modes.

Which entry point to use:

| Entry point | When to use it |
| --- | --- |
| `RetryTransport` / `AsyncRetryTransport` | Default choice; client API stays unchanged and streaming is supported |
| `retry_request` / `aretry_request` | When you need to retry body-read errors or buffer the full response |

## Example: checking a connection

`examples/check_connection.py` tests connectivity to a URL and prints the retry count,
cumulative sleep, and stop reason:

```bash
uv run python examples/check_connection.py https://example.com
uv run python examples/check_connection.py https://example.com -n 3 -t 5
uv run python examples/check_connection.py https://example.com --async
uv run python examples/check_connection.py https://example.com --async --trio
```

Common options: `-n/--retries` retry count, `-t/--timeout` per-attempt timeout in seconds,
`--proxy` proxy URL, `--insecure` skip TLS verification, `--no-redirects` do not follow
redirects, `--no-body` skip reading the response body.
Exit codes: `0` success, `1` HTTP status error, `2` connection failure or retry deadline
exceeded.

## Common policy options

| Option | Default | Meaning |
| --- | --- | --- |
| `total` | 5 | Retries allowed after the initial request |
| `allowed_methods` | HEAD / GET / PUT / DELETE / OPTIONS / TRACE | POST/PATCH must be allowed explicitly |
| `status_forcelist` | 429 / 500 / 502 / 503 / 504 | Retryable response status codes |
| `retry_on_exceptions` | TimeoutException / NetworkError / ProxyError / RemoteProtocolError | HTTPX2 exception types |
| `backoff_factor` | 1 | Exponential backoff factor; 0 disables exponential waiting |
| `max_backoff_wait` | 60 seconds | Cap for a single wait |
| `backoff_jitter` | 1 | 0 disables jitter; 1 is full jitter |
| `respect_retry_after_header` | True | Prefer a valid Retry-After header |
| `max_total_sleep` | None | Cumulative sleep budget per call |
| `total_timeout` | None | Alias for max_total_sleep; not a total network timeout |
| `max_elapsed_time` | None | Overall time budget including sending, waiting, and validation |
| `validate_response` | None | Response validation callback |

Backoff caps are 2, 4, 8, 16, 32, and 60 seconds; by default the wait is random between 0
and the current cap. Retry-After accepts seconds and HTTP dates; 0 and past times retry
immediately, and invalid values fall back to backoff. If Retry-After exceeds the single
wait cap, or the next wait does not fit in the cumulative sleep budget, retrying stops and
the current response or exception is returned; server-specified waits are never shortened.

When `max_elapsed_time` is exhausted, or the next wait does not fit in the remaining time,
`RetryDeadlineExceeded` is raised. It inherits from `httpx2.TimeoutException` and is never
retried by this library. Async uses an AnyIO cancel scope covering cancellable sends, body
reads, waits, and async validation. Sync shortens HTTPX2's phase I/O timeouts and checks the
actual elapsed time between sending, validation, and waiting; **it cannot interrupt
arbitrary blocking code, custom transports, or slow validators**. Blocking code inside
async callbacks also cannot be preempted.

## Business response validation

Put the validator inside `Retry` so it applies with the transport:

```python
import httpx2
from httpx2_retries import AsyncRetryTransport, Retry


class TemporaryBusinessError(Exception):
    pass


def validate(response: httpx2.Response) -> None:
    if response.json().get("code") == "TEMPORARY_FAILURE":
        raise TemporaryBusinessError("server is temporarily unavailable")


policy = Retry(
    total=3,
    max_elapsed_time=30,
    validate_response=validate,
    retry_on_exceptions=(*Retry.RETRYABLE_EXCEPTIONS, TemporaryBusinessError),
)


async def fetch_json(url: str):
    async with httpx2.AsyncClient(
        transport=AsyncRetryTransport(retry=policy), timeout=10
    ) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.json()
```

Validation runs on responses whose status code is not already retryable, including the
final response; a validation failure is retried only when it matches
`retry_on_exceptions`. Reading the body in a validator means that response can no longer be
used as a streaming response.

## Advanced transport options

You can pass `verify`, `cert`, `proxy`, `limits`, `http1`, `http2`, `uds`,
`local_address`, `socket_options`, and `trust_env` directly to the transport:

```python
import httpx2

from httpx2_retries import AsyncRetryTransport

transport = AsyncRetryTransport(
    proxy="http://127.0.0.1:8080",
    limits=httpx2.Limits(max_connections=10, max_keepalive_connections=5),
    trust_env=False,
)
```

Timeouts, auth, cookies, and redirects are handled by the client. HTTP/2 and SOCKS need the
corresponding HTTPX2 optional dependencies.

You can also pass an existing `httpx2.HTTPTransport`, `AsyncHTTPTransport`,
`MockTransport`, or a custom transport; in that case you cannot also pass transport
construction options. A sync-only or async-only transport can only serve that mode. The
wrapper owns the inner transport and closes it when the client exits. Do not share a
transport between clients with different lifetimes.

HTTPX2 routing rules to keep in mind:

- Passing a separate `proxy=` to the client creates its own proxy transport and can bypass
  the wrapper; put the proxy on the retry transport instead.
- With `mounts`, every route that needs retries must be wrapped.
- With an explicit custom transport, HTTPX2 does not load environment proxies
  automatically; set the proxy explicitly.
- Nested `retries>0` or another retry wrapper is rejected by default. If you really need
  it, pass `allow_nested_retries=True`; outer statistics then exclude inner connection
  attempts, and `extensions={"retry": False}` does not disable inner connection retries.

## Full-body retries (helpers)

The transport only sees response headers. If the connection returns headers but the body
read fails, a client-level helper is needed to retry it:

```python
import httpx2
from httpx2_retries import Retry, aretry_request


async def fetch_json(url: str):
    policy = Retry(total=3, max_total_sleep=10, max_elapsed_time=30)
    async with httpx2.AsyncClient(timeout=10) as client:
        response = await aretry_request(client, "GET", url, retry=policy)
        response.raise_for_status()
        return response.json()
```

The sync entry point is `retry_request(client, method, url, *, retry=None, **kwargs)`; the
async one is `aretry_request`. They accept `params`, `headers`, `cookies`, `json`, `data`,
`files`, `content`, `timeout`, and `extensions`, plus `auth` and `follow_redirects` at send
time. `stream=True` raises an explicit error.

The helpers buffer the full response and read the body first, so they are not suitable for
infinite streams or very large downloads. Streaming content already handed to the caller
cannot be retried transparently. Async entry points accept plain functions, coroutine
functions, and async callable objects; the sync entry point rejects async validators.

The helpers reject clients with a retry transport, retry mounts, or a built-in transport
with connection retries enabled, to avoid multiplying attempts. Detection uses HTTPX2's
internal transport/pool attributes and recognizes wrappers exposing an inner object via
`.transport`; arbitrary third-party retry implementations cannot be guaranteed to be
detected, so callers must still avoid duplicate configuration.

## Per-request overrides and uploads

```python
# Retry POST only when the business operation is known to be repeatable.
post_policy = policy.copy_with(allowed_methods={"POST"})
response = client.post(
    "https://example.com/query",
    json={"q": "example"},
    extensions={"retry": post_policy},
)

# Disable retries for this request; validators stay enabled.
response = client.get("https://example.com", extensions={"retry": False})
```

`extensions['retry']` takes precedence over the transport/helper default policy.
`None` collections mean defaults; an empty `allowed_methods`, `status_forcelist`, or
`retry_on_exceptions` disables that condition.

Only `ByteStream` request bodies are replayed automatically, including ordinary JSON,
urlencoded forms, strings, and bytes. Generators, streaming content, multipart, and file
uploads are sent only once by default, including connection-phase failures. For small
uploads that can safely be buffered, call `build_request()`, then `request.read()` /
`await request.aread()`, and send through the retry transport. A read/write failure does
not mean the server did not execute the operation; POST/PATCH retries should be paired
with an idempotency mechanism.

## Metrics and extensions

Every response provides:

- `extensions['retry_attempts']`: retries performed, excluding the initial send.
- `extensions['retry_state']`: an immutable `RetryState` with `attempts_made`,
  `requests_made`, `elapsed_sleep`, `elapsed_time`, `stop_reason`, `last_status_code`,
  and `last_exception`.
- `extensions['retry']`: an immutable Retry snapshot with the final counters.

The same state is written to the original request's `extensions['retry_state']`. Exceptions
keep their original type and object, and `exc.retry_state` is attached when possible.

`stop_reason` values include `success` (no further retry needed, not equivalent to HTTP
2xx), `retries_exhausted`, `method_not_allowed`, `body_not_replayable`,
`retry_after_exceeds_limit`, `sleep_budget_exhausted`, `deadline_exceeded`,
`exception_not_retryable`, `cancelled`, and `internal_error`.
The DEBUG logger is named `httpx2_retries.transport`.

## Custom backoff and policy methods

```python
from httpx2_retries import Retry


class LinearRetry(Retry):
    def backoff_strategy(self) -> float:
        return self.attempts_made * 0.5
```

Public policy methods:

```text
copy_with(**changes) -> Retry
increment(*, elapsed_sleep=0) -> Retry
is_retryable_method(method) -> bool
is_retryable_status_code(status_code) -> bool
is_retryable_exception(exception) -> bool
is_retry(method, status_code, has_retry_after=False) -> bool
is_exhausted() -> bool
parse_retry_after(value) -> float
backoff_strategy() -> float
delay(retry_number, headers=None) -> float | None
sleep(response_or_exception, *, delay=None) -> float | None
asleep(response_or_exception, *, delay=None) -> float | None
```

`Retry` itself is immutable. A manual loop advances attempts with the value returned by
`increment()` and passes the newly completed wait via `elapsed_sleep=`.
`sleep/asleep` return the scheduled seconds or None and do not mutate the shared policy.
Do not use a response snapshot with final counters as the default policy for a new
request.

`py.typed` is included, along with the `RetryChanges`, `TransportOptions`, and
`RequestOptions` parameter types.

## Migration differences from httpx_retries 0.6.0

This library uses HTTPX2 types and cannot mix `httpx` clients, transports, or exception
types. It adds the policy methods, unified sync/async transport, helpers, validation, and
cumulative sleep interfaces, but it is not an identical behavioral replacement.

| Item | This library | httpx_retries 0.6.0 |
| --- | --- | --- |
| total / factor / per-wait cap | 5 / 1 / 60 | 10 / 0 / 120 |
| Default status codes | 429, 500, 502, 503, 504 | 429, 502, 503, 504 |
| Default exceptions | Includes HTTPX2 ProxyError | ProxyError excluded by default |
| Empty method/status sets | Disable that condition | Fall back to defaults |
| Overlong Retry-After | Stop retrying | Shorten the wait |
| Retry-After 0 or in the past | Retry immediately | May fall back to exponential backoff |
| Jitter order | Cap then jitter | Jitter then cap |
| `has_retry_after` in `is_retry` | Does not change status eligibility | Affects the result |
| State object | Immutable snapshot; sleep does not modify config | sleep updates elapsed_sleep |
| Constructor parameter order | Preserves this project's order | Different order |

Use keyword arguments when migrating. To keep the upstream numeric defaults, set them
explicitly:

```python
retry = Retry(
    total=10,
    backoff_factor=0,
    max_backoff_wait=120,
    status_forcelist={429, 502, 503, 504},
)
```

When upgrading from an earlier version of this project, the default status codes gained
429 while the other numeric defaults stayed the same; set
`status_forcelist={500, 502, 503, 504}` explicitly to keep the old behavior.

## Tests

Run from the repository root; all tests use the local MockTransport and never contact
business endpoints:

```bash
uv sync
uv run python -B -m unittest discover -s tests -v
```

`uv sync` installs the `dev` dependency group (including Trio) by default; Trio tests are
skipped when Trio is not installed. Current tests cover sync/async transport retries,
helper buffering and retries, per-request retry disabling, nested retry detection,
cumulative sleep budgets, deadlines, and both asyncio and Trio runtimes.

Reference: [HTTPX2 transports](https://pydantic.dev/docs/httpx2/advanced/transports/)

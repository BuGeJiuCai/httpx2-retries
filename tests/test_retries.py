from __future__ import annotations

import asyncio
import time
import unittest

import httpx2

from httpx2_retries import (
    AsyncRetryTransport,
    Retry,
    RetryDeadlineExceeded,
    RetryTransport,
    aretry_request,
    retry_request,
)

try:
    import trio
except ImportError:  # pragma: no cover - trio is optional
    trio = None


def _response(status_code: int, **kwargs) -> httpx2.Response:
    return httpx2.Response(status_code, **kwargs)


class SyncTransportTests(unittest.TestCase):
    def test_retries_retryable_status_codes(self):
        statuses = iter([503, 429, 200])

        def handler(request: httpx2.Request) -> httpx2.Response:
            return _response(next(statuses))

        transport = RetryTransport(
            httpx2.MockTransport(handler), retry=Retry(total=3, backoff_factor=0)
        )
        with httpx2.Client(transport=transport, trust_env=False) as client:
            response = client.get("https://example.com/resource")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.extensions["retry_attempts"], 2)
        self.assertEqual(response.extensions["retry_state"].stop_reason, "success")

    def test_request_extension_disables_retry(self):
        calls = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            calls.append(request)
            return _response(503)

        transport = RetryTransport(
            httpx2.MockTransport(handler), retry=Retry(total=3, backoff_factor=0)
        )
        with httpx2.Client(transport=transport, trust_env=False) as client:
            response = client.get(
                "https://example.com/resource", extensions={"retry": False}
            )

        self.assertEqual(len(calls), 1)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.extensions["retry_attempts"], 0)

    def test_sleep_budget_stops_retry(self):
        calls = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            calls.append(request)
            return _response(503)

        transport = RetryTransport(
            httpx2.MockTransport(handler),
            retry=Retry(
                total=3,
                backoff_factor=1,
                max_backoff_wait=10,
                max_total_sleep=0.5,
                backoff_jitter=0,
            ),
        )
        with httpx2.Client(transport=transport, trust_env=False) as client:
            response = client.get("https://example.com/resource")

        self.assertEqual(len(calls), 1)
        self.assertEqual(
            response.extensions["retry_state"].stop_reason, "sleep_budget_exhausted"
        )

    def test_nested_retries_rejected(self):
        with self.assertRaises(ValueError):
            RetryTransport(retries=1)
        with self.assertRaises(ValueError):
            RetryTransport(
                RetryTransport(
                    httpx2.MockTransport(lambda request: _response(200)),
                    retry=Retry(total=1),
                )
            )


class SyncHelperTests(unittest.TestCase):
    def test_helper_buffers_body_and_retries(self):
        calls = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            calls.append(request)
            if len(calls) == 1:
                return _response(503)
            return _response(200, json={"ok": True})

        with httpx2.Client(
            transport=httpx2.MockTransport(handler), trust_env=False
        ) as client:
            response = retry_request(
                client,
                "GET",
                "https://example.com/resource",
                retry=Retry(total=2, backoff_factor=0),
            )

        self.assertEqual(len(calls), 2)
        self.assertEqual(response.json(), {"ok": True})
        self.assertEqual(response.extensions["retry_attempts"], 1)

    def test_helper_rejects_retrying_client(self):
        transport = RetryTransport(
            httpx2.MockTransport(lambda request: _response(200)),
            retry=Retry(total=1, backoff_factor=0),
        )
        with httpx2.Client(transport=transport, trust_env=False) as client:
            with self.assertRaises(ValueError):
                retry_request(client, "GET", "https://example.com/resource")

    def test_helper_enforces_deadline(self):
        def handler(request: httpx2.Request) -> httpx2.Response:
            time.sleep(0.05)
            return _response(200)

        with httpx2.Client(
            transport=httpx2.MockTransport(handler), trust_env=False
        ) as client:
            with self.assertRaises(RetryDeadlineExceeded):
                retry_request(
                    client,
                    "GET",
                    "https://example.com/resource",
                    retry=Retry(total=1, backoff_factor=0, max_elapsed_time=0.01),
                )


class AsyncTransportTests(unittest.TestCase):
    def test_asyncio_transport_retries(self):
        calls = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            calls.append(request)
            if len(calls) == 1:
                return _response(502)
            return _response(200)

        async def run() -> httpx2.Response:
            transport = AsyncRetryTransport(
                httpx2.MockTransport(handler), retry=Retry(total=2, backoff_factor=0)
            )
            async with httpx2.AsyncClient(
                transport=transport, trust_env=False
            ) as client:
                return await client.get("https://example.com/resource")

        response = asyncio.run(run())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.extensions["retry_attempts"], 1)

    @unittest.skipIf(trio is None, "trio is not installed")
    def test_trio_helper_retries(self):
        calls = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            calls.append(request)
            if len(calls) == 1:
                return _response(503)
            return _response(200, json={"ok": True})

        async def run() -> httpx2.Response:
            async with httpx2.AsyncClient(
                transport=httpx2.MockTransport(handler), trust_env=False
            ) as client:
                return await aretry_request(
                    client,
                    "GET",
                    "https://example.com/resource",
                    retry=Retry(total=2, backoff_factor=0),
                )

        response = trio.run(run)
        self.assertEqual(response.json(), {"ok": True})
        self.assertEqual(response.extensions["retry_attempts"], 1)


if __name__ == "__main__":
    unittest.main()

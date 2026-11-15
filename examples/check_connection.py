#!/usr/bin/env python3
"""Test connectivity to a URL with HTTPX2 retries.

Examples:
    uv run python examples/check_connection.py https://example.com
    uv run python examples/check_connection.py https://example.com -n 3 -t 5
    uv run python examples/check_connection.py https://example.com --async
    uv run python examples/check_connection.py https://example.com --async --trio
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from typing import TYPE_CHECKING

import httpx2

# Pylance cannot follow uv's editable-install finder for the flat src layout:
# check types against the source tree, run against the installed package.
if TYPE_CHECKING:
    from src import (
        AsyncRetryTransport,
        Retry,
        RetryDeadlineExceeded,
        RetryTransport,
        TransportOptions,
    )
else:
    from httpx2_retries import (
        AsyncRetryTransport,
        Retry,
        RetryDeadlineExceeded,
        RetryTransport,
        TransportOptions,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Test a URL through HTTPX2 retry transports."
    )
    parser.add_argument(
        "url", nargs="?", default="https://example.com", help="URL to test"
    )
    parser.add_argument(
        "-n", "--retries", type=int, default=3, help="retries after the first request"
    )
    parser.add_argument(
        "-t", "--timeout", type=float, default=10.0, help="timeout per attempt (s)"
    )
    parser.add_argument("--method", default="GET", help="HTTP method")
    parser.add_argument("--proxy", default=None, help="proxy URL")
    parser.add_argument(
        "--insecure", action="store_true", help="skip TLS certificate verification"
    )
    parser.add_argument(
        "--no-redirects", action="store_true", help="do not follow redirects"
    )
    parser.add_argument(
        "--no-body", action="store_true", help="skip reading the response body"
    )
    parser.add_argument("--async", dest="use_async", action="store_true")
    parser.add_argument("--trio", action="store_true", help="use Trio with --async")
    return parser


def make_policy(args: argparse.Namespace) -> Retry:
    return Retry(
        total=args.retries,
        backoff_factor=0.5,
        max_backoff_wait=10,
        max_elapsed_time=max(args.timeout * (args.retries + 1), 1.0),
    )


def transport_options(args: argparse.Namespace) -> TransportOptions:
    options: TransportOptions = {"trust_env": True}
    if args.proxy:
        options["proxy"] = args.proxy
    if args.insecure:
        options["verify"] = False
    return options


def report(response: httpx2.Response, elapsed: float, body_size: int | None) -> int:
    state = response.extensions.get("retry_state")
    print(f"URL: {response.url}")
    print(f"状态码: {response.status_code}")
    print(f"耗时: {elapsed:.3f}s")
    if body_size is not None:
        print(f"正文: {body_size} bytes")
    if state is not None:
        print(f"重试次数: {state.attempts_made}")
        print(f"累计等待: {state.elapsed_sleep:.3f}s")
        print(f"停止原因: {state.stop_reason}")
    ok = 200 <= response.status_code < 400
    print("连接结果:", "成功" if ok else "失败")
    return 0 if ok else 1


def run_sync(args: argparse.Namespace) -> int:
    transport = RetryTransport(retry=make_policy(args), **transport_options(args))
    started = time.monotonic()
    try:
        with httpx2.Client(
            transport=transport,
            timeout=args.timeout,
            follow_redirects=not args.no_redirects,
        ) as client:
            response = client.request(args.method, args.url)
            body_size = None if args.no_body else len(response.read())
    except RetryDeadlineExceeded as exc:
        print(f"失败: 超过重试截止时间: {exc}", file=sys.stderr)
        return 2
    except httpx2.HTTPError as exc:
        print(f"失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return report(response, time.monotonic() - started, body_size)


async def run_async(args: argparse.Namespace) -> int:
    transport = AsyncRetryTransport(retry=make_policy(args), **transport_options(args))
    started = time.monotonic()
    try:
        async with httpx2.AsyncClient(
            transport=transport,
            timeout=args.timeout,
            follow_redirects=not args.no_redirects,
        ) as client:
            response = await client.request(args.method, args.url)
            body_size = None if args.no_body else len(await response.aread())
    except RetryDeadlineExceeded as exc:
        print(f"失败: 超过重试截止时间: {exc}", file=sys.stderr)
        return 2
    except httpx2.HTTPError as exc:
        print(f"失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return report(response, time.monotonic() - started, body_size)


def main() -> int:
    args = build_parser().parse_args()
    if args.trio:
        if not args.use_async:
            print("--trio 需要同时指定 --async", file=sys.stderr)
            return 2
        try:
            import trio
        except ImportError:
            print("未安装 trio,请执行 uv sync --extra trio", file=sys.stderr)
            return 2
        return trio.run(run_async, args)
    if args.use_async:
        return asyncio.run(run_async(args))
    return run_sync(args)


if __name__ == "__main__":
    raise SystemExit(main())

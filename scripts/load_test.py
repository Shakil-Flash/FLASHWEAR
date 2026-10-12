#!/usr/bin/env python3
"""FLASHWEAR Lightweight Staged Load Testing Runner.

Measures throughput, latency percentiles (p50, p95, p99), error rates,
and per-endpoint performance across critical safe read and write paths.

Can be run:
1. As a standalone CLI against a running server:
   python scripts/load_test.py --base-url http://127.0.0.1:8000 --users 15 --requests 100
2. As an imported module in automated tests using custom request runners (e.g. Django Client).
"""

from __future__ import annotations

import argparse
import concurrent.futures
import statistics
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

# Default safe read-heavy endpoints
DEFAULT_SAFE_ENDPOINTS = [
    "/",
    "/shop/",
    "/shop/?q=t-shirt",
    "/shop/?category=apparel",
    "/health/live/",
    "/health/ready/",
]


class LoadTestResult:
    """Encapsulates latency, throughput, and error metrics from a load test run."""

    def __init__(self, endpoint: str):
        self.endpoint = endpoint
        self.latencies_ms: list[float] = []
        self.status_codes: dict[int, int] = {}
        self.errors: list[str] = []

    def record_success(self, latency_ms: float, status_code: int = 200) -> None:
        self.latencies_ms.append(latency_ms)
        self.status_codes[status_code] = self.status_codes.get(status_code, 0) + 1

    def record_failure(self, latency_ms: float, error_msg: str, status_code: int = 500) -> None:
        self.latencies_ms.append(latency_ms)
        self.status_codes[status_code] = self.status_codes.get(status_code, 0) + 1
        self.errors.append(error_msg)

    @property
    def total_requests(self) -> int:
        return len(self.latencies_ms)

    @property
    def failed_requests(self) -> int:
        return sum(
            count
            for code, count in self.status_codes.items()
            if code >= 400 and code not in (404, 405)  # Expected probe codes
        ) + len(self.errors)

    @property
    def error_rate(self) -> float:
        if not self.total_requests:
            return 0.0
        return (self.failed_requests / self.total_requests) * 100.0

    @property
    def p50_ms(self) -> float:
        if not self.latencies_ms:
            return 0.0
        return statistics.median(self.latencies_ms)

    @property
    def p95_ms(self) -> float:
        if not self.latencies_ms:
            return 0.0
        sorted_lat = sorted(self.latencies_ms)
        idx = int(0.95 * len(sorted_lat))
        return sorted_lat[min(idx, len(sorted_lat) - 1)]

    @property
    def p99_ms(self) -> float:
        if not self.latencies_ms:
            return 0.0
        sorted_lat = sorted(self.latencies_ms)
        idx = int(0.99 * len(sorted_lat))
        return sorted_lat[min(idx, len(sorted_lat) - 1)]

    @property
    def min_ms(self) -> float:
        return min(self.latencies_ms) if self.latencies_ms else 0.0

    @property
    def max_ms(self) -> float:
        return max(self.latencies_ms) if self.latencies_ms else 0.0


def default_http_runner(url: str, timeout: float = 10.0) -> tuple[int, float]:
    """Execute standard HTTP GET using urllib."""
    start = time.perf_counter()
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "FLASHWEAR-LoadTest/1.0"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as response:
            status_code = response.getcode()
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            return status_code, elapsed_ms
    except urllib.error.HTTPError as exc:
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        return exc.code, elapsed_ms
    except Exception as exc:
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        raise RuntimeError(f"Connection error: {exc}") from exc


def run_load_benchmark(
    base_url: str = "http://127.0.0.1:8000",
    endpoints: list[str] | None = None,
    concurrency: int = 10,
    total_requests: int = 50,
    runner_fn: Callable[[str], tuple[int, float]] | None = None,
) -> dict[str, Any]:
    """Execute a concurrent load test profile against specified endpoints.

    Parameters:
        base_url: Root URL (used with default HTTP runner).
        endpoints: List of URI paths to request.
        concurrency: Max worker threads.
        total_requests: Total number of requests distributed across endpoints.
        runner_fn: Custom invocation callback(path) -> (status_code, elapsed_ms).
    """
    endpoints = endpoints or DEFAULT_SAFE_ENDPOINTS
    runner = runner_fn or (lambda path: default_http_runner(f"{base_url.rstrip('/')}{path}"))

    results: dict[str, LoadTestResult] = {ep: LoadTestResult(ep) for ep in endpoints}
    requests_per_endpoint = max(1, total_requests // len(endpoints))

    # Build queue of tasks
    work_items: list[str] = []
    for ep in endpoints:
        work_items.extend([ep] * requests_per_endpoint)

    start_wall_time = time.perf_counter()

    def _worker_task(ep: str) -> tuple[str, int, float, str | None]:
        try:
            status, duration_ms = runner(ep)
            return ep, status, duration_ms, None
        except Exception as err:
            return ep, 500, 0.0, str(err)

    if concurrency == 1:
        for ep in work_items:
            ep, status_code, latency_ms, error = _worker_task(ep)
            res = results[ep]
            if error:
                res.record_failure(latency_ms, error, status_code)
            elif status_code in (200, 301, 302, 304):
                res.record_success(latency_ms, status_code)
            else:
                res.record_failure(latency_ms, f"HTTP {status_code}", status_code)
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = [executor.submit(_worker_task, ep) for ep in work_items]
            for future in concurrent.futures.as_completed(futures):
                ep, status_code, latency_ms, error = future.result()
                res = results[ep]
                if error:
                    res.record_failure(latency_ms, error, status_code)
                elif status_code in (200, 301, 302, 304):
                    res.record_success(latency_ms, status_code)
                else:
                    res.record_failure(latency_ms, f"HTTP {status_code}", status_code)

    total_wall_duration = time.perf_counter() - start_wall_time
    total_reqs = sum(r.total_requests for r in results.values())
    total_fails = sum(r.failed_requests for r in results.values())
    all_latencies = [lat for r in results.values() for lat in r.latencies_ms]

    overall_p50 = statistics.median(all_latencies) if all_latencies else 0.0
    sorted_all = sorted(all_latencies)
    overall_p95 = sorted_all[int(0.95 * len(sorted_all))] if sorted_all else 0.0
    overall_p99 = sorted_all[int(0.99 * len(sorted_all))] if sorted_all else 0.0
    throughput_rps = (total_reqs / total_wall_duration) if total_wall_duration > 0 else 0.0
    overall_error_rate = (total_fails / total_reqs * 100.0) if total_reqs > 0 else 0.0

    return {
        "concurrency": concurrency,
        "total_requests": total_reqs,
        "failed_requests": total_fails,
        "error_rate_pct": round(overall_error_rate, 2),
        "duration_seconds": round(total_wall_duration, 3),
        "throughput_rps": round(throughput_rps, 2),
        "latency_min_ms": round(min(all_latencies), 2) if all_latencies else 0.0,
        "latency_p50_ms": round(overall_p50, 2),
        "latency_p95_ms": round(overall_p95, 2),
        "latency_p99_ms": round(overall_p99, 2),
        "latency_max_ms": round(max(all_latencies), 2) if all_latencies else 0.0,
        "endpoints": {
            ep: {
                "requests": r.total_requests,
                "failed": r.failed_requests,
                "error_rate_pct": round(r.error_rate, 2),
                "p50_ms": round(r.p50_ms, 2),
                "p95_ms": round(r.p95_ms, 2),
            }
            for ep, r in results.items()
        },
    }


def print_summary_table(summary: dict[str, Any]) -> None:
    """Print a clean ASCII summary table of the load test results."""
    print("\n" + "=" * 70)
    print("FLASHWEAR LOAD TEST RESULTS SUMMARY")
    print("=" * 70)
    print(f"Concurrency:      {summary['concurrency']} worker threads")
    print(f"Total Requests:   {summary['total_requests']}")
    print(f"Failed Requests:  {summary['failed_requests']} ({summary['error_rate_pct']}%)")
    print(f"Total Duration:   {summary['duration_seconds']}s")
    print(f"Throughput:       {summary['throughput_rps']} req/s")
    print("-" * 70)
    print("LATENCY DISTRIBUTION (ms):")
    print(f"  Min:  {summary['latency_min_ms']:>8.2f} ms")
    print(f"  p50:  {summary['latency_p50_ms']:>8.2f} ms (Median)")
    print(f"  p95:  {summary['latency_p95_ms']:>8.2f} ms")
    print(f"  p99:  {summary['latency_p99_ms']:>8.2f} ms")
    print(f"  Max:  {summary['latency_max_ms']:>8.2f} ms")
    print("-" * 70)
    print(f"{'Endpoint':<35} {'Reqs':<8} {'Fails':<8} {'p50(ms)':<10} {'p95(ms)':<10}")
    print("-" * 70)
    for ep, data in summary["endpoints"].items():
        print(
            f"{ep:<35} {data['requests']:<8} {data['failed']:<8} "
            f"{data['p50_ms']:<10.2f} {data['p95_ms']:<10.2f}"
        )
    print("=" * 70 + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="FLASHWEAR Staged Load Testing Runner")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="Root target URL")
    parser.add_argument("--users", type=int, default=10, help="Worker threads / concurrent users")
    parser.add_argument("--requests", type=int, default=60, help="Total requests across endpoints")
    parser.add_argument("--staged", action="store_true", help="Execute 3-stage load curve")

    args = parser.parse_args()

    if args.staged:
        print("[*] Running Staged Load Profile:")
        stages = [
            ("Stage 1 (Warmup)", 5, 25),
            ("Stage 2 (Moderate Load)", 15, 75),
            ("Stage 3 (Peak Load)", 30, 150),
        ]
        for name, concurrency, req_count in stages:
            print(f"\n---> Executing {name}: {concurrency} users, {req_count} requests...")
            res = run_load_benchmark(
                base_url=args.base_url,
                concurrency=concurrency,
                total_requests=req_count,
            )
            print_summary_table(res)
    else:
        res = run_load_benchmark(
            base_url=args.base_url,
            concurrency=args.users,
            total_requests=args.requests,
        )
        print_summary_table(res)


if __name__ == "__main__":
    main()

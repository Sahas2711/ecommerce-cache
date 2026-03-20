"""
benchmark.py — Latency measurement script.

Measures response time BEFORE (cold cache / cache miss) and AFTER (warm cache / HIT)
to demonstrate the performance improvement of the Lazy Loading pattern.

Usage:
  python benchmark.py --base-url http://localhost:8080 --iterations 50

Output:
  Prints a summary table and per-request log lines compatible with
  CloudWatch Logs Insights format.
"""

import argparse
import json
import statistics
import time
from typing import List, Dict
import urllib.request
import urllib.error


def make_request(url: str) -> Dict:
    """Issue HTTP GET and return (latency_ms, cache_status, http_status)."""
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            body       = json.loads(resp.read())
            elapsed_ms = (time.perf_counter() - start) * 1000
            return {
                "latency_ms"   : round(elapsed_ms, 3),
                "cache_status" : body.get("cache_status", "UNKNOWN"),
                "http_status"  : resp.status,
                "error"        : None,
            }
    except urllib.error.URLError as exc:
        elapsed_ms = (time.perf_counter() - start) * 1000
        return {
            "latency_ms"   : round(elapsed_ms, 3),
            "cache_status" : "ERROR",
            "http_status"  : 0,
            "error"        : str(exc),
        }


def run_benchmark(base_url: str, product_id: int, iterations: int) -> None:
    url = f"{base_url}/products/{product_id}"
    results: List[Dict] = []

    print(f"\n{'='*65}")
    print(f"  Benchmark: GET {url}")
    print(f"  Iterations: {iterations}")
    print(f"{'='*65}")
    print(f"{'#':>4}  {'Cache':>6}  {'Latency (ms)':>14}  {'HTTP':>6}")
    print(f"{'-'*4}  {'-'*6}  {'-'*14}  {'-'*6}")

    for i in range(1, iterations + 1):
        r = make_request(url)
        results.append(r)
        marker = "◀ MISS" if r["cache_status"] == "MISS" else ""
        print(
            f"{i:>4}  {r['cache_status']:>6}  {r['latency_ms']:>14.3f}  "
            f"{r['http_status']:>6}  {marker}"
        )
        time.sleep(0.05)   # small delay to avoid saturating the local loopback

    # ── Summary statistics ─────────────────────────────────────────────────
    hits   = [r for r in results if r["cache_status"] == "HIT"]
    misses = [r for r in results if r["cache_status"] == "MISS"]

    def stats(data: List[Dict]) -> Dict:
        if not data:
            return {"count": 0, "min": 0, "max": 0, "mean": 0, "p95": 0, "p99": 0}
        lats = [r["latency_ms"] for r in data]
        lats.sort()
        p95_idx = int(len(lats) * 0.95)
        p99_idx = int(len(lats) * 0.99)
        return {
            "count": len(lats),
            "min"  : round(min(lats), 3),
            "max"  : round(max(lats), 3),
            "mean" : round(statistics.mean(lats), 3),
            "p95"  : round(lats[min(p95_idx, len(lats)-1)], 3),
            "p99"  : round(lats[min(p99_idx, len(lats)-1)], 3),
        }

    h = stats(hits)
    m = stats(misses)

    improvement = (
        round(m["mean"] / h["mean"], 1)
        if h["mean"] > 0 and m["mean"] > 0
        else "N/A"
    )

    print(f"\n{'='*65}")
    print(f"  RESULTS SUMMARY  (product_id={product_id})")
    print(f"{'='*65}")
    print(f"  {'Metric':<20}  {'Cache HIT':>12}  {'Cache MISS':>12}")
    print(f"  {'-'*20}  {'-'*12}  {'-'*12}")
    print(f"  {'Count':<20}  {h['count']:>12}  {m['count']:>12}")
    print(f"  {'Min (ms)':<20}  {h['min']:>12.3f}  {m['min']:>12.3f}")
    print(f"  {'Max (ms)':<20}  {h['max']:>12.3f}  {m['max']:>12.3f}")
    print(f"  {'Mean (ms)':<20}  {h['mean']:>12.3f}  {m['mean']:>12.3f}")
    print(f"  {'P95 (ms)':<20}  {h['p95']:>12.3f}  {m['p95']:>12.3f}")
    print(f"  {'P99 (ms)':<20}  {h['p99']:>12.3f}  {m['p99']:>12.3f}")
    print(f"\n  ✅ Speed improvement (MISS mean / HIT mean): {improvement}×")
    print(f"{'='*65}\n")

    # ── Emit structured log lines for CloudWatch ───────────────────────────
    for r in results:
        print(json.dumps({
            "benchmark"   : True,
            "product_id"  : product_id,
            "cache_status": r["cache_status"],
            "latency_ms"  : r["latency_ms"],
            "http_status" : r["http_status"],
        }))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Cache latency benchmark")
    parser.add_argument("--base-url",   default="http://localhost:8080")
    parser.add_argument("--product-id", type=int, default=1)
    parser.add_argument("--iterations", type=int, default=30)
    args = parser.parse_args()

    run_benchmark(args.base_url, args.product_id, args.iterations)

#!/usr/bin/env python3
"""Send steady /chat traffic so the dashboards and SLOs have data.

  python3 scripts/load_test.py --api https://<id>.execute-api.<region>.amazonaws.com/prod \\
      --rps 0.5 --minutes 30 --topic RAG

Each /chat call invokes Bedrock, so cost scales with --rps x --minutes. The stage
throttle defaults to 5 req/s; requests over it get 429 from API Gateway.
Prints client-side p50/p95/p99 and status counts at the end. Standard library only.
"""
import argparse
import json
import random
import statistics
import threading
import time
import urllib.error
import urllib.request
from collections import Counter

QUESTIONS = [
    "How does retrieval reduce hallucination in language models?",
    "What are the trade-offs between dense and sparse retrieval?",
    "How is retrieval quality evaluated in RAG systems?",
    "What chunking strategies work best for long documents?",
    "How do rerankers improve retrieval augmented generation?",
    "What are common failure modes of RAG pipelines?",
    "How does the number of retrieved passages affect answer quality?",
    "What methods exist for citing sources in generated answers?",
]


def call(api, question, topic, timeout):
    body = {"query": question, "top_k": 5}
    if topic:
        body["topic"] = topic
    req = urllib.request.Request(f"{api}/chat", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
            status = r.status
    except urllib.error.HTTPError as e:
        status = e.code
    except Exception:
        status = 0  # timeout / connection error
    return status, time.perf_counter() - started


def pct(values, q):
    if not values:
        return float("nan")
    values = sorted(values)
    return values[min(len(values) - 1, int(round(q * (len(values) - 1))))]


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--api", required=True, help="API base URL (no trailing /chat)")
    p.add_argument("--rps", type=float, default=0.5)
    p.add_argument("--minutes", type=float, default=10)
    p.add_argument("--topic", default=None)
    p.add_argument("--timeout", type=float, default=30)
    args = p.parse_args()
    api = args.api.rstrip("/")

    results, lock = [], threading.Lock()

    def worker(q):
        r = call(api, q, args.topic, args.timeout)
        with lock:
            results.append(r)

    end = time.time() + args.minutes * 60
    threads, sent = [], 0
    print(f"Sending ~{args.rps} req/s to {api}/chat for {args.minutes} min")
    while time.time() < end:
        t = threading.Thread(target=worker, args=(random.choice(QUESTIONS),), daemon=True)
        t.start()
        threads.append(t)
        sent += 1
        if sent % 50 == 0:
            with lock:
                done = len(results)
            print(f"  sent {sent}, completed {done}")
        time.sleep(random.expovariate(args.rps))  # Poisson arrivals
    for t in threads:
        t.join(timeout=args.timeout + 5)

    codes = Counter(s for s, _ in results)
    ok = [d for s, d in results if 200 <= s < 300]
    print(f"\nrequests: {len(results)}  status: {dict(sorted(codes.items()))}")
    if ok:
        print(f"client latency (2xx): p50 {pct(ok, .5) * 1000:.0f} ms  p95 {pct(ok, .95) * 1000:.0f} ms  "
              f"p99 {pct(ok, .99) * 1000:.0f} ms  mean {statistics.mean(ok) * 1000:.0f} ms")
    server_errors = sum(n for s, n in codes.items() if s >= 500 or s == 0)
    print(f"success (non-5xx): {100 * (1 - server_errors / max(1, len(results))):.2f}%")


if __name__ == "__main__":
    main()

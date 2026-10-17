#!/usr/bin/env python3
"""Evaluate the deployed RAG API against a hand-labeled question set.

Two modes:
  retrieval  Calls /chat with retrieval_only=true (no LLM call, cheap). Reports
             hit rate@1/3/5, MRR@5, recall@5, and retrieval latency.
  full       Calls /chat normally. Adds end-to-end latency, token usage and,
             if you pass Bedrock prices, LLM cost per query.

Examples:
  python eval/run_eval.py --api https://abc.execute-api.us-east-1.amazonaws.com/prod \
      --eval-set eval/eval_set.jsonl --mode retrieval

  python eval/run_eval.py --api ... --eval-set eval/eval_set.jsonl --mode full \
      --input-price 0.00007 --output-price 0.0003   # USD per 1K tokens; check current Bedrock pricing

Results are written to eval/results/<timestamp>_<mode>.json and .md.
Standard library only, so no install is needed.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from eval.metrics import first_relevant_rank, latency_summary, retrieval_summary, token_cost  # noqa: E402


def load_eval_set(path: str) -> List[Dict[str, Any]]:
    rows = []
    with open(path) as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if not row.get("question", "").strip():
                raise SystemExit(f"{path}:{line_no}: empty 'question'. Fill in every question first.")
            if not row.get("relevant_arxiv_ids"):
                raise SystemExit(f"{path}:{line_no}: 'relevant_arxiv_ids' must list at least one ID.")
            rows.append(row)
    if not rows:
        raise SystemExit(f"{path}: no questions found")
    return rows


def call_chat(api: str, payload: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    request = urllib.request.Request(
        api.rstrip("/") + "/chat",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return json.loads(resp.read())


def run(args) -> Dict[str, Any]:
    questions = load_eval_set(args.eval_set)
    retrieval_only = args.mode == "retrieval"
    per_query, client_latencies, server_retrieval_ms = [], [], []
    prompt_tokens = completion_tokens = errors = 0

    # Warm-up requests absorb Lambda cold starts so they don't skew latency.
    for _ in range(args.warmup):
        try:
            call_chat(args.api, {"query": questions[0]["question"], "top_k": args.top_k,
                                 "retrieval_only": True}, args.timeout)
        except Exception as e:
            print(f"warm-up request failed: {e}", file=sys.stderr)

    for i, q in enumerate(questions, 1):
        payload: Dict[str, Any] = {"query": q["question"], "top_k": args.top_k, "retrieval_only": retrieval_only}
        if q.get("topic") and not args.ignore_topic:
            payload["topic"] = q["topic"]

        started = time.perf_counter()
        try:
            data = call_chat(args.api, payload, args.timeout)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            errors += 1
            print(f"[{i}/{len(questions)}] ERROR {e}", file=sys.stderr)
            per_query.append({"question": q["question"], "relevant": q["relevant_arxiv_ids"],
                              "retrieved": [], "error": str(e)})
            continue
        latency_ms = (time.perf_counter() - started) * 1000
        client_latencies.append(latency_ms)

        retrieved = [s["arxiv_id"] for s in data.get("sources", [])]
        timings = data.get("timings_ms", {})
        if "retrieval_ms" in timings:
            server_retrieval_ms.append(timings["retrieval_ms"])
        usage = data.get("usage", {})
        prompt_tokens += usage.get("prompt_tokens", 0)
        completion_tokens += usage.get("completion_tokens", 0)

        rank = first_relevant_rank(retrieved, q["relevant_arxiv_ids"])
        per_query.append({
            "question": q["question"],
            "relevant": q["relevant_arxiv_ids"],
            "retrieved": retrieved,
            "first_relevant_rank": rank,
            "client_latency_ms": round(latency_ms, 1),
            "server_timings_ms": timings,
            "usage": usage,
            "answer": data.get("response") if not retrieval_only else None,
        })
        print(f"[{i}/{len(questions)}] rank={rank if rank else '-'}  {latency_ms:.0f} ms  {q['question'][:60]}")

    scored = [r for r in per_query if "error" not in r]
    summary: Dict[str, Any] = {
        "mode": args.mode,
        "top_k": args.top_k,
        "topic_filter": not args.ignore_topic,
        "errors": errors,
        "retrieval": retrieval_summary(scored, ks=sorted({1, 3, args.top_k})),
        "client_latency": latency_summary(client_latencies),
        "server_retrieval_latency": latency_summary(server_retrieval_ms),
    }
    if not retrieval_only and scored:
        n = len(scored)
        summary["avg_prompt_tokens"] = round(prompt_tokens / n, 1)
        summary["avg_completion_tokens"] = round(completion_tokens / n, 1)
        if args.input_price is not None and args.output_price is not None:
            summary["llm_cost_per_query_usd"] = round(
                token_cost(prompt_tokens, completion_tokens, args.input_price, args.output_price) / n, 6)
    return {"summary": summary, "per_query": per_query}


def to_markdown(report: Dict[str, Any], eval_set: str) -> str:
    s = report["summary"]
    lines = [
        f"# RAG evaluation ({s['mode']} mode)",
        "",
        f"- Eval set: `{eval_set}`, {s['retrieval'].get('queries', 0)} scored queries, {s['errors']} errors",
        f"- top_k = {s['top_k']}, topic filter = {s['topic_filter']}",
        "",
        "| Metric | Value |",
        "| --- | --- |",
    ]
    for key, value in s["retrieval"].items():
        if key != "queries":
            lines.append(f"| {key} | {value:.3f} |")
    for label, block in (("client latency", s["client_latency"]), ("server retrieval latency", s["server_retrieval_latency"])):
        for key, value in block.items():
            lines.append(f"| {label} {key} | {value} |")
    for key in ("avg_prompt_tokens", "avg_completion_tokens", "llm_cost_per_query_usd"):
        if key in s:
            lines.append(f"| {key} | {s[key]} |")
    misses = [r for r in report["per_query"] if r.get("first_relevant_rank") is None]
    if misses:
        lines += ["", f"## Misses ({len(misses)})", ""]
        lines += [f"- {r['question']} (expected {', '.join(r['relevant'])})" for r in misses]
    return "\n".join(lines) + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api", required=True, help="API base URL, e.g. https://xxx.execute-api.us-east-1.amazonaws.com/prod")
    parser.add_argument("--eval-set", required=True, help="JSONL file of questions and relevant arXiv IDs")
    parser.add_argument("--mode", choices=["retrieval", "full"], default="retrieval")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--warmup", type=int, default=1, help="Warm-up requests excluded from latency stats")
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--ignore-topic", action="store_true", help="Search all papers instead of filtering by topic (harder)")
    parser.add_argument("--input-price", type=float, help="USD per 1K input tokens for the chat model")
    parser.add_argument("--output-price", type=float, help="USD per 1K output tokens for the chat model")
    parser.add_argument("--out-dir", default="eval/results")
    args = parser.parse_args(argv)

    report = run(args)
    os.makedirs(args.out_dir, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = os.path.join(args.out_dir, f"{stamp}_{args.mode}")
    with open(base + ".json", "w") as f:
        json.dump(report, f, indent=2)
    markdown = to_markdown(report, args.eval_set)
    with open(base + ".md", "w") as f:
        f.write(markdown)
    print("\n" + markdown)
    print(f"Saved {base}.json and {base}.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())

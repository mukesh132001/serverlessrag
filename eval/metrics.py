"""Retrieval and latency metrics for the RAG evaluation (standard library only)."""
import math
import re
from typing import Dict, Iterable, List, Optional, Sequence

_VERSION_SUFFIX = re.compile(r"v\d+$")


def normalize_arxiv_id(arxiv_id: str) -> str:
    """'2301.12345v2' -> '2301.12345' so a paper matches regardless of version."""
    return _VERSION_SUFFIX.sub("", arxiv_id.strip())


def first_relevant_rank(retrieved: Sequence[str], relevant: Iterable[str]) -> Optional[int]:
    """1-based rank of the first relevant paper in the results, or None if absent."""
    wanted = {normalize_arxiv_id(r) for r in relevant}
    for rank, arxiv_id in enumerate(retrieved, 1):
        if normalize_arxiv_id(arxiv_id) in wanted:
            return rank
    return None


def recall_at_k(retrieved: Sequence[str], relevant: Iterable[str], k: int) -> float:
    wanted = {normalize_arxiv_id(r) for r in relevant}
    if not wanted:
        return 0.0
    found = {normalize_arxiv_id(a) for a in retrieved[:k]} & wanted
    return len(found) / len(wanted)


def retrieval_summary(results: List[Dict], ks: Sequence[int] = (1, 3, 5)) -> Dict[str, float]:
    """results: [{'retrieved': [...ids in rank order], 'relevant': [...ids]}, ...]"""
    n = len(results)
    if n == 0:
        return {"queries": 0}
    ranks = [first_relevant_rank(r["retrieved"], r["relevant"]) for r in results]
    max_k = max(ks)
    summary: Dict[str, float] = {"queries": n}
    for k in ks:
        summary[f"hit_rate@{k}"] = sum(1 for rank in ranks if rank is not None and rank <= k) / n
    summary[f"mrr@{max_k}"] = sum(1.0 / rank for rank in ranks if rank is not None and rank <= max_k) / n
    summary[f"recall@{max_k}"] = sum(recall_at_k(r["retrieved"], r["relevant"], max_k) for r in results) / n
    return summary


def percentile(values: Sequence[float], pct: float) -> float:
    """Nearest-rank percentile (pct in 0-100)."""
    if not values:
        raise ValueError("percentile of empty list")
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return ordered[rank - 1]


def latency_summary(latencies_ms: Sequence[float]) -> Dict[str, float]:
    if not latencies_ms:
        return {}
    return {
        "p50_ms": round(percentile(latencies_ms, 50), 1),
        "p95_ms": round(percentile(latencies_ms, 95), 1),
        "mean_ms": round(sum(latencies_ms) / len(latencies_ms), 1),
        "max_ms": round(max(latencies_ms), 1),
    }


def token_cost(prompt_tokens: int, completion_tokens: int, input_price_per_1k: float, output_price_per_1k: float) -> float:
    return prompt_tokens / 1000 * input_price_per_1k + completion_tokens / 1000 * output_price_per_1k

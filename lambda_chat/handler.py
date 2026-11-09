"""Chat Lambda: embed the question, retrieve similar papers, answer with sources."""
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from common.bedrock_utils import generate_chat_response, generate_embeddings
from common.db_utils import execute_query, to_pgvector
from common.http_utils import BadRequest, clamp_int, parse_body, require_text, response, server_error
from common import telemetry

logger = logging.getLogger()
logger.setLevel(logging.INFO)

MAX_QUERY_CHARS = 1000
MAX_TOP_K = 20
NO_RESULTS_MESSAGE = "I could not find relevant information to answer your query."


@telemetry.instrumented("/chat")
def main(event, context):
    """
    GET /health  -> health_check()
    POST /chat
    {
        "query": "What are recent advances in quantum error correction?",
        "topic": "Quantum Computing",   # optional; or "topic_id"
        "top_k": 5,                     # optional, 1-20
        "retrieval_only": false         # optional; skip the LLM call (used by evaluation)
    }
    """
    if isinstance(event, dict) and event.get("resource") == "/health":
        return health_check(context)

    started = time.perf_counter()
    try:
        body = parse_body(event)
        user_query = require_text(body.get("query"), "query", MAX_QUERY_CHARS)
        top_k = clamp_int(body.get("top_k"), default=5, low=1, high=MAX_TOP_K, name="top_k")
        retrieval_only = bool(body.get("retrieval_only", False))

        topic_id = body.get("topic_id")
        if topic_id is not None:
            topic_id = clamp_int(topic_id, default=None, low=1, high=2**31 - 1, name="topic_id")
        elif body.get("topic"):
            topic_id = get_topic_id_by_name(require_text(body["topic"], "topic", 255))
            if topic_id is None:
                return response(404, {"error": f"Topic '{body['topic']}' not found"})
    except BadRequest as e:
        return response(400, {"error": str(e)})
    except Exception:
        logger.exception("Error validating chat request")
        return server_error(context)

    try:
        timings: Dict[str, float] = {}

        t = time.perf_counter()
        query_embedding = generate_embeddings([user_query])[0]
        timings["embedding_ms"] = _ms_since(t)

        t = time.perf_counter()
        papers = search_similar_papers(query_embedding, topic_id=topic_id, top_k=top_k)
        timings["retrieval_ms"] = _ms_since(t)

        sources = [
            {
                "paper_title": p["paper_title"],
                "arxiv_id": p["arxiv_id"],
                "similarity": round(p["similarity"], 4),
                "url": p["url"],
            }
            for p in papers
        ]

        usage = {"prompt_tokens": 0, "completion_tokens": 0}
        if not papers:
            answer = NO_RESULTS_MESSAGE
        elif retrieval_only:
            answer = None
        else:
            t = time.perf_counter()
            answer, usage = generate_answer(user_query, build_context(papers))
            timings["generation_ms"] = _ms_since(t)

        timings["total_ms"] = _ms_since(started)
        telemetry.record_stages("/chat", timings)
        telemetry.record_tokens(usage)
        logger.info("chat ok top_k=%s sources=%s timings=%s usage=%s", top_k, len(sources), timings, usage)

        return response(200, {
            "response": answer,
            "sources": sources,
            "num_sources": len(sources),
            "timings_ms": timings,
            "usage": usage,
        })
    except Exception:
        logger.exception("Error in chat handler")
        return server_error(context)


def health_check(context):
    """GET /health: API Gateway -> Lambda -> database round trip, no Bedrock cost.

    Target of the external blackbox_exporter uptime probe.
    """
    t = time.perf_counter()
    try:
        execute_query("SELECT 1", fetch=True)
    except Exception:
        logger.exception("health check: database unreachable")
        return response(503, {"status": "unhealthy", "database": "unreachable"})
    return response(200, {"status": "ok", "database": "ok", "db_ms": _ms_since(t)})


def build_search_query(topic_id: Optional[int]) -> str:
    """SQL for nearest-neighbour search.

    pgvector's <=> operator returns cosine DISTANCE (0 = identical, 2 = opposite),
    so similarity is reported as 1 - distance and results are ordered by distance.
    """
    where = "WHERE p.topic_id = %s" if topic_id else ""
    return f"""
        SELECT
            p.abstract AS content,
            1 - (p.embedding <=> %s::vector) AS similarity,
            p.title AS paper_title,
            p.arxiv_id
        FROM papers p
        {where}
        ORDER BY p.embedding <=> %s::vector
        LIMIT %s
    """


def build_search_params(embedding_str: str, topic_id: Optional[int], top_k: int) -> Tuple:
    params: List[Any] = [embedding_str]
    if topic_id:
        params.append(topic_id)
    params.extend([embedding_str, top_k])
    return tuple(params)


def row_to_paper(row: Any) -> Dict[str, Any]:
    """Normalize a result row from psycopg2 (dict) or pg8000 (list/tuple)."""
    if isinstance(row, dict):
        content, similarity, title, arxiv_id = row["content"], row["similarity"], row["paper_title"], row["arxiv_id"]
    else:
        content, similarity, title, arxiv_id = row[0], row[1], row[2], row[3]
    return {
        "content": content,
        "similarity": float(similarity),
        "paper_title": title,
        "arxiv_id": arxiv_id,
        "url": f"https://arxiv.org/abs/{arxiv_id}",
    }


def search_similar_papers(query_embedding, topic_id: Optional[int] = None, top_k: int = 5) -> List[Dict[str, Any]]:
    """Return the top_k most similar papers (one title+abstract embedding per paper)."""
    embedding_str = to_pgvector(query_embedding)
    rows = execute_query(
        build_search_query(topic_id),
        build_search_params(embedding_str, topic_id, top_k),
        fetch=True,
    )
    return [row_to_paper(r) for r in rows]


def build_context(papers: List[Dict[str, Any]]) -> str:
    """Numbered source blocks the model can cite as [Source N]."""
    return "\n\n".join(
        f"[Source {i}: {p['paper_title']} (arXiv:{p['arxiv_id']})]\n{p['content']}"
        for i, p in enumerate(papers, 1)
    )


def build_messages(user_query: str, context: str) -> List[Dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "You answer questions about research papers using only the provided sources. "
                "Cite sources as [Source N]. If the sources do not contain the answer, say so."
            ),
        },
        {"role": "user", "content": f"Sources:\n{context}\n\nQuestion: {user_query}"},
    ]


def generate_answer(user_query: str, context: str) -> Tuple[str, Dict[str, int]]:
    """Generate a grounded answer with the gpt-oss model on Bedrock."""
    return generate_chat_response(build_messages(user_query, context))


def get_topic_id_by_name(topic_name: str) -> Optional[int]:
    rows = execute_query("SELECT id FROM topics WHERE name = %s", (topic_name,), fetch=True)
    if not rows:
        return None
    row = rows[0]
    if isinstance(row, dict):
        return row["id"]
    if isinstance(row, (list, tuple)):
        return row[0]
    return row


def _ms_since(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 1)

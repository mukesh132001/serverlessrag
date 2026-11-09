"""Ingest Lambda: fetch arXiv papers, embed title+abstract, store in pgvector."""
import logging
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any, Dict, List

from common.db_utils import get_or_create_topic, initialize_database, insert_paper, paper_exists
from common.http_utils import BadRequest, clamp_int, parse_body, require_text, response, server_error
from common import telemetry

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ARXIV_API_URL = "https://export.arxiv.org/api/query"
ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}
MAX_PAPERS = 50
ARXIV_TIMEOUT_SECONDS = 30


def _text(entry: ET.Element, tag: str, default: str = "") -> str:
    element = entry.find(tag, ATOM_NS)
    if element is None or element.text is None:
        return default
    return " ".join(element.text.split())  # collapse newlines and repeated spaces


def parse_arxiv_response(xml_content: str) -> List[Dict[str, Any]]:
    """Parse an arXiv Atom feed into paper dicts. Entries without an ID are skipped."""
    root = ET.fromstring(xml_content)
    papers = []
    for entry in root.findall("atom:entry", ATOM_NS):
        raw_id = _text(entry, "atom:id")
        if not raw_id:
            continue
        published = _text(entry, "atom:published")
        papers.append({
            "arxiv_id": raw_id.rstrip("/").split("/abs/")[-1],
            "title": _text(entry, "atom:title", "Untitled"),
            "authors": "; ".join(
                _text(author, "atom:name") for author in entry.findall("atom:author", ATOM_NS)
            ),
            "abstract": _text(entry, "atom:summary"),
            "published_date": published[:10] if published else None,
            "categories": ", ".join(
                c.get("term") for c in entry.findall("atom:category", ATOM_NS) if c.get("term")
            ),
        })
    return papers


def build_arxiv_url(query: str, max_results: int) -> str:
    params = {"search_query": f"all:{query}", "start": 0, "max_results": max_results}
    return f"{ARXIV_API_URL}?{urllib.parse.urlencode(params)}"


def fetch_arxiv_papers(query: str, max_results: int = 10) -> List[Dict[str, Any]]:
    url = build_arxiv_url(query, max_results)
    logger.info("Fetching from arXiv: %s", url)
    request = urllib.request.Request(url, headers={"User-Agent": "ServerlessRAG/1.0"})
    with urllib.request.urlopen(request, timeout=ARXIV_TIMEOUT_SECONDS) as resp:
        return parse_arxiv_response(resp.read().decode("utf-8"))


@telemetry.instrumented("/ingest")
def main(event, context):
    """
    POST /ingest
    {
        "query": "quantum error correction",
        "max_results": 10,              # optional, 1-50
        "topic_name": "Quantum Computing"  # optional, defaults to the query in title case
    }
    """
    started = time.perf_counter()
    try:
        body = parse_body(event)
        query = require_text(body.get("query"), "query", 300)
        max_results = clamp_int(body.get("max_results"), default=10, low=1, high=MAX_PAPERS, name="max_results")
        topic_name = require_text(body.get("topic_name") or query.title(), "topic_name", 255)
    except BadRequest as e:
        return response(400, {"error": str(e)})

    try:
        papers = fetch_arxiv_papers(query, max_results)
        if not papers:
            return response(404, {"error": "No papers found on arXiv for this query"})

        initialize_database()
        topic_id = get_or_create_topic(topic_name, f"Research papers about {topic_name}")

        inserted, skipped, failed = [], [], []
        for paper in papers:
            try:
                if paper_exists(paper["arxiv_id"]):
                    skipped.append(paper["arxiv_id"])
                    continue
                insert_paper(paper, topic_id)
                inserted.append(paper["arxiv_id"])
            except Exception:
                logger.exception("Failed to ingest %s", paper["arxiv_id"])
                failed.append(paper["arxiv_id"])

        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        logger.info("ingest ok topic=%s inserted=%s skipped=%s failed=%s ms=%s",
                    topic_name, len(inserted), len(skipped), len(failed), elapsed_ms)

        return response(200, {
            "message": f"Inserted {len(inserted)} new papers",
            "topic": topic_name,
            "total_papers_fetched": len(papers),
            "inserted_count": len(inserted),
            "skipped_duplicates": len(skipped),
            "failed_count": len(failed),
            "elapsed_ms": elapsed_ms,
            "papers": [
                {
                    "arxiv_id": p["arxiv_id"],
                    "title": p["title"],
                    "authors": p["authors"],
                    "published_date": p["published_date"],
                }
                for p in papers
            ],
        })
    except Exception:
        logger.exception("Error in ingest handler")
        return server_error(context)

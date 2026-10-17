#!/usr/bin/env python3
"""Turn saved /ingest responses into a template eval set you fill in by hand.

Usage:
    curl -s -X POST "$API/ingest" -H 'Content-Type: application/json' \
        -d '{"query": "retrieval augmented generation", "max_results": 20, "topic_name": "RAG"}' > eval/ingest_rag.json
    python eval/make_eval_template.py eval/ingest_*.json -o eval/eval_set.jsonl

Then open eval/eval_set.jsonl and write one question per paper in the "question" field.
"""
import argparse
import json
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("ingest_files", nargs="+", help="Saved JSON responses from POST /ingest")
    parser.add_argument("-o", "--output", default="eval/eval_set.jsonl")
    args = parser.parse_args()

    seen, rows = set(), []
    for path in args.ingest_files:
        with open(path) as f:
            data = json.load(f)
        topic = data.get("topic")
        for paper in data.get("papers", []):
            if paper["arxiv_id"] in seen:
                continue
            seen.add(paper["arxiv_id"])
            rows.append({
                "question": "",
                "relevant_arxiv_ids": [paper["arxiv_id"]],
                "topic": topic,
                "_paper_title": paper["title"],  # reference only; ignored by run_eval.py
            })

    with open(args.output, "w") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")
    print(f"Wrote {len(rows)} rows to {args.output}. Fill in each 'question' before running run_eval.py.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

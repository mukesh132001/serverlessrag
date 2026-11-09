#!/usr/bin/env python3
"""Run the Lambda handlers behind a local HTTP server that mimics API Gateway.

Useful for exercising the monitoring stack without deploying:

  # Real handlers: Bedrock in your AWS account + a local Postgres with pgvector
  LOCAL_DEV=1 DB_HOST=localhost DB_NAME=research DB_USER=postgres DB_PASSWORD=... \\
  OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 python3 scripts/local_api.py

  # Synthetic backends (no AWS, no database) to test dashboards and alerts.
  # Latencies and errors are SIMULATED, so never quote these numbers.
  OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 python3 scripts/local_api.py --fake --error-rate 0.01

Routes: POST /chat, POST /ingest, GET /health (same event shape as API Gateway proxy).
"""
import argparse
import importlib.util
import json
import os
import random
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def load(name):
    spec = importlib.util.spec_from_file_location(f"{name}_handler", os.path.join(ROOT, f"lambda_{name}", "handler.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeContext:
    def __init__(self):
        self.aws_request_id = str(uuid.uuid4())


def install_fakes(chat, ingest, error_rate, slow_rate):
    """Replace Bedrock and the database with simulated latency and failures."""
    def sleep_ms(mu, sigma):
        time.sleep(random.lognormvariate(mu, sigma) / 1000.0)

    def embed(texts):
        sleep_ms(4.4, 0.25)  # ~80 ms
        return [[0.0] * 1024 for _ in texts]

    def search(embedding, topic_id=None, top_k=5):
        sleep_ms(2.3, 0.4)  # ~10 ms
        return [{"content": "abstract", "similarity": 0.8, "paper_title": f"Paper {i}",
                 "arxiv_id": f"2401.{i:05d}v1", "url": "https://arxiv.org"} for i in range(top_k)]

    def answer(query, context):
        if random.random() < error_rate:
            raise RuntimeError("simulated Bedrock ThrottlingException")
        mu = 8.6 if random.random() < slow_rate else 7.5  # ~5.4 s vs ~1.8 s
        sleep_ms(mu, 0.3)
        return "simulated answer [Source 1]", {"prompt_tokens": random.randint(900, 1600), "completion_tokens": random.randint(80, 300)}

    chat.generate_embeddings = embed
    chat.search_similar_papers = search
    chat.generate_answer = answer
    chat.execute_query = lambda *a, **k: [(1,)]


def make_server(chat, ingest):
    routes = {("POST", "/chat"): chat.main, ("GET", "/health"): chat.main, ("POST", "/ingest"): ingest.main}

    class Handler(BaseHTTPRequestHandler):
        def _dispatch(self, method):
            path = self.path.split("?")[0].rstrip("/") or "/"
            fn = routes.get((method, path))
            if fn is None:
                self.send_response(404)
                self.end_headers()
                return
            length = int(self.headers.get("Content-Length") or 0)
            event = {"resource": path, "path": path, "httpMethod": method,
                     "body": self.rfile.read(length).decode() if length else None}
            try:
                result = fn(event, FakeContext())
            except Exception:
                # Real API Gateway returns 502 when the Lambda itself errors.
                result = {"statusCode": 502, "headers": {}, "body": json.dumps({"message": "Internal server error"})}
            body = (result.get("body") or "").encode()
            self.send_response(int(result.get("statusCode", 200)))
            for key, value in (result.get("headers") or {}).items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def log_message(self, fmt, *args):
            pass

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--fake", action="store_true", help="simulate Bedrock and the database")
    parser.add_argument("--error-rate", type=float, default=0.0, help="--fake: fraction of /chat calls that fail")
    parser.add_argument("--slow-rate", type=float, default=0.03, help="--fake: fraction of /chat calls that are slow")
    args = parser.parse_args()

    os.environ.setdefault("OTEL_SERVICE_NAME", "rag-local")
    if args.fake and importlib.util.find_spec("boto3") is None:
        import types  # --fake never calls AWS, so boto3 is optional
        sys.modules["boto3"] = types.SimpleNamespace(client=lambda *a, **k: None)
    chat, ingest = load("chat"), load("ingest")
    if args.fake:
        install_fakes(chat, ingest, args.error_rate, args.slow_rate)
        print("FAKE backends: latencies and errors are simulated")
    server = ThreadingHTTPServer(("0.0.0.0", args.port), make_server(chat, ingest))
    print(f"Local API on http://localhost:{args.port}  (POST /chat, POST /ingest, GET /health)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

# Changes

## Correctness
- **Similarity score:** pgvector's `<=>` returns cosine *distance*. The API now returns `1 − distance` as `similarity`, so higher means more similar, and orders results by distance.
- **Context formatting:** sources are separated by real newlines instead of a literal `\n`.
- **Vector index:** replaced the IVFFlat index built on an empty table with HNSW, which keeps recall high as papers are added. Schema setup is now idempotent.
- **arXiv ingestion:** HTTPS, a request timeout and User-Agent, cleaner whitespace handling, and a separate `parse_arxiv_response` function for testing. The response now lists all fetched papers with inserted, skipped, and failed counts.
- **Prompting:** a system message tells the model to answer only from the sources and cite `[Source N]`.
- Fixed stale comments that referred to Claude, and a README that used `max_papers` instead of `max_results`.

## Security
- Removed a pg8000 fallback that formatted values directly into SQL (SQL injection risk). Parameters are always bound by the driver.
- Closed SSH (port 22) to the internet and removed the key pair. Use Session Manager instead; IMDSv2 is required.
- API errors return a generic message and request ID instead of raw exception text.
- Input validation: required fields, length limits, and `top_k` / `max_results` clamped to safe ranges.
- API Gateway stage throttling; CORS origin configurable.
- The Chat UI instance now clones this repository (the URL is configurable) instead of a hard-coded external repo.

## Repository hygiene
- Removed about 14 MB of committed dependencies and compiled binaries. The Lambda layer is built at deploy time by `scripts/build_layer.sh`.
- One copy of `common/`; CDK copies it into each Lambda package at synth time.
- Removed the unused `openai` dependency, a debug script, and the unused `setup-chatui.sh`.
- Fixed CDK dependencies: CLI version compatible with `aws-cdk-lib`, and `ts-node` added.
- `deploy.sh` reads the correct stack output names. Added `.gitignore` and `.gitattributes`, and normalized line endings to LF.

## New
- Per-stage timings and token usage in every chat response.
- `retrieval_only` mode for cheap retrieval evaluation.
- `eval/`: metrics (hit rate@k, MRR, recall@k, p50/p95 latency, LLM cost), an evaluation runner, and an eval-set template builder.
- `tests/`: 30 offline unit tests.

## Observability
- OpenTelemetry metrics in both Lambdas (`common/telemetry.py`): request latency by route and status, per-stage latency, LLM tokens, cold starts. Flushed at the end of each invocation; a no-op when no collector is configured.
- `monitoring/`: OTel Collector, Prometheus, Grafana (RED and SLO dashboards), Alertmanager and blackbox_exporter in Docker Compose.
- Two SLOs on `POST /chat`, 99.5% non-5xx and 95% of successful requests under 4 s, with 30-day error budgets and multi-window burn-rate alerts (14.4x/6x page, 3x/1x ticket), unit-tested with `promtool test rules`.
- `GET /health` endpoint and an external uptime probe against it.
- CDK: optional monitoring instance (`-c monitoring=true`), CloudWatch alarms (Lambda errors and throttles, API 5XX rate, API p95 latency) with an SNS topic, and a CloudWatch dashboard.
- `scripts/local_api.py`, `scripts/load_test.py` and `scripts/slo_report.py` to generate traffic and read measured SLO numbers from Prometheus.

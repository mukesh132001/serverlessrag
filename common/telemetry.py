"""OpenTelemetry metrics for the Lambda handlers.

Metrics are pushed over OTLP/HTTP to an OpenTelemetry Collector, which exposes
them to Prometheus. Lambda freezes the container as soon as the handler
returns, so the meter provider is flushed at the end of every invocation
instead of relying on a background export interval.

Telemetry is optional and never breaks a request:
- If OTEL_EXPORTER_OTLP_ENDPOINT is unset, or the OpenTelemetry SDK is not
  installed, every function here is a no-op.
- Export errors are logged and swallowed.

Metrics (Prometheus names after the collector's prometheus exporter):
    rag_request_duration_seconds   histogram  {route, method, status_code, status_class}
    rag_stage_duration_seconds     histogram  {route, stage}  embedding / retrieval / generation
    rag_llm_tokens_total           counter    {type}          prompt / completion
    rag_cold_starts_total          counter    {}
"""
import functools
import logging
import os
import time
import uuid
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

# Bucket boundaries in seconds. They include the SLO latency thresholds
# (see monitoring/prometheus/rules) so threshold queries are exact, and they
# cover the long tail of an LLM call.
LATENCY_BUCKETS_S = (0.025, 0.05, 0.1, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 7.5, 10.0, 15.0, 30.0, 60.0)

FLUSH_TIMEOUT_MS = int(os.getenv("OTEL_FLUSH_TIMEOUT_MS", "1000"))

_state: Dict[str, Any] = {"initialized": False, "provider": None, "instruments": None}
_cold_start = True


def _init() -> Optional[Dict[str, Any]]:
    """Create the meter provider once per container. Returns instruments or None."""
    if _state["initialized"]:
        return _state["instruments"]
    _state["initialized"] = True

    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        logger.info("telemetry disabled: OTEL_EXPORTER_OTLP_ENDPOINT not set")
        return None

    try:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
    except ImportError:
        logger.warning("telemetry disabled: opentelemetry SDK not installed")
        return None

    service = os.getenv("OTEL_SERVICE_NAME") or os.getenv("AWS_LAMBDA_FUNCTION_NAME") or "serverlessrag"
    resource = Resource.create({
        "service.name": service,
        "service.namespace": "serverlessrag",
        # One id per Lambda container: each container reports its own cumulative
        # series and dashboards sum across them.
        "service.instance.id": uuid.uuid4().hex[:12],
        "cloud.provider": "aws",
        "faas.name": os.getenv("AWS_LAMBDA_FUNCTION_NAME", "local"),
    })
    exporter = OTLPMetricExporter(
        endpoint=endpoint.rstrip("/") + "/v1/metrics",
        timeout=float(os.getenv("OTEL_EXPORTER_TIMEOUT_S", "2")),
    )
    # Long interval: exports happen through force_flush() at the end of each invocation.
    reader = PeriodicExportingMetricReader(exporter, export_interval_millis=10 * 60 * 1000)
    provider = MeterProvider(resource=resource, metric_readers=[reader], views=latency_views())
    instruments = _create_instruments(provider)
    _state["provider"] = provider
    _state["instruments"] = instruments
    logger.info("telemetry enabled: exporting to %s as %s", endpoint, service)
    return instruments


def latency_views():
    from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
    aggregation = ExplicitBucketHistogramAggregation(boundaries=LATENCY_BUCKETS_S)
    return [
        View(instrument_name="rag.request.duration", aggregation=aggregation),
        View(instrument_name="rag.stage.duration", aggregation=aggregation),
    ]


def _create_instruments(provider) -> Dict[str, Any]:
    meter = provider.get_meter("serverlessrag", "1.0.0")
    return {
        "request_duration": meter.create_histogram(
            "rag.request.duration", unit="s", description="End-to-end handler latency"),
        "stage_duration": meter.create_histogram(
            "rag.stage.duration", unit="s", description="Latency of one RAG pipeline stage"),
        "tokens": meter.create_counter(
            "rag.llm.tokens", description="LLM tokens used"),
        "cold_starts": meter.create_counter(
            "rag.cold_starts", description="Lambda cold starts"),
    }


def status_class(status_code: int) -> str:
    return f"{status_code // 100}xx"


def route_of(event: Any, default: str) -> str:
    """API Gateway resource path (e.g. /chat), or the default for direct invokes."""
    if isinstance(event, dict) and event.get("resource"):
        return str(event["resource"])
    return default


def record_stages(route: str, timings_ms: Dict[str, float]) -> None:
    """Record per-stage latencies from a handler's timings dict (keys like 'embedding_ms')."""
    instruments = _init()
    if not instruments:
        return
    for key, ms in timings_ms.items():
        if key == "total_ms" or not key.endswith("_ms"):
            continue
        instruments["stage_duration"].record(ms / 1000.0, {"route": route, "stage": key[:-3]})


def record_tokens(usage: Dict[str, int]) -> None:
    instruments = _init()
    if not instruments:
        return
    for kind in ("prompt", "completion"):
        count = int(usage.get(f"{kind}_tokens", 0) or 0)
        if count:
            instruments["tokens"].add(count, {"type": kind})


def flush() -> None:
    provider = _state["provider"]
    if provider is None:
        return
    try:
        provider.force_flush(timeout_millis=FLUSH_TIMEOUT_MS)
    except Exception:  # never fail a request because telemetry is down
        logger.warning("telemetry flush failed", exc_info=True)


def instrumented(default_route: str) -> Callable:
    """Decorator for a Lambda handler: records request latency and status, then flushes.

    An exception that escapes the handler is counted as a 500 and re-raised.
    """
    def decorator(handler: Callable) -> Callable:
        @functools.wraps(handler)
        def wrapper(event, context):
            global _cold_start
            instruments = _init()
            started = time.perf_counter()
            status_code = 500
            try:
                result = handler(event, context)
                if isinstance(result, dict):
                    status_code = int(result.get("statusCode", 200))
                return result
            finally:
                if instruments:
                    try:
                        attributes = {
                            "route": route_of(event, default_route),
                            "method": (event or {}).get("httpMethod", "POST") if isinstance(event, dict) else "POST",
                            "status_code": str(status_code),
                            "status_class": status_class(status_code),
                        }
                        instruments["request_duration"].record(time.perf_counter() - started, attributes)
                        if _cold_start:
                            instruments["cold_starts"].add(1)
                    except Exception:
                        logger.warning("telemetry record failed", exc_info=True)
                    flush()
                _cold_start = False
        return wrapper
    return decorator


def _install_for_tests(provider) -> None:
    """Use a caller-supplied MeterProvider (e.g. with an InMemoryMetricReader)."""
    global _cold_start
    _state.update({"initialized": True, "provider": provider, "instruments": _create_instruments(provider)})
    _cold_start = True


def _reset_for_tests() -> None:
    global _cold_start
    _state.update({"initialized": False, "provider": None, "instruments": None})
    _cold_start = True

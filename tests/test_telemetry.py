import json
import os
import unittest
from unittest import mock

from tests._setup import load_handler

from common import telemetry

try:
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader
    HAVE_SDK = True
except ImportError:  # SDK is optional for the rest of the test suite
    HAVE_SDK = False

chat = load_handler("chat")


def collect(reader):
    """{metric_name: [data_point, ...]} from an InMemoryMetricReader."""
    out = {}
    data = reader.get_metrics_data()
    for rm in data.resource_metrics if data else []:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                out.setdefault(metric.name, []).extend(metric.data.data_points)
    return out


class DisabledTelemetryTests(unittest.TestCase):
    def setUp(self):
        telemetry._reset_for_tests()

    def tearDown(self):
        telemetry._reset_for_tests()

    def test_noop_without_endpoint(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
            wrapped = telemetry.instrumented("/x")(lambda e, c: {"statusCode": 204})
            self.assertEqual(wrapped({}, None), {"statusCode": 204})
            telemetry.record_stages("/x", {"embedding_ms": 5})
            telemetry.record_tokens({"prompt_tokens": 3})
            self.assertIsNone(telemetry._state["provider"])

    def test_helpers(self):
        self.assertEqual(telemetry.status_class(503), "5xx")
        self.assertEqual(telemetry.route_of({"resource": "/chat"}, "/d"), "/chat")
        self.assertEqual(telemetry.route_of({}, "/d"), "/d")
        self.assertEqual(telemetry.route_of(None, "/d"), "/d")

    def test_slo_thresholds_are_bucket_boundaries(self):
        # Latency SLO queries read the le="2.5" and le="4" buckets directly.
        self.assertIn(2.5, telemetry.LATENCY_BUCKETS_S)
        self.assertIn(4.0, telemetry.LATENCY_BUCKETS_S)


@unittest.skipUnless(HAVE_SDK, "opentelemetry-sdk not installed")
class EnabledTelemetryTests(unittest.TestCase):
    def setUp(self):
        self.reader = InMemoryMetricReader()
        telemetry._install_for_tests(MeterProvider(metric_readers=[self.reader], views=telemetry.latency_views()))

    def tearDown(self):
        telemetry._reset_for_tests()

    def test_records_status_route_and_cold_start(self):
        handler = telemetry.instrumented("/chat")(lambda e, c: {"statusCode": 400, "body": "{}"})
        handler({"resource": "/chat", "httpMethod": "POST"}, None)
        handler({"resource": "/chat", "httpMethod": "POST"}, None)
        metrics = collect(self.reader)
        [point] = metrics["rag.request.duration"]
        self.assertEqual(point.count, 2)
        self.assertEqual(dict(point.attributes), {
            "route": "/chat", "method": "POST", "status_code": "400", "status_class": "4xx"})
        self.assertEqual(tuple(point.explicit_bounds), telemetry.LATENCY_BUCKETS_S)
        [cold] = metrics["rag.cold_starts"]
        self.assertEqual(cold.value, 1)  # only the first invocation in a container

    def test_unhandled_exception_counts_as_500_and_reraises(self):
        def boom(event, context):
            raise RuntimeError("x")
        with self.assertRaises(RuntimeError):
            telemetry.instrumented("/ingest")(boom)({}, None)
        [point] = collect(self.reader)["rag.request.duration"]
        self.assertEqual(point.attributes["status_code"], "500")
        self.assertEqual(point.attributes["route"], "/ingest")

    def test_chat_records_stages_and_tokens(self):
        papers = [{"content": "c", "similarity": 0.9, "paper_title": "t", "arxiv_id": "1", "url": "u"}]
        with mock.patch.object(chat, "generate_embeddings", return_value=[[0.1]]), \
                mock.patch.object(chat, "search_similar_papers", return_value=papers), \
                mock.patch.object(chat, "generate_answer", return_value=("a [Source 1]", {"prompt_tokens": 120, "completion_tokens": 30})):
            result = chat.main({"resource": "/chat", "httpMethod": "POST", "body": json.dumps({"query": "q"})}, None)
        self.assertEqual(result["statusCode"], 200)
        metrics = collect(self.reader)
        stages = {p.attributes["stage"] for p in metrics["rag.stage.duration"]}
        self.assertEqual(stages, {"embedding", "retrieval", "generation"})
        tokens = {p.attributes["type"]: p.value for p in metrics["rag.llm.tokens"]}
        self.assertEqual(tokens, {"prompt": 120, "completion": 30})

    def test_health_check(self):
        with mock.patch.object(chat, "execute_query", return_value=[(1,)]):
            ok = chat.main({"resource": "/health", "httpMethod": "GET"}, None)
        with mock.patch.object(chat, "execute_query", side_effect=Exception("db down")):
            down = chat.main({"resource": "/health", "httpMethod": "GET"}, None)
        self.assertEqual(ok["statusCode"], 200)
        self.assertEqual(json.loads(ok["body"])["status"], "ok")
        self.assertEqual(down["statusCode"], 503)
        codes = {p.attributes["status_code"]: p.count for p in collect(self.reader)["rag.request.duration"]}
        self.assertEqual(codes, {"200": 1, "503": 1})


if __name__ == "__main__":
    unittest.main()

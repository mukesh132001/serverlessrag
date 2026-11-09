"""Monitoring config stays consistent with the metrics the code emits."""
import importlib.util
import json
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MON = os.path.join(ROOT, "monitoring")

try:
    import yaml
except ImportError:
    yaml = None


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


EMITTED = {
    "rag_request_duration_seconds_bucket", "rag_request_duration_seconds_count", "rag_request_duration_seconds_sum",
    "rag_stage_duration_seconds_bucket", "rag_llm_tokens_total", "rag_cold_starts_total",
}


class DashboardTests(unittest.TestCase):
    def test_dashboards_are_up_to_date(self):
        builder = load_module(os.path.join(MON, "grafana", "build_dashboards.py"), "build_dashboards")
        for board in (builder.red_dashboard(), builder.slo_dashboard()):
            with open(os.path.join(MON, "grafana", "dashboards", f"{board['uid']}.json")) as f:
                committed = json.load(f)
            self.assertEqual(len(committed["panels"]), len(board["panels"]),
                             "run monitoring/grafana/build_dashboards.py")

    def test_dashboard_metrics_exist(self):
        for name in ("serverlessrag-red", "serverlessrag-slo"):
            with open(os.path.join(MON, "grafana", "dashboards", f"{name}.json")) as f:
                text = f.read()
            for metric in set(re.findall(r"\brag_[a-z_]+", text)):
                self.assertIn(metric, EMITTED, f"{name} uses {metric}, which common/telemetry.py does not emit")


@unittest.skipUnless(yaml, "pyyaml not installed")
class SloRuleTests(unittest.TestCase):
    def test_rules_file_is_up_to_date(self):
        gen = load_module(os.path.join(MON, "prometheus", "generate_slo_rules.py"), "generate_slo_rules")
        with open(os.path.join(MON, "prometheus", "rules", "slo.yml")) as f:
            committed = yaml.safe_load(f)
        self.assertEqual(committed, gen.build(), "run monitoring/prometheus/generate_slo_rules.py")

    def test_burn_rate_thresholds(self):
        gen = load_module(os.path.join(MON, "prometheus", "generate_slo_rules.py"), "generate_slo_rules")
        alerts = [r for g in gen.build()["groups"] for r in g["rules"] if "alert" in r]
        page = [a for a in alerts if a["labels"]["slo"] == "chat-availability" and a["labels"]["burn_rate"] == "14.4"][0]
        self.assertIn("> 0.072", page["expr"])  # 14.4 x 0.5% budget
        self.assertEqual(len(alerts), 8)  # 4 windows x 2 SLOs

    def test_latency_threshold_is_a_bucket(self):
        from common import telemetry
        gen = load_module(os.path.join(MON, "prometheus", "generate_slo_rules.py"), "generate_slo_rules")
        for slo in gen.SLOS:
            if "threshold_seconds" in slo:
                self.assertIn(float(slo["threshold_seconds"]), telemetry.LATENCY_BUCKETS_S)


if __name__ == "__main__":
    unittest.main()

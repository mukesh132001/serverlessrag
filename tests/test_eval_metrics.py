import unittest

import tests._setup  # noqa: F401
from eval import metrics


class MetricsTests(unittest.TestCase):
    def test_version_suffix_is_ignored(self):
        self.assertEqual(metrics.first_relevant_rank(["2401.1v1", "2005.11401v4"], ["2005.11401"]), 2)
        self.assertIsNone(metrics.first_relevant_rank(["a", "b"], ["c"]))

    def test_retrieval_summary(self):
        results = [
            {"retrieved": ["a", "b", "c"], "relevant": ["a"]},        # rank 1
            {"retrieved": ["x", "y", "b", "z", "w"], "relevant": ["b"]},  # rank 3
            {"retrieved": ["x", "y", "z"], "relevant": ["q"]},        # miss
            {"retrieved": ["x", "p", "q"], "relevant": ["p", "q"]},   # rank 2, both found
        ]
        s = metrics.retrieval_summary(results, ks=(1, 3, 5))
        self.assertEqual(s["queries"], 4)
        self.assertAlmostEqual(s["hit_rate@1"], 0.25)
        self.assertAlmostEqual(s["hit_rate@3"], 0.75)
        self.assertAlmostEqual(s["mrr@5"], (1 + 1 / 3 + 0 + 1 / 2) / 4)
        self.assertAlmostEqual(s["recall@5"], (1 + 1 + 0 + 1) / 4)

    def test_percentiles(self):
        values = list(range(1, 101))
        self.assertEqual(metrics.percentile(values, 50), 50)
        self.assertEqual(metrics.percentile(values, 95), 95)
        self.assertEqual(metrics.latency_summary([100, 200, 300])["p50_ms"], 200)

    def test_token_cost(self):
        self.assertAlmostEqual(metrics.token_cost(1000, 500, 0.1, 0.4), 0.1 + 0.2)


if __name__ == "__main__":
    unittest.main()

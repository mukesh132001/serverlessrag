import unittest
from unittest import mock

import tests._setup  # noqa: F401  (path + boto3 stub)
from common import bedrock_utils, db_utils


class FakePg8000Connection:
    def __init__(self):
        self.calls = []

    def run(self, query, **params):
        self.calls.append((query, params))
        return [[1]]


class DbUtilsTests(unittest.TestCase):
    def test_to_pgvector(self):
        self.assertEqual(db_utils.to_pgvector([0.5, -1.0, 2]), "[0.5,-1.0,2]")

    def test_pg8000_placeholders_are_bound_not_formatted(self):
        conn = FakePg8000Connection()
        value = "x'); DROP TABLE papers;--"
        db_utils._execute_pg8000(conn, "SELECT id FROM papers WHERE arxiv_id = %s AND topic_id = %s",
                                 (value, 3), fetch=True)
        query, params = conn.calls[0]
        self.assertEqual(query, "SELECT id FROM papers WHERE arxiv_id = :1 AND topic_id = :2")
        self.assertEqual(params, {"1": value, "2": 3})
        self.assertNotIn("DROP TABLE", query)

    def test_pg8000_errors_propagate_without_fallback(self):
        conn = mock.Mock()
        conn.run.side_effect = RuntimeError("syntax error")
        with self.assertRaises(RuntimeError):
            db_utils._execute_pg8000(conn, "SELECT %s", ("a",), fetch=True)
        self.assertEqual(conn.run.call_count, 1)

    def test_schema_uses_hnsw_index(self):
        schema = " ".join(db_utils.SCHEMA_STATEMENTS)
        self.assertIn("USING hnsw (embedding vector_cosine_ops)", schema)
        self.assertNotIn("ivfflat", schema.lower())


class BedrockUtilsTests(unittest.TestCase):
    def test_parse_chat_result_with_usage(self):
        result = {"choices": [{"message": {"content": "hi"}}],
                  "usage": {"prompt_tokens": 10, "completion_tokens": 3}}
        self.assertEqual(bedrock_utils.parse_chat_result(result),
                         ("hi", {"prompt_tokens": 10, "completion_tokens": 3}))

    def test_parse_chat_result_without_usage(self):
        content, usage = bedrock_utils.parse_chat_result({"choices": [{"message": {"content": "hi"}}]})
        self.assertEqual(usage, {"prompt_tokens": 0, "completion_tokens": 0})


if __name__ == "__main__":
    unittest.main()

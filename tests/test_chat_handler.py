import json
import unittest
from unittest import mock

from tests._setup import load_handler

chat = load_handler("chat")


def event(body):
    return {"body": json.dumps(body)}


PAPERS = [
    {"content": "Abstract A", "similarity": 0.91, "paper_title": "Paper A", "arxiv_id": "2401.00001v1",
     "url": "https://arxiv.org/abs/2401.00001v1"},
    {"content": "Abstract B", "similarity": 0.85, "paper_title": "Paper B", "arxiv_id": "2401.00002v2",
     "url": "https://arxiv.org/abs/2401.00002v2"},
]


class SearchQueryTests(unittest.TestCase):
    def test_similarity_is_one_minus_cosine_distance(self):
        sql = chat.build_search_query(topic_id=None)
        self.assertIn("1 - (p.embedding <=> %s::vector) AS similarity", sql)
        self.assertIn("ORDER BY p.embedding <=> %s::vector", sql)
        self.assertNotIn("WHERE", sql)

    def test_topic_filter_adds_where_and_param(self):
        sql = chat.build_search_query(topic_id=7)
        self.assertIn("WHERE p.topic_id = %s", sql)
        self.assertEqual(chat.build_search_params("[0.1]", 7, 5), ("[0.1]", 7, "[0.1]", 5))
        self.assertEqual(chat.build_search_params("[0.1]", None, 5), ("[0.1]", "[0.1]", 5))
        self.assertEqual(sql.count("%s"), len(chat.build_search_params("[0.1]", 7, 5)))

    def test_row_to_paper_handles_dict_and_tuple_rows(self):
        from_dict = chat.row_to_paper({"content": "c", "similarity": "0.5", "paper_title": "t", "arxiv_id": "1"})
        from_tuple = chat.row_to_paper(("c", 0.5, "t", "1"))
        self.assertEqual(from_dict, from_tuple)
        self.assertEqual(from_dict["similarity"], 0.5)


class ContextTests(unittest.TestCase):
    def test_context_uses_real_newlines(self):
        context = chat.build_context(PAPERS)
        self.assertNotIn("\\n", context)
        self.assertIn("[Source 1: Paper A (arXiv:2401.00001v1)]\nAbstract A", context)
        self.assertIn("\n\n[Source 2: Paper B", context)

    def test_messages_ground_the_model_in_sources(self):
        messages = chat.build_messages("What is X?", "ctx")
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("only the provided sources", messages[0]["content"])
        self.assertIn("Question: What is X?", messages[1]["content"])


class HandlerTests(unittest.TestCase):
    def test_missing_query_returns_400(self):
        result = chat.main(event({"top_k": 5}), None)
        self.assertEqual(result["statusCode"], 400)

    def test_invalid_json_returns_400(self):
        self.assertEqual(chat.main({"body": "{not json"}, None)["statusCode"], 400)

    def test_top_k_is_clamped(self):
        with mock.patch.object(chat, "generate_embeddings", return_value=[[0.0]]), \
             mock.patch.object(chat, "search_similar_papers", return_value=[]) as search:
            chat.main(event({"query": "q", "top_k": 500}), None)
        self.assertEqual(search.call_args.kwargs["top_k"], chat.MAX_TOP_K)

    def test_retrieval_only_skips_llm(self):
        with mock.patch.object(chat, "generate_embeddings", return_value=[[0.0]]), \
             mock.patch.object(chat, "search_similar_papers", return_value=PAPERS), \
             mock.patch.object(chat, "generate_answer") as llm:
            result = chat.main(event({"query": "q", "retrieval_only": True}), None)
        llm.assert_not_called()
        body = json.loads(result["body"])
        self.assertEqual(result["statusCode"], 200)
        self.assertIsNone(body["response"])
        self.assertEqual([s["arxiv_id"] for s in body["sources"]], ["2401.00001v1", "2401.00002v2"])
        self.assertIn("retrieval_ms", body["timings_ms"])

    def test_full_mode_returns_answer_usage_and_timings(self):
        usage = {"prompt_tokens": 900, "completion_tokens": 120}
        with mock.patch.object(chat, "generate_embeddings", return_value=[[0.0]]), \
             mock.patch.object(chat, "search_similar_papers", return_value=PAPERS), \
             mock.patch.object(chat, "generate_answer", return_value=("Answer [Source 1]", usage)):
            body = json.loads(chat.main(event({"query": "q"}), None)["body"])
        self.assertEqual(body["response"], "Answer [Source 1]")
        self.assertEqual(body["usage"], usage)
        self.assertEqual(body["num_sources"], 2)
        for key in ("embedding_ms", "retrieval_ms", "generation_ms", "total_ms"):
            self.assertIn(key, body["timings_ms"])

    def test_no_results_message(self):
        with mock.patch.object(chat, "generate_embeddings", return_value=[[0.0]]), \
             mock.patch.object(chat, "search_similar_papers", return_value=[]):
            body = json.loads(chat.main(event({"query": "q"}), None)["body"])
        self.assertEqual(body["response"], chat.NO_RESULTS_MESSAGE)

    def test_unknown_topic_returns_404(self):
        with mock.patch.object(chat, "get_topic_id_by_name", return_value=None):
            result = chat.main(event({"query": "q", "topic": "Nope"}), None)
        self.assertEqual(result["statusCode"], 404)

    def test_internal_errors_are_not_leaked(self):
        ctx = mock.Mock(aws_request_id="req-123")
        with mock.patch.object(chat, "generate_embeddings", side_effect=RuntimeError("secret db host 10.0.0.5")):
            result = chat.main(event({"query": "q"}), ctx)
        self.assertEqual(result["statusCode"], 500)
        self.assertNotIn("10.0.0.5", result["body"])
        self.assertEqual(json.loads(result["body"])["request_id"], "req-123")


if __name__ == "__main__":
    unittest.main()

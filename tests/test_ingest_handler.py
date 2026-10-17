import json
import unittest
from unittest import mock

from tests._setup import load_handler

ingest = load_handler("ingest")

SAMPLE_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2005.11401v4</id>
    <published>2020-05-22T17:58:00Z</published>
    <title>Retrieval-Augmented Generation for
      Knowledge-Intensive NLP Tasks</title>
    <summary>  Large pre-trained language models
      have been shown to store factual knowledge.  </summary>
    <author><name>Patrick Lewis</name></author>
    <author><name>Ethan Perez</name></author>
    <category term="cs.CL"/>
    <category term="cs.LG"/>
  </entry>
  <entry>
    <title>Entry without an id is skipped</title>
  </entry>
</feed>"""


class ParseTests(unittest.TestCase):
    def setUp(self):
        self.papers = ingest.parse_arxiv_response(SAMPLE_FEED)

    def test_skips_entries_without_id(self):
        self.assertEqual(len(self.papers), 1)

    def test_fields_are_cleaned(self):
        paper = self.papers[0]
        self.assertEqual(paper["arxiv_id"], "2005.11401v4")
        self.assertEqual(paper["title"], "Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks")
        self.assertEqual(paper["abstract"], "Large pre-trained language models have been shown to store factual knowledge.")
        self.assertEqual(paper["authors"], "Patrick Lewis; Ethan Perez")
        self.assertEqual(paper["categories"], "cs.CL, cs.LG")
        self.assertEqual(paper["published_date"], "2020-05-22")

    def test_url_uses_https_and_encodes_query(self):
        url = ingest.build_arxiv_url("quantum error correction", 5)
        self.assertTrue(url.startswith("https://export.arxiv.org/api/query?"))
        self.assertIn("search_query=all%3Aquantum+error+correction", url)
        self.assertIn("max_results=5", url)


class HandlerTests(unittest.TestCase):
    def test_missing_query_returns_400(self):
        self.assertEqual(ingest.main({"body": "{}"}, None)["statusCode"], 400)

    def test_skips_duplicates_and_reports_counts(self):
        papers = ingest.parse_arxiv_response(SAMPLE_FEED) * 2
        papers[1] = dict(papers[1], arxiv_id="2005.99999v1")
        with mock.patch.object(ingest, "fetch_arxiv_papers", return_value=papers), \
             mock.patch.object(ingest, "initialize_database"), \
             mock.patch.object(ingest, "get_or_create_topic", return_value=1), \
             mock.patch.object(ingest, "paper_exists", side_effect=[True, False]), \
             mock.patch.object(ingest, "insert_paper") as insert:
            result = ingest.main({"body": json.dumps({"query": "rag", "max_results": 999})}, None)
        body = json.loads(result["body"])
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual((body["inserted_count"], body["skipped_duplicates"], body["failed_count"]), (1, 1, 0))
        self.assertEqual(len(body["papers"]), 2)
        insert.assert_called_once()

    def test_max_results_is_clamped(self):
        with mock.patch.object(ingest, "fetch_arxiv_papers", return_value=[]) as fetch:
            ingest.main({"body": json.dumps({"query": "rag", "max_results": 999})}, None)
        self.assertEqual(fetch.call_args.args[1], ingest.MAX_PAPERS)

    def test_internal_errors_are_not_leaked(self):
        with mock.patch.object(ingest, "fetch_arxiv_papers", side_effect=RuntimeError("password=hunter2")):
            result = ingest.main({"body": json.dumps({"query": "rag"})}, None)
        self.assertEqual(result["statusCode"], 500)
        self.assertNotIn("hunter2", result["body"])


if __name__ == "__main__":
    unittest.main()

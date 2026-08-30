import unittest
from pathlib import Path
from unittest.mock import patch

from paper_service.reader_issue_backfill import backfill_reader_issues


class ReaderIssueBackfillTests(unittest.TestCase):
    @patch("paper_service.reader_issue_backfill.import_article_warnings")
    @patch("paper_service.reader_issue_backfill.build_selector_index")
    @patch("paper_service.reader_issue_backfill.read_article")
    @patch("paper_service.reader_issue_backfill.list_reader_v2_ready_ids")
    def test_imports_each_ready_article_without_names_or_content_output(
        self, ready_ids, read_article, selector_index, import_warnings
    ):
        ready_ids.return_value = [2, 7]
        read_article.return_value = {
            "warnings": ["figure_asset_missing:fig-1"],
        }
        selector_index.return_value.article_fingerprint = "a" * 64
        import_warnings.return_value = [{"id": "warning"}]

        result = backfill_reader_issues(Path("/data"))

        self.assertEqual(
            result,
            {
                "papers_seen": 2,
                "papers_imported": 2,
                "warnings_seen": 2,
                "failed": 0,
            },
        )
        self.assertEqual(import_warnings.call_count, 2)

    @patch("paper_service.reader_issue_backfill.import_article_warnings")
    @patch("paper_service.reader_issue_backfill.build_selector_index")
    @patch("paper_service.reader_issue_backfill.read_article")
    @patch("paper_service.reader_issue_backfill.list_reader_v2_ready_ids")
    def test_one_bad_artifact_does_not_skip_remaining_papers(
        self, ready_ids, read_article, selector_index, import_warnings
    ):
        ready_ids.return_value = [2, 7]
        read_article.side_effect = [FileNotFoundError(), {"warnings": []}]
        selector_index.return_value.article_fingerprint = "b" * 64
        import_warnings.return_value = []

        result = backfill_reader_issues(Path("/data"))

        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["papers_imported"], 1)


if __name__ == "__main__":
    unittest.main()

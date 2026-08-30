from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import call, patch

from paper_service.search_index_runner import execute_cycle, run_worker
from paper_service.search_store import SearchIndexNotReady


def rebuild_result(paper_id: int, *, reused: bool) -> dict:
    return {
        "paper_id": paper_id,
        "generation_id": "unused-in-runner-result",
        "corpus_digest": "a" * 64,
        "document_count": 4,
        "chunk_count": 8,
        "reused_generation": reused,
    }


class Heartbeat:
    def __init__(self):
        self.events = []

    def __enter__(self):
        self.events.append(("enter",))
        return self

    def __exit__(self, *_args):
        self.events.append(("exit",))

    def processing(self, paper_id):
        self.events.append(("processing", paper_id))

    def idle(self, *, successful=False):
        self.events.append(("idle", successful))


class SearchIndexRunnerTests(unittest.TestCase):
    def test_empty_ready_set_is_idle(self):
        with TemporaryDirectory() as directory, patch(
            "paper_service.search_index_runner.list_reader_v2_ready_ids",
            return_value=[],
        ), patch(
            "paper_service.search_index_runner.rebuild_paper_search_index"
        ) as rebuild:
            result = execute_cycle(data_dir=directory)

        self.assertEqual(result.status, "idle")
        self.assertEqual(result.seen, 0)
        self.assertEqual(result.papers, ())
        rebuild.assert_not_called()

    def test_cycle_uses_deterministic_generation_reuse(self):
        heartbeat = Heartbeat()
        with TemporaryDirectory() as directory, patch(
            "paper_service.search_index_runner.list_reader_v2_ready_ids",
            return_value=[2, 7],
        ), patch(
            "paper_service.search_index_runner.rebuild_paper_search_index",
            side_effect=(
                rebuild_result(2, reused=False),
                rebuild_result(7, reused=True),
            ),
        ) as rebuild:
            result = execute_cycle(data_dir=directory, heartbeat=heartbeat)

        self.assertEqual(result.status, "succeeded")
        self.assertEqual((result.seen, result.rebuilt, result.reused, result.failed), (2, 1, 1, 0))
        self.assertEqual(
            [paper.status for paper in result.papers], ["rebuilt", "reused"]
        )
        rebuild.assert_has_calls(
            [call(2, Path(directory)), call(7, Path(directory))]
        )
        self.assertEqual(
            heartbeat.events,
            [
                ("processing", 2),
                ("idle", True),
                ("processing", 7),
                ("idle", True),
            ],
        )

    def test_one_paper_failure_does_not_block_later_papers(self):
        secret = "private annotation body must not escape"
        heartbeat = Heartbeat()
        with TemporaryDirectory() as directory, patch(
            "paper_service.search_index_runner.list_reader_v2_ready_ids",
            return_value=[1, 2, 3],
        ), patch(
            "paper_service.search_index_runner.rebuild_paper_search_index",
            side_effect=(
                SearchIndexNotReady("reader_v2_invalid"),
                RuntimeError(secret),
                rebuild_result(3, reused=True),
            ),
        ) as rebuild:
            result = execute_cycle(data_dir=directory, heartbeat=heartbeat)

        self.assertEqual(result.status, "partial")
        self.assertEqual((result.seen, result.rebuilt, result.reused, result.failed), (3, 0, 1, 2))
        self.assertEqual(
            [paper.error_code for paper in result.papers],
            ["reader_v2_invalid", "search_index_rebuild_failed", None],
        )
        self.assertNotIn(secret, repr(result))
        self.assertEqual([item.args[0] for item in rebuild.call_args_list], [1, 2, 3])
        self.assertEqual(
            heartbeat.events,
            [
                ("processing", 1),
                ("idle", False),
                ("processing", 2),
                ("idle", False),
                ("processing", 3),
                ("idle", True),
            ],
        )

    def test_invalid_rebuild_result_is_isolated_like_a_paper_failure(self):
        with TemporaryDirectory() as directory, patch(
            "paper_service.search_index_runner.list_reader_v2_ready_ids",
            return_value=[4, 5],
        ), patch(
            "paper_service.search_index_runner.rebuild_paper_search_index",
            side_effect=(
                {"paper_id": 99, "reused_generation": False},
                rebuild_result(5, reused=False),
            ),
        ):
            result = execute_cycle(data_dir=directory)

        self.assertEqual(result.status, "partial")
        self.assertEqual((result.rebuilt, result.failed), (1, 1))
        self.assertEqual(result.papers[0].error_code, "search_index_result_invalid")

    def test_ready_list_failure_is_sanitized(self):
        secret = "database failure includes paper title"
        with TemporaryDirectory() as directory, patch(
            "paper_service.search_index_runner.list_reader_v2_ready_ids",
            side_effect=RuntimeError(secret),
        ):
            result = execute_cycle(data_dir=directory)

        self.assertEqual(result.status, "error")
        self.assertEqual(result.error_code, "search_ready_papers_failed")
        self.assertNotIn(secret, repr(result))

    def test_ready_ids_must_be_positive_unique_sorted_integers(self):
        invalid_values = ([2, 1], [1, 1], [True], [0], ["1"], (1,))
        with TemporaryDirectory() as directory:
            for value in invalid_values:
                with self.subTest(value=value), patch(
                    "paper_service.search_index_runner.list_reader_v2_ready_ids",
                    return_value=value,
                ), patch(
                    "paper_service.search_index_runner.rebuild_paper_search_index"
                ) as rebuild:
                    result = execute_cycle(data_dir=directory)
                self.assertEqual(result.status, "error")
                self.assertEqual(result.error_code, "search_ready_papers_invalid")
                rebuild.assert_not_called()

    def test_data_directory_is_validated_before_database_access(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            regular_file = root / "not-a-directory"
            regular_file.write_text("x", encoding="utf-8")
            link = root / "linked-directory"
            link.symlink_to(root, target_is_directory=True)
            invalid_values = (
                "relative/path",
                root / "missing",
                regular_file,
                link,
                "bad\npath",
            )
            for value in invalid_values:
                with self.subTest(value=value), patch(
                    "paper_service.search_index_runner.list_reader_v2_ready_ids"
                ) as ready:
                    with self.assertRaisesRegex(RuntimeError, "PAPER_DATA_DIR_invalid"):
                        execute_cycle(data_dir=value)
                    ready.assert_not_called()

    def test_invalid_poll_value_fails_before_schema_or_loop(self):
        with TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "PAPER_DATA_DIR": directory,
                "PAPER_SEARCH_INDEX_POLL_SECONDS": " 5",
            },
            clear=False,
        ), patch("paper_service.search_index_runner.ensure_schema") as schema:
            with self.assertRaisesRegex(
                RuntimeError, "PAPER_SEARCH_INDEX_POLL_SECONDS_invalid"
            ):
                run_worker()
        schema.assert_not_called()

    def test_worker_runs_one_cycle_then_sleeps_for_validated_interval(self):
        heartbeat = Heartbeat()
        with TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "PAPER_DATA_DIR": directory,
                "PAPER_SEARCH_INDEX_POLL_SECONDS": "7",
            },
            clear=False,
        ), patch(
            "paper_service.search_index_runner.ensure_schema"
        ) as schema, patch(
            "paper_service.search_index_runner.worker_heartbeat_from_env",
            return_value=heartbeat,
        ) as heartbeat_factory, patch(
            "paper_service.search_index_runner.execute_cycle"
        ) as cycle, patch(
            "paper_service.search_index_runner.time.sleep",
            side_effect=StopIteration,
        ) as sleep:
            with self.assertRaises(StopIteration):
                run_worker()

        schema.assert_called_once_with()
        heartbeat_factory.assert_called_once_with("search-index-worker-1")
        cycle.assert_called_once_with(
            data_dir=Path(directory), heartbeat=heartbeat
        )
        sleep.assert_called_once_with(7)
        self.assertEqual(heartbeat.events, [("enter",), ("exit",)])


if __name__ == "__main__":
    unittest.main()

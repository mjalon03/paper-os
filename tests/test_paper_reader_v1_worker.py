from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from paper_service.reader import ReaderFailure
from paper_service.reader_v1_worker import execute_reader_batch, run_worker


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


class ReaderV1WorkerTests(unittest.TestCase):
    def test_parser_failure_uses_existing_durable_failure_recovery_state(self):
        heartbeat = Heartbeat()
        paper = {"id": 7}
        with patch.dict(os.environ, {"PAPER_DATA_DIR": "/data"}, clear=True), patch(
            "paper_service.reader_v1_worker.retry_failed_reader_papers"
        ) as retry, patch(
            "paper_service.reader_v1_worker.claim_paper_for_reader",
            side_effect=[paper, None],
        ), patch(
            "paper_service.reader_v1_worker.convert_paper",
            side_effect=ReaderFailure("parser_timeout"),
        ), patch(
            "paper_service.reader_v1_worker.complete_reader_paper"
        ) as complete, patch(
            "paper_service.reader_v1_worker.fail_reader_paper"
        ) as fail:
            stats = execute_reader_batch(heartbeat=heartbeat)
        retry.assert_called_once()
        complete.assert_not_called()
        fail.assert_called_once_with(7, "parser_timeout")
        self.assertEqual((stats.ready, stats.failed), (0, 1))
        self.assertEqual(
            heartbeat.events,
            [("processing", 7), ("idle", False)],
        )

    def test_success_completes_the_same_reader_v1_contract(self):
        paper = {"id": 8}
        with patch(
            "paper_service.reader_v1_worker.retry_failed_reader_papers"
        ), patch(
            "paper_service.reader_v1_worker.claim_paper_for_reader",
            side_effect=[paper, None],
        ), patch(
            "paper_service.reader_v1_worker.convert_paper",
            return_value="8/v1/manifest.json",
        ), patch(
            "paper_service.reader_v1_worker.complete_reader_paper"
        ) as complete, patch(
            "paper_service.reader_v1_worker.fail_reader_paper"
        ) as fail:
            stats = execute_reader_batch()
        complete.assert_called_once_with(8, "8/v1/manifest.json", 1)
        fail.assert_not_called()
        self.assertEqual((stats.ready, stats.failed), (1, 0))

    def test_startup_recovers_interrupted_reader_jobs_before_polling(self):
        heartbeat = Heartbeat()
        with patch.dict(
            os.environ,
            {"PAPER_READER_V1_POLL_SECONDS": "15"},
            clear=True,
        ), patch(
            "paper_service.reader_v1_worker.ensure_schema"
        ), patch(
            "paper_service.reader_v1_worker.recover_reader_papers"
        ) as recover, patch(
            "paper_service.reader_v1_worker.worker_heartbeat_from_env",
            return_value=heartbeat,
        ) as heartbeat_factory, patch(
            "paper_service.reader_v1_worker.execute_reader_batch",
            side_effect=StopIteration,
        ):
            with self.assertRaises(StopIteration):
                run_worker()
        recover.assert_called_once()
        heartbeat_factory.assert_called_once_with("reader-v1-worker-1")
        self.assertEqual(heartbeat.events, [("enter",), ("exit",)])


if __name__ == "__main__":
    unittest.main()

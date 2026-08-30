from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from paper_service.ingest import IngestStats
from paper_service.worker import execute_sync, run_worker


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


class PaperWorkerHeartbeatTests(unittest.TestCase):
    def test_sync_tracks_only_exact_metadata_processing_paper_ids(self):
        heartbeat = Heartbeat()
        with patch.dict(
            os.environ,
            {"PAPER_DATA_DIR": "/data"},
            clear=True,
        ), patch(
            "paper_service.worker.copy_drive_inbox"
        ), patch(
            "paper_service.worker.ingest_pdfs",
            return_value=IngestStats(),
        ), patch(
            "paper_service.worker.deepseek_api_key", return_value="configured"
        ), patch(
            "paper_service.worker.retry_failed_papers"
        ), patch(
            "paper_service.worker.claim_paper_for_processing",
            side_effect=[{"id": 7}, None],
        ), patch(
            "paper_service.worker.process_paper",
            return_value=({}, "organized/path"),
        ), patch(
            "paper_service.worker.complete_paper_processing"
        ), patch(
            "paper_service.worker.finish_sync"
        ) as finish, patch(
            "paper_service.worker.fail_sync"
        ) as fail_sync:
            execute_sync(11, heartbeat=heartbeat)

        self.assertEqual(
            heartbeat.events,
            [
                ("processing", 7),
                ("idle", True),
            ],
        )
        finish.assert_called_once()
        fail_sync.assert_not_called()

    def test_worker_loop_is_wrapped_by_environment_heartbeat(self):
        heartbeat = Heartbeat()
        with patch.dict(
            os.environ,
            {
                "PAPER_SYNC_TIMES": "07:00,19:00",
                "PAPER_TIMEZONE": "Asia/Hong_Kong",
                "PAPER_WORKER_POLL_SECONDS": "15",
            },
            clear=True,
        ), patch(
            "paper_service.worker.ensure_schema"
        ), patch(
            "paper_service.worker.recover_interrupted_sync"
        ), patch(
            "paper_service.worker.recover_processing_papers"
        ), patch(
            "paper_service.worker.worker_heartbeat_from_env",
            return_value=heartbeat,
        ) as heartbeat_factory, patch(
            "paper_service.worker.latest_due_slot",
            side_effect=StopIteration,
        ):
            with self.assertRaises(StopIteration):
                run_worker()

        heartbeat_factory.assert_called_once_with("paper-worker-1")
        self.assertEqual(heartbeat.events, [("enter",), ("exit",)])


if __name__ == "__main__":
    unittest.main()

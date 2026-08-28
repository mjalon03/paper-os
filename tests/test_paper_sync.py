import os
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from paper_service.sync import latest_due_slot, parse_sync_times, rclone_copy_command


class PaperSyncTests(unittest.TestCase):
    def test_schedule_uses_latest_hong_kong_slot(self):
        times = parse_sync_times("07:00,19:00")
        before_evening = latest_due_slot(
            datetime(2026, 8, 29, 8, 0, tzinfo=timezone.utc), times, "Asia/Hong_Kong"
        )
        after_evening = latest_due_slot(
            datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc), times, "Asia/Hong_Kong"
        )
        self.assertEqual(before_evening.key, "2026-08-29T07:00@Asia/Hong_Kong")
        self.assertEqual(after_evening.key, "2026-08-29T19:00@Asia/Hong_Kong")

    def test_schedule_before_first_slot_uses_previous_evening(self):
        slot = latest_due_slot(
            datetime(2026, 8, 28, 18, 0, tzinfo=timezone.utc),
            parse_sync_times("07:00,19:00"),
            "Asia/Hong_Kong",
        )
        self.assertEqual(slot.key, "2026-08-28T19:00@Asia/Hong_Kong")

    def test_invalid_schedule_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "HH:MM"):
            parse_sync_times("morning,19:00")

    def test_rclone_command_is_copy_only_and_pdf_filtered(self):
        with patch.dict(os.environ, {}, clear=True):
            command = rclone_copy_command(Path("/data/incoming"))
        self.assertEqual(command[1], "copy")
        self.assertNotIn("sync", command)
        self.assertNotIn("delete", " ".join(command))
        self.assertIn("*.[pP][dD][fF]", command)
        self.assertEqual(command[2], "paper-drive:Paper Inbox")


if __name__ == "__main__":
    unittest.main()

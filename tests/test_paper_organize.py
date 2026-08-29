import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from paper_service.metadata import ExtractedContext, PaperMetadata
from paper_service.organize import (
    OrganizationFailure,
    drive_destination,
    first_author_name,
    process_paper,
    rclone_moveto,
    sanitize_name,
)


class PaperOrganizeTests(unittest.TestCase):
    def setUp(self):
        self.metadata = PaperMetadata(
            title="Brain / Machine: Study?",
            authors=["Lovelace, Ada"],
            year=2026,
            doi="10.1000/test",
            category="NeuroAI",
            paper_type="Research",
            short_title="Brain / Machine: Study?",
            confidence=0.9,
        )

    def test_destination_is_collision_resistant_and_drive_safe(self):
        destination = drive_destination(self.metadata, "a" * 64)
        self.assertEqual(first_author_name(self.metadata.authors), "Lovelace")
        self.assertIn("paper-drive:Original/NeuroAI/2026/", destination)
        self.assertTrue(destination.endswith(" - aaaaaaaa.pdf"))
        self.assertNotIn("?", destination)
        self.assertNotIn(": Study", destination)
        self.assertEqual(sanitize_name(" bad/name? "), "bad name")

    def test_multibyte_filename_stays_within_drive_limit(self):
        metadata = PaperMetadata(
            **{**self.metadata.to_dict(), "short_title": "뇌" * 100}
        )
        filename = drive_destination(metadata, "b" * 64).rsplit("/", 1)[1]
        self.assertLessEqual(len(filename.encode("utf-8")), 224)
        self.assertTrue(filename.endswith(".pdf"))

    @patch("paper_service.organize.subprocess.run")
    def test_rclone_uses_moveto_and_never_shell(self, run):
        run.return_value.returncode = 0
        with patch.dict(os.environ, {}, clear=True):
            rclone_moveto("nested/paper.pdf", "paper-drive:Original/Other/2026/paper.pdf")
        command = run.call_args.args[0]
        self.assertEqual(command[1], "moveto")
        self.assertEqual(command[2], "paper-drive:Paper Inbox/nested/paper.pdf")
        self.assertNotIn("sync", command)

    def test_parent_traversal_is_rejected(self):
        with self.assertRaisesRegex(OrganizationFailure, "unsafe_source_path"):
            rclone_moveto("../paper.pdf", "paper-drive:Original/paper.pdf")

    @patch("paper_service.organize.extract_pdf_context")
    def test_processing_moves_drive_file_then_removes_staging(self, extract):
        extract.return_value = ExtractedContext(None, None, None, "text")
        moves = []
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            raw = data / "library" / "raw" / "aa" / "hash.pdf"
            raw.parent.mkdir(parents=True)
            raw.write_bytes(b"%PDF-test")
            staged = data / "incoming" / "paper.pdf"
            staged.parent.mkdir(parents=True)
            staged.write_bytes(b"%PDF-test")
            metadata, destination = process_paper(
                {
                    "sha256": "a" * 64,
                    "source_path": "paper.pdf",
                    "stored_path": "aa/hash.pdf",
                },
                data,
                classify=lambda _context: self.metadata,
                move=lambda source, target: moves.append((source, target)),
            )
            self.assertEqual(metadata["category"], "NeuroAI")
            self.assertEqual(moves, [("paper.pdf", destination)])
            self.assertFalse(staged.exists())
            self.assertTrue(raw.exists())


if __name__ == "__main__":
    unittest.main()

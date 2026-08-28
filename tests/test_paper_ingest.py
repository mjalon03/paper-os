import hashlib
import os
import tempfile
import unittest
from pathlib import Path

from paper_service.ingest import (
    StorageIntegrityError,
    ingest_pdfs,
    looks_like_pdf,
    sha256_file,
)


class FakeRegistry:
    def __init__(self):
        self.hashes: set[str] = set()
        self.records: list[tuple[str, int, str, str, int]] = []

    def __call__(self, sha256, size_bytes, source_path, stored_path, job_id):
        record = (sha256, size_bytes, source_path, stored_path, job_id)
        self.records.append(record)
        if sha256 in self.hashes:
            return False
        self.hashes.add(sha256)
        return True


class PaperIngestTests(unittest.TestCase):
    def test_valid_pdf_is_stored_by_hash_and_duplicate_is_not_reimported(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            incoming = root / "incoming"
            raw = root / "raw"
            incoming.mkdir()
            content = b"%PDF-1.7\nsmall test document\n%%EOF\n"
            (incoming / "first.pdf").write_bytes(content)
            nested = incoming / "nested"
            nested.mkdir()
            (nested / "copy.PDF").write_bytes(content)
            registry = FakeRegistry()

            stats = ingest_pdfs(incoming, raw, 12, registry)

            digest = hashlib.sha256(content).hexdigest()
            stored = raw / digest[:2] / f"{digest}.pdf"
            self.assertEqual(stats.seen, 2)
            self.assertEqual(stats.imported, 1)
            self.assertEqual(stats.duplicate, 1)
            self.assertEqual(stored.read_bytes(), content)
            self.assertEqual(os.stat(stored).st_mode & 0o777, 0o640)

    def test_invalid_pdf_is_counted_and_non_pdf_is_ignored(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            incoming = root / "incoming"
            incoming.mkdir()
            invalid = incoming / "invalid.pdf"
            invalid.write_bytes(b"not a pdf")
            (incoming / "notes.txt").write_text("ignored", encoding="utf-8")
            registry = FakeRegistry()

            stats = ingest_pdfs(incoming, root / "raw", 1, registry)

            self.assertEqual((stats.seen, stats.imported, stats.invalid), (1, 0, 1))
            self.assertEqual(registry.records, [])
            self.assertFalse(looks_like_pdf(invalid))

    def test_pdf_header_may_appear_within_first_kibibyte(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "paper.pdf"
            path.write_bytes(b"prefix\n%PDF-1.4\n")
            self.assertTrue(looks_like_pdf(path))
            self.assertEqual(sha256_file(path), hashlib.sha256(path.read_bytes()).hexdigest())

    def test_existing_content_addressed_file_is_verified(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            incoming = root / "incoming"
            raw = root / "raw"
            incoming.mkdir()
            content = b"%PDF-1.7\nsource\n%%EOF\n"
            (incoming / "paper.pdf").write_bytes(content)
            digest = hashlib.sha256(content).hexdigest()
            target = raw / digest[:2] / f"{digest}.pdf"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"corrupted")

            with self.assertRaises(StorageIntegrityError):
                ingest_pdfs(incoming, raw, 1, FakeRegistry())


if __name__ == "__main__":
    unittest.main()

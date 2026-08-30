from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from paper_service.data_archive import (
    DataArchiveError,
    create_archive,
    load_manifest,
    verify_and_restore,
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


class DurableDataArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.data = self.root / "data"
        self.raw = self.data / "library" / "raw"
        self.mobile = self.data / "library" / "mobile"
        self.raw.mkdir(parents=True)
        self.mobile.mkdir(parents=True)
        (self.raw / "aa").mkdir()
        (self.raw / "aa" / "paper.pdf").write_bytes(b"%PDF-1.7\nimmutable")
        version = self.mobile / "7" / "versions" / ("rv-" + "a" * 32)
        version.mkdir(parents=True)
        (version / "article.json").write_text('{"reader":2}\n', encoding="utf-8")
        (self.mobile / "7" / "current.json").write_text(
            '{"version":"rv-' + "a" * 32 + '"}\n', encoding="utf-8"
        )

    def _create(self, suffix: str = "") -> tuple[Path, Path]:
        archive = self.root / f"paper_data{suffix}.tar"
        manifest = self.root / f"paper_data{suffix}.json"
        create_archive(self.data, archive, manifest)
        return archive, manifest

    def test_create_is_deterministic_and_restore_rehashes_every_file(self):
        first_archive, first_manifest = self._create("-one")
        second_archive, second_manifest = self._create("-two")
        self.assertEqual(sha256(first_archive), sha256(second_archive))
        self.assertEqual(first_manifest.read_bytes(), second_manifest.read_bytes())

        restore = self.root / "restore"
        restore.mkdir(mode=0o700)
        proven = verify_and_restore(first_archive, first_manifest, restore)

        self.assertEqual(proven, load_manifest(first_manifest))
        self.assertEqual(
            (restore / "library" / "raw" / "aa" / "paper.pdf").read_bytes(),
            b"%PDF-1.7\nimmutable",
        )
        self.assertEqual(
            (restore / "library" / "mobile" / "7" / "current.json").read_text(),
            '{"version":"rv-' + "a" * 32 + '"}\n',
        )
        for path in proven["files"]:
            self.assertEqual((restore / path).stat().st_mode & 0o777, 0o600)

    def test_only_raw_and_mobile_are_included(self):
        incoming = self.data / "incoming"
        incoming.mkdir()
        (incoming / "not-durable.pdf").write_bytes(b"temporary")
        (self.data / "reader-v2-jobs").mkdir()
        archive, manifest_path = self._create()

        manifest = load_manifest(manifest_path)
        serialized = json.dumps(manifest)
        self.assertNotIn("incoming", serialized)
        self.assertNotIn("reader-v2-jobs", serialized)
        self.assertTrue(archive.is_file())

    def test_symlink_in_durable_tree_is_rejected(self):
        outside = self.root / "outside.pdf"
        outside.write_bytes(b"private")
        (self.raw / "linked.pdf").symlink_to(outside)
        with self.assertRaisesRegex(DataArchiveError, "entry_unsafe"):
            self._create()

    def test_changed_manifest_hash_is_rejected_during_restore(self):
        archive, manifest_path = self._create()
        manifest = json.loads(manifest_path.read_text())
        key = next(iter(manifest["files"]))
        manifest["files"][key]["sha256"] = "0" * 64
        manifest_path.write_text(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
        )
        restore = self.root / "restore"
        restore.mkdir()
        with self.assertRaisesRegex(DataArchiveError, "hash_mismatch"):
            verify_and_restore(archive, manifest_path, restore)

    def test_restore_requires_an_empty_real_directory(self):
        archive, manifest_path = self._create()
        restore = self.root / "restore"
        restore.mkdir()
        (restore / "keep.txt").write_text("keep")
        with self.assertRaisesRegex(DataArchiveError, "restore_not_empty"):
            verify_and_restore(archive, manifest_path, restore)
        self.assertEqual((restore / "keep.txt").read_text(), "keep")


if __name__ == "__main__":
    unittest.main()

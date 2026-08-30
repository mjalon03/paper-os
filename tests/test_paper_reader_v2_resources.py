from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from paper_service import reader_v2
from paper_service.artifact_versions import MAX_FILE_BYTES, MAX_FILES, MAX_TOTAL_BYTES
from paper_service.reader_v2 import (
    READER_V2_SCRATCH_BUILD_PREFIX,
    ReaderV2Failure,
    ReaderV2ScratchConfig,
    cleanup_reader_v2_scratch,
    reader_v2_scratch_config,
)
from paper_service.reader_v2_worker import (
    _isolated_pdf_converter,
    _isolated_pmc_builder,
    convert_isolated,
)


class ReaderV2ScratchTests(unittest.TestCase):
    def test_config_is_absolute_strict_and_bounded(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = reader_v2_scratch_config(
                {
                    "PAPER_READER_V2_SCRATCH_DIR": str(root),
                    "PAPER_READER_V2_SCRATCH_MAX_BYTES": str(MAX_TOTAL_BYTES),
                    "PAPER_READER_V2_SCRATCH_MIN_FREE_BYTES": "0",
                    "PAPER_READER_V2_SCRATCH_STALE_SECONDS": "60",
                }
            )
            self.assertEqual(config.root, root)
            self.assertEqual(config.max_bytes, MAX_TOTAL_BYTES)
            invalid = (
                {"PAPER_READER_V2_SCRATCH_DIR": "relative"},
                {
                    "PAPER_READER_V2_SCRATCH_DIR": str(root),
                    "PAPER_READER_V2_SCRATCH_MAX_BYTES": str(MAX_TOTAL_BYTES - 1),
                },
                {
                    "PAPER_READER_V2_SCRATCH_DIR": str(root),
                    "PAPER_READER_V2_SCRATCH_STALE_SECONDS": "0",
                },
                {
                    "PAPER_READER_V2_SCRATCH_DIR": str(root),
                    "PAPER_READER_V2_SCRATCH_MIN_FREE_BYTES": "-1",
                },
            )
            for env in invalid:
                with self.subTest(env=env), self.assertRaises(ReaderV2Failure):
                    reader_v2_scratch_config(env)

    def test_cleanup_removes_only_marked_stale_builds(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = ReaderV2ScratchConfig(root, MAX_TOTAL_BYTES, 0, 60)
            stale = root / f"{READER_V2_SCRATCH_BUILD_PREFIX}stale"
            stale.mkdir()
            reader_v2._write_scratch_owner_marker(stale)
            (stale / "payload.bin").write_bytes(b"owned")
            os.utime(stale, (0, 0))
            fresh = root / f"{READER_V2_SCRATCH_BUILD_PREFIX}fresh"
            fresh.mkdir()
            reader_v2._write_scratch_owner_marker(fresh)
            unowned = root / f"{READER_V2_SCRATCH_BUILD_PREFIX}manual"
            unowned.mkdir()
            (unowned / "keep.txt").write_text("keep")
            unrelated = root / "operator-data"
            unrelated.write_text("keep")

            removed = cleanup_reader_v2_scratch(
                config=config,
                now=1_000,
            )

            self.assertEqual(removed, 1)
            self.assertFalse(stale.exists())
            self.assertTrue(fresh.is_dir())
            self.assertTrue(unowned.is_dir())
            self.assertTrue(unrelated.is_file())

    def test_logical_scratch_size_limit_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "large.bin").write_bytes(b"x" * 9)
            config = ReaderV2ScratchConfig(root, 8, 0, 60)
            with self.assertRaisesRegex(
                ReaderV2Failure, "reader_v2_scratch_size_limit"
            ):
                reader_v2._enforce_scratch_budget(config)


class ReaderV2WorkerResourceTests(unittest.TestCase):
    def test_isolated_conversion_uses_remote_converter_callback(self):
        with patch(
            "paper_service.reader_v2_worker.convert_paper_v2",
            return_value=("7/v2/article.json", "docling_pdf"),
        ) as convert:
            result = convert_isolated({"id": 7}, Path("/data"))

        self.assertEqual(result, ("7/v2/article.json", "docling_pdf"))
        self.assertEqual(convert.call_args.args, ({"id": 7}, Path("/data")))
        self.assertFalse(convert.call_args.kwargs["force_reprocess"])
        self.assertIs(
            convert.call_args.kwargs["pdf_article_converter"],
            _isolated_pdf_converter,
        )
        self.assertIs(
            convert.call_args.kwargs["pmc_article_builder"],
            _isolated_pmc_builder,
        )


class ReaderV2DeploymentEnvelopeTests(unittest.TestCase):
    def test_artifact_caps_match_small_host_envelope(self):
        self.assertEqual(MAX_FILES, 512)
        self.assertEqual(MAX_FILE_BYTES, 64 * 1024 * 1024)
        self.assertEqual(MAX_TOTAL_BYTES, 256 * 1024 * 1024)

    def test_compose_separates_coordinator_and_heavy_converter_resources(self):
        repository = Path(__file__).resolve().parents[1]
        compose = (repository / "infra" / "papers" / "compose.yml").read_text()
        self.assertIn("paper-reader-v2-scratch-init:", compose)
        self.assertIn("paper_reader_v2_scratch:/scratch", compose)
        worker = compose.split("  paper-reader-v2-worker:\n", 1)[1].split(
            "\n  paper-knowledge-worker:\n", 1
        )[0]
        for expected in (
            "PAPER_READER_V2_SCRATCH_DIR: /scratch",
            "PAPER_READER_V2_SCRATCH_MAX_BYTES:",
            "PAPER_READER_V2_SCRATCH_MIN_FREE_BYTES:",
            "PAPER_READER_V2_JOBS_DIR: /jobs",
            "PAPER_READER_V2_CONVERTER_URL:",
            "PAPER_PMC_BROKER_URL:",
            "mem_limit: 512m",
            "paper_reader_v2_jobs:/jobs",
        ):
            with self.subTest(service="worker", expected=expected):
                self.assertIn(expected, worker)
        for forbidden in (
            "PAPER_CONVERSION_LOCK_FILE",
            "paper_conversion_lock:/locks",
            "paper_reader_v2_models:/models",
            "paper_egress",
        ):
            with self.subTest(service="worker", forbidden=forbidden):
                self.assertNotIn(forbidden, worker)

        converter = compose.split("  paper-reader-v2-converter:\n", 1)[1].split(
            "\n  paper-pmc-broker:\n", 1
        )[0]
        for expected in (
            "PAPER_CONVERSION_LOCK_FILE: /locks/heavy-conversion.lock",
            "mem_limit: 2500m",
            "memswap_limit: 2500m",
            "pids_limit: 128",
            "paper_conversion_lock:/locks",
            "paper_reader_v2_models:/models:ro",
            "paper_reader_v2_jobs:/jobs",
            "paper_reader_v2_internal",
        ):
            with self.subTest(service="converter", expected=expected):
                self.assertIn(expected, converter)
        for forbidden in ("PAPER_DB_", "paper_data:/data", "paper_egress"):
            with self.subTest(service="converter", forbidden=forbidden):
                self.assertNotIn(forbidden, converter)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from paper_service.artifact_versions import (
    artifact_store_paths,
    resolve_current_artifact,
)
from paper_service.jats import parse_jats
from paper_service.reader_v2 import (
    READER_V2_RENDER_VERSION,
    ReaderV2Failure,
    convert_paper_v2,
    read_article,
    resume_prepared_paper_v2_version,
    v2_asset_path,
    write_article,
)
from paper_service.reader_v2_worker import (
    convert_isolated,
    parse_force_reprocess,
    run_worker,
)


JATS = b"""<?xml version="1.0" encoding="UTF-8"?>
<article xmlns:xlink="http://www.w3.org/1999/xlink">
  <front><article-meta>
    <article-id pub-id-type="doi">10.1234/versioned.1</article-id>
    <title-group><article-title>Immutable reader article</article-title></title-group>
    <contrib-group><contrib contrib-type="author"><name><surname>Jo</surname><given-names>Minjae</given-names></name></contrib></contrib-group>
    <pub-date><year>2026</year></pub-date>
  </article-meta></front>
  <body><sec id="results"><title>Results</title>
    <p>The measured neural response increased after stimulation.</p>
    <fig id="f1"><label>Figure 1</label><caption><p>Neural response.</p></caption><graphic xlink:href="fig1.jpg"/></fig>
  </sec></body>
</article>"""


FIXED_NOW = datetime(2026, 8, 29, 12, 34, 56, tzinfo=timezone.utc)


class ReaderArtifactFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.data = Path(self.temporary.name)
        self.raw_bytes = b"immutable source pdf"
        self.raw = self.data / "library" / "raw" / "aa" / "paper.pdf"
        self.raw.parent.mkdir(parents=True)
        self.raw.write_bytes(self.raw_bytes)
        self.paper = {
            "id": 7,
            "stored_path": "aa/paper.pdf",
            "sha256": hashlib.sha256(self.raw_bytes).hexdigest(),
            "doi": "10.1234/versioned.1",
        }
        self.article = parse_jats(JATS, {"source": "pmc_jats"})
        self.article["figures"][0]["asset_path"] = "assets/fig1.jpg"

    def tearDown(self):
        self.temporary.cleanup()

    def builder(self, paper, output_dir):
        self.assertEqual(paper["id"], 7)
        assets = output_dir / "assets"
        assets.mkdir(parents=True)
        (assets / "fig1.jpg").write_bytes(b"verified image bytes")
        return copy.deepcopy(self.article)

    def convert(self, **kwargs):
        with patch(
            "paper_service.reader_v2.build_pmc_article",
            side_effect=self.builder,
        ), patch("paper_service.reader_v2._utc_now", return_value=FIXED_NOW):
            return convert_paper_v2(self.paper, self.data, **kwargs)


class ReaderArtifactIntegrationTests(ReaderArtifactFixture):
    def test_conversion_does_not_buffer_generated_artifacts(self):
        scratch = self.data / "scratch"
        scratch.mkdir(mode=0o700)
        with patch.dict(
            os.environ,
            {
                "PAPER_READER_V2_SCRATCH_DIR": str(scratch),
                "PAPER_READER_V2_SCRATCH_MAX_BYTES": str(256 * 1024 * 1024),
                "PAPER_READER_V2_SCRATCH_MIN_FREE_BYTES": "0",
                "PAPER_READER_V2_SCRATCH_STALE_SECONDS": "60",
            },
            clear=False,
        ), patch(
            "paper_service.reader_v2._read_regular_bytes",
            side_effect=AssertionError("generated artifact was buffered"),
        ), patch(
            "paper_service.reader_v2.build_pmc_article",
            side_effect=self.builder,
        ), patch(
            "paper_service.reader_v2._utc_now", return_value=FIXED_NOW
        ):
            manifest, source = convert_paper_v2(self.paper, self.data)

        self.assertEqual((manifest, source), ("7/v2/article.json", "pmc_jats"))
        self.assertEqual(tuple(scratch.iterdir()), ())

    def test_conversion_publishes_verified_article_and_every_asset(self):
        manifest, source = self.convert()

        self.assertEqual(manifest, "7/v2/article.json")
        self.assertEqual(source, "pmc_jats")
        self.assertEqual(self.raw.read_bytes(), self.raw_bytes)
        self.assertFalse(
            (self.data / "library" / "mobile" / "7" / "v2").exists()
        )
        resolution = resolve_current_artifact(self.data, 7)
        self.assertEqual(resolution.mode, "versioned")
        descriptor = dict(resolution.descriptor or {})
        self.assertEqual(descriptor["source_sha256"], self.paper["sha256"])
        self.assertEqual(descriptor["extractor_version"], "pmc-jats:1+adapter.1")
        self.assertEqual(descriptor["render_version"], READER_V2_RENDER_VERSION)
        self.assertEqual(descriptor["created_at"], "2026-08-29T12:34:56Z")
        self.assertEqual(
            set(descriptor["files"]), {"article.json", "assets/fig1.jpg"}
        )
        self.assertEqual(read_article(self.data, 7)["schema_version"], 2)
        self.assertEqual(
            v2_asset_path(self.data, 7, "fig1.jpg").read_bytes(),
            b"verified image bytes",
        )

    def test_normal_repeat_reuses_healthy_current_source_without_extraction(self):
        with patch(
            "paper_service.reader_v2.build_pmc_article",
            side_effect=self.builder,
        ) as builder, patch(
            "paper_service.reader_v2._utc_now", return_value=FIXED_NOW
        ):
            first = convert_paper_v2(self.paper, self.data)
            first_resolution = resolve_current_artifact(self.data, 7)
            second = convert_paper_v2(self.paper, self.data)
            second_resolution = resolve_current_artifact(self.data, 7)

        self.assertEqual(first, second)
        self.assertEqual(first_resolution.version_id, second_resolution.version_id)
        self.assertEqual(builder.call_count, 1)
        versions = artifact_store_paths(self.data, 7).versions_root
        self.assertEqual(len(list(versions.iterdir())), 1)

    def test_resume_installed_version_streams_assets_back_to_stage(self):
        self.convert()
        current = resolve_current_artifact(self.data, 7)
        from paper_service import reader_v2 as reader_v2_module

        original_read = reader_v2_module._read_regular_bytes

        def article_only(path, *, maximum=reader_v2_module.MAX_FILE_BYTES):
            if Path(path).name != "article.json":
                raise AssertionError("asset was buffered")
            return original_read(Path(path), maximum=maximum)

        with patch(
            "paper_service.reader_v2._read_regular_bytes",
            side_effect=article_only,
        ):
            recovered = resume_prepared_paper_v2_version(
                self.paper,
                self.data,
                str(current.version_id),
            )

        self.assertIsNotNone(recovered.staged)
        self.assertEqual(
            (recovered.staged.path / "assets" / "fig1.jpg").read_bytes(),
            b"verified image bytes",
        )

    def test_force_reprocess_creates_a_distinct_monotonic_build(self):
        with patch(
            "paper_service.reader_v2.build_pmc_article",
            side_effect=self.builder,
        ) as builder, patch(
            "paper_service.reader_v2._utc_now", return_value=FIXED_NOW
        ):
            convert_paper_v2(self.paper, self.data)
            first = resolve_current_artifact(self.data, 7)
            convert_paper_v2(self.paper, self.data, force_reprocess=True)
            second = resolve_current_artifact(self.data, 7)

        self.assertNotEqual(first.version_id, second.version_id)
        self.assertEqual(second.descriptor["created_at"], "2026-08-29T12:34:57Z")
        self.assertEqual(builder.call_count, 2)
        versions = artifact_store_paths(self.data, 7).versions_root
        self.assertEqual(len(list(versions.iterdir())), 2)

    def test_failed_publish_keeps_previous_current_reader_live(self):
        self.convert()
        before = resolve_current_artifact(self.data, 7)

        def fail_before_pointer(point):
            if point == "before_pointer_swap":
                raise RuntimeError("simulated publish failure")

        with self.assertRaisesRegex(
            ReaderV2Failure, "reader_v2_artifact_publish_failed"
        ):
            self.convert(force_reprocess=True, failure_injector=fail_before_pointer)

        after = resolve_current_artifact(self.data, 7)
        self.assertEqual(after.version_id, before.version_id)
        self.assertEqual(read_article(self.data, 7)["identity"]["title"], "Immutable reader article")

    def test_failed_first_version_publish_keeps_legacy_fallback_live(self):
        legacy_article = copy.deepcopy(self.article)
        legacy_article["identity"]["title"] = "Still-live legacy title"
        legacy = self.data / "library" / "mobile" / "7" / "v2"
        write_article(legacy_article, legacy)

        def fail_before_pointer(point):
            if point == "before_pointer_swap":
                raise RuntimeError("simulated first publish failure")

        with self.assertRaisesRegex(
            ReaderV2Failure, "reader_v2_artifact_publish_failed"
        ):
            self.convert(force_reprocess=True, failure_injector=fail_before_pointer)

        self.assertEqual(
            read_article(self.data, 7)["identity"]["title"],
            "Still-live legacy title",
        )

    def test_source_change_during_conversion_prevents_publish(self):
        def mutate_source(_paper, output_dir):
            assets = output_dir / "assets"
            assets.mkdir()
            (assets / "fig1.jpg").write_bytes(b"verified image bytes")
            self.raw.write_bytes(b"changed while extracting")
            return copy.deepcopy(self.article)

        with patch(
            "paper_service.reader_v2.build_pmc_article",
            side_effect=mutate_source,
        ), patch("paper_service.reader_v2._utc_now", return_value=FIXED_NOW):
            with self.assertRaisesRegex(ReaderV2Failure, "reader_v2_raw_changed"):
                convert_paper_v2(self.paper, self.data)

        with self.assertRaisesRegex(Exception, "current_artifact_missing"):
            resolve_current_artifact(self.data, 7)

    def test_symlinked_generated_asset_fails_closed(self):
        outside = self.data / "outside.jpg"
        outside.write_bytes(b"must not be published")

        def symlink_builder(_paper, output_dir):
            assets = output_dir / "assets"
            assets.mkdir()
            (assets / "fig1.jpg").symlink_to(outside)
            return copy.deepcopy(self.article)

        with patch(
            "paper_service.reader_v2.build_pmc_article",
            side_effect=symlink_builder,
        ):
            with self.assertRaisesRegex(
                ReaderV2Failure, "reader_v2_artifact_unsafe"
            ):
                convert_paper_v2(self.paper, self.data)

    def test_legacy_fallback_survives_and_versioned_current_takes_precedence(self):
        legacy_article = copy.deepcopy(self.article)
        legacy_article["identity"]["title"] = "Legacy title"
        legacy = self.data / "library" / "mobile" / "7" / "v2"
        write_article(legacy_article, legacy)
        self.assertEqual(read_article(self.data, 7)["identity"]["title"], "Legacy title")

        self.convert(force_reprocess=True)

        self.assertEqual(
            read_article(self.data, 7)["identity"]["title"],
            "Immutable reader article",
        )
        self.assertTrue((legacy / "article.json").is_file())

    def test_symlinked_legacy_article_and_asset_fail_closed(self):
        legacy = self.data / "library" / "mobile" / "7" / "v2"
        legacy.mkdir(parents=True)
        outside_article = self.data / "outside-article.json"
        write_article(self.article, self.data / "safe-article")
        outside_article.write_bytes(
            (self.data / "safe-article" / "article.json").read_bytes()
        )
        (legacy / "article.json").symlink_to(outside_article)
        with self.assertRaisesRegex(ValueError, "reader_v2_invalid"):
            read_article(self.data, 7)

        (legacy / "article.json").unlink()
        write_article(self.article, legacy)
        assets = legacy / "assets"
        assets.mkdir()
        outside_asset = self.data / "outside-asset.jpg"
        outside_asset.write_bytes(b"outside")
        (assets / "fig1.jpg").symlink_to(outside_asset)
        with self.assertRaisesRegex(FileNotFoundError, "reader_v2_asset_missing"):
            v2_asset_path(self.data, 7, "fig1.jpg")


class ReaderArtifactWorkerTests(unittest.TestCase):
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

    def test_force_parser_is_explicit_and_strict(self):
        self.assertFalse(parse_force_reprocess(None))
        self.assertFalse(parse_force_reprocess("false"))
        self.assertFalse(parse_force_reprocess("0"))
        self.assertTrue(parse_force_reprocess("true"))
        self.assertTrue(parse_force_reprocess("1"))
        for invalid in ("TRUE", "yes", " false", "2"):
            with self.subTest(invalid=invalid), self.assertRaises(RuntimeError):
                parse_force_reprocess(invalid)

    def test_isolated_force_reprocess_uses_same_remote_boundary(self):
        with patch(
            "paper_service.reader_v2_worker.convert_paper_v2",
            return_value=("7/v2/article.json", "docling_pdf"),
        ) as convert:
            converted = convert_isolated(
                {"id": 7},
                Path("/data"),
                force_reprocess=True,
            )

        self.assertEqual(converted, ("7/v2/article.json", "docling_pdf"))
        self.assertTrue(convert.call_args.kwargs["force_reprocess"])
        self.assertTrue(callable(convert.call_args.kwargs["pmc_article_builder"]))
        self.assertTrue(callable(convert.call_args.kwargs["pdf_article_converter"]))

    def test_worker_loop_heartbeat_tracks_only_claimed_paper(self):
        heartbeat = self.Heartbeat()
        paper = {"id": 7}
        with patch.dict(
            os.environ,
            {
                "PAPER_READER_V2_ALLOWED_IDS": "all",
                "PAPER_READER_V2_FORCE_REPROCESS": "false",
                "PAPER_OPS_EXECUTION_ENABLED": "false",
                "PAPER_READER_V2_POLL_SECONDS": "30",
                "PAPER_DATA_DIR": "/data",
            },
            clear=True,
        ), patch(
            "paper_service.reader_v2_worker.ensure_schema"
        ), patch(
            "paper_service.reader_v2_worker.cleanup_reader_v2_scratch"
        ), patch(
            "paper_service.reader_v2_worker.recover_reader_v2_papers"
        ), patch(
            "paper_service.reader_v2_worker.worker_heartbeat_from_env",
            return_value=heartbeat,
        ) as heartbeat_factory, patch(
            "paper_service.reader_v2_worker.run_operations_cycle"
        ) as operations, patch(
            "paper_service.reader_v2_worker.retry_failed_reader_v2_papers",
            side_effect=[None, StopIteration],
        ), patch(
            "paper_service.reader_v2_worker.claim_paper_for_reader_v2",
            return_value=paper,
        ), patch(
            "paper_service.reader_v2_worker.convert_isolated",
            return_value=("7/v2/article.json", "pmc_jats"),
        ), patch(
            "paper_service.reader_v2_worker.read_article",
            return_value={"warnings": []},
        ), patch(
            "paper_service.reader_v2_worker.build_selector_index",
            return_value=SimpleNamespace(article_fingerprint="a" * 64),
        ), patch(
            "paper_service.reader_v2_worker.import_article_warnings"
        ), patch(
            "paper_service.reader_v2_worker.complete_reader_v2_paper"
        ):
            with self.assertRaises(StopIteration):
                run_worker()

        heartbeat_factory.assert_called_once_with("reader-v2-worker-1")
        operations.assert_not_called()
        self.assertEqual(
            heartbeat.events,
            [
                ("enter",),
                ("processing", 7),
                ("idle", True),
                ("exit",),
            ],
        )


if __name__ == "__main__":
    unittest.main()

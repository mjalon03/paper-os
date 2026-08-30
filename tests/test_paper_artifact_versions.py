from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import paper_service.artifact_versions as artifact_versions
from paper_service.artifact_versions import (
    CURRENT_POINTER_NAME,
    MANIFEST_NAME,
    OWNER_MARKER_NAME,
    STORE_DIRECTORY_NAME,
    ArtifactVersionError,
    activate_staged_version,
    apply_retention,
    artifact_store_paths,
    build_version_descriptor,
    build_version_descriptor_from_directory,
    canonical_descriptor_bytes,
    create_staged_version,
    create_staged_version_from_directory,
    descriptor_sha256,
    plan_retention,
    publish_version,
    resolve_current_artifact,
    rollback_current_version,
    safe_relative_artifact_path,
    validate_version_descriptor,
    verify_raw_source_sha256,
    verify_version_directory,
)
from paper_service.selectors import SELECTOR_VERSION


RAW_RELATIVE = "aa/paper.pdf"
RAW_BYTES = b"immutable source pdf"
SOURCE_SHA = hashlib.sha256(RAW_BYTES).hexdigest()
ARTICLE_FINGERPRINT = "b" * 64


def artifact_files(label: str = "one") -> dict[str, bytes]:
    return {
        "article.json": json.dumps(
            {"schema_version": 2, "label": label},
            sort_keys=True,
            separators=(",", ":"),
        ).encode(),
        "assets/figure-1.png": f"image-{label}".encode(),
    }


def descriptor(
    files: dict[str, bytes],
    *,
    created_at: str = "2026-08-29T01:00:00Z",
    extractor_version: str = "docling:2.61.2",
    render_version: str = "reader-html:3",
) -> dict:
    return build_version_descriptor(
        paper_id=7,
        source_sha256=SOURCE_SHA,
        article_fingerprint=ARTICLE_FINGERPRINT,
        selector_version=SELECTOR_VERSION,
        extractor_version=extractor_version,
        render_version=render_version,
        created_at=created_at,
        files=files,
    )


class ArtifactFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.data = Path(self.temporary.name)
        self.raw = self.data / "library" / "raw" / RAW_RELATIVE
        self.raw.parent.mkdir(parents=True)
        self.raw.write_bytes(RAW_BYTES)

    def tearDown(self):
        self.temporary.cleanup()

    def publish(
        self,
        label: str,
        created_at: str,
        *,
        failure_injector=None,
    ) -> dict:
        files = artifact_files(label)
        item = descriptor(
            files,
            created_at=created_at,
            render_version=f"reader-html:{label}",
        )
        return publish_version(
            self.data,
            item,
            files,
            raw_source_relative=RAW_RELATIVE,
            failure_injector=failure_injector,
        )


class ArtifactDescriptorTests(ArtifactFixture):
    def test_descriptor_is_content_addressed_deterministic_and_strict(self):
        files = artifact_files()
        first = descriptor(files)
        second = descriptor({key: files[key] for key in reversed(files)})
        self.assertEqual(first, second)
        self.assertRegex(first["version_id"], r"^rv-[0-9a-f]{32}$")
        self.assertEqual(first["selector_version"], SELECTOR_VERSION)
        self.assertEqual(first["files"]["article.json"]["size"], len(files["article.json"]))
        self.assertEqual(
            first["files"]["article.json"]["sha256"],
            hashlib.sha256(files["article.json"]).hexdigest(),
        )
        self.assertEqual(first, validate_version_descriptor(first))
        self.assertRegex(descriptor_sha256(first), r"^[0-9a-f]{64}$")
        self.assertEqual(canonical_descriptor_bytes(first), canonical_descriptor_bytes(second))

        tampered = copy.deepcopy(first)
        tampered["render_version"] = "reader-html:changed"
        with self.assertRaisesRegex(ArtifactVersionError, "version_id_mismatch"):
            validate_version_descriptor(tampered)
        unknown = dict(first, title="private paper title")
        with self.assertRaisesRegex(ArtifactVersionError, "unknown_field"):
            validate_version_descriptor(unknown)

    def test_selector_versions_timestamps_versions_and_payload_types_are_bounded(self):
        files = artifact_files()
        cases = []
        wrong_selector = descriptor(files)
        wrong_selector["selector_version"] += 1
        cases.append((wrong_selector, "selector_version_invalid"))
        invalid_time = descriptor(files)
        invalid_time["created_at"] = "2026-02-30T00:00:00Z"
        cases.append((invalid_time, "created_at_invalid"))
        invalid_version = descriptor(files)
        invalid_version["extractor_version"] = "../extractor"
        cases.append((invalid_version, "extractor_version_invalid"))
        boolean_schema = descriptor(files)
        boolean_schema["schema_version"] = True
        cases.append((boolean_schema, "schema_version_invalid"))
        for payload, code in cases:
            with self.subTest(code=code):
                with self.assertRaisesRegex(ArtifactVersionError, code):
                    validate_version_descriptor(payload)
        with self.assertRaisesRegex(ArtifactVersionError, "file_payload_must_be_bytes"):
            build_version_descriptor(
                paper_id=7,
                source_sha256=SOURCE_SHA,
                article_fingerprint=ARTICLE_FINGERPRINT,
                extractor_version="docling:2",
                render_version="reader:3",
                created_at="2026-08-29T00:00:00Z",
                files={"article.json": "not bytes"},
            )
        with self.assertRaisesRegex(ArtifactVersionError, "files_path_collision"):
            build_version_descriptor(
                paper_id=7,
                source_sha256=SOURCE_SHA,
                article_fingerprint=ARTICLE_FINGERPRINT,
                extractor_version="docling:2",
                render_version="reader:3",
                created_at="2026-08-29T00:00:00Z",
                files={"assets": b"file", "assets/figure.png": b"nested"},
            )

    def test_relative_paths_and_store_paths_are_deterministic_and_confined(self):
        self.assertEqual(
            safe_relative_artifact_path("assets/figure-1.png"),
            "assets/figure-1.png",
        )
        for unsafe in (
            "../article.json",
            "/article.json",
            "assets/../../raw.pdf",
            "assets\\figure.png",
            MANIFEST_NAME,
            OWNER_MARKER_NAME,
            "assets//figure.png",
        ):
            with self.subTest(path=unsafe):
                with self.assertRaises(ArtifactVersionError):
                    safe_relative_artifact_path(unsafe)
        paths = artifact_store_paths(self.data, 7)
        self.assertEqual(
            paths.store_root,
            self.data / "library" / "mobile" / "7" / STORE_DIRECTORY_NAME,
        )
        self.assertEqual(paths.current_pointer.name, CURRENT_POINTER_NAME)
        with self.assertRaisesRegex(ArtifactVersionError, "data_dir_must_be_absolute"):
            artifact_store_paths(Path("relative"), 7)

    def test_directory_descriptor_limits_are_enforced_without_payload_buffers(self):
        source = self.data / "generated"
        (source / "assets").mkdir(parents=True)
        (source / "article.json").write_bytes(b"a" * 8)
        (source / "assets" / "figure.png").write_bytes(b"b" * 4)
        arguments = {
            "paper_id": 7,
            "source_sha256": SOURCE_SHA,
            "article_fingerprint": ARTICLE_FINGERPRINT,
            "extractor_version": "docling:2",
            "render_version": "reader:3",
            "created_at": "2026-08-29T00:00:00Z",
            "source_root": source,
            "files": ("article.json", "assets/figure.png"),
        }
        with patch.object(artifact_versions, "MAX_FILE_BYTES", 8), patch.object(
            artifact_versions, "MAX_TOTAL_BYTES", 12
        ):
            item = build_version_descriptor_from_directory(**arguments)
            self.assertEqual(item["files"]["article.json"]["size"], 8)
            (source / "assets" / "figure.png").write_bytes(b"b" * 5)
            with self.assertRaisesRegex(
                ArtifactVersionError, "files_total_size_exceeded"
            ):
                build_version_descriptor_from_directory(**arguments)
            (source / "assets" / "figure.png").write_bytes(b"b" * 9)
            with self.assertRaisesRegex(ArtifactVersionError, "file_too_large"):
                build_version_descriptor_from_directory(**arguments)


class ArtifactPublicationTests(ArtifactFixture):
    def test_directory_stage_streams_and_fails_closed_if_source_changed(self):
        source = self.data / "generated"
        (source / "assets").mkdir(parents=True)
        payloads = artifact_files("streamed")
        for relative, payload in payloads.items():
            destination = source / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(payload)
        item = build_version_descriptor_from_directory(
            paper_id=7,
            source_sha256=SOURCE_SHA,
            article_fingerprint=ARTICLE_FINGERPRINT,
            extractor_version="docling:2",
            render_version="reader:3",
            created_at="2026-08-29T00:00:00Z",
            source_root=source,
            files=tuple(payloads),
        )
        (source / "assets" / "figure-1.png").write_bytes(b"changed")
        with self.assertRaisesRegex(
            ArtifactVersionError, "staging_source_checksum_mismatch"
        ):
            create_staged_version_from_directory(
                self.data,
                item,
                source_root=source,
                files=tuple(payloads),
                raw_source_relative=RAW_RELATIVE,
            )
        paths = artifact_store_paths(self.data, 7)
        self.assertEqual(tuple(paths.staging_root.iterdir()), ())
        with self.assertRaisesRegex(ArtifactVersionError, "current_artifact_missing"):
            resolve_current_artifact(self.data, 7, allow_legacy_v2=False)

        (source / "assets" / "figure-1.png").write_bytes(
            payloads["assets/figure-1.png"]
        )
        staged = create_staged_version_from_directory(
            self.data,
            item,
            source_root=source,
            files=tuple(payloads),
            raw_source_relative=RAW_RELATIVE,
        )
        self.assertEqual(
            (staged.path / "assets" / "figure-1.png").read_bytes(),
            payloads["assets/figure-1.png"],
        )

    def test_directory_stage_detects_source_mutation_during_stream_copy(self):
        source = self.data / "generated"
        source.mkdir()
        generated = source / "article.json"
        generated.write_bytes(b"a" * (1024 * 1024 + 1))
        item = build_version_descriptor_from_directory(
            paper_id=7,
            source_sha256=SOURCE_SHA,
            article_fingerprint=ARTICLE_FINGERPRINT,
            extractor_version="docling:2",
            render_version="reader:3",
            created_at="2026-08-29T00:00:00Z",
            source_root=source,
            files=("article.json",),
        )
        target_inode = generated.stat().st_ino
        original_read = artifact_versions.os.read
        changed = False

        def mutate_after_first_source_chunk(descriptor_number, maximum):
            nonlocal changed
            chunk = original_read(descriptor_number, maximum)
            if (
                chunk
                and not changed
                and os.fstat(descriptor_number).st_ino == target_inode
            ):
                changed = True
                with generated.open("ab") as handle:
                    handle.write(b"changed")
            return chunk

        with patch.object(
            artifact_versions.os,
            "read",
            side_effect=mutate_after_first_source_chunk,
        ), self.assertRaisesRegex(
            ArtifactVersionError, "artifact_source_changed_while_copying"
        ):
            create_staged_version_from_directory(
                self.data,
                item,
                source_root=source,
                files=("article.json",),
                raw_source_relative=RAW_RELATIVE,
            )

        self.assertTrue(changed)
        self.assertEqual(
            tuple(artifact_store_paths(self.data, 7).staging_root.iterdir()),
            (),
        )

    def test_directory_descriptor_and_stage_reject_symlink_source(self):
        source = self.data / "generated"
        source.mkdir()
        outside = self.data / "outside.json"
        outside.write_bytes(b"{}")
        (source / "article.json").symlink_to(outside)
        with self.assertRaisesRegex(ArtifactVersionError, "artifact_source_unsafe"):
            build_version_descriptor_from_directory(
                paper_id=7,
                source_sha256=SOURCE_SHA,
                article_fingerprint=ARTICLE_FINGERPRINT,
                extractor_version="docling:2",
                render_version="reader:3",
                created_at="2026-08-29T00:00:00Z",
                source_root=source,
                files=("article.json",),
            )

    def test_failed_first_pointer_swap_preserves_legacy_reader(self):
        legacy = self.data / "library" / "mobile" / "7" / "v2"
        legacy.mkdir(parents=True)
        (legacy / "article.json").write_bytes(b"legacy reader")

        def fail_inside_atomic_replace(point):
            if point == "after_pointer_file_replace_before_fsync":
                raise RuntimeError("injected")

        with self.assertRaisesRegex(ArtifactVersionError, "activation_failed"):
            self.publish(
                "first-failure",
                "2026-08-29T01:00:00Z",
                failure_injector=fail_inside_atomic_replace,
            )
        resolution = resolve_current_artifact(self.data, 7)
        self.assertEqual(resolution.mode, "legacy_v2")
        self.assertEqual((resolution.root / "article.json").read_bytes(), b"legacy reader")

    def test_publish_verifies_fsyncs_and_atomically_resolves_current(self):
        legacy = self.data / "library" / "mobile" / "7" / "v2"
        legacy.mkdir(parents=True)
        (legacy / "article.json").write_text('{"legacy":true}')
        original_legacy = (legacy / "article.json").read_bytes()

        published = self.publish("one", "2026-08-29T01:00:00Z")
        resolution = resolve_current_artifact(self.data, 7)
        self.assertEqual(resolution.mode, "versioned")
        self.assertEqual(resolution.version_id, published["version_id"])
        self.assertEqual(resolution.descriptor["source_sha256"], SOURCE_SHA)
        with self.assertRaises(TypeError):
            resolution.descriptor["source_sha256"] = "0" * 64
        with self.assertRaises(TypeError):
            resolution.descriptor["files"]["article.json"]["size"] = 0
        self.assertEqual(
            (resolution.root / "article.json").read_bytes(),
            artifact_files("one")["article.json"],
        )
        self.assertEqual((legacy / "article.json").read_bytes(), original_legacy)
        self.assertEqual(self.raw.read_bytes(), RAW_BYTES)
        self.assertEqual(verify_version_directory(resolution.root), published)
        self.assertEqual(
            verify_raw_source_sha256(self.data, 7, RAW_RELATIVE, SOURCE_SHA),
            SOURCE_SHA,
        )

    def test_expected_set_and_checksums_fail_before_visibility(self):
        files = artifact_files()
        item = descriptor(files)
        missing = dict(files)
        missing.pop("article.json")
        with self.assertRaisesRegex(ArtifactVersionError, "staging_file_set_mismatch"):
            create_staged_version(
                self.data,
                item,
                missing,
                raw_source_relative=RAW_RELATIVE,
            )
        tampered = dict(files, **{"article.json": b"tampered"})
        with self.assertRaisesRegex(ArtifactVersionError, "payload_checksum_mismatch"):
            create_staged_version(
                self.data,
                item,
                tampered,
                raw_source_relative=RAW_RELATIVE,
            )
        with self.assertRaisesRegex(ArtifactVersionError, "current_artifact_missing"):
            resolve_current_artifact(self.data, 7, allow_legacy_v2=False)

    def test_checksum_tamper_and_extra_file_reject_activation_keep_old_current(self):
        old = self.publish("old", "2026-08-29T01:00:00Z")
        files = artifact_files("new")
        new = descriptor(
            files,
            created_at="2026-08-29T02:00:00Z",
            render_version="reader-html:new",
        )
        staged = create_staged_version(
            self.data,
            new,
            files,
            raw_source_relative=RAW_RELATIVE,
        )
        os.chmod(staged.path, 0o750)
        os.chmod(staged.path / "article.json", 0o640)
        (staged.path / "article.json").write_bytes(b"tampered")
        with self.assertRaisesRegex(ArtifactVersionError, "checksum_mismatch"):
            activate_staged_version(staged)
        self.assertEqual(resolve_current_artifact(self.data, 7).version_id, old["version_id"])

        files = artifact_files("extra")
        extra = descriptor(
            files,
            created_at="2026-08-29T03:00:00Z",
            render_version="reader-html:extra",
        )
        staged_extra = create_staged_version(
            self.data,
            extra,
            files,
            raw_source_relative=RAW_RELATIVE,
        )
        os.chmod(staged_extra.path, 0o750)
        (staged_extra.path / "rogue.txt").write_bytes(b"rogue")
        with self.assertRaisesRegex(ArtifactVersionError, "file_set_mismatch"):
            activate_staged_version(staged_extra)
        self.assertEqual(resolve_current_artifact(self.data, 7).version_id, old["version_id"])

    def test_failure_before_and_after_pointer_swap_preserves_previous_reader(self):
        old = self.publish("old", "2026-08-29T01:00:00Z")

        def fail_before(point):
            if point == "before_pointer_swap":
                raise RuntimeError("injected")

        with self.assertRaisesRegex(ArtifactVersionError, "activation_failed"):
            self.publish(
                "before",
                "2026-08-29T02:00:00Z",
                failure_injector=fail_before,
            )
        self.assertEqual(resolve_current_artifact(self.data, 7).version_id, old["version_id"])

        def fail_after(point):
            if point == "after_pointer_swap":
                raise RuntimeError("injected")

        with self.assertRaisesRegex(ArtifactVersionError, "activation_failed"):
            self.publish(
                "after",
                "2026-08-29T03:00:00Z",
                failure_injector=fail_after,
            )
        self.assertEqual(resolve_current_artifact(self.data, 7).version_id, old["version_id"])

        def fail_inside_atomic_replace(point):
            if point == "after_pointer_file_replace_before_fsync":
                raise RuntimeError("injected")

        with self.assertRaisesRegex(ArtifactVersionError, "activation_failed"):
            self.publish(
                "inside-atomic",
                "2026-08-29T04:00:00Z",
                failure_injector=fail_inside_atomic_replace,
            )
        self.assertEqual(resolve_current_artifact(self.data, 7).version_id, old["version_id"])

    def test_raw_source_sha_is_checked_before_stage_before_swap_and_after_swap(self):
        old = self.publish("old", "2026-08-29T01:00:00Z")
        files = artifact_files("new")
        item = descriptor(
            files,
            created_at="2026-08-29T02:00:00Z",
            render_version="reader-html:new",
        )
        staged = create_staged_version(
            self.data,
            item,
            files,
            raw_source_relative=RAW_RELATIVE,
        )
        self.raw.write_bytes(b"changed source")
        with self.assertRaisesRegex(ArtifactVersionError, "raw_source_sha256_mismatch"):
            activate_staged_version(staged)
        self.assertEqual(resolve_current_artifact(self.data, 7).version_id, old["version_id"])

        self.raw.write_bytes(RAW_BYTES)
        files = artifact_files("after")
        after = descriptor(
            files,
            created_at="2026-08-29T03:00:00Z",
            render_version="reader-html:after",
        )

        def change_after_swap(point):
            if point == "after_pointer_swap":
                self.raw.write_bytes(b"changed during activation")

        with self.assertRaisesRegex(ArtifactVersionError, "raw_source_sha256_mismatch"):
            publish_version(
                self.data,
                after,
                files,
                raw_source_relative=RAW_RELATIVE,
                failure_injector=change_after_swap,
            )
        self.assertEqual(resolve_current_artifact(self.data, 7).version_id, old["version_id"])

    def test_simulated_crash_during_stage_never_changes_current_pointer(self):
        old = self.publish("old", "2026-08-29T01:00:00Z")
        files = artifact_files("crash")
        item = descriptor(
            files,
            created_at="2026-08-29T02:00:00Z",
            render_version="reader-html:crash",
        )

        class SimulatedCrash(BaseException):
            pass

        def crash(point):
            if point == "after_staging_file_written":
                raise SimulatedCrash()

        with self.assertRaises(SimulatedCrash):
            create_staged_version(
                self.data,
                item,
                files,
                raw_source_relative=RAW_RELATIVE,
                failure_injector=crash,
            )
        self.assertEqual(resolve_current_artifact(self.data, 7).version_id, old["version_id"])


class ArtifactSafetyAndLegacyTests(ArtifactFixture):
    def test_legacy_fallback_is_explicit_read_only_and_not_used_for_corrupt_pointer(self):
        legacy = self.data / "library" / "mobile" / "7" / "v2"
        legacy.mkdir(parents=True)
        article = legacy / "article.json"
        article.write_bytes(b"legacy unchanged")
        before = article.read_bytes()
        resolution = resolve_current_artifact(self.data, 7)
        self.assertEqual(resolution.mode, "legacy_v2")
        self.assertIsNone(resolution.descriptor)
        self.assertEqual(article.read_bytes(), before)
        with self.assertRaisesRegex(ArtifactVersionError, "current_artifact_missing"):
            resolve_current_artifact(self.data, 7, allow_legacy_v2=False)

        self.publish("one", "2026-08-29T01:00:00Z")
        pointer = artifact_store_paths(self.data, 7).current_pointer
        pointer.write_text("not json")
        with self.assertRaisesRegex(ArtifactVersionError, "json_invalid"):
            resolve_current_artifact(self.data, 7)
        self.assertEqual(article.read_bytes(), before)

    def test_traversal_and_symlink_raw_source_store_legacy_and_version_are_rejected(self):
        files = artifact_files()
        item = descriptor(files)
        with self.assertRaises(ArtifactVersionError):
            create_staged_version(
                self.data,
                item,
                files,
                raw_source_relative="../outside.pdf",
            )

        outside = self.data / "outside.pdf"
        outside.write_bytes(RAW_BYTES)
        self.raw.unlink()
        self.raw.symlink_to(outside)
        with self.assertRaisesRegex(ArtifactVersionError, "raw_source_unsafe"):
            create_staged_version(
                self.data,
                item,
                files,
                raw_source_relative=RAW_RELATIVE,
            )

        self.raw.unlink()
        self.raw.write_bytes(RAW_BYTES)
        published = self.publish("one", "2026-08-29T01:00:00Z")
        version = artifact_store_paths(self.data, 7).versions_root / published["version_id"]
        os.chmod(version, 0o750)
        article = version / "article.json"
        article.unlink()
        article.symlink_to(outside)
        with self.assertRaisesRegex(ArtifactVersionError, "version_tree_unsafe"):
            resolve_current_artifact(self.data, 7)

    def test_symlinked_store_and_legacy_article_fail_closed(self):
        paper_root = self.data / "library" / "mobile" / "7"
        paper_root.mkdir(parents=True)
        outside_store = self.data / "outside-store"
        outside_store.mkdir()
        (paper_root / STORE_DIRECTORY_NAME).symlink_to(outside_store, target_is_directory=True)
        with self.assertRaisesRegex(ArtifactVersionError, "artifact_store_unsafe"):
            resolve_current_artifact(self.data, 7)

        (paper_root / STORE_DIRECTORY_NAME).unlink()
        legacy = paper_root / "v2"
        legacy.mkdir()
        outside_article = self.data / "article.json"
        outside_article.write_text("{}")
        (legacy / "article.json").symlink_to(outside_article)
        with self.assertRaisesRegex(ArtifactVersionError, "unsafe_file_type"):
            resolve_current_artifact(self.data, 7)

    def test_symlinked_current_pointer_is_never_followed(self):
        self.publish("one", "2026-08-29T01:00:00Z")
        pointer = artifact_store_paths(self.data, 7).current_pointer
        outside_pointer = self.data / "outside-pointer.json"
        outside_pointer.write_bytes(pointer.read_bytes())
        pointer.unlink()
        pointer.symlink_to(outside_pointer)
        with self.assertRaisesRegex(ArtifactVersionError, "unsafe_file_type"):
            resolve_current_artifact(self.data, 7)

    def test_symlinked_data_root_and_raw_directory_fail_closed(self):
        real_data = self.data / "real-data"
        real_raw = real_data / "library" / "raw" / RAW_RELATIVE
        real_raw.parent.mkdir(parents=True)
        real_raw.write_bytes(RAW_BYTES)
        linked_data = self.data / "linked-data"
        linked_data.symlink_to(real_data, target_is_directory=True)
        with self.assertRaisesRegex(ArtifactVersionError, "raw_source_unsafe"):
            verify_raw_source_sha256(linked_data, 7, RAW_RELATIVE, SOURCE_SHA)
        with self.assertRaisesRegex(ArtifactVersionError, "unsafe_directory"):
            resolve_current_artifact(linked_data, 7)

        raw_root = self.data / "library" / "raw"
        moved_raw = self.data / "moved-raw"
        raw_root.rename(moved_raw)
        raw_root.symlink_to(moved_raw, target_is_directory=True)
        with self.assertRaisesRegex(ArtifactVersionError, "raw_source_unsafe"):
            verify_raw_source_sha256(self.data, 7, RAW_RELATIVE, SOURCE_SHA)


class ArtifactRollbackAndRetentionTests(ArtifactFixture):
    def test_rollback_dry_run_apply_and_failure_restore_are_safe(self):
        first = self.publish("one", "2026-08-29T01:00:00Z")
        second = self.publish("two", "2026-08-29T02:00:00Z")
        preview = rollback_current_version(
            self.data,
            7,
            first["version_id"],
            raw_source_relative=RAW_RELATIVE,
        )
        self.assertTrue(preview["dry_run"])
        self.assertEqual(resolve_current_artifact(self.data, 7).version_id, second["version_id"])
        applied = rollback_current_version(
            self.data,
            7,
            first["version_id"],
            raw_source_relative=RAW_RELATIVE,
            dry_run=False,
        )
        self.assertTrue(applied["changed"])
        self.assertEqual(resolve_current_artifact(self.data, 7).version_id, first["version_id"])

        def fail_after(point):
            if point == "after_rollback_pointer_swap":
                raise RuntimeError("injected")

        with self.assertRaisesRegex(ArtifactVersionError, "rollback_failed"):
            rollback_current_version(
                self.data,
                7,
                second["version_id"],
                raw_source_relative=RAW_RELATIVE,
                dry_run=False,
                failure_injector=fail_after,
            )
        self.assertEqual(resolve_current_artifact(self.data, 7).version_id, first["version_id"])

        def fail_inside_atomic_replace(point):
            if point == "after_rollback_pointer_file_replace_before_fsync":
                raise RuntimeError("injected")

        with self.assertRaisesRegex(ArtifactVersionError, "rollback_failed"):
            rollback_current_version(
                self.data,
                7,
                second["version_id"],
                raw_source_relative=RAW_RELATIVE,
                dry_run=False,
                failure_injector=fail_inside_atomic_replace,
            )
        self.assertEqual(resolve_current_artifact(self.data, 7).version_id, first["version_id"])

    def test_rollback_rejects_stale_raw_source(self):
        first = self.publish("one", "2026-08-29T01:00:00Z")
        self.publish("two", "2026-08-29T02:00:00Z")
        self.raw.write_bytes(b"stale raw")
        with self.assertRaisesRegex(ArtifactVersionError, "raw_source_sha256_mismatch"):
            rollback_current_version(
                self.data,
                7,
                first["version_id"],
                raw_source_relative=RAW_RELATIVE,
                dry_run=False,
            )

    def test_retention_dry_run_deletes_only_owned_nonactive_versions(self):
        versions = [
            self.publish("one", "2026-08-29T01:00:00Z"),
            self.publish("two", "2026-08-29T02:00:00Z"),
            self.publish("three", "2026-08-29T03:00:00Z"),
            self.publish("four", "2026-08-29T04:00:00Z"),
        ]
        paths = artifact_store_paths(self.data, 7)
        unowned = paths.versions_root / "manual-directory"
        unowned.mkdir()
        (unowned / "do-not-delete.txt").write_text("user data")

        preview = apply_retention(self.data, 7, keep=2)
        self.assertTrue(preview["dry_run"])
        self.assertEqual(len(preview["delete_version_ids"]), 2)
        self.assertIn("manual-directory", preview["skipped_unowned"])
        self.assertTrue(
            all(
                (paths.versions_root / item["version_id"]).exists()
                for item in versions
            )
        )

        applied = apply_retention(self.data, 7, keep=2, dry_run=False)
        self.assertEqual(applied["deleted_version_ids"], preview["delete_version_ids"])
        self.assertTrue(unowned.is_dir())
        active = resolve_current_artifact(self.data, 7).version_id
        self.assertEqual(active, versions[-1]["version_id"])
        self.assertTrue((paths.versions_root / active).is_dir())

    def test_retention_always_keeps_active_even_when_it_is_old(self):
        first = self.publish("one", "2026-08-29T01:00:00Z")
        self.publish("two", "2026-08-29T02:00:00Z")
        latest = self.publish("three", "2026-08-29T03:00:00Z")
        rollback_current_version(
            self.data,
            7,
            first["version_id"],
            raw_source_relative=RAW_RELATIVE,
            dry_run=False,
        )
        plan = plan_retention(self.data, 7, keep=1)
        self.assertIn(first["version_id"], plan["retain_version_ids"])
        self.assertIn(latest["version_id"], plan["retain_version_ids"])
        self.assertNotIn(first["version_id"], plan["delete_version_ids"])

    def test_corrupt_owned_version_blocks_retention_instead_of_deleting(self):
        first = self.publish("one", "2026-08-29T01:00:00Z")
        self.publish("two", "2026-08-29T02:00:00Z")
        paths = artifact_store_paths(self.data, 7)
        root = paths.versions_root / first["version_id"]
        os.chmod(root, 0o750)
        os.chmod(root / "article.json", 0o640)
        (root / "article.json").write_bytes(b"corrupt")
        with self.assertRaisesRegex(ArtifactVersionError, "checksum_mismatch"):
            apply_retention(self.data, 7, keep=1, dry_run=False)
        self.assertTrue(root.is_dir())


if __name__ == "__main__":
    unittest.main()

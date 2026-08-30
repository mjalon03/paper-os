from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

from paper_service.conversion_lock import (
    ConversionLockFailure,
    exclusive_conversion_lock,
)
from paper_service.metadata import MetadataFailure
from paper_service import parser_client, parser_service, parser_task
from paper_service.reader import ReaderFailure


class FakeHeaders:
    def __init__(self, content_type: str):
        self.content_type = content_type

    def get_content_type(self):
        return self.content_type


class FakeResponse:
    def __init__(self, body: bytes, content_type="application/json"):
        self.body = body
        self.headers = FakeHeaders(content_type)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, maximum):
        return self.body[:maximum]


def valid_reader_bundle(paper_id: int) -> bytes:
    manifest = {
        "schema_version": 1,
        "reader_version": 1,
        "paper_id": paper_id,
        "title": "Paper",
        "authors": ["Author"],
        "year": 2026,
        "doi": None,
        "category": "NeuroAI",
        "paper_type": "Research",
        "page_count": 1,
        "headings": [],
        "blocks": [
            {
                "kind": "caption",
                "text": "Figure 1",
                "page": 1,
                "page_asset": "assets/page-001.jpg",
            }
        ],
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("manifest.json", json.dumps(manifest))
        bundle.writestr("assets/page-001.jpg", b"jpeg")
    return output.getvalue()


class ParserServiceIsolationTests(unittest.TestCase):
    def test_child_environment_is_allowlisted_and_scrubs_all_credentials(self):
        environment = parser_service._child_environment(
            {
                "PATH": "/safe/bin",
                "DEEPSEEK_API_KEY": "secret",
                "PAPER_DEEPSEEK_API_KEY_FILE": "/run/secrets/deepseek",
                "PAPER_RCLONE_CONFIG": "/config/rclone/rclone.conf",
                "PAPER_DB_PASSWORD": "database-secret",
                "AWS_SECRET_ACCESS_KEY": "cloud-secret",
            }
        )
        self.assertEqual(environment["PATH"], "/safe/bin")
        self.assertEqual(
            set(environment),
            {
                "PATH",
                "LANG",
                "LC_ALL",
                "PYTHONUNBUFFERED",
                "PYTHONDONTWRITEBYTECODE",
                "PAPER_PARSER_CHILD",
            },
        )
        serialized = json.dumps(environment)
        for forbidden in ("secret", "/run/secrets", "/config/rclone", "PAPER_DB"):
            self.assertNotIn(forbidden, serialized)

    def test_source_is_copied_to_private_file_and_bound_to_exact_sha(self):
        content = b"%PDF-1.7\nverified bytes"
        with tempfile.TemporaryDirectory() as temporary_name:
            root = Path(temporary_name)
            source = root / "library" / "raw" / "aa" / "paper.pdf"
            source.parent.mkdir(parents=True)
            source.write_bytes(content)
            destination = root / "copy.pdf"
            with patch.dict(os.environ, {"PAPER_DATA_DIR": str(root)}, clear=True):
                parser_service._copy_verified_pdf(
                    "aa/paper.pdf",
                    hashlib.sha256(content).hexdigest(),
                    destination,
                    expected_size=len(content),
                )
            self.assertEqual(destination.read_bytes(), content)
            self.assertEqual(destination.stat().st_mode & 0o777, 0o600)

            other = root / "other.pdf"
            with patch.dict(os.environ, {"PAPER_DATA_DIR": str(root)}, clear=True):
                with self.assertRaisesRegex(
                    parser_service.ParserServiceFailure,
                    "source_sha_mismatch",
                ):
                    parser_service._copy_verified_pdf(
                        "aa/paper.pdf",
                        "0" * 64,
                        other,
                        expected_size=len(content),
                    )

    def test_symlink_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary_name:
            root = Path(temporary_name)
            raw = root / "library" / "raw"
            raw.mkdir(parents=True)
            target = root / "target.pdf"
            target.write_bytes(b"%PDF-target")
            (raw / "paper.pdf").symlink_to(target)
            with patch.dict(os.environ, {"PAPER_DATA_DIR": str(root)}, clear=True):
                with self.assertRaisesRegex(
                    parser_service.ParserServiceFailure,
                    "parser_source_missing",
                ):
                    parser_service._copy_verified_pdf(
                        "paper.pdf",
                        hashlib.sha256(target.read_bytes()).hexdigest(),
                        root / "copy.pdf",
                    )

    def test_intermediate_symlink_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary_name:
            root = Path(temporary_name)
            raw = root / "library" / "raw"
            raw.mkdir(parents=True)
            outside = root / "outside"
            outside.mkdir()
            target = outside / "paper.pdf"
            target.write_bytes(b"%PDF-target")
            (raw / "linked").symlink_to(outside, target_is_directory=True)
            with patch.dict(os.environ, {"PAPER_DATA_DIR": str(root)}, clear=True):
                with self.assertRaisesRegex(
                    parser_service.ParserServiceFailure,
                    "parser_source_missing",
                ):
                    parser_service._copy_verified_pdf(
                        "linked/paper.pdf",
                        hashlib.sha256(target.read_bytes()).hexdigest(),
                        root / "copy.pdf",
                    )

    def test_child_timeout_kills_process_group_and_never_uses_shell(self):
        process = Mock(pid=321)
        process.wait.side_effect = [subprocess.TimeoutExpired("parser", 10), 0]
        with tempfile.TemporaryDirectory() as temporary_name, patch.object(
            parser_service.subprocess,
            "Popen",
            return_value=process,
        ) as popen, patch.object(parser_service.os, "killpg") as killpg:
            with self.assertRaisesRegex(
                parser_service.ParserServiceFailure, "parser_timeout"
            ):
                parser_service._run_child(
                    {"operation": "metadata"},
                    Path(temporary_name) / "status.json",
                    timeout=10,
                )
        argv = popen.call_args.args[0]
        kwargs = popen.call_args.kwargs
        self.assertEqual(argv[:3], [parser_service.sys.executable, "-m", "paper_service.parser_task"])
        self.assertNotIn("shell", kwargs)
        self.assertNotIn("DEEPSEEK_API_KEY", kwargs["env"])
        self.assertTrue(kwargs["close_fds"])
        self.assertTrue(kwargs["start_new_session"])
        killpg.assert_called_once_with(321, parser_service.signal.SIGKILL)

    def test_status_output_is_hard_capped(self):
        with tempfile.TemporaryDirectory() as temporary_name:
            status = Path(temporary_name) / "status.json"
            status.write_bytes(b"x" * (parser_service.MAX_STATUS_BYTES + 1))
            with self.assertRaisesRegex(
                parser_service.ParserServiceFailure, "parser_status_invalid"
            ):
                parser_service._read_status(status)

    def test_child_resource_limits_cover_cpu_memory_files_and_descriptors(self):
        with patch.object(parser_task.resource, "setrlimit") as set_limit:
            parser_task._apply_resource_limits()
        kinds = {call.args[0] for call in set_limit.call_args_list}
        self.assertTrue(
            {
                parser_task.resource.RLIMIT_CORE,
                parser_task.resource.RLIMIT_CPU,
                parser_task.resource.RLIMIT_AS,
                parser_task.resource.RLIMIT_FSIZE,
                parser_task.resource.RLIMIT_NOFILE,
            }.issubset(kinds)
        )

    def test_shared_conversion_lock_timeout_is_bounded(self):
        with tempfile.TemporaryDirectory() as temporary_name, patch(
            "paper_service.conversion_lock.fcntl.flock",
            side_effect=BlockingIOError,
        ), patch(
            "paper_service.conversion_lock.time.monotonic",
            side_effect=[0.0, 0.0, 1.0],
        ), patch(
            "paper_service.conversion_lock.time.sleep"
        ) as sleep:
            with self.assertRaisesRegex(ConversionLockFailure, "lock_timeout"):
                with exclusive_conversion_lock(
                    Path(temporary_name) / "conversion.lock",
                    timeout_seconds=1,
                ):
                    self.fail("the contended lock must not be entered")
        sleep.assert_called_once()

    def test_reader_scratch_budget_fails_before_child_spawn(self):
        details = Mock(f_bavail=1, f_frsize=1)
        with patch.object(parser_service.os, "statvfs", return_value=details):
            with self.assertRaisesRegex(
                parser_service.ParserServiceFailure,
                "parser_scratch_insufficient",
            ):
                parser_service._require_reader_scratch_budget(Path("/tmp"))


class ParserClientBoundaryTests(unittest.TestCase):
    def test_metadata_response_is_bounded_before_json_parse(self):
        oversized = b"x" * (parser_client.MAX_METADATA_RESPONSE_BYTES + 1)
        with patch.dict(
            os.environ,
            {
                "PAPER_PARSER_URL": "http://paper-parser:8090",
                "PAPER_PARSER_ALLOWED_HOSTS": "paper-parser",
            },
            clear=True,
        ), patch.object(
            parser_client,
            "_open_parser_request",
            return_value=FakeResponse(oversized),
        ):
            with self.assertRaisesRegex(MetadataFailure, "parser_output_too_large"):
                parser_client.extract_pdf_context("aa/paper.pdf", "a" * 64)

    def test_reader_zip_traversal_is_rejected(self):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as bundle:
            bundle.writestr("manifest.json", "{}")
            bundle.writestr("../escape", "bad")
        with self.assertRaisesRegex(ReaderFailure, "reader_output_invalid"):
            parser_client._validated_bundle(output.getvalue(), 7)

    def test_reader_manifest_elements_are_strictly_validated(self):
        bundle_bytes = valid_reader_bundle(7)
        with zipfile.ZipFile(io.BytesIO(bundle_bytes)) as bundle:
            manifest = json.loads(bundle.read("manifest.json"))
        manifest["blocks"][0]["unexpected"] = "field"
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr("manifest.json", json.dumps(manifest))
            bundle.writestr("assets/page-001.jpg", b"jpeg")
        with self.assertRaisesRegex(ReaderFailure, "reader_manifest_invalid"):
            parser_client._validated_bundle(output.getvalue(), 7)

    def test_valid_reader_bundle_is_atomically_installed(self):
        paper = {
            "id": 7,
            "stored_path": "aa/paper.pdf",
            "sha256": "a" * 64,
            "size_bytes": 100,
            "title": "Paper",
            "authors": ["Author"],
            "publication_year": 2026,
            "doi": None,
            "category": "NeuroAI",
            "paper_type": "Research",
        }
        with tempfile.TemporaryDirectory() as temporary_name, patch.object(
            parser_client,
            "_reader_request",
            return_value=valid_reader_bundle(7),
        ):
            root = Path(temporary_name)
            manifest = parser_client.convert_paper(paper, root)
            installed = root / "library" / "mobile" / manifest
            self.assertTrue(installed.is_file())
            self.assertEqual(json.loads(installed.read_text())["paper_id"], 7)
            self.assertTrue(
                (installed.parent / "assets" / "page-001.jpg").is_file()
            )

    def test_parser_url_cannot_be_redirected_to_an_unapproved_host(self):
        with patch.dict(
            os.environ,
            {
                "PAPER_PARSER_URL": "http://attacker.invalid:8090",
                "PAPER_PARSER_ALLOWED_HOSTS": "paper-parser",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(MetadataFailure, "configuration_invalid"):
                parser_client.extract_pdf_context("aa/paper.pdf", "a" * 64)


class ParserComposeIsolationTests(unittest.TestCase):
    def test_parser_and_reader_v1_have_no_external_secrets_or_egress(self):
        compose = (
            Path(__file__).parents[1] / "infra" / "papers" / "compose.yml"
        ).read_text(encoding="utf-8")
        parser_block = compose.split("  paper-parser:\n", 1)[1].split(
            "\n  paper-worker:\n", 1
        )[0]
        self.assertIn("paper_data:/data:ro", parser_block)
        self.assertIn("paper_parser_internal", parser_block)
        for forbidden in (
            "PAPER_DB_",
            "DEEPSEEK",
            "rclone",
            "/run/secrets",
            "paper_egress",
        ):
            self.assertNotIn(forbidden, parser_block)

        reader_block = compose.split("  paper-reader-v1-worker:\n", 1)[1].split(
            "\n  paper-reader-v2-converter:\n", 1
        )[0]
        self.assertIn("paper_parser_internal", reader_block)
        for forbidden in ("DEEPSEEK", "rclone", "/run/secrets", "paper_egress"):
            self.assertNotIn(forbidden, reader_block)

        worker_source = (
            Path(__file__).parents[1] / "paper_service" / "worker.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("convert_paper", worker_source)
        self.assertNotIn("extract_pdf_context", worker_source)


if __name__ == "__main__":
    unittest.main()

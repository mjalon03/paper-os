from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from paper_service.jats import parse_jats
from paper_service import (
    pmc_broker_client,
    pmc_broker_service,
    reader_v2_converter_client,
    reader_v2_converter_service,
)
from paper_service.pmc_broker_contract import (
    PMC_BROKER_METADATA_NAME,
    PMCBrokerContractFailure,
    build_request as build_pmc_request,
    canonical_json_bytes,
    payload_sha256,
    result_metadata,
)
from paper_service.pmc_broker_client import PMCBrokerClientFailure
from paper_service.reader_v2 import write_article
from paper_service.reader_v2_job import build_output_manifest, write_private_json


JATS = b"""<article><front><article-meta>
<article-id pub-id-type="doi">10.1234/boundary.1</article-id>
<title-group><article-title>Boundary article</article-title></title-group>
</article-meta></front><body><sec id="results"><title>Results</title>
<p>Verified result.</p></sec></body></article>"""


def paper(source: bytes = b"%PDF-1.7\nsource") -> dict:
    return {
        "id": 7,
        "title": "Boundary article",
        "authors": ["Minjae Jo"],
        "publication_year": 2026,
        "doi": "https://doi.org/10.1234/BOUNDARY.1",
        "stored_path": "aa/paper.pdf",
        "sha256": hashlib.sha256(source).hexdigest(),
    }


def article(source: str) -> dict:
    return parse_jats(JATS, {"source": source})


class PMCBrokerContractTests(unittest.TestCase):
    def test_request_normalizes_doi_and_rejects_arbitrary_shape(self):
        request = build_pmc_request(identifier="pmc-" + "a" * 32, paper=paper())
        self.assertEqual(request["paper"]["doi"], "10.1234/boundary.1")
        with self.assertRaises(PMCBrokerContractFailure):
            build_pmc_request(
                identifier="pmc-" + "a" * 32,
                paper={**paper(), "doi": "https://example.com/private"},
            )


class PMCBrokerBundleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.request = build_pmc_request(
            identifier="pmc-" + "b" * 32,
            paper=paper(),
        )

    def tearDown(self):
        self.temporary.cleanup()

    def _service_bundle(self) -> Path:
        with patch(
            "paper_service.pmc_broker_service.build_pmc_article",
            return_value=article("pmc_jats"),
        ):
            bundle = pmc_broker_service.build_bundle(self.request, self.root)
        self.assertIsNotNone(bundle)
        return bundle

    def test_service_bundle_is_request_bound_and_client_validates_it(self):
        output = self.root / "client-output"
        output.mkdir()

        def download(request_value, destination):
            workspace = self.root / request_value["request_id"]
            workspace.mkdir()
            bundle = pmc_broker_service.build_bundle(request_value, workspace)
            self.assertIsNotNone(bundle)
            shutil.copyfile(bundle, destination)
            return True

        with patch(
            "paper_service.pmc_broker_service.build_pmc_article",
            return_value=article("pmc_jats"),
        ), patch.object(
            pmc_broker_client, "_download_bundle", side_effect=download
        ):
            fetched = pmc_broker_client.fetch_pmc_article(
                self.request["paper"], output
            )

        self.assertEqual(fetched["provenance"]["source"], "pmc_jats")
        self.assertEqual(
            json.loads((output / "article.json").read_text())["identity"]["title"],
            "Boundary article",
        )

    def test_bundle_for_other_request_is_rejected(self):
        bundle = self._service_bundle()
        other = build_pmc_request(
            identifier="pmc-" + "c" * 32,
            paper=self.request["paper"],
        )
        output = self.root / "other-output"
        output.mkdir()
        with self.assertRaisesRegex(PMCBrokerClientFailure, "result_mismatch"):
            pmc_broker_client._extract_verified_bundle(
                bundle,
                output_dir=output,
                request_value=other,
            )

    def test_duplicate_archive_entry_is_rejected(self):
        bundle = self.root / "duplicate.zip"
        artifact = canonical_json_bytes(article("pmc_jats"))
        files = {
            "article.json": {
                "sha256": hashlib.sha256(artifact).hexdigest(),
                "size": len(artifact),
            }
        }
        metadata = result_metadata(
            identifier=self.request["request_id"],
            request_sha256=payload_sha256(self.request),
            files=files,
        )
        with zipfile.ZipFile(bundle, "w") as archive:
            archive.writestr(PMC_BROKER_METADATA_NAME, canonical_json_bytes(metadata))
            archive.writestr("article.json", artifact)
            with self.assertWarns(UserWarning):
                archive.writestr("article.json", artifact)
        output = self.root / "duplicate-output"
        output.mkdir()
        with self.assertRaisesRegex(PMCBrokerClientFailure, "bundle_invalid"):
            pmc_broker_client._extract_verified_bundle(
                bundle,
                output_dir=output,
                request_value=self.request,
            )

    def test_client_configuration_allows_only_named_internal_host(self):
        self.assertEqual(
            pmc_broker_client._base_url(
                {
                    "PAPER_PMC_BROKER_URL": "http://paper-pmc-broker:8092",
                    "PAPER_PMC_BROKER_ALLOWED_HOSTS": "paper-pmc-broker",
                }
            ),
            "http://paper-pmc-broker:8092",
        )
        for url in (
            "https://paper-pmc-broker:8092",
            "http://127.0.0.1:8092",
            "http://paper-pmc-broker:8092/path",
        ):
            with self.subTest(url=url), self.assertRaises(PMCBrokerClientFailure):
                pmc_broker_client._base_url(
                    {
                        "PAPER_PMC_BROKER_URL": url,
                        "PAPER_PMC_BROKER_ALLOWED_HOSTS": "paper-pmc-broker",
                    }
                )


class ReaderV2ConverterBoundaryTests(unittest.TestCase):
    def test_child_environment_is_allowlisted_and_credential_free(self):
        environment = reader_v2_converter_service._child_environment(
            Path("/jobs/job-" + "a" * 32),
            {
                "PATH": "/safe/bin",
                "PAPER_DB_PASSWORD": "database-secret",
                "PAPER_DEEPSEEK_API_KEY_FILE": "/run/secrets/deepseek",
                "PAPER_RCLONE_CONFIG": "/config/rclone/rclone.conf",
                "AWS_SECRET_ACCESS_KEY": "cloud-secret",
                "HF_HOME": "/models",
                "DOCLING_ARTIFACTS_PATH": "/models/docling",
            },
        )
        serialized = json.dumps(environment)
        self.assertEqual(environment["PATH"], "/safe/bin")
        self.assertEqual(environment["HF_HOME"], "/models")
        self.assertEqual(environment["DOCLING_ARTIFACTS_PATH"], "/models/docling")
        for forbidden in ("secret", "/run/secrets", "/config/rclone", "PAPER_DB"):
            self.assertNotIn(forbidden, serialized)

    def test_client_and_service_bind_exact_pdf_and_verified_output(self):
        source = b"%PDF-1.7\nexact source"
        with tempfile.TemporaryDirectory() as temporary_name:
            root = Path(temporary_name)
            data = root / "data"
            raw = data / "library" / "raw" / "aa" / "paper.pdf"
            raw.parent.mkdir(parents=True)
            raw.write_bytes(source)
            jobs = root / "jobs"
            jobs.mkdir(mode=0o700)
            locks = root / "locks"
            locks.mkdir()
            output = root / "output"
            output.mkdir()
            value = paper(source)

            def fake_child(job_root: Path, *, timeout: int) -> None:
                self.assertGreaterEqual(timeout, 60)
                child_output = job_root / "output"
                child_output.mkdir(mode=0o700)
                write_article(article("docling_pdf"), child_output)
                manifest = build_output_manifest(child_output)
                write_private_json(
                    job_root / "task-status.json",
                    {"ok": True, "source": "docling_pdf", "files": manifest},
                    mode=0o400,
                )

            def execute(request_value):
                return reader_v2_converter_service.execute_converter_request(
                    request_value
                )

            with patch.dict(
                os.environ,
                {
                    "PAPER_READER_V2_JOBS_DIR": str(jobs),
                    "PAPER_CONVERSION_LOCK_FILE": str(locks / "conversion.lock"),
                    "PAPER_CONVERSION_LOCK_TIMEOUT_SECONDS": "10",
                },
                clear=True,
            ), patch.object(
                reader_v2_converter_service,
                "_run_child",
                side_effect=fake_child,
            ), patch.object(
                reader_v2_converter_client,
                "_post_converter",
                side_effect=execute,
            ):
                converted = reader_v2_converter_client.convert_pdf_to_directory(
                    value,
                    data,
                    output,
                )

            self.assertEqual(converted, "docling_pdf")
            self.assertEqual(
                json.loads((output / "article.json").read_text())["provenance"][
                    "source"
                ],
                "docling_pdf",
            )
            self.assertEqual(
                [path.name for path in jobs.iterdir()],
                [".jobs.lock"],
            )

    def test_converter_service_rejects_wrong_source_hash_before_child(self):
        source = b"%PDF-1.7\nexact source"
        value = paper(source)
        value["sha256"] = "0" * 64
        with tempfile.TemporaryDirectory() as temporary_name:
            root = Path(temporary_name)
            data = root / "data"
            raw = data / "library" / "raw" / "aa" / "paper.pdf"
            raw.parent.mkdir(parents=True)
            raw.write_bytes(source)
            jobs = root / "jobs"
            jobs.mkdir()
            output = root / "output"
            output.mkdir()
            with patch.dict(
                os.environ,
                {"PAPER_READER_V2_JOBS_DIR": str(jobs)},
                clear=True,
            ), self.assertRaisesRegex(
                reader_v2_converter_client.ReaderV2ConverterClientFailure,
                "boundary_failed",
            ):
                reader_v2_converter_client.convert_pdf_to_directory(
                    value, data, output
                )


class ReaderV2ComposeBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        repository = Path(__file__).resolve().parents[1]
        cls.compose = (repository / "infra" / "papers" / "compose.yml").read_text()

    def _block(self, start: str, end: str) -> str:
        return self.compose.split(f"  {start}:\n", 1)[1].split(
            f"\n  {end}:\n", 1
        )[0]

    def _service_blocks(self) -> dict[str, str]:
        section = self.compose.split("services:\n", 1)[1].split("\nvolumes:\n", 1)[0]
        matches = list(re.finditer(r"(?m)^  ([a-z0-9-]+):\n", section))
        return {
            match.group(1): section[
                match.end() : matches[index + 1].start()
                if index + 1 < len(matches)
                else len(section)
            ]
            for index, match in enumerate(matches)
        }

    def test_converter_has_only_projected_jobs_models_and_lock(self):
        block = self._block("paper-reader-v2-converter", "paper-pmc-broker")
        for expected in (
            "paper_reader_v2_jobs:/jobs",
            "paper_reader_v2_models:/models:ro",
            "paper_conversion_lock:/locks",
            "paper_reader_v2_internal",
        ):
            self.assertIn(expected, block)
        for forbidden in (
            "PAPER_DB_",
            "PAPER_DATA_DIR",
            "paper_data:/data",
            "PAPER_DEEPSEEK",
            "rclone",
            "/run/secrets",
            "paper_egress",
            "paper_internal",
            "paper_pmc_internal",
        ):
            self.assertNotIn(forbidden, block)

    def test_pmc_broker_has_egress_but_no_durable_or_secret_mount(self):
        block = self._block("paper-pmc-broker", "paper-reader-v2-worker")
        self.assertIn("paper_pmc_scratch:/work/pmc", block)
        self.assertIn("paper_pmc_internal", block)
        self.assertIn("paper_egress", block)
        for forbidden in (
            "PAPER_DB_",
            "paper_data:/data",
            "paper_reader_v2_jobs:/jobs",
            "paper_reader_v2_models:/models",
            "PAPER_DEEPSEEK",
            "rclone",
            "/run/secrets",
            "paper_internal",
            "paper_reader_v2_internal",
        ):
            self.assertNotIn(forbidden, block)

    def test_coordinator_has_data_and_control_networks_but_no_egress_or_models(self):
        block = self._block("paper-reader-v2-worker", "paper-knowledge-worker")
        for expected in (
            "paper_data:/data",
            "paper_reader_v2_jobs:/jobs",
            "paper_reader_v2_internal",
            "paper_pmc_internal",
            "paper_internal",
        ):
            self.assertIn(expected, block)
        for forbidden in (
            "paper_egress",
            "paper_reader_v2_models:/models",
            "paper_conversion_lock:/locks",
            "PAPER_DEEPSEEK",
            "rclone",
            "/run/secrets",
        ):
            self.assertNotIn(forbidden, block)

    def test_disabled_ops_webhook_has_no_default_egress(self):
        block = self.compose.split("  paper-ops-monitor:\n", 1)[1].split(
            "\nvolumes:\n", 1
        )[0]
        self.assertIn("paper_internal", block)
        self.assertNotIn("paper_egress", block)
        self.assertNotIn("/run/secrets", block)

    def test_global_network_and_secret_matrix_is_least_privilege(self):
        blocks = self._service_blocks()
        egress = {
            name for name, block in blocks.items() if "- paper_egress" in block
        }
        self.assertEqual(
            egress,
            {
                "paper-api",
                "paper-knowledge-worker",
                "paper-pmc-broker",
                "paper-qa-worker",
                "paper-taxonomy-worker",
                "paper-worker",
            },
        )
        web = {name for name, block in blocks.items() if "- web" in block}
        self.assertEqual(web, {"paper-api"})
        self.assertNotIn("\n    ports:", self.compose)
        deepseek = {
            name
            for name, block in blocks.items()
            if "/run/secrets/deepseek_api_key" in block
        }
        self.assertEqual(
            deepseek,
            {
                "paper-worker",
                "paper-knowledge-worker",
                "paper-qa-worker",
                "paper-taxonomy-worker",
            },
        )
        rclone = {
            name for name, block in blocks.items() if "secrets/rclone" in block
        }
        self.assertEqual(rclone, {"paper-worker", "paper-taxonomy-worker"})

    def test_compose_variables_and_example_are_exactly_synchronized(self):
        repository = Path(__file__).resolve().parents[1]
        example = (repository / "infra" / "papers" / ".env.example").read_text()
        compose_variables = set(re.findall(r"\$\{([A-Z0-9_]+)", self.compose))
        example_variables = set(
            re.findall(r"(?m)^([A-Z0-9_]+)=", example)
        )
        self.assertEqual(compose_variables, example_variables)


if __name__ == "__main__":
    unittest.main()

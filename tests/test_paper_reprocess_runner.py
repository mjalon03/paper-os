from __future__ import annotations

import copy
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from paper_service.artifact_versions import (
    ArtifactVersionError,
    StagedArtifactVersion,
    activate_staged_version,
    artifact_store_paths,
    build_version_descriptor,
    create_staged_version,
    resolve_current_artifact,
    rollback_current_version,
)
from paper_service.jats import parse_jats
from paper_service.ops_store import OperationsLeaseLost
from paper_service.reader_v2 import (
    PreparedPaperV2Version,
    ReaderV2Failure,
    activate_prepared_paper_v2_version,
    prepare_paper_v2_version,
    resume_prepared_paper_v2_version,
)
from paper_service.reader_v2_worker import (
    parse_operations_enabled,
    prepare_reprocess_isolated,
)
from paper_service.reprocess_runner import (
    _actual_source_sha256,
    run_cycle,
    run_once,
)


JOB_ID = "f7347b69-0443-54d6-9a3e-f37c995af301"
ROLLBACK_JOB_ID = "7c407f66-1ac3-4f54-a784-bcfb004572df"
REQUEST_ID = "7b2acb2a-0707-5c12-a05b-e46292cd6b21"
LEASE_TOKEN = "8da71e38-b8e0-46aa-aa5f-9edb5129b4f8"
OLD_VERSION = "rv-" + "1" * 32
NEW_VERSION = "rv-" + "2" * 32
TARGET_VERSION = "rv-" + "3" * 32
SOURCE_SHA = "a" * 64


def reprocess_job(*, planned=None):
    return {
        "id": JOB_ID,
        "request_id": REQUEST_ID,
        "paper_id": 7,
        "status": "leased",
        "lease_token": LEASE_TOKEN,
        "planned_result_version_id": planned,
    }


def rollback_job():
    return {
        "id": ROLLBACK_JOB_ID,
        "request_id": REQUEST_ID,
        "paper_id": 7,
        "status": "leased",
        "lease_token": LEASE_TOKEN,
    }


def paper():
    return {
        "id": 7,
        "stored_path": "aa/paper.pdf",
        "sha256": SOURCE_SHA,
    }


def prepared(
    data_dir: Path,
    *,
    version_id=NEW_VERSION,
    expected_current_version_id=OLD_VERSION,
):
    staged = StagedArtifactVersion(
        data_dir=data_dir,
        paper_id=7,
        version_id=version_id,
        path=data_dir / "unused-stage",
        descriptor={},
        raw_source_relative="aa/paper.pdf",
    )
    return PreparedPaperV2Version(
        data_dir=data_dir,
        paper_id=7,
        logical_manifest_path="7/v2/article.json",
        source="docling_pdf",
        source_sha256=SOURCE_SHA,
        version_id=version_id,
        expected_current_version_id=expected_current_version_id,
        staged=staged,
    )


def authorized_reprocess(*, status="running", planned=None, error=None):
    return {
        "id": JOB_ID,
        "status": status,
        "planned_result_version_id": planned,
        "last_error_code": error,
    }


def authorized_rollback(*, status="running", error=None):
    return {
        "id": ROLLBACK_JOB_ID,
        "status": status,
        "last_error_code": error,
    }


class RunnerFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.data = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def base_reprocess_patches(self, *, events=None):
        events = [] if events is None else events
        return (
            patch(
                "paper_service.reprocess_runner.claim_reprocess_job",
                side_effect=lambda **_kwargs: events.append("claim") or reprocess_job(),
            ),
            patch("paper_service.reprocess_runner.claim_rollback_job"),
            patch(
                "paper_service.reprocess_runner._load_paper",
                side_effect=lambda _paper_id: events.append("load") or paper(),
            ),
            patch(
                "paper_service.reprocess_runner._actual_source_sha256",
                side_effect=lambda *_args: events.append("sha") or SOURCE_SHA,
            ),
        )


class ReprocessExecutionTests(RunnerFixture):
    def test_legacy_current_none_is_authorized_and_upgraded_to_new_version(self):
        prepared_legacy = prepared(
            self.data, expected_current_version_id=None
        )
        prepare = Mock(return_value=prepared_legacy)
        activate = Mock()
        with patch(
            "paper_service.reprocess_runner.claim_reprocess_job",
            return_value=reprocess_job(),
        ), patch(
            "paper_service.reprocess_runner.claim_rollback_job"
        ), patch(
            "paper_service.reprocess_runner._load_paper", return_value=paper()
        ), patch(
            "paper_service.reprocess_runner._actual_source_sha256",
            return_value=SOURCE_SHA,
        ), patch(
            "paper_service.reprocess_runner._actual_current_version",
            side_effect=[None, None, NEW_VERSION],
        ), patch(
            "paper_service.reprocess_runner.authorize_reprocess_job",
            return_value=authorized_reprocess(),
        ) as authorize, patch(
            "paper_service.reprocess_runner.bind_reprocess_result_version",
            return_value={
                "status": "running",
                "planned_result_version_id": NEW_VERSION,
            },
        ), patch(
            "paper_service.reprocess_runner.complete_reprocess_job",
            return_value={
                "id": JOB_ID,
                "status": "succeeded",
                "result_version_id": NEW_VERSION,
            },
        ):
            result = run_once(
                worker_id="reader-worker",
                data_dir=self.data,
                prepare_version=prepare,
                activate_version=activate,
            )

        self.assertEqual(result.status, "succeeded")
        self.assertIsNone(
            authorize.call_args.kwargs["actual_current_version_id"]
        )
        self.assertIsNone(
            prepare.return_value.expected_current_version_id
        )
        activate.assert_called_once_with(prepared_legacy)

    def test_reprocess_binds_staged_version_before_pointer_publish(self):
        events = []
        claim_patch, rollback_claim_patch, load_patch, sha_patch = (
            self.base_reprocess_patches(events=events)
        )
        prepare = Mock(
            side_effect=lambda *_args: events.append("prepare")
            or prepared(self.data)
        )
        activate = Mock(side_effect=lambda _value: events.append("activate"))
        with claim_patch, rollback_claim_patch, load_patch, sha_patch, patch(
            "paper_service.reprocess_runner._actual_current_version",
            side_effect=lambda *_args: events.append("current")
            or [OLD_VERSION, OLD_VERSION, NEW_VERSION][events.count("current") - 1],
        ), patch(
            "paper_service.reprocess_runner.authorize_reprocess_job",
            side_effect=lambda *_args, **_kwargs: events.append("authorize")
            or authorized_reprocess(),
        ), patch(
            "paper_service.reprocess_runner.bind_reprocess_result_version",
            side_effect=lambda *_args, **kwargs: events.append("bind")
            or {
                "status": "running",
                "planned_result_version_id": kwargs["planned_result_version_id"],
            },
        ) as bind, patch(
            "paper_service.reprocess_runner.complete_reprocess_job",
            side_effect=lambda *_args, **_kwargs: events.append("complete")
            or {
                "id": JOB_ID,
                "status": "succeeded",
                "result_version_id": NEW_VERSION,
            },
        ), patch("paper_service.reprocess_runner.nack_reprocess_job") as nack:
            result = run_once(
                worker_id="reader-worker",
                data_dir=self.data,
                prepare_version=prepare,
                activate_version=activate,
            )

        self.assertEqual(result.status, "succeeded")
        self.assertFalse(result.recovered)
        self.assertLess(events.index("authorize"), events.index("prepare"))
        self.assertLess(events.index("prepare"), events.index("bind"))
        self.assertLess(events.index("bind"), events.index("activate"))
        self.assertLess(events.index("activate"), events.index("complete"))
        self.assertEqual(events.count("current"), 3)
        self.assertEqual(
            bind.call_args.kwargs["planned_result_version_id"], NEW_VERSION
        )
        nack.assert_not_called()

    def test_authorization_stale_fails_without_build_publish_or_nack(self):
        prepare = Mock()
        activate = Mock()
        claim_patch, rollback_claim_patch, load_patch, sha_patch = (
            self.base_reprocess_patches()
        )
        with claim_patch, rollback_claim_patch, load_patch, sha_patch, patch(
            "paper_service.reprocess_runner._actual_current_version",
            return_value=OLD_VERSION,
        ), patch(
            "paper_service.reprocess_runner.authorize_reprocess_job",
            return_value=authorized_reprocess(
                status="failed",
                error="reprocess_source_sha256_stale",
            ),
        ), patch(
            "paper_service.reprocess_runner.bind_reprocess_result_version"
        ) as bind, patch(
            "paper_service.reprocess_runner.nack_reprocess_job"
        ) as nack:
            result = run_once(
                worker_id="reader-worker",
                data_dir=self.data,
                prepare_version=prepare,
                activate_version=activate,
            )

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error_code, "reprocess_source_sha256_stale")
        prepare.assert_not_called()
        bind.assert_not_called()
        activate.assert_not_called()
        nack.assert_not_called()

    def test_authorize_recovery_success_has_no_external_mutation(self):
        prepare = Mock()
        resume = Mock()
        activate = Mock()
        with patch(
            "paper_service.reprocess_runner.claim_reprocess_job",
            return_value=reprocess_job(planned=NEW_VERSION),
        ), patch(
            "paper_service.reprocess_runner._load_paper", return_value=paper()
        ), patch(
            "paper_service.reprocess_runner._actual_source_sha256",
            return_value=SOURCE_SHA,
        ), patch(
            "paper_service.reprocess_runner._actual_current_version",
            return_value=NEW_VERSION,
        ), patch(
            "paper_service.reprocess_runner.authorize_reprocess_job",
            return_value=authorized_reprocess(
                status="succeeded", planned=NEW_VERSION
            ),
        ), patch(
            "paper_service.reprocess_runner.bind_reprocess_result_version"
        ) as bind, patch(
            "paper_service.reprocess_runner.complete_reprocess_job"
        ) as complete, patch(
            "paper_service.reprocess_runner.nack_reprocess_job"
        ) as nack:
            result = run_once(
                worker_id="reader-worker",
                data_dir=self.data,
                prepare_version=prepare,
                resume_version=resume,
                activate_version=activate,
            )

        self.assertEqual(result.status, "succeeded")
        self.assertTrue(result.recovered)
        prepare.assert_not_called()
        resume.assert_not_called()
        bind.assert_not_called()
        activate.assert_not_called()
        complete.assert_not_called()
        nack.assert_not_called()

    def test_bound_prepared_version_is_resumed_after_crash(self):
        resume = Mock(return_value=prepared(self.data))
        prepare = Mock()
        activate = Mock()
        with patch(
            "paper_service.reprocess_runner.claim_reprocess_job",
            return_value=reprocess_job(planned=NEW_VERSION),
        ), patch(
            "paper_service.reprocess_runner._load_paper", return_value=paper()
        ), patch(
            "paper_service.reprocess_runner._actual_source_sha256",
            return_value=SOURCE_SHA,
        ), patch(
            "paper_service.reprocess_runner._actual_current_version",
            side_effect=[OLD_VERSION, OLD_VERSION, NEW_VERSION],
        ), patch(
            "paper_service.reprocess_runner.authorize_reprocess_job",
            return_value=authorized_reprocess(
                status="running", planned=NEW_VERSION
            ),
        ), patch(
            "paper_service.reprocess_runner.bind_reprocess_result_version",
            return_value={
                "status": "running",
                "planned_result_version_id": NEW_VERSION,
            },
        ), patch(
            "paper_service.reprocess_runner.complete_reprocess_job",
            return_value={
                "id": JOB_ID,
                "status": "succeeded",
                "result_version_id": NEW_VERSION,
            },
        ):
            result = run_once(
                worker_id="reader-worker",
                data_dir=self.data,
                prepare_version=prepare,
                resume_version=resume,
                activate_version=activate,
            )

        self.assertEqual(result.status, "succeeded")
        prepare.assert_not_called()
        resume.assert_called_once_with(
            paper(),
            self.data,
            NEW_VERSION,
            expected_current_version_id=OLD_VERSION,
        )
        activate.assert_called_once()

    def test_lease_loss_before_publish_never_nacks_or_activates(self):
        activate = Mock()
        with patch(
            "paper_service.reprocess_runner.claim_reprocess_job",
            return_value=reprocess_job(),
        ), patch(
            "paper_service.reprocess_runner._load_paper", return_value=paper()
        ), patch(
            "paper_service.reprocess_runner._actual_source_sha256",
            return_value=SOURCE_SHA,
        ), patch(
            "paper_service.reprocess_runner._actual_current_version",
            return_value=OLD_VERSION,
        ), patch(
            "paper_service.reprocess_runner.authorize_reprocess_job",
            return_value=authorized_reprocess(),
        ), patch(
            "paper_service.reprocess_runner.bind_reprocess_result_version",
            side_effect=OperationsLeaseLost("secret lease detail"),
        ), patch(
            "paper_service.reprocess_runner.nack_reprocess_job"
        ) as nack:
            result = run_once(
                worker_id="reader-worker",
                data_dir=self.data,
                prepare_version=lambda *_args: prepared(self.data),
                activate_version=activate,
            )

        self.assertEqual(result.status, "lease_lost")
        self.assertEqual(result.error_code, "reprocess_lease_lost")
        activate.assert_not_called()
        nack.assert_not_called()

    def test_unknown_error_is_sanitized_and_retried(self):
        with patch(
            "paper_service.reprocess_runner.claim_reprocess_job",
            return_value=reprocess_job(),
        ), patch(
            "paper_service.reprocess_runner._load_paper", return_value=paper()
        ), patch(
            "paper_service.reprocess_runner._actual_source_sha256",
            return_value=SOURCE_SHA,
        ), patch(
            "paper_service.reprocess_runner._actual_current_version",
            return_value=OLD_VERSION,
        ), patch(
            "paper_service.reprocess_runner.authorize_reprocess_job",
            return_value=authorized_reprocess(),
        ), patch(
            "paper_service.reprocess_runner.nack_reprocess_job",
            return_value={"status": "retry_wait"},
        ) as nack:
            result = run_once(
                worker_id="reader-worker",
                data_dir=self.data,
                prepare_version=Mock(
                    side_effect=RuntimeError(
                        "question/body/path/API-key must never escape"
                    )
                ),
            )

        self.assertEqual(result.status, "retry_wait")
        self.assertEqual(result.error_code, "reprocess_execution_failed")
        self.assertEqual(
            nack.call_args.kwargs["error_code"], "reprocess_execution_failed"
        )
        self.assertNotIn("API", repr(result))

    def test_lease_lost_during_complete_is_not_nacked(self):
        with patch(
            "paper_service.reprocess_runner.claim_reprocess_job",
            return_value=reprocess_job(),
        ), patch(
            "paper_service.reprocess_runner._load_paper", return_value=paper()
        ), patch(
            "paper_service.reprocess_runner._actual_source_sha256",
            return_value=SOURCE_SHA,
        ), patch(
            "paper_service.reprocess_runner._actual_current_version",
            side_effect=[OLD_VERSION, OLD_VERSION, NEW_VERSION],
        ), patch(
            "paper_service.reprocess_runner.authorize_reprocess_job",
            return_value=authorized_reprocess(),
        ), patch(
            "paper_service.reprocess_runner.bind_reprocess_result_version",
            return_value={
                "status": "running",
                "planned_result_version_id": NEW_VERSION,
            },
        ), patch(
            "paper_service.reprocess_runner.complete_reprocess_job",
            side_effect=OperationsLeaseLost("expired"),
        ), patch(
            "paper_service.reprocess_runner.nack_reprocess_job"
        ) as nack:
            result = run_once(
                worker_id="reader-worker",
                data_dir=self.data,
                prepare_version=lambda *_args: prepared(self.data),
                activate_version=Mock(),
            )

        self.assertEqual(result.status, "lease_lost")
        nack.assert_not_called()


class RollbackExecutionTests(RunnerFixture):
    def target_request(self):
        return {
            "request_id": REQUEST_ID,
            "decision": "approved",
            "request": {
                "targets": [
                    {
                        "paper_id": 7,
                        "rollback_to_version_id": TARGET_VERSION,
                    }
                ]
            },
        }

    def test_rollback_authorizes_before_atomic_pointer_change_then_completes(self):
        events = []
        with patch(
            "paper_service.reprocess_runner.claim_rollback_job",
            side_effect=lambda **_kwargs: events.append("claim") or rollback_job(),
        ), patch(
            "paper_service.reprocess_runner.claim_reprocess_job"
        ), patch(
            "paper_service.reprocess_runner._load_paper",
            side_effect=lambda _paper_id: events.append("load") or paper(),
        ), patch(
            "paper_service.reprocess_runner._actual_source_sha256",
            side_effect=lambda *_args: events.append("sha") or SOURCE_SHA,
        ), patch(
            "paper_service.reprocess_runner._actual_current_version",
            side_effect=lambda *_args: events.append("current")
            or [OLD_VERSION, OLD_VERSION, TARGET_VERSION][events.count("current") - 1],
        ), patch(
            "paper_service.reprocess_runner.authorize_rollback_job",
            side_effect=lambda *_args, **_kwargs: events.append("authorize")
            or authorized_rollback(),
        ), patch(
            "paper_service.reprocess_runner.get_rollback_request",
            side_effect=lambda _request_id: events.append("target")
            or self.target_request(),
        ), patch(
            "paper_service.reprocess_runner.rollback_current_version",
            side_effect=lambda *_args, **_kwargs: events.append("rollback")
            or {
                "paper_id": 7,
                "from_version_id": OLD_VERSION,
                "to_version_id": TARGET_VERSION,
                "dry_run": False,
                "changed": True,
            },
        ) as rollback, patch(
            "paper_service.reprocess_runner.complete_rollback_job",
            side_effect=lambda *_args, **_kwargs: events.append("complete")
            or {
                "id": ROLLBACK_JOB_ID,
                "status": "succeeded",
                "result_version_id": TARGET_VERSION,
            },
        ), patch("paper_service.reprocess_runner.nack_rollback_job") as nack:
            result = run_once(
                worker_id="reader-worker",
                data_dir=self.data,
                preferred_operation="rollback",
            )

        self.assertEqual(result.status, "succeeded")
        self.assertLess(events.index("authorize"), events.index("rollback"))
        self.assertLess(events.index("rollback"), events.index("complete"))
        self.assertIs(rollback.call_args.kwargs["dry_run"], False)
        self.assertEqual(
            rollback.call_args.kwargs["expected_current_version_id"],
            OLD_VERSION,
        )
        nack.assert_not_called()

    def test_rollback_recovery_does_not_move_pointer_again(self):
        with patch(
            "paper_service.reprocess_runner.claim_rollback_job",
            return_value=rollback_job(),
        ), patch(
            "paper_service.reprocess_runner._load_paper", return_value=paper()
        ), patch(
            "paper_service.reprocess_runner._actual_source_sha256",
            return_value=SOURCE_SHA,
        ), patch(
            "paper_service.reprocess_runner._actual_current_version",
            return_value=TARGET_VERSION,
        ), patch(
            "paper_service.reprocess_runner.authorize_rollback_job",
            return_value=authorized_rollback(status="succeeded"),
        ), patch(
            "paper_service.reprocess_runner.get_rollback_request"
        ) as target, patch(
            "paper_service.reprocess_runner.rollback_current_version"
        ) as rollback, patch(
            "paper_service.reprocess_runner.complete_rollback_job"
        ) as complete, patch(
            "paper_service.reprocess_runner.nack_rollback_job"
        ) as nack:
            result = run_once(
                worker_id="reader-worker",
                data_dir=self.data,
                preferred_operation="rollback",
            )

        self.assertEqual(result.status, "succeeded")
        self.assertTrue(result.recovered)
        target.assert_not_called()
        rollback.assert_not_called()
        complete.assert_not_called()
        nack.assert_not_called()

    def test_pointer_change_after_authorize_fails_closed(self):
        with patch(
            "paper_service.reprocess_runner.claim_rollback_job",
            return_value=rollback_job(),
        ), patch(
            "paper_service.reprocess_runner._load_paper", return_value=paper()
        ), patch(
            "paper_service.reprocess_runner._actual_source_sha256",
            return_value=SOURCE_SHA,
        ), patch(
            "paper_service.reprocess_runner._actual_current_version",
            side_effect=[OLD_VERSION, NEW_VERSION],
        ), patch(
            "paper_service.reprocess_runner.authorize_rollback_job",
            return_value=authorized_rollback(),
        ), patch(
            "paper_service.reprocess_runner.get_rollback_request",
            return_value=self.target_request(),
        ), patch(
            "paper_service.reprocess_runner.rollback_current_version"
        ) as rollback, patch(
            "paper_service.reprocess_runner.nack_rollback_job",
            return_value={"status": "retry_wait"},
        ) as nack:
            result = run_once(
                worker_id="reader-worker",
                data_dir=self.data,
                preferred_operation="rollback",
            )

        self.assertEqual(result.status, "retry_wait")
        self.assertEqual(
            result.error_code, "rollback_current_changed_before_publish"
        )
        rollback.assert_not_called()
        nack.assert_called_once()


class SourceAndCycleSafetyTests(RunnerFixture):
    def test_actual_source_hash_uses_no_follow_path_walk(self):
        raw = self.data / "library" / "raw" / "aa" / "paper.pdf"
        raw.parent.mkdir(parents=True)
        payload = b"actual approved source"
        raw.write_bytes(payload)
        self.assertEqual(
            _actual_source_sha256(self.data, paper()),
            hashlib.sha256(payload).hexdigest(),
        )

        outside = self.data / "outside"
        outside.mkdir()
        (outside / "paper.pdf").write_bytes(b"must not be followed")
        raw.unlink()
        raw.parent.rmdir()
        (self.data / "library" / "raw" / "aa").symlink_to(outside)
        with self.assertRaisesRegex(
            Exception, "operations_source_unsafe"
        ):
            _actual_source_sha256(self.data, paper())

    def test_cycle_is_strictly_bounded_and_alternates_preference(self):
        outcomes = [
            Mock(status="succeeded"),
            Mock(status="retry_wait"),
            Mock(status="idle"),
        ]
        # Use real safe result objects through the public constructor path.
        from paper_service.reprocess_runner import OperationResult

        outcomes = [
            OperationResult("succeeded", "reprocess", JOB_ID, 7),
            OperationResult(
                "retry_wait",
                "rollback",
                ROLLBACK_JOB_ID,
                7,
                error_code="rollback_execution_failed",
            ),
            OperationResult("idle"),
        ]
        with patch(
            "paper_service.reprocess_runner.run_once", side_effect=outcomes
        ) as once:
            cycle = run_cycle(
                worker_id="reader-worker",
                data_dir=self.data,
                max_jobs=3,
            )
        self.assertEqual(cycle.claimed, 2)
        self.assertEqual(cycle.succeeded, 1)
        self.assertEqual(cycle.failed, 1)
        self.assertEqual(
            [call.kwargs["preferred_operation"] for call in once.call_args_list],
            ["reprocess", "rollback", "reprocess"],
        )

    def test_claim_failure_is_not_reported_as_a_claimed_job(self):
        from paper_service.reprocess_runner import OperationResult

        with patch(
            "paper_service.reprocess_runner.run_once",
            return_value=OperationResult(
                "error", error_code="operations_claim_failed"
            ),
        ):
            cycle = run_cycle(
                worker_id="reader-worker",
                data_dir=self.data,
                max_jobs=3,
            )
        self.assertEqual(cycle.claimed, 0)
        self.assertEqual(cycle.failed, 1)
        self.assertEqual(len(cycle.results), 1)

    def test_configuration_is_strict(self):
        for kwargs in (
            {"worker_id": "bad worker"},
            {"lease_seconds": 59},
            {"retry_delay_seconds": 0},
            {"preferred_operation": "unknown"},
        ):
            values = {
                "worker_id": "reader-worker",
                "data_dir": self.data,
                **kwargs,
            }
            with self.assertRaises(RuntimeError):
                run_once(**values)

    def test_integrated_worker_flag_is_explicit_and_strict(self):
        self.assertFalse(parse_operations_enabled(None))
        self.assertFalse(parse_operations_enabled("false"))
        self.assertFalse(parse_operations_enabled("0"))
        self.assertTrue(parse_operations_enabled("true"))
        self.assertTrue(parse_operations_enabled("1"))
        for invalid in ("True", "yes", " true ", "2"):
            with self.assertRaises(RuntimeError):
                parse_operations_enabled(invalid)

    def test_integrated_reprocess_build_uses_remote_converter_boundary(self):
        expected = prepared(self.data)
        with patch(
            "paper_service.reader_v2_worker.prepare_paper_v2_version",
            return_value=expected,
        ) as prepare:
            actual = prepare_reprocess_isolated(paper(), self.data)

        self.assertIs(actual, expected)
        self.assertEqual(prepare.call_args.args, (paper(), self.data))
        self.assertTrue(prepare.call_args.kwargs["force_reprocess"])
        self.assertTrue(callable(prepare.call_args.kwargs["pmc_article_builder"]))
        self.assertTrue(callable(prepare.call_args.kwargs["pdf_article_converter"]))


JATS = b"""<article xmlns:xlink="http://www.w3.org/1999/xlink">
<front><article-meta><article-id pub-id-type="doi">10.1/staged</article-id>
<title-group><article-title>Staged paper</article-title></title-group></article-meta></front>
<body><sec><title>Results</title><p>Verified result.</p></sec></body></article>"""


class ReaderPrepareActivateTests(RunnerFixture):
    def setUp(self):
        super().setUp()
        self.raw_bytes = b"immutable source"
        raw = self.data / "library" / "raw" / "aa" / "paper.pdf"
        raw.parent.mkdir(parents=True)
        raw.write_bytes(self.raw_bytes)
        self.paper = {
            "id": 7,
            "stored_path": "aa/paper.pdf",
            "sha256": hashlib.sha256(self.raw_bytes).hexdigest(),
            "doi": "10.1/staged",
        }
        self.article = parse_jats(JATS, {"source": "pmc_jats"})

    def test_prepare_is_invisible_until_activation(self):
        with patch(
            "paper_service.reader_v2.build_pmc_article",
            return_value=copy.deepcopy(self.article),
        ):
            staged = prepare_paper_v2_version(
                self.paper, self.data, force_reprocess=True
            )
        with self.assertRaisesRegex(
            ArtifactVersionError, "current_artifact_missing"
        ):
            resolve_current_artifact(self.data, 7)

        activate_prepared_paper_v2_version(staged)
        self.assertEqual(resolve_current_artifact(self.data, 7).version_id, staged.version_id)

    def test_installed_but_unpublished_version_can_be_resumed(self):
        with patch(
            "paper_service.reader_v2.build_pmc_article",
            return_value=copy.deepcopy(self.article),
        ):
            first = prepare_paper_v2_version(
                self.paper, self.data, force_reprocess=True
            )
        activate_prepared_paper_v2_version(first)
        old = resolve_current_artifact(self.data, 7).version_id

        with patch(
            "paper_service.reader_v2.build_pmc_article",
            return_value=copy.deepcopy(self.article),
        ):
            second = prepare_paper_v2_version(
                self.paper, self.data, force_reprocess=True
            )

        def fail_after_install(point):
            if point == "after_version_install":
                raise RuntimeError("simulated crash boundary")

        with self.assertRaisesRegex(
            ReaderV2Failure, "reader_v2_artifact_publish_failed"
        ):
            activate_prepared_paper_v2_version(
                second, failure_injector=fail_after_install
            )
        self.assertEqual(resolve_current_artifact(self.data, 7).version_id, old)

        recovered = resume_prepared_paper_v2_version(
            self.paper, self.data, second.version_id
        )
        activate_prepared_paper_v2_version(recovered)
        self.assertEqual(
            resolve_current_artifact(self.data, 7).version_id,
            second.version_id,
        )

    def test_unbound_owned_stage_is_reused_and_unknown_directory_is_preserved(self):
        builder = Mock(return_value=copy.deepcopy(self.article))
        with patch(
            "paper_service.reader_v2.build_pmc_article",
            side_effect=builder,
        ):
            first = prepare_paper_v2_version(
                self.paper, self.data, force_reprocess=True
            )
            unknown = artifact_store_paths(self.data, 7).staging_root / "user-data"
            unknown.mkdir()
            (unknown / "keep.txt").write_text("do not delete", encoding="utf-8")
            retried = prepare_paper_v2_version(
                self.paper, self.data, force_reprocess=True
            )

        self.assertEqual(retried.version_id, first.version_id)
        self.assertEqual(builder.call_count, 1)
        self.assertEqual(
            (unknown / "keep.txt").read_text(encoding="utf-8"),
            "do not delete",
        )
        with self.assertRaisesRegex(
            ArtifactVersionError, "current_artifact_missing"
        ):
            resolve_current_artifact(self.data, 7)

    def test_activation_cas_rejects_pointer_changed_after_authorization(self):
        with patch(
            "paper_service.reader_v2.build_pmc_article",
            return_value=copy.deepcopy(self.article),
        ):
            first = prepare_paper_v2_version(
                self.paper, self.data, force_reprocess=True
            )
        activate_prepared_paper_v2_version(first)
        first_version = first.version_id
        with patch(
            "paper_service.reader_v2.build_pmc_article",
            return_value=copy.deepcopy(self.article),
        ):
            approved_candidate = prepare_paper_v2_version(
                self.paper, self.data, force_reprocess=True
            )

        descriptor = dict(approved_candidate.staged.descriptor)
        stage = approved_candidate.staged.path
        files = {
            relative: (stage / relative).read_bytes()
            for relative in descriptor["files"]
        }
        competing_descriptor = build_version_descriptor(
            paper_id=7,
            source_sha256=descriptor["source_sha256"],
            article_fingerprint=descriptor["article_fingerprint"],
            extractor_version=descriptor["extractor_version"],
            render_version=descriptor["render_version"],
            created_at="2099-01-01T00:00:00Z",
            files=files,
        )
        competing = create_staged_version(
            self.data,
            competing_descriptor,
            files,
            raw_source_relative=self.paper["stored_path"],
        )
        activate_staged_version(
            competing,
            expected_current_version_id=first_version,
        )
        competing_version = competing.version_id

        with self.assertRaisesRegex(
            ReaderV2Failure, "reader_v2_current_changed"
        ):
            activate_prepared_paper_v2_version(approved_candidate)
        self.assertEqual(
            resolve_current_artifact(self.data, 7).version_id,
            competing_version,
        )

    def test_rollback_cas_rejects_pointer_changed_after_authorization(self):
        with patch(
            "paper_service.reader_v2.build_pmc_article",
            return_value=copy.deepcopy(self.article),
        ):
            first = prepare_paper_v2_version(
                self.paper, self.data, force_reprocess=True
            )
        activate_prepared_paper_v2_version(first)
        with patch(
            "paper_service.reader_v2.build_pmc_article",
            return_value=copy.deepcopy(self.article),
        ):
            second = prepare_paper_v2_version(
                self.paper, self.data, force_reprocess=True
            )
        activate_prepared_paper_v2_version(second)

        with self.assertRaisesRegex(
            ArtifactVersionError, "rollback_current_version_mismatch"
        ):
            rollback_current_version(
                self.data,
                7,
                first.version_id,
                raw_source_relative=self.paper["stored_path"],
                dry_run=False,
                expected_current_version_id=first.version_id,
            )
        self.assertEqual(
            resolve_current_artifact(self.data, 7).version_id,
            second.version_id,
        )


if __name__ == "__main__":
    unittest.main()

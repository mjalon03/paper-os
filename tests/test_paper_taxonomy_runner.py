import hashlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from contextlib import nullcontext
from datetime import datetime, timezone
from unittest.mock import Mock, patch
from uuid import uuid4

from paper_service.deepseek_client import DeepSeekCompletion, DeepSeekTokenUsage
from paper_service.taxonomy import (
    DEEPSEEK_PRO_MODEL,
    TaxonomyLeaseLost,
    build_deepseek_pro_request,
    evaluate_taxonomy_audit,
    taxonomy_generation_request_digest,
)
from paper_service import taxonomy_runner as runner


DRIVE_ROOT = "paper-drive:Original"
CATEGORY = "Machine Learning"
CATEGORY_ROOT = f"{DRIVE_ROOT}/{CATEGORY}"


def papers(count=12, *, diverse=True):
    output = []
    for index in range(1, count + 1):
        term = f"domain{index}" if diverse else "shared"
        output.append(
            {
                "id": index,
                "sha256": f"{index:064x}",
                "title": f"Neural representation {term}",
                "category": CATEGORY,
                "drive_path": f"{CATEGORY_ROOT}/paper-{index}.pdf",
                "imported_at": "2026-08-01T00:00:00Z",
                "topic_terms": ["neural", "representation", term],
            }
        )
    return output


def eligible_audit(rows=None):
    source = papers() if rows is None else rows
    return evaluate_taxonomy_audit(
        source,
        category=CATEGORY,
        category_path=CATEGORY_ROOT,
        evaluated_at="2026-08-29T00:00:00Z",
    )


def valid_proposal(request):
    assignments = []
    for index, paper in enumerate(request.source_snapshot["papers"]):
        subtheme = "Representations" if index < 6 else "Learning Systems"
        assignments.append(
            {
                "paper_id": paper["paper_id"],
                "subtheme": subtheme,
            }
        )
    return {
        "subthemes": [
            {
                "name": "Representations",
                "description": "Representation learning methods.",
            },
            {
                "name": "Learning Systems",
                "description": "Learning systems and objectives.",
            },
        ],
        "assignments": assignments,
        "rationale": "Two bounded and sufficiently populated conceptual groups.",
    }


def generation_job(rows=None):
    source = papers() if rows is None else rows
    audit = eligible_audit(source)
    proposal_id = str(uuid4())
    request = build_deepseek_pro_request(
        source,
        audit,
        proposal_id=proposal_id,
    )
    audit_id = str(uuid4())
    actor_id = "human-1"
    job = {
        "id": str(uuid4()),
        "audit_id": audit_id,
        "proposal_id": proposal_id,
        "provider": "deepseek",
        "model": DEEPSEEK_PRO_MODEL,
        "request_actor_kind": "human",
        "request_actor_id": actor_id,
        "metadata_and_cost_acknowledged": True,
        "provider_body_digest": request.request_sha256,
        "max_output_tokens": request.request_body["max_tokens"],
        "attempt_count": 1,
        "lease_token": str(uuid4()),
    }
    job["request_digest"] = taxonomy_generation_request_digest(
        audit_id=audit_id,
        proposal_id=proposal_id,
        provider="deepseek",
        model=DEEPSEEK_PRO_MODEL,
        request_actor_kind="human",
        request_actor_id=actor_id,
        metadata_and_cost_acknowledged=True,
        provider_body_digest=request.request_sha256,
        max_output_tokens=request.request_body["max_tokens"],
    )
    return job, audit, request


class RowsResult:
    def __init__(self, *, one=None, many=None):
        self.one = one
        self.many = [] if many is None else many

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.many


class RecordingDatabase:
    def __init__(self, results):
        self.results = iter(results)
        self.calls = []

    def execute(self, sql, parameters=None):
        self.calls.append((sql, parameters))
        return next(self.results)


class AuditRuntimeTests(unittest.TestCase):
    def test_no_op_is_not_persisted_and_does_not_reset_cadence(self):
        with (
            patch.object(runner, "_load_organized_papers", return_value=papers(11)),
            patch.object(runner, "_existing_eligible_audit", return_value=None),
            patch.object(
                runner,
                "_latest_substantive_checkpoint",
                return_value="2026-08-01T00:00:00Z",
            ),
            patch.object(runner, "persist_taxonomy_audit") as persist,
            patch.object(runner, "enqueue_taxonomy_generation") as enqueue,
        ):
            result = runner.run_audit_cycle(
                evaluated_at="2026-08-29T00:00:00Z"
            )
        self.assertEqual(result.no_op, 1)
        self.assertEqual(result.pending_generation, 0)
        persist.assert_not_called()
        enqueue.assert_not_called()

    def test_eligible_audit_defaults_to_pending_without_provider_enqueue(self):
        persisted_ids = []

        def remember(_audit, *, audit_id):
            persisted_ids.append(audit_id)
            return {"id": audit_id}

        with (
            patch.object(runner, "_load_organized_papers", return_value=papers()),
            patch.object(runner, "_existing_eligible_audit", return_value=None),
            patch.object(runner, "_latest_substantive_checkpoint", return_value=None),
            patch.object(runner, "persist_taxonomy_audit", side_effect=remember),
            patch.object(runner, "enqueue_taxonomy_generation") as enqueue,
        ):
            first = runner.run_audit_cycle(
                evaluated_at="2026-08-29T00:00:00Z"
            )
            second = runner.run_audit_cycle(
                evaluated_at="2026-08-29T00:00:00Z"
            )
        self.assertEqual(first.pending_generation, 1)
        self.assertEqual(first.enqueued, 0)
        self.assertEqual(persisted_ids[0], persisted_ids[1])
        enqueue.assert_not_called()

    def test_scheduled_audit_never_enqueues_provider_work(self):
        audit = eligible_audit()
        audit_id = str(uuid4())
        with (
            patch.object(runner, "_load_organized_papers", return_value=papers()),
            patch.object(
                runner,
                "_existing_eligible_audit",
                return_value=(audit_id, audit),
            ),
            patch.object(runner, "enqueue_generation_for_audit") as enqueue,
        ):
            result = runner.run_audit_cycle(
                evaluated_at="2026-08-29T00:00:00Z",
            )
        self.assertEqual(result.enqueued, 0)
        self.assertEqual(result.pending_generation, 1)
        self.assertEqual(result.reused, 1)
        enqueue.assert_not_called()

    def test_bad_category_path_fails_closed_without_enqueue(self):
        source = papers()
        source[0]["drive_path"] = "paper-drive:Original/Other/paper-1.pdf"
        with (
            patch.object(runner, "_load_organized_papers", return_value=source),
            patch.object(runner, "_existing_eligible_audit", return_value=None),
            patch.object(runner, "persist_taxonomy_audit") as persist,
            patch.object(runner, "enqueue_taxonomy_generation") as enqueue,
        ):
            result = runner.run_audit_cycle(
                evaluated_at="2026-08-29T00:00:00Z",
            )
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.failed, 1)
        persist.assert_not_called()
        enqueue.assert_not_called()

    def test_db_loader_selects_only_minimal_metadata(self):
        row = {
            "id": 1,
            "sha256": "1" * 64,
            "title": "Neural decoding",
            "category": CATEGORY,
            "drive_path": f"{CATEGORY_ROOT}/one.pdf",
            "imported_at": datetime(2026, 8, 1, tzinfo=timezone.utc),
        }
        database = RecordingDatabase([RowsResult(many=[row])])
        with patch.object(runner, "connection", return_value=nullcontext(database)):
            loaded = runner._load_organized_papers()
        sql = database.calls[0][0].casefold()
        self.assertNotIn("abstract", sql)
        self.assertNotIn("body", sql)
        self.assertNotIn("metadata", sql)
        self.assertEqual(set(loaded[0]), {
            "id",
            "sha256",
            "title",
            "category",
            "drive_path",
            "imported_at",
            "topic_terms",
        })
        self.assertLessEqual(len(loaded[0]["topic_terms"]), runner.MAX_TOPIC_TERMS)

    def test_checkpoint_query_excludes_no_op_audits(self):
        database = RecordingDatabase(
            [RowsResult(one={"checkpoint_at": "2026-08-10T00:00:00Z"})]
        )
        with patch.object(runner, "connection", return_value=nullcontext(database)):
            value = runner._latest_substantive_checkpoint(CATEGORY)
        sql = " ".join(database.calls[0][0].split()).casefold()
        self.assertIn("decision = 'request-proposal'", sql)
        self.assertIn("paper_taxonomy_review_events", sql)
        self.assertEqual(value, "2026-08-10T00:00:00Z")

    def test_audit_api_has_no_generation_enqueue_gate(self):
        with patch.object(runner, "_load_organized_papers") as load:
            with self.assertRaises(TypeError):
                runner.run_audit_cycle(enqueue_generation=True)
        load.assert_not_called()

    def test_public_enqueue_reloads_current_metadata_and_is_idempotent(self):
        rows = papers()
        audit = eligible_audit(rows)
        audit_id = str(uuid4())
        stored = {"id": str(uuid4()), "status": "queued"}
        with (
            patch.object(runner, "_load_generation_audit", return_value=audit) as load,
            patch.object(
                runner, "_load_current_snapshot_papers", return_value=rows
            ) as current,
            patch.object(
                runner, "enqueue_taxonomy_generation", return_value=stored
            ) as enqueue,
        ):
            result = runner.enqueue_generation_for_audit(
                audit_id,
                request_actor_kind="human",
                request_actor_id="human-1",
                metadata_and_cost_acknowledged=True,
            )
            repeated = runner.enqueue_generation_for_audit(
                audit_id,
                request_actor_kind="human",
                request_actor_id="human-1",
                metadata_and_cost_acknowledged=True,
            )
        self.assertIs(result, stored)
        self.assertIs(repeated, stored)
        self.assertEqual(load.call_count, 2)
        self.assertEqual(current.call_count, 2)
        first_request = enqueue.call_args_list[0].kwargs["request"]
        second_request = enqueue.call_args_list[1].kwargs["request"]
        self.assertEqual(enqueue.call_args.kwargs["audit_id"], audit_id)
        self.assertEqual(enqueue.call_args.kwargs["request_actor_kind"], "human")
        self.assertEqual(enqueue.call_args.kwargs["request_actor_id"], "human-1")
        self.assertIs(
            enqueue.call_args.kwargs["metadata_and_cost_acknowledged"], True
        )
        self.assertEqual(first_request.proposal_id, second_request.proposal_id)
        self.assertEqual(
            first_request.request_sha256, second_request.request_sha256
        )
        request = second_request
        self.assertEqual(request.source_snapshot, audit["source_snapshot"])
        self.assertEqual(request.request_body["model"], DEEPSEEK_PRO_MODEL)

    def test_public_enqueue_fails_closed_when_current_path_changed(self):
        rows = papers()
        audit = eligible_audit(rows)
        changed = papers()
        changed[0]["drive_path"] = f"{CATEGORY_ROOT}/changed.pdf"
        with (
            patch.object(runner, "_load_generation_audit", return_value=audit),
            patch.object(
                runner, "_load_current_snapshot_papers", return_value=changed
            ),
            patch.object(runner, "enqueue_taxonomy_generation") as enqueue,
        ):
            with self.assertRaisesRegex(Exception, "snapshot_mismatch"):
                runner.enqueue_generation_for_audit(
                    str(uuid4()),
                    request_actor_kind="human",
                    request_actor_id="human-1",
                    metadata_and_cost_acknowledged=True,
                )
        enqueue.assert_not_called()


class GenerationRuntimeTests(unittest.TestCase):
    def test_exact_pro_request_is_rebuilt_then_usage_and_draft_are_stored(self):
        rows = papers()
        rows[0]["abstract"] = "SECRET ABSTRACT"
        rows[0]["body"] = "SECRET FULL TEXT"
        job, audit, expected = generation_job(rows)
        proposal_text = json.dumps(valid_proposal(expected), ensure_ascii=False)
        events = []

        def complete(body, **kwargs):
            events.append("complete")
            serialized = json.dumps(body, ensure_ascii=False)
            self.assertNotIn("SECRET ABSTRACT", serialized)
            self.assertNotIn("SECRET FULL TEXT", serialized)
            self.assertEqual(body["model"], DEEPSEEK_PRO_MODEL)
            self.assertEqual(body["thinking"], {"type": "enabled"})
            self.assertEqual(body["response_format"], {"type": "json_object"})
            return DeepSeekCompletion(
                content=proposal_text,
                usage=DeepSeekTokenUsage(100, 20, 120),
                request_sha256=expected.request_sha256,
                response_sha256="a" * 64,
                provider_request_id=None,
            )

        def record(**kwargs):
            events.append("usage")
            self.assertEqual(kwargs["request_digest"], expected.request_sha256)
            self.assertEqual(kwargs["response_digest"], "a" * 64)
            return {"id": str(uuid4())}

        def store(**kwargs):
            events.append("store")
            self.assertEqual(kwargs["proposal"]["status"], "ai-draft")
            return {"id": kwargs["proposal"]["proposal_id"]}

        with (
            patch.object(runner, "claim_taxonomy_generation", return_value=job),
            patch.object(runner, "_load_generation_audit", return_value=audit),
            patch.object(runner, "_load_current_snapshot_papers", return_value=rows),
            patch.object(runner, "record_taxonomy_usage", side_effect=record),
            patch.object(runner, "store_taxonomy_proposal", side_effect=store),
            patch.object(runner, "nack_taxonomy_generation") as nack,
        ):
            result = runner.execute_generation_one(
                worker_id="taxonomy-worker-1",
                api_key="x" * 32,
                complete=complete,
            )
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(events, ["complete", "usage", "store"])
        nack.assert_not_called()

    def test_invalid_json_records_raw_response_usage_before_nack(self):
        rows = papers()
        job, audit, expected = generation_job(rows)
        events = []

        def complete(_body, **_kwargs):
            return DeepSeekCompletion(
                content="{}",
                usage=DeepSeekTokenUsage(30, 2, 32),
                request_sha256=expected.request_sha256,
                response_sha256="b" * 64,
                provider_request_id=None,
            )

        def record(**kwargs):
            events.append(("usage", kwargs["response_digest"]))
            return {}

        def nack(**kwargs):
            events.append(("nack", kwargs["error_code"]))
            return {"status": "failed"}

        with (
            patch.object(runner, "claim_taxonomy_generation", return_value=job),
            patch.object(runner, "_load_generation_audit", return_value=audit),
            patch.object(runner, "_load_current_snapshot_papers", return_value=rows),
            patch.object(runner, "record_taxonomy_usage", side_effect=record),
            patch.object(runner, "store_taxonomy_proposal") as store,
            patch.object(runner, "nack_taxonomy_generation", side_effect=nack),
        ):
            result = runner.execute_generation_one(
                worker_id="taxonomy-worker-1",
                api_key="x" * 32,
                complete=complete,
            )
        self.assertEqual(result.status, "failed")
        self.assertEqual(events[0], ("usage", "b" * 64))
        self.assertEqual(events[1][0], "nack")
        store.assert_not_called()

    def test_transport_request_digest_mismatch_is_accounted_then_rejected(self):
        rows = papers()
        job, audit, expected = generation_job(rows)
        completion = DeepSeekCompletion(
            content=json.dumps(valid_proposal(expected)),
            usage=DeepSeekTokenUsage(20, 4, 24),
            request_sha256="e" * 64,
            response_sha256="f" * 64,
            provider_request_id=None,
        )
        events = []
        with (
            patch.object(runner, "claim_taxonomy_generation", return_value=job),
            patch.object(runner, "_load_generation_audit", return_value=audit),
            patch.object(runner, "_load_current_snapshot_papers", return_value=rows),
            patch.object(
                runner,
                "record_taxonomy_usage",
                side_effect=lambda **_kwargs: events.append("usage") or {},
            ),
            patch.object(runner, "store_taxonomy_proposal") as store,
            patch.object(
                runner,
                "nack_taxonomy_generation",
                side_effect=lambda **_kwargs: events.append("nack")
                or {"status": "failed"},
            ),
        ):
            result = runner.execute_generation_one(
                worker_id="taxonomy-worker-1",
                api_key="x" * 32,
                complete=Mock(return_value=completion),
            )
        self.assertEqual(result.error_code, "taxonomy_generation_transport_digest_mismatch")
        self.assertEqual(events, ["usage", "nack"])
        store.assert_not_called()

    def test_stale_current_snapshot_never_calls_provider(self):
        rows = papers()
        job, audit, _expected = generation_job(rows)
        changed = papers()
        changed[0]["drive_path"] = f"{CATEGORY_ROOT}/changed.pdf"
        complete = Mock()
        with (
            patch.object(runner, "claim_taxonomy_generation", return_value=job),
            patch.object(runner, "_load_generation_audit", return_value=audit),
            patch.object(
                runner, "_load_current_snapshot_papers", return_value=changed
            ),
            patch.object(
                runner,
                "nack_taxonomy_generation",
                return_value={"status": "failed"},
            ) as nack,
        ):
            result = runner.execute_generation_one(
                worker_id="taxonomy-worker-1",
                api_key="x" * 32,
                complete=complete,
            )
        self.assertEqual(result.status, "failed")
        complete.assert_not_called()
        self.assertFalse(nack.call_args.kwargs["retryable"])

    def test_durable_authorization_is_revalidated_before_provider(self):
        rows = papers()
        for mutation, error_code in (
            ({"request_actor_kind": "service"}, "human_actor_required"),
            (
                {"metadata_and_cost_acknowledged": False},
                "acknowledgement_required",
            ),
            ({"request_actor_id": "different-human"}, "request_digest_mismatch"),
            ({"provider_body_digest": "f" * 64}, "request_digest_mismatch"),
        ):
            with self.subTest(error_code=error_code):
                job, _audit, _expected = generation_job(rows)
                job.update(mutation)
                complete = Mock()
                load_audit = Mock()
                with (
                    patch.object(
                        runner, "claim_taxonomy_generation", return_value=job
                    ),
                    patch.object(
                        runner, "_load_generation_audit", load_audit
                    ),
                    patch.object(
                        runner,
                        "nack_taxonomy_generation",
                        return_value={"status": "failed"},
                    ) as nack,
                ):
                    result = runner.execute_generation_one(
                        worker_id="taxonomy-worker-1",
                        api_key="x" * 32,
                        complete=complete,
                    )
                self.assertIn(error_code, result.error_code)
                complete.assert_not_called()
                load_audit.assert_not_called()
                self.assertFalse(nack.call_args.kwargs["retryable"])

    def test_provider_body_digest_is_rebuilt_and_checked_before_network(self):
        rows = papers()
        job, audit, _expected = generation_job(rows)
        job["provider_body_digest"] = "f" * 64
        job["request_digest"] = taxonomy_generation_request_digest(
            audit_id=job["audit_id"],
            proposal_id=job["proposal_id"],
            provider=job["provider"],
            model=job["model"],
            request_actor_kind=job["request_actor_kind"],
            request_actor_id=job["request_actor_id"],
            metadata_and_cost_acknowledged=job[
                "metadata_and_cost_acknowledged"
            ],
            provider_body_digest=job["provider_body_digest"],
            max_output_tokens=job["max_output_tokens"],
        )
        complete = Mock()
        with (
            patch.object(runner, "claim_taxonomy_generation", return_value=job),
            patch.object(runner, "_load_generation_audit", return_value=audit),
            patch.object(runner, "_load_current_snapshot_papers", return_value=rows),
            patch.object(
                runner,
                "nack_taxonomy_generation",
                return_value={"status": "failed"},
            ) as nack,
        ):
            result = runner.execute_generation_one(
                worker_id="taxonomy-worker-1",
                api_key="x" * 32,
                complete=complete,
            )
        self.assertEqual(
            result.error_code,
            "taxonomy_generation_provider_body_digest_stale",
        )
        complete.assert_not_called()
        self.assertFalse(nack.call_args.kwargs["retryable"])

    def test_lease_loss_never_nacks(self):
        rows = papers()
        job, audit, expected = generation_job(rows)
        completion = DeepSeekCompletion(
            content=json.dumps(valid_proposal(expected)),
            usage=DeepSeekTokenUsage(10, 5, 15),
            request_sha256=expected.request_sha256,
            response_sha256="c" * 64,
            provider_request_id=None,
        )
        with (
            patch.object(runner, "claim_taxonomy_generation", return_value=job),
            patch.object(runner, "_load_generation_audit", return_value=audit),
            patch.object(runner, "_load_current_snapshot_papers", return_value=rows),
            patch.object(runner, "record_taxonomy_usage", return_value={}),
            patch.object(
                runner,
                "store_taxonomy_proposal",
                side_effect=TaxonomyLeaseLost("taxonomy_generation_lease_lost"),
            ),
            patch.object(runner, "nack_taxonomy_generation") as nack,
        ):
            result = runner.execute_generation_one(
                worker_id="taxonomy-worker-1",
                api_key="x" * 32,
                complete=Mock(return_value=completion),
            )
        self.assertEqual(result.status, "lease_lost")
        nack.assert_not_called()

    def test_missing_key_does_not_claim_or_call_network(self):
        with patch.object(runner, "claim_taxonomy_generation") as claim:
            result = runner.execute_generation_one(
                worker_id="taxonomy-worker-1",
                api_key="",
                complete=Mock(),
            )
        self.assertEqual(result.status, "disabled")
        claim.assert_not_called()

    def test_malformed_key_does_not_consume_a_generation_job(self):
        with patch.object(runner, "claim_taxonomy_generation") as claim:
            result = runner.execute_generation_one(
                worker_id="taxonomy-worker-1",
                api_key="too-short",
                complete=Mock(),
            )
        self.assertEqual(result.status, "disabled")
        self.assertEqual(result.error_code, "deepseek_configuration_invalid")
        claim.assert_not_called()

    def test_generation_lease_must_outlive_provider_timeout(self):
        with patch.object(runner, "claim_taxonomy_generation") as claim:
            with self.assertRaisesRegex(
                runner.TaxonomyRuntimeError,
                "taxonomy_generation_lease_too_short",
            ):
                runner.execute_generation_one(
                    worker_id="taxonomy-worker-1",
                    api_key="x" * 32,
                    lease_seconds=180,
                    timeout_seconds=120,
                    complete=Mock(),
                )
        claim.assert_not_called()


class FakeRclone:
    def __init__(self, files, *, ids=None):
        self.files = dict(files)
        self.ids = dict(ids or {})
        self.calls = []
        self.move_behavior = None
        self.after_copy = None
        self.extra_listing_entries = []

    @staticmethod
    def generated_id(path):
        return "drivefile_" + hashlib.sha256(path.encode()).hexdigest()[:32]

    def file_id(self, path):
        return self.ids.setdefault(path, self.generated_id(path))

    def _path_for_id(self, file_id):
        matches = [path for path in self.files if self.file_id(path) == file_id]
        return matches[0] if len(matches) == 1 else None

    def __call__(self, argv, _timeout, capture):
        self.calls.append((list(argv), capture))
        command = argv[1]
        if command == "lsjson":
            parent = argv[2]
            entries = []
            for remote_path in self.files:
                if remote_path.rsplit("/", 1)[0] != parent:
                    continue
                entries.append(
                    {
                        "Name": remote_path.rsplit("/", 1)[1],
                        "IsDir": False,
                        "MimeType": "application/pdf",
                        "ID": self.file_id(remote_path),
                    }
                )
            entries.extend(self.extra_listing_entries)
            return runner.CommandResult(
                0,
                json.dumps(entries).encode(),
            )
        if command == "backend" and argv[2] == "copyid":
            source = self._path_for_id(argv[4])
            local = Path(argv[5])
            if source is None:
                return runner.CommandResult(1)
            local.write_bytes(self.files[source])
            local.chmod(0o600)
            if self.after_copy is not None:
                self.after_copy(self, source)
            return runner.CommandResult(0)
        if command == "backend" and argv[2] == "moveid":
            source = self._path_for_id(argv[4])
            destination = argv[5]
            if source is None:
                return runner.CommandResult(1)
            source_id = self.file_id(source)
            destination_preexisting = destination in self.files
            if self.move_behavior is not None:
                try:
                    return self.move_behavior(self, source, destination)
                finally:
                    if source not in self.files and destination in self.files:
                        self.ids.pop(source, None)
                        self.ids[destination] = source_id
                    elif destination in self.files and not destination_preexisting:
                        self.file_id(destination)
            if destination in self.files or source not in self.files:
                return runner.CommandResult(1)
            self.files[destination] = self.files.pop(source)
            self.ids.pop(source, None)
            self.ids[destination] = source_id
            return runner.CommandResult(0)
        raise AssertionError(f"unexpected fake command: {command}")


class MoveRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        root.chmod(0o700)
        binary = root / "rclone"
        binary.write_text("fake", encoding="utf-8")
        binary.chmod(0o700)
        config = root / "rclone.conf"
        config.write_text("[paper-drive]\ntype = drive\n", encoding="utf-8")
        config.chmod(0o600)
        self.config = runner.MoveRuntimeConfig(
            drive_root=DRIVE_ROOT,
            rclone_binary=binary,
            rclone_config=config,
            timeout_seconds=60,
            temporary_root=root,
        )
        self.content = b"%PDF-1.7\nverified paper bytes"
        self.source = f"{CATEGORY_ROOT}/paper.pdf"
        self.destination = f"{CATEGORY_ROOT}/Neural Systems/paper.pdf"
        self.job = {
            "id": str(uuid4()),
            "status": "running",
            "direction": "apply",
            "proposal_id": str(uuid4()),
            "approval_event_id": str(uuid4()),
            "approval_decision": "approve",
            "rollback_proposal_id": None,
            "rollback_approval_event_id": None,
            "rollback_approval_decision": None,
            "paper_id": 1,
            "source_sha256": hashlib.sha256(self.content).hexdigest(),
            "old_drive_path": self.source,
            "new_drive_path": self.destination,
            "idempotency_key_digest": "d" * 64,
            "lease_token": str(uuid4()),
            "attempt_count": 1,
        }

    def tearDown(self):
        self.temporary.cleanup()

    def execute(self, fake, *, complete=None):
        complete_mock = Mock(return_value={}) if complete is None else complete
        def bind(job, *, observed_source_file_id, worker_id):
            del worker_id
            bound = dict(job)
            existing = bound.get("source_drive_file_id")
            if existing is not None and existing != observed_source_file_id:
                raise runner.TaxonomyRuntimeError("source_file_id_stale")
            bound["source_drive_file_id"] = observed_source_file_id
            self.job["source_drive_file_id"] = observed_source_file_id
            return bound
        with (
            patch.object(runner, "claim_taxonomy_move_job", return_value=self.job),
            patch.object(
                runner,
                "bind_taxonomy_move_source_identity",
                side_effect=bind,
            ),
            patch.object(
                runner, "complete_taxonomy_move_job", complete_mock
            ) as completed,
            patch.object(
                runner,
                "nack_taxonomy_move_job",
                side_effect=lambda **kwargs: {
                    "status": "retry_wait" if kwargs["retryable"] else "failed"
                },
            ) as nack,
        ):
            result = runner.execute_move_one(
                worker_id="taxonomy-worker-1",
                config=self.config,
                command_runner=fake,
            )
        return result, completed, nack

    def test_success_uses_no_overwrite_move_and_verifies_destination_sha(self):
        fake = FakeRclone({self.source: self.content})
        result, completed, nack = self.execute(fake)
        self.assertEqual(result.status, "succeeded")
        self.assertNotIn(self.source, fake.files)
        self.assertEqual(fake.files[self.destination], self.content)
        move_argv = next(
            call[0]
            for call in fake.calls
            if call[0][1:3] == ["backend", "moveid"]
        )
        self.assertEqual(
            move_argv[1:9],
            [
                "backend",
                "moveid",
                "paper-drive:",
                fake.file_id(self.destination),
                self.destination,
                "--immutable",
                "--checksum",
                "--ignore-existing",
            ],
        )
        self.assertIn("--ignore-existing", move_argv)
        copy_argv = next(
            call[0]
            for call in fake.calls
            if call[0][1:3] == ["backend", "copyid"]
        )
        self.assertEqual(copy_argv[1:5], [
            "backend",
            "copyid",
            "paper-drive:",
            fake.file_id(self.destination),
        ])
        listing_calls = [call[0] for call in fake.calls if call[0][1] == "lsjson"]
        self.assertTrue(listing_calls)
        self.assertTrue(all("--drive-skip-shortcuts" in call for call in listing_calls))
        completed.assert_called_once()
        nack.assert_not_called()

    def test_explicitly_approved_rollback_job_uses_same_safe_path(self):
        self.job.update(
            {
                "direction": "rollback",
                "proposal_id": None,
                "approval_event_id": None,
                "approval_decision": None,
                "rollback_proposal_id": str(uuid4()),
                "rollback_approval_event_id": str(uuid4()),
                "rollback_approval_decision": "approve",
            }
        )
        fake = FakeRclone({self.source: self.content})
        result, completed, nack = self.execute(fake)
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(completed.call_args.args[0].direction, "rollback")
        nack.assert_not_called()

    def test_command_crash_after_move_reconciles_identical_sha(self):
        fake = FakeRclone({self.source: self.content})

        def crash_after_move(state, source, destination):
            state.files[destination] = state.files.pop(source)
            raise runner.TaxonomyRuntimeError(
                "taxonomy_rclone_unavailable", retryable=True
            )

        fake.move_behavior = crash_after_move
        result, completed, nack = self.execute(fake)
        self.assertEqual(result.status, "succeeded")
        execution = completed.call_args.args[0]
        self.assertEqual(execution.source_sha256, self.job["source_sha256"])
        nack.assert_not_called()

    def test_existing_destination_with_different_sha_is_collision(self):
        fake = FakeRclone(
            {
                self.source: self.content,
                self.destination: b"different bytes",
            }
        )
        result, completed, nack = self.execute(fake)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error_code, "destination_collision")
        self.assertEqual(fake.files[self.source], self.content)
        self.assertFalse(any(call[0][1:3] == ["backend", "moveid"] for call in fake.calls))
        completed.assert_not_called()
        nack.assert_called_once()

    def test_destination_race_cannot_overwrite_or_delete_source(self):
        fake = FakeRclone({self.source: self.content})
        racing = b"concurrent different bytes"

        def destination_race(state, _source, destination):
            state.files[destination] = racing
            return runner.CommandResult(0)

        fake.move_behavior = destination_race
        result, completed, nack = self.execute(fake)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error_code, "destination_collision")
        self.assertEqual(fake.files[self.source], self.content)
        self.assertEqual(fake.files[self.destination], racing)
        completed.assert_not_called()
        nack.assert_called_once()

    def test_source_path_replacement_after_id_download_is_never_moved(self):
        fake = FakeRclone({self.source: self.content})
        original_id = fake.file_id(self.source)
        replacement = b"%PDF-1.7\nreplacement bytes"
        replacement_id = "replacement_drive_file_id_001"

        def replace_after_copy(state, source):
            state.files[source] = replacement
            state.ids[source] = replacement_id

        fake.after_copy = replace_after_copy
        result, completed, nack = self.execute(fake)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error_code, "source_file_id_stale")
        self.assertEqual(fake.files[self.source], replacement)
        self.assertEqual(fake.file_id(self.source), replacement_id)
        self.assertNotIn(self.destination, fake.files)
        self.assertFalse(
            any(call[0][1:3] == ["backend", "moveid"] for call in fake.calls)
        )
        self.assertTrue(
            any(
                call[0][1:5]
                == ["backend", "copyid", "paper-drive:", original_id]
                for call in fake.calls
            )
        )
        completed.assert_not_called()
        nack.assert_called_once()

    def test_replacement_appearing_during_id_move_is_preserved(self):
        fake = FakeRclone({self.source: self.content})
        original_id = fake.file_id(self.source)
        replacement = b"%PDF-1.7\nreplacement bytes"
        replacement_id = "replacement_drive_file_id_002"

        def replace_during_move(state, source, destination):
            state.files[destination] = state.files[source]
            state.ids[destination] = original_id
            state.files[source] = replacement
            state.ids[source] = replacement_id
            return runner.CommandResult(0)

        fake.move_behavior = replace_during_move
        result, completed, nack = self.execute(fake)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error_code, "destination_collision")
        self.assertEqual(fake.files[self.source], replacement)
        self.assertEqual(fake.file_id(self.source), replacement_id)
        self.assertEqual(fake.file_id(self.destination), original_id)
        move_argv = next(
            call[0]
            for call in fake.calls
            if call[0][1:3] == ["backend", "moveid"]
        )
        self.assertEqual(move_argv[4], original_id)
        completed.assert_not_called()
        nack.assert_called_once()

    def test_duplicate_source_names_fail_closed_before_download(self):
        fake = FakeRclone({self.source: self.content})
        fake.extra_listing_entries.append(
            {
                "Name": "paper.pdf",
                "IsDir": False,
                "MimeType": "application/pdf",
                "ID": "duplicate_drive_file_id_003",
            }
        )
        result, completed, nack = self.execute(fake)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error_code, "taxonomy_remote_path_ambiguous")
        self.assertFalse(
            any(call[0][1:3] == ["backend", "copyid"] for call in fake.calls)
        )
        completed.assert_not_called()
        nack.assert_called_once()

    def test_identical_destination_race_fails_closed_without_source_delete(self):
        fake = FakeRclone({self.source: self.content})

        def identical_destination_race(state, _source, destination):
            state.files[destination] = self.content
            return runner.CommandResult(0)

        fake.move_behavior = identical_destination_race
        result, completed, nack = self.execute(fake)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error_code, "destination_collision")
        self.assertEqual(fake.files[self.source], self.content)
        self.assertEqual(fake.files[self.destination], self.content)
        self.assertFalse(any(call[0][1] == "deletefile" for call in fake.calls))
        completed.assert_not_called()
        nack.assert_called_once()

    def test_prior_completed_command_reconciles_without_second_move(self):
        fake = FakeRclone({self.destination: self.content})
        self.job["source_drive_file_id"] = fake.file_id(self.destination)
        result, completed, nack = self.execute(fake)
        self.assertEqual(result.status, "succeeded")
        self.assertFalse(any(call[0][1:3] == ["backend", "moveid"] for call in fake.calls))
        completed.assert_called_once()
        nack.assert_not_called()

    def test_missing_human_approval_never_touches_drive(self):
        self.job["approval_decision"] = "reject"
        fake = FakeRclone({self.source: self.content})
        result, completed, nack = self.execute(fake)
        self.assertEqual(result.status, "failed")
        self.assertEqual(
            result.error_code, "taxonomy_move_human_approval_missing"
        )
        self.assertEqual(fake.calls, [])
        completed.assert_not_called()
        nack.assert_called_once()

    def test_move_lease_must_outlive_command_timeout_and_margin(self):
        fake = FakeRclone({self.source: self.content})
        with patch.object(runner, "claim_taxonomy_move_job") as claim:
            with self.assertRaisesRegex(
                runner.TaxonomyRuntimeError, "taxonomy_move_lease_too_short"
            ):
                runner.execute_move_one(
                    worker_id="taxonomy-worker-1",
                    config=self.config,
                    lease_seconds=(
                        self.config.timeout_seconds
                        + runner.MOVE_LEASE_MARGIN_SECONDS
                    ),
                    command_runner=fake,
                )
        claim.assert_not_called()
        self.assertEqual(fake.calls, [])

    def test_destination_sha_mismatch_never_completes_database(self):
        fake = FakeRclone({self.source: self.content})

        def corrupt_after_move(state, source, destination):
            state.files.pop(source)
            state.files[destination] = b"corrupted"
            return runner.CommandResult(0)

        fake.move_behavior = corrupt_after_move
        result, completed, nack = self.execute(fake)
        self.assertEqual(result.status, "failed")
        self.assertEqual(
            result.error_code, "taxonomy_move_destination_sha_mismatch"
        )
        completed.assert_not_called()
        nack.assert_called_once()

    def test_lease_loss_does_not_nack(self):
        fake = FakeRclone({self.source: self.content})
        complete = Mock(
            side_effect=TaxonomyLeaseLost("taxonomy_move_lease_lost")
        )
        result, _completed, nack = self.execute(fake, complete=complete)
        self.assertEqual(result.status, "lease_lost")
        nack.assert_not_called()

    def test_result_never_contains_path_filename_or_command_output(self):
        fake = FakeRclone(
            {self.source: self.content, self.destination: b"private output"}
        )
        result, _completed, _nack = self.execute(fake)
        rendered = repr(result)
        self.assertNotIn(self.source, rendered)
        self.assertNotIn("paper.pdf", rendered)
        self.assertNotIn("private output", rendered)

    def test_config_requires_private_file_and_matching_remote(self):
        root = Path(self.temporary.name)
        bad_config = root / "other.conf"
        bad_config.write_text("[other]\ntype = drive\n", encoding="utf-8")
        bad_config.chmod(0o600)
        with self.assertRaisesRegex(
            runner.TaxonomyRuntimeError, "taxonomy_rclone_remote_missing"
        ):
            runner.MoveRuntimeConfig(
                drive_root=DRIVE_ROOT,
                rclone_binary=self.config.rclone_binary,
                rclone_config=bad_config,
            )
        bad_config.write_text("[paper-drive]\ntype = local\n", encoding="utf-8")
        bad_config.chmod(0o600)
        with self.assertRaisesRegex(
            runner.TaxonomyRuntimeError,
            "taxonomy_rclone_remote_type_invalid",
        ):
            runner.MoveRuntimeConfig(
                drive_root=DRIVE_ROOT,
                rclone_binary=self.config.rclone_binary,
                rclone_config=bad_config,
            )
        bad_config.chmod(0o644)
        with self.assertRaisesRegex(
            runner.TaxonomyRuntimeError, "taxonomy_rclone_config_invalid"
        ):
            runner.MoveRuntimeConfig(
                drive_root="other:Original",
                rclone_binary=self.config.rclone_binary,
                rclone_config=bad_config,
            )


class ProcessBoundaryTests(unittest.TestCase):
    def test_background_heartbeat_is_started_without_fake_processing(self):
        heartbeat = Mock()
        with patch.object(
            runner, "worker_heartbeat_from_env", return_value=heartbeat
        ) as factory:
            with runner._background_heartbeat("taxonomy-worker-1") as active:
                self.assertIs(active, heartbeat)
        factory.assert_called_once_with("taxonomy-worker-1")
        heartbeat.start.assert_called_once_with()
        heartbeat.processing.assert_not_called()
        heartbeat.stop.assert_called_once_with()

    def test_heartbeat_setup_failure_is_isolated(self):
        with patch.object(
            runner,
            "worker_heartbeat_from_env",
            side_effect=RuntimeError("private setup detail"),
        ):
            with runner._background_heartbeat("taxonomy-worker-1") as active:
                self.assertIsNone(active)

    def test_subprocess_boundary_forces_no_shell_and_discards_stderr(self):
        process = Mock()
        process.stdout = io.BytesIO(b"[]")
        process.wait.return_value = 0
        with patch.object(subprocess, "Popen", return_value=process) as popen:
            result = runner._default_command_runner(
                ["/safe/rclone", "lsjson", "remote:path"],
                30,
                True,
            )
        self.assertEqual(result.stdout, b"[]")
        kwargs = popen.call_args.kwargs
        self.assertIs(kwargs["shell"], False)
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)

    def test_oversized_command_output_is_rejected_not_returned(self):
        process = Mock()
        process.stdout = io.BytesIO(b"x" * (runner.MAX_RCLONE_OUTPUT_BYTES + 1))
        process.wait.return_value = 0
        with patch.object(subprocess, "Popen", return_value=process):
            with self.assertRaisesRegex(
                runner.TaxonomyRuntimeError,
                "taxonomy_rclone_output_too_large",
            ):
                runner._default_command_runner(
                    ["/safe/rclone", "lsjson", "remote:path"],
                    30,
                    True,
                )

    def test_sequential_cycle_continues_after_audit_failure(self):
        generation = runner.RuntimeJobResult("idle")
        move = runner.RuntimeJobResult("idle")
        with (
            patch.object(
                runner,
                "run_audit_cycle",
                side_effect=runner.TaxonomyRuntimeError("taxonomy_audit_failed"),
            ),
            patch.object(
                runner, "execute_generation_one", return_value=generation
            ) as generate,
            patch.object(runner, "execute_move_one", return_value=move) as execute_move,
        ):
            result = runner.run_once(
                worker_id="taxonomy-worker-1",
                move_config=Mock(spec=runner.MoveRuntimeConfig),
                api_key=None,
                generation_lease_seconds=400,
                generation_timeout_seconds=100,
                move_lease_seconds=1_900,
            )
        self.assertEqual(result.audit.status, "failed")
        self.assertIs(result.generation, generation)
        self.assertIs(result.move, move)
        generate.assert_called_once()
        execute_move.assert_called_once()
        self.assertEqual(generate.call_args.kwargs["lease_seconds"], 400)
        self.assertEqual(generate.call_args.kwargs["timeout_seconds"], 100)
        self.assertEqual(execute_move.call_args.kwargs["lease_seconds"], 1_900)

    def test_worker_has_no_environment_controlled_generation_enqueue(self):
        self.assertFalse(hasattr(runner, "_environment_flag"))


if __name__ == "__main__":
    unittest.main()

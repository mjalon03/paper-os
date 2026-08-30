from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import inspect
import unittest
from unittest.mock import patch
from uuid import uuid5

from paper_service.migrations.v008_operations import SQL as OPERATIONS_SQL
from paper_service.operations import (
    GLOBAL_REPROCESS_CONFIRMATION,
    build_selective_reprocess_request,
    derive_reprocess_idempotency_key,
    worker_heartbeat,
)
from paper_service.ops_store import (
    OperationsApprovalRequired,
    OperationsReviewConflict,
    OperationsStoreError,
    authorize_reprocess_job,
    claim_reprocess_job,
    configure_expected_workers,
    create_reprocess_request,
    create_rollback_request,
    enqueue_approved_reprocess,
    get_cost_summary,
    get_operation_request_detail,
    get_operations_summary,
    get_worker_health_snapshot,
    list_operation_failures,
    list_pending_operation_reviews,
    list_recent_operation_requests,
    review_reprocess_request,
    upsert_worker_heartbeat,
)
from paper_service.ops_store import _REPROCESS_JOB_NAMESPACE


PAPER_SHA = "a" * 64
OLD_VERSION = "rv-" + "1" * 32
NEW_VERSION = "rv-" + "2" * 32
REQUEST_ID = "7b2acb2a-0707-5c12-a05b-e46292cd6b21"
JOB_ID = "f7347b69-0443-54d6-9a3e-f37c995af301"
LEASE_TOKEN = "8da71e38-b8e0-46aa-aa5f-9edb5129b4f8"
APPROVAL_EVENT_ID = "5d80ef36-74d4-593b-a523-06e2d293dc73"


class Result:
    def __init__(self, *, one=None, many=None, rowcount=0):
        self.one = one
        self.many = [] if many is None else many
        self.rowcount = rowcount

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.many


class SequenceDatabase:
    def __init__(self, results):
        self.results = iter(results)
        self.calls = []

    def execute(self, sql, parameters=None):
        self.calls.append((sql, parameters))
        return next(self.results)


@contextmanager
def database_context(database):
    yield database


def target():
    return {
        "paper_id": 7,
        "expected_source_sha256": PAPER_SHA,
        "expected_current_version_id": OLD_VERSION,
    }


def selective_request():
    return build_selective_reprocess_request(
        targets=[target()],
        reason_code="extractor_upgrade",
        requested_at="2026-08-29T10:00:00Z",
    )


def global_request():
    requested_at = "2026-08-29T10:00:00Z"
    reason = "extractor_upgrade"
    return {
        "schema": "paper-reader-reprocess-request",
        "schema_version": 1,
        "scope": "all",
        "reason_code": reason,
        "requested_at": requested_at,
        "targets": [],
        "idempotency_key": derive_reprocess_idempotency_key(
            scope="all", reason_code=reason, requested_at=requested_at, targets=[]
        ),
        "global_confirmation": GLOBAL_REPROCESS_CONFIRMATION,
    }


def stored_request_row(request, *, decision="pending", revision=0):
    from paper_service.ops_store import _digest
    from paper_service.operations import reprocess_request_fingerprint

    targets = request["targets"] if request["scope"] == "selected" else [target()]
    approved = decision == "approved"
    rejected = decision == "rejected"
    return {
        "id": REQUEST_ID,
        "idempotency_key": request["idempotency_key"],
        "request_fingerprint": reprocess_request_fingerprint(
            request, allow_global=request["scope"] == "all"
        ),
        "target_snapshot_sha256": _digest(targets),
        "scope": request["scope"],
        "reason_code": request["reason_code"],
        "requested_at": request["requested_at"],
        "global_confirmed": request["scope"] == "all",
        "target_count": len(targets),
        "canonical_request": request,
        "review_revision": revision,
        "decision": decision,
        "approval_event_id": APPROVAL_EVENT_ID if approved or rejected else None,
        "event_review_revision": revision if approved or rejected else None,
        "event_decision": (
            "approve" if approved else "reject" if rejected else None
        ),
        "event_actor_kind": "human" if approved or rejected else None,
    }


def job_row(**changes):
    row = {
        "id": JOB_ID,
        "request_id": REQUEST_ID,
        "paper_id": 7,
        "status": "leased",
        "attempt_count": 1,
        "max_attempts": 3,
        "available_at": None,
        "lease_owner": "reader-worker",
        "lease_token": LEASE_TOKEN,
        "lease_expires_at": None,
        "planned_result_version_id": None,
        "result_version_id": None,
        "last_error_code": None,
        "created_at": None,
        "started_at": None,
        "finished_at": None,
        "expected_source_sha256": PAPER_SHA,
        "expected_current_version_id": OLD_VERSION,
        "current_source_sha256": PAPER_SHA,
        "lease_valid": True,
    }
    row.update(changes)
    return row


class OperationsMigrationTests(unittest.TestCase):
    def test_schema_has_strict_approval_history_and_crash_safe_queues(self):
        for table in (
            "paper_worker_heartbeats",
            "paper_incident_events",
            "paper_webhook_outbox",
            "paper_reprocess_requests",
            "paper_reprocess_targets",
            "paper_reprocess_review_events",
            "paper_reprocess_execution_jobs",
            "paper_rollback_requests",
            "paper_rollback_targets",
            "paper_rollback_review_events",
            "paper_rollback_execution_jobs",
        ):
            self.assertIn(f"CREATE TABLE {table}", OPERATIONS_SQL)
        self.assertIn("actor_kind = 'human'", OPERATIONS_SQL)
        self.assertIn("approval_event_id UUID NOT NULL", OPERATIONS_SQL)
        self.assertIn("apply_paper_operation_review_event", OPERATIONS_SQL)
        self.assertIn("paper_reprocess_requests_immutable", OPERATIONS_SQL)
        self.assertIn("paper_rollback_requests_immutable", OPERATIONS_SQL)
        self.assertIn("paper_incident_events_immutable", OPERATIONS_SQL)
        self.assertGreaterEqual(OPERATIONS_SQL.count("FOR EACH ROW EXECUTE"), 9)
        self.assertIn("WHERE status IN ('leased', 'running')", OPERATIONS_SQL)
        self.assertIn("planned_result_version_id", OPERATIONS_SQL)

    def test_persistence_schema_does_not_store_delivery_secrets_or_paper_content(self):
        lowered = OPERATIONS_SQL.lower()
        for forbidden in (
            "webhook_url",
            "webhook_secret",
            "paper_title",
            "source_path",
            "stored_path",
            "drive_path",
            "paper_body",
        ):
            self.assertNotIn(forbidden, lowered)


class WorkerStateTests(unittest.TestCase):
    def test_configuration_is_bounded_and_heartbeat_is_monotonic(self):
        database = SequenceDatabase([Result(), Result(), Result()])
        with patch(
            "paper_service.ops_store.connection",
            return_value=database_context(database),
        ):
            configured = configure_expected_workers(["reader", "sync"])
        self.assertEqual(configured, ("reader", "sync"))
        self.assertIn("enabled = FALSE", database.calls[0][0])

        heartbeat = worker_heartbeat(
            worker_id="reader",
            observed_at="2026-08-29T10:00:00Z",
            sequence=8,
            state="processing",
            active_paper_id=7,
            last_success_at="2026-08-29T09:59:00Z",
        )
        database = SequenceDatabase(
            [
                Result(one={"enabled": True}),
                Result(
                    one={
                        "worker_id": "reader",
                        "observed_at": datetime(
                            2026, 8, 29, 10, tzinfo=timezone.utc
                        ),
                        "sequence": 8,
                        "state": "processing",
                        "active_paper_id": 7,
                        "last_success_at": datetime(
                            2026, 8, 29, 9, 59, tzinfo=timezone.utc
                        ),
                        "error_code": None,
                    }
                ),
            ]
        )
        with patch(
            "paper_service.ops_store.connection",
            return_value=database_context(database),
        ):
            self.assertEqual(upsert_worker_heartbeat(heartbeat), heartbeat)
        self.assertIn("EXCLUDED.sequence >", database.calls[1][0])

    def test_health_snapshot_exposes_only_worker_state_and_counts(self):
        heartbeat_row = {
            "worker_id": "reader",
            "observed_at": "2026-08-29T10:00:00Z",
            "sequence": 1,
            "state": "processing",
            "active_paper_id": 7,
            "last_success_at": None,
            "error_code": None,
        }
        database = SequenceDatabase(
            [Result(many=[{"worker_id": "reader"}]), Result(many=[heartbeat_row])]
        )
        with patch(
            "paper_service.ops_store.connection",
            return_value=database_context(database),
        ):
            snapshot = get_worker_health_snapshot(
                evaluated_at="2026-08-29T10:00:05Z"
            )
        self.assertEqual(snapshot["status"], "healthy")
        serialized = repr(snapshot)
        self.assertNotIn("active_paper_id", serialized)
        self.assertNotIn("title", serialized)
        self.assertNotIn("path", serialized)


class ReprocessRequestTests(unittest.TestCase):
    def test_full_scope_requires_opt_in_confirmation_and_target_snapshot(self):
        request = global_request()
        with self.assertRaisesRegex(Exception, "global_reprocess_forbidden"):
            create_reprocess_request(request)
        with self.assertRaisesRegex(
            OperationsStoreError, "global_reprocess_target_snapshot_required"
        ):
            create_reprocess_request(request, allow_global=True)

        missing_confirmation = dict(request)
        missing_confirmation.pop("global_confirmation")
        with self.assertRaisesRegex(Exception, "global_reprocess_confirmation_required"):
            create_reprocess_request(
                missing_confirmation,
                allow_global=True,
                global_target_snapshot=[target()],
            )

    def test_selective_request_is_immutable_and_source_verified_at_insert(self):
        database = SequenceDatabase(
            [Result(one={"id": REQUEST_ID}), Result(one={"paper_id": 7}), Result()]
        )
        with patch(
            "paper_service.ops_store.connection",
            return_value=database_context(database),
        ):
            created = create_reprocess_request(selective_request())
        self.assertEqual(created["decision"], "pending")
        self.assertEqual(created["targets"], [target()])
        self.assertIn("FROM papers p WHERE p.id = %s AND p.sha256 = %s", database.calls[1][0])

        stale_database = SequenceDatabase(
            [Result(one={"id": REQUEST_ID}), Result(one=None)]
        )
        with patch(
            "paper_service.ops_store.connection",
            return_value=database_context(stale_database),
        ):
            with self.assertRaisesRegex(
                OperationsStoreError, "reprocess_target_source_stale_or_missing"
            ):
                create_reprocess_request(selective_request())

    def test_review_is_human_only_append_only_and_optimistic(self):
        conflict_database = SequenceDatabase(
            [Result(one={"review_revision": 1, "decision": "approved"})]
        )
        with patch(
            "paper_service.ops_store.connection",
            return_value=database_context(conflict_database),
        ):
            with self.assertRaises(OperationsReviewConflict) as raised:
                review_reprocess_request(
                    REQUEST_ID,
                    expected_revision=0,
                    decision="approve",
                    actor_id="cf-user-opaque",
                )
        self.assertEqual(raised.exception.current_revision, 1)
        self.assertEqual(raised.exception.decision, "approved")

        event = {
            "id": "5d80ef36-74d4-593b-a523-06e2d293dc73",
            "request_id": REQUEST_ID,
            "review_revision": 1,
            "expected_revision": 0,
            "decision": "approve",
            "actor_kind": "human",
            "actor_id": "cf-user-opaque",
            "comment": None,
            "created_at": datetime.now(timezone.utc),
        }
        database = SequenceDatabase(
            [Result(one={"review_revision": 0, "decision": "pending"}), Result(one=event)]
        )
        with patch(
            "paper_service.ops_store.connection",
            return_value=database_context(database),
        ):
            result = review_reprocess_request(
                REQUEST_ID,
                expected_revision=0,
                decision="approve",
                actor_id="cf-user-opaque",
            )
        self.assertEqual(result["actor_kind"], "human")
        self.assertIn("INSERT INTO paper_reprocess_review_events", database.calls[1][0])

    def test_enqueue_reads_verified_request_and_requires_approval(self):
        request = selective_request()
        database = SequenceDatabase(
            [
                Result(one=stored_request_row(request, decision="pending")),
                Result(many=[target()]),
            ]
        )
        with patch(
            "paper_service.ops_store.connection",
            return_value=database_context(database),
        ):
            with self.assertRaisesRegex(
                OperationsApprovalRequired, "reprocess_human_approval_required"
            ):
                enqueue_approved_reprocess(REQUEST_ID)

        returned_job = job_row(
            id=str(uuid5(_REPROCESS_JOB_NAMESPACE, f"{REQUEST_ID}:7")),
            max_attempts=3,
        )
        database = SequenceDatabase(
            [
                Result(one=stored_request_row(request, decision="approved", revision=1)),
                Result(many=[target()]),
                Result(one=returned_job),
            ]
        )
        with patch(
            "paper_service.ops_store.connection",
            return_value=database_context(database),
        ):
            jobs = enqueue_approved_reprocess(REQUEST_ID)
        self.assertEqual(len(jobs), 1)
        self.assertIn("paper_reprocess_execution_jobs", database.calls[2][0])

    def test_approved_head_without_human_event_cannot_enqueue(self):
        request = selective_request()
        forged = stored_request_row(request, decision="approved", revision=1)
        forged.update(
            approval_event_id=None,
            event_review_revision=None,
            event_decision=None,
            event_actor_kind=None,
        )
        database = SequenceDatabase(
            [Result(one=forged), Result(many=[target()])]
        )
        with patch(
            "paper_service.ops_store.connection",
            return_value=database_context(database),
        ):
            with self.assertRaisesRegex(
                OperationsStoreError, "stored_reprocess_review_invalid"
            ):
                enqueue_approved_reprocess(REQUEST_ID)


class ReprocessExecutionTests(unittest.TestCase):
    def test_claim_has_approval_join_skip_locked_and_crash_recovery(self):
        database = SequenceDatabase([Result(rowcount=1), Result(one=None)])
        with patch(
            "paper_service.ops_store.connection",
            return_value=database_context(database),
        ):
            self.assertIsNone(claim_reprocess_job(worker_id="reader-worker"))
        self.assertIn("lease_expired", database.calls[0][0])
        self.assertIn("h.decision = 'approved'", database.calls[1][0])
        self.assertIn("FOR UPDATE OF j SKIP LOCKED", database.calls[1][0])

    def test_stale_source_or_pointer_is_persisted_as_terminal_failure(self):
        failed = job_row(
            status="failed",
            lease_owner=None,
            lease_token=None,
            last_error_code="reprocess_source_sha256_stale",
        )
        database = SequenceDatabase(
            [Result(one=job_row(current_source_sha256="b" * 64)), Result(one=failed)]
        )
        with patch(
            "paper_service.ops_store.connection",
            return_value=database_context(database),
        ):
            result = authorize_reprocess_job(
                JOB_ID,
                worker_id="reader-worker",
                lease_token=LEASE_TOKEN,
                actual_source_sha256=PAPER_SHA,
                actual_current_version_id=OLD_VERSION,
            )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["last_error_code"], "reprocess_source_sha256_stale")

        pointer_failed = job_row(
            status="failed",
            lease_owner=None,
            lease_token=None,
            last_error_code="reprocess_current_version_stale",
        )
        database = SequenceDatabase(
            [Result(one=job_row()), Result(one=pointer_failed)]
        )
        with patch(
            "paper_service.ops_store.connection",
            return_value=database_context(database),
        ):
            result = authorize_reprocess_job(
                JOB_ID,
                worker_id="reader-worker",
                lease_token=LEASE_TOKEN,
                actual_source_sha256=PAPER_SHA,
                actual_current_version_id=NEW_VERSION,
            )
        self.assertEqual(result["last_error_code"], "reprocess_current_version_stale")

    def test_planned_version_recovers_idempotently_after_publish_crash(self):
        succeeded = job_row(
            status="succeeded",
            lease_owner=None,
            lease_token=None,
            planned_result_version_id=NEW_VERSION,
            result_version_id=NEW_VERSION,
            last_error_code="recovered_after_publish",
        )
        database = SequenceDatabase(
            [
                Result(one=job_row(planned_result_version_id=NEW_VERSION)),
                Result(one=succeeded),
            ]
        )
        with patch(
            "paper_service.ops_store.connection",
            return_value=database_context(database),
        ):
            result = authorize_reprocess_job(
                JOB_ID,
                worker_id="reader-worker",
                lease_token=LEASE_TOKEN,
                actual_source_sha256=PAPER_SHA,
                actual_current_version_id=NEW_VERSION,
            )
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["result_version_id"], NEW_VERSION)


class RollbackAndReadModelTests(unittest.TestCase):
    def test_rollback_is_a_distinct_pending_human_review_request(self):
        rollback_target = {
            **target(),
            "rollback_to_version_id": NEW_VERSION,
        }
        database = SequenceDatabase(
            [Result(one={"id": REQUEST_ID}), Result(one={"paper_id": 7}), Result()]
        )
        with patch(
            "paper_service.ops_store.connection",
            return_value=database_context(database),
        ):
            request = create_rollback_request(
                targets=[rollback_target],
                reason_code="bad_extraction",
                requested_at="2026-08-29T10:00:00Z",
            )
        self.assertEqual(request["decision"], "pending")
        self.assertRegex(request["request"]["idempotency_key"], r"^rb-[0-9a-f]{32}$")
        self.assertIn("paper_rollback_review_heads", database.calls[2][0])

    def test_summary_failure_and_cost_queries_return_allowlisted_data(self):
        summary_row = {
            "expected_workers": 4,
            "observed_workers": 3,
            "open_incidents": 1,
            "pending_webhooks": 1,
            "dead_webhooks": 0,
            "reprocess_pending_review": 2,
            "reprocess_active": 1,
            "reprocess_failed": 1,
            "rollback_pending_review": 1,
            "rollback_active": 0,
            "rollback_failed": 0,
            "knowledge_active": 1,
            "knowledge_failed": 0,
            "qa_active": 1,
            "qa_failed": 0,
            "taxonomy_generation_pending": 2,
            "taxonomy_generation_active": 1,
            "taxonomy_generation_failed": 1,
            "taxonomy_move_pending": 3,
            "taxonomy_move_active": 1,
            "taxonomy_move_failed": 0,
        }
        database = SequenceDatabase([Result(one=summary_row)])
        with patch(
            "paper_service.ops_store.connection",
            return_value=database_context(database),
        ):
            summary = get_operations_summary()
        self.assertEqual(summary["open_incidents"], 1)
        self.assertNotIn("title", summary)
        summary_sql = database.calls[0][0]
        self.assertIn("paper_taxonomy_generation_jobs", summary_sql)
        self.assertIn("paper_taxonomy_move_jobs", summary_sql)
        self.assertNotIn("drive_path", summary_sql)
        self.assertNotIn("prompt", summary_sql)

        failure = {
            "operation_kind": "reprocess",
            "operation_id": JOB_ID,
            "paper_id": 7,
            "status": "failed",
            "error_code": "reprocess_current_version_stale",
            "attempt_count": 1,
            "occurred_at": datetime.now(timezone.utc),
            "title": "must not escape",
            "source_path": "/must/not/escape.pdf",
        }
        database = SequenceDatabase([Result(many=[failure])])
        with patch(
            "paper_service.ops_store.connection",
            return_value=database_context(database),
        ):
            failures = list_operation_failures()
        self.assertEqual(
            set(failures[0]),
            {
                "operation_kind",
                "operation_id",
                "paper_id",
                "status",
                "error_code",
                "attempt_count",
                "occurred_at",
            },
        )
        self.assertNotIn("must not escape", repr(failures))
        failure_sql = database.calls[0][0]
        self.assertIn("'taxonomy_generation'", failure_sql)
        self.assertIn("'taxonomy_move'", failure_sql)
        self.assertNotIn("drive_path", failure_sql)
        self.assertNotIn("title", failure_sql)
        self.assertNotIn("prompt", failure_sql)

        cost_row = {
            "attempt_count": 2,
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
            "known_cost_microusd": 55,
            "unknown_cost_attempts": 1,
        }
        database = SequenceDatabase([Result(one=cost_row)])
        with patch(
            "paper_service.ops_store.connection",
            return_value=database_context(database),
        ):
            costs = get_cost_summary()
        self.assertEqual(costs["known_cost_microusd"], 55)
        self.assertNotIn("model", costs)
        self.assertIn("paper_taxonomy_usage_ledger", database.calls[0][0])

    def test_pending_and_recent_request_lists_are_bounded_safe_projections(self):
        pending_row = {
            "request_kind": "reprocess",
            "request_id": REQUEST_ID,
            "scope": "selected",
            "reason_code": "reader_quality_fix",
            "requested_at": datetime.now(timezone.utc),
            "target_count": 1,
            "review_revision": 0,
            "decision": "pending",
            "job_count": 0,
            "active_job_count": 0,
            "succeeded_job_count": 0,
            "failed_job_count": 0,
            "title": "must not escape",
            "paper_path": "/private/paper.pdf",
            "lease_token": LEASE_TOKEN,
        }
        database = SequenceDatabase([Result(many=[pending_row])])
        with patch(
            "paper_service.ops_store.connection",
            return_value=database_context(database),
        ):
            pending = list_pending_operation_reviews(limit=10)
        self.assertEqual(pending[0]["state"], "pending_review")
        self.assertEqual(database.calls[0][1], (10,))
        self.assertNotIn("must not escape", repr(pending))
        self.assertNotIn("/private", repr(pending))
        self.assertNotIn(LEASE_TOKEN, repr(pending))
        pending_sql = database.calls[0][0]
        for forbidden in ("title", "path", "lease_token", "canonical_request"):
            self.assertNotIn(forbidden, pending_sql)

        recent_row = {
            **pending_row,
            "request_kind": "rollback",
            "scope": "rollback",
            "decision": "approved",
            "review_revision": 1,
            "job_count": 2,
            "succeeded_job_count": 2,
        }
        database = SequenceDatabase([Result(many=[recent_row])])
        with patch(
            "paper_service.ops_store.connection",
            return_value=database_context(database),
        ):
            recent = list_recent_operation_requests(limit=20)
        self.assertEqual(recent[0]["state"], "succeeded")
        self.assertEqual(database.calls[0][1], (20,))
        self.assertNotIn("must not escape", repr(recent))
        recent_sql = database.calls[0][0]
        for forbidden in ("title", "path", "lease_token", "canonical_request"):
            self.assertNotIn(forbidden, recent_sql)

        for invalid_limit in (0, 201):
            with self.assertRaises(OperationsStoreError):
                list_pending_operation_reviews(limit=invalid_limit)

    def test_request_detail_uses_existing_verified_reads_and_strips_extras(self):
        stored = {
            "request_id": REQUEST_ID,
            "request": {
                "idempotency_key": "rr-" + "a" * 32,
                "scope": "selected",
                "reason_code": "reader_quality_fix",
                "requested_at": "2026-08-29T10:00:00Z",
            },
            "targets": [
                {
                    **target(),
                    "paper_path": "/private/paper.pdf",
                    "title": "private title",
                }
            ],
            "review_revision": 1,
            "decision": "approved",
            "approval_event_id": APPROVAL_EVENT_ID,
            "lease_token": LEASE_TOKEN,
        }
        target_states = (
            {
                "paper_id": 7,
                "status": "queued",
                "attempt_count": 0,
                "error_code": None,
                "result_version_id": None,
                "lease_token": LEASE_TOKEN,
            },
        )
        with patch(
            "paper_service.ops_store.get_reprocess_request",
            return_value=stored,
        ), patch(
            "paper_service.ops_store.list_reprocess_target_states",
            return_value=target_states,
        ):
            detail = get_operation_request_detail("reprocess", REQUEST_ID)
        self.assertEqual(detail["target_count"], 1)
        self.assertEqual(detail["targets"][0]["paper_id"], 7)
        self.assertNotIn("private title", repr(detail))
        self.assertNotIn("/private", repr(detail))
        self.assertNotIn(LEASE_TOKEN, repr(detail))
        self.assertNotIn("lease_token", detail["target_states"][0])
        with self.assertRaisesRegex(
            OperationsStoreError, "operation_request_kind_invalid"
        ):
            get_operation_request_detail("other", REQUEST_ID)

    def test_public_execution_contract_never_performs_network_or_filesystem_io(self):
        source = inspect.getsource(__import__("paper_service.ops_store", fromlist=["*"]))
        for forbidden in ("requests.", "urllib", "httpx", "subprocess", "Path(", "open("):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()

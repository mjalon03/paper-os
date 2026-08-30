import copy
import json
import unittest
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import uuid4

from paper_service.migrations.v007_taxonomy import SQL as TAXONOMY_SQL
from paper_service.taxonomy import (
    AuditPolicy,
    DEEPSEEK_PRO_MODEL,
    TaxonomyContractError,
    TaxonomyUsage,
    build_deepseek_pro_request,
    build_rollback_proposal,
    claim_taxonomy_generation,
    create_rollback_proposal,
    enqueue_taxonomy_generation,
    evaluate_taxonomy_audit,
    parse_taxonomy_proposal,
    persist_taxonomy_audit,
    prepare_move_execution,
    prepare_move_reconciliation,
    review_rollback_proposal,
    review_taxonomy_proposal,
    sanitize_subtheme_name,
    source_snapshot_sha256,
    taxonomy_generation_request_digest,
    validate_taxonomy_generation_job_authorization,
    validate_rollback_proposal,
    validate_taxonomy_proposal,
)


ROOT = "paper-drive:Original/Machine Learning"
PROPOSAL_ID = "0c7b708c-2fc8-4303-9b99-4bc26fb807f0"
LEASE_ID = "abac06fa-254a-46e8-aab2-11f51a21c3de"


def paper_rows(
    count=12,
    *,
    imported_at="2026-08-01T00:00:00Z",
    grouped=False,
    diverse=False,
):
    rows = []
    for index in range(1, count + 1):
        if grouped:
            leaf = ("Representation", "Learning", "Systems")[(index - 1) // 4]
            path = f"{ROOT}/{leaf}/paper-{index}.pdf"
        else:
            path = f"{ROOT}/paper-{index}.pdf"
        terms = ["shared", "representation"]
        if diverse:
            terms = [f"unique-{index}", f"domain-{index}"]
        rows.append(
            {
                "id": index,
                "sha256": f"{index:064x}",
                "drive_path": path,
                "category": "Machine Learning",
                "title": f"Paper {index} about {'diverse' if diverse else 'shared'} models",
                "topic_terms": terms,
                "imported_at": imported_at,
            }
        )
    return rows


def eligible_audit(rows=None):
    rows = paper_rows(diverse=True) if rows is None else rows
    return evaluate_taxonomy_audit(
        rows,
        category="Machine Learning",
        category_path=ROOT,
        evaluated_at="2026-08-29T00:00:00Z",
    )


def model_proposal(request, *, assignments=None, subthemes=None):
    snapshot = request.source_snapshot
    if subthemes is None:
        subthemes = [
            {"name": "Representation", "description": "Representations and encoding."},
            {"name": "Learning", "description": "Learning methods and objectives."},
        ]
    if assignments is None:
        assignments = []
        for index, paper in enumerate(snapshot["papers"]):
            subtheme = "Representation" if index < 6 else "Learning"
            assignments.append(
                {
                    "paper_id": paper["paper_id"],
                    "subtheme": subtheme,
                }
            )
    return {
        "subthemes": subthemes,
        "assignments": assignments,
        "rationale": "Two stable conceptual groups fit the supplied titles and terms.",
    }


class Result:
    def __init__(self, one=None, many=None, rowcount=0):
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


class TaxonomyAuditTests(unittest.TestCase):
    def test_small_categories_are_a_deterministic_no_op(self):
        result = evaluate_taxonomy_audit(
            paper_rows(11, diverse=True),
            category="Machine Learning",
            category_path=ROOT,
            evaluated_at="2026-08-29T00:00:00Z",
        )
        self.assertEqual(result["decision"], "no-op")
        self.assertIn("category_too_small", result["reason_codes"])
        self.assertEqual(result["metrics"]["paper_count"], 11)

    def test_audit_requires_four_new_papers_or_ninety_days(self):
        rows = paper_rows(diverse=True)
        for index, row in enumerate(rows):
            row["imported_at"] = (
                "2026-08-25T00:00:00Z" if index < 3 else "2026-01-01T00:00:00Z"
            )
        recent = evaluate_taxonomy_audit(
            rows,
            category="Machine Learning",
            category_path=ROOT,
            evaluated_at="2026-08-29T00:00:00Z",
            last_review_or_audit_at="2026-08-20T00:00:00Z",
        )
        self.assertEqual(recent["decision"], "no-op")
        self.assertIn("audit_not_due", recent["reason_codes"])
        aged = evaluate_taxonomy_audit(
            rows,
            category="Machine Learning",
            category_path=ROOT,
            evaluated_at="2026-08-29T00:00:00Z",
            last_review_or_audit_at="2026-05-01T00:00:00Z",
        )
        self.assertEqual(aged["decision"], "request-proposal")

    def test_four_new_papers_make_an_overcrowded_flat_category_due(self):
        rows = paper_rows(diverse=False)
        for index, row in enumerate(rows):
            row["imported_at"] = (
                "2026-08-25T00:00:00Z" if index < 4 else "2026-01-01T00:00:00Z"
            )
        result = evaluate_taxonomy_audit(
            rows,
            category="Machine Learning",
            category_path=ROOT,
            evaluated_at="2026-08-29T00:00:00Z",
            last_review_or_audit_at="2026-08-20T00:00:00Z",
        )
        self.assertEqual(result["decision"], "request-proposal")
        self.assertIn("overcrowded_leaf", result["reason_codes"])
        self.assertEqual(result["metrics"]["new_paper_count"], 4)

    def test_already_balanced_homogeneous_subthemes_are_no_op(self):
        result = evaluate_taxonomy_audit(
            paper_rows(grouped=True),
            category="Machine Learning",
            category_path=ROOT,
            evaluated_at="2026-08-29T00:00:00Z",
        )
        self.assertEqual(result["decision"], "no-op")
        self.assertIn("structure_not_needed", result["reason_codes"])
        self.assertEqual(result["metrics"]["max_leaf_size"], 4)

    def test_snapshot_retains_no_title_or_topic_content(self):
        result = eligible_audit()
        serialized = json.dumps(result["source_snapshot"], ensure_ascii=False)
        self.assertNotIn("title", serialized)
        self.assertNotIn("topic_terms", serialized)
        self.assertEqual(
            result["source_snapshot_sha256"],
            source_snapshot_sha256(result["source_snapshot"]),
        )

    def test_path_and_time_fail_closed(self):
        rows = paper_rows()
        rows[0]["drive_path"] = "paper-drive:Original/Machine Learning/../secret.pdf"
        with self.assertRaisesRegex(TaxonomyContractError, "drive_path_invalid"):
            eligible_audit(rows)
        rows = paper_rows()
        rows[0]["imported_at"] = "2026-09-01T00:00:00Z"
        with self.assertRaisesRegex(TaxonomyContractError, "paper_imported_after"):
            eligible_audit(rows)


class TaxonomyModelContractTests(unittest.TestCase):
    def setUp(self):
        self.rows = paper_rows(diverse=True)
        self.request = build_deepseek_pro_request(
            self.rows,
            eligible_audit(self.rows),
            proposal_id=PROPOSAL_ID,
        )

    def parse(self, value):
        return parse_taxonomy_proposal(
            json.dumps(value, ensure_ascii=False), self.request
        )

    def test_request_is_bounded_pro_only_and_contains_no_body(self):
        body = self.request.request_body
        self.assertEqual(body["model"], DEEPSEEK_PRO_MODEL)
        self.assertEqual(body["thinking"], {"type": "enabled"})
        self.assertFalse(body["stream"])
        self.assertEqual(body["response_format"], {"type": "json_object"})
        user = json.loads(body["messages"][1]["content"])
        self.assertEqual(len(user["papers"]), 12)
        self.assertEqual(
            set(user), {"task", "category", "output_contract", "papers"}
        )
        self.assertEqual(
            set(user["papers"][0]), {"paper_id", "title", "topic_terms"}
        )
        self.assertNotIn("abstract", body["messages"][1]["content"])
        self.assertNotIn("body", body["messages"][1]["content"])
        self.assertNotIn("authors", body["messages"][1]["content"])
        for forbidden in (
            "proposal_id",
            "category_path",
            "source_snapshot_sha256",
            "source_sha256",
            "current_drive_path",
            "proposed_drive_path",
            "paper-drive:",
            ".pdf",
        ):
            self.assertNotIn(forbidden, body["messages"][1]["content"])

    def test_request_inherits_the_exact_audited_policy(self):
        policy = AuditPolicy(min_subtheme_size=5, max_other_fraction=0.1)
        audit = evaluate_taxonomy_audit(
            self.rows,
            category="Machine Learning",
            category_path=ROOT,
            evaluated_at="2026-08-29T00:00:00Z",
            policy=policy,
        )
        request = build_deepseek_pro_request(
            self.rows, audit, proposal_id=str(uuid4())
        )
        self.assertEqual(request.policy, policy)
        with self.assertRaisesRegex(TaxonomyContractError, "policy_mismatch"):
            build_deepseek_pro_request(
                self.rows,
                audit,
                proposal_id=str(uuid4()),
                policy=AuditPolicy(),
            )

    def test_valid_proposal_binds_every_source_and_adds_move_identity(self):
        parsed = self.parse(model_proposal(self.request))
        self.assertEqual(parsed["status"], "ai-draft")
        self.assertEqual(len(parsed["assignments"]), 12)
        self.assertRegex(parsed["proposal_payload_sha256"], r"^[0-9a-f]{64}$")
        self.assertTrue(all(item["move_job_id"] for item in parsed["assignments"]))
        self.assertEqual(validate_taxonomy_proposal(parsed), parsed)

    def test_response_must_be_strict_json_with_no_unknown_fields(self):
        value = model_proposal(self.request)
        with self.assertRaisesRegex(TaxonomyContractError, "not_strict_json"):
            parse_taxonomy_proposal(f"```json\n{json.dumps(value)}\n```", self.request)
        value["unexpected"] = True
        with self.assertRaisesRegex(TaxonomyContractError, "unknown_field"):
            self.parse(value)

    def test_every_paper_is_assigned_exactly_once(self):
        value = model_proposal(self.request)
        value["assignments"][-1] = copy.deepcopy(value["assignments"][0])
        with self.assertRaisesRegex(TaxonomyContractError, "assignment_paper_invalid"):
            self.parse(value)

    def test_model_cannot_supply_source_binding_or_destination(self):
        for field, replacement in (
            ("source_sha256", "f" * 64),
            ("current_drive_path", f"{ROOT}/wrong.pdf"),
            ("proposed_drive_path", f"{ROOT}/Representation/wrong.pdf"),
        ):
            value = model_proposal(self.request)
            value["assignments"][0][field] = replacement
            with self.assertRaisesRegex(TaxonomyContractError, "unknown_field"):
                self.parse(value)

    def test_destination_is_derived_locally_and_subtheme_cannot_traverse(self):
        value = model_proposal(self.request)
        parsed = self.parse(value)
        self.assertEqual(
            parsed["assignments"][0]["proposed_drive_path"],
            f"{ROOT}/Representation/paper-1.pdf",
        )
        with self.assertRaises(TaxonomyContractError):
            sanitize_subtheme_name("../escape")

    def test_year_folders_are_forbidden(self):
        value = model_proposal(self.request)
        value["subthemes"][0]["name"] = "2026"
        with self.assertRaisesRegex(TaxonomyContractError, "year_subtheme_forbidden"):
            self.parse(value)
        with self.assertRaisesRegex(TaxonomyContractError, "catch_all_forbidden"):
            sanitize_subtheme_name("Miscellaneous")

    def test_non_other_subtheme_must_have_four_papers(self):
        value = model_proposal(self.request)
        for index, assignment in enumerate(value["assignments"]):
            name = "Small" if index < 3 else "Large"
            assignment["subtheme"] = name
        value["subthemes"] = [
            {"name": "Small", "description": "Small group"},
            {"name": "Large", "description": "Large group"},
        ]
        with self.assertRaisesRegex(TaxonomyContractError, "subtheme_too_small"):
            self.parse(value)

    def test_other_is_the_only_limited_catch_all(self):
        value = model_proposal(self.request)
        for index, assignment in enumerate(value["assignments"]):
            name = "Other" if index < 4 else "Learning"
            assignment["subtheme"] = name
        value["subthemes"] = [
            {"name": "Other", "description": "Catch-all"},
            {"name": "Learning", "description": "Main group"},
        ]
        with self.assertRaisesRegex(TaxonomyContractError, "other_too_large"):
            self.parse(value)
        value["subthemes"][0]["name"] = "other"
        with self.assertRaisesRegex(TaxonomyContractError, "other_name_noncanonical"):
            self.parse(value)

    def test_mutating_the_request_after_construction_fails_closed(self):
        self.request.request_body["model"] = "deepseek-v4-flash"
        with self.assertRaisesRegex(TaxonomyContractError, "request_mutated"):
            parse_taxonomy_proposal("{}", self.request)


class TaxonomyRollbackAndMoveTests(unittest.TestCase):
    def successful_moves(self):
        return [
            {
                "id": str(uuid4()),
                "proposal_id": PROPOSAL_ID,
                "paper_id": index,
                "source_sha256": f"{index:064x}",
                "old_drive_path": f"{ROOT}/paper-{index}.pdf",
                "new_drive_path": f"{ROOT}/Learning/paper-{index}.pdf",
                "status": "succeeded",
                "direction": "apply",
            }
            for index in range(1, 5)
        ]

    def test_rollback_is_a_new_pending_human_review_contract(self):
        rollback = build_rollback_proposal(
            self.successful_moves(),
            original_proposal_id=PROPOSAL_ID,
            rollback_proposal_id="79aef857-a19d-41bb-ae42-d0f3a12cbd46",
            reason="The approved grouping was not useful after review.",
        )
        self.assertEqual(rollback["status"], "pending-human-review")
        self.assertEqual(
            rollback["items"][0]["current_drive_path"],
            f"{ROOT}/Learning/paper-1.pdf",
        )
        self.assertEqual(
            rollback["items"][0]["restored_drive_path"],
            f"{ROOT}/paper-1.pdf",
        )
        self.assertEqual(validate_rollback_proposal(rollback), rollback)

    def test_unsuccessful_move_cannot_be_used_for_rollback(self):
        rows = self.successful_moves()
        rows[0]["status"] = "failed"
        with self.assertRaisesRegex(TaxonomyContractError, "not_succeeded"):
            build_rollback_proposal(
                rows,
                original_proposal_id=PROPOSAL_ID,
                reason="Failed source must not be rolled back.",
            )

    def test_rollback_can_restore_exact_canonical_legacy_year_path(self):
        rows = self.successful_moves()[:1]
        legacy = f"{ROOT}/2025/paper-1.pdf"
        rows[0]["old_drive_path"] = legacy
        rollback = build_rollback_proposal(
            rows,
            original_proposal_id=PROPOSAL_ID,
            reason="Restore the exact historical location.",
        )
        self.assertEqual(rollback["items"][0]["restored_drive_path"], legacy)
        self.assertEqual(validate_rollback_proposal(rollback), rollback)

    def test_rollback_legacy_path_still_rejects_traversal(self):
        rows = self.successful_moves()[:1]
        rows[0]["old_drive_path"] = f"{ROOT}/2025/../outside.pdf"
        with self.assertRaisesRegex(TaxonomyContractError, "restored_drive_path_invalid"):
            build_rollback_proposal(
                rows,
                original_proposal_id=PROPOSAL_ID,
                reason="Unsafe historical paths must remain forbidden.",
            )

    def test_move_execution_requires_exact_live_sha_and_source_path(self):
        job = {
            "id": str(uuid4()),
            "status": "running",
            "lease_token": LEASE_ID,
            "paper_id": 7,
            "source_sha256": "a" * 64,
            "source_drive_file_id": "drivefile_0123456789abcdef",
            "old_drive_path": f"{ROOT}/paper-7.pdf",
            "new_drive_path": f"{ROOT}/Learning/paper-7.pdf",
            "direction": "apply",
            "idempotency_key_digest": "b" * 64,
        }
        execution = prepare_move_execution(
            job,
            observed_source_sha256="a" * 64,
            observed_source_path=f"{ROOT}/paper-7.pdf",
            observed_source_file_id="drivefile_0123456789abcdef",
        )
        self.assertEqual(execution.new_drive_path, f"{ROOT}/Learning/paper-7.pdf")
        self.assertEqual(
            execution.source_drive_file_id, "drivefile_0123456789abcdef"
        )
        with self.assertRaisesRegex(TaxonomyContractError, "source_file_id_stale"):
            prepare_move_execution(
                job,
                observed_source_sha256="a" * 64,
                observed_source_path=f"{ROOT}/paper-7.pdf",
                observed_source_file_id="different_drive_file_id_01",
            )
        with self.assertRaisesRegex(TaxonomyContractError, "source_sha_stale"):
            prepare_move_execution(
                job,
                observed_source_sha256="c" * 64,
                observed_source_path=f"{ROOT}/paper-7.pdf",
                observed_source_file_id="drivefile_0123456789abcdef",
            )
        with self.assertRaisesRegex(TaxonomyContractError, "source_path_stale"):
            prepare_move_execution(
                job,
                observed_source_sha256="a" * 64,
                observed_source_path=f"{ROOT}/wrong.pdf",
                observed_source_file_id="drivefile_0123456789abcdef",
            )
        with self.assertRaisesRegex(TaxonomyContractError, "destination_collision"):
            prepare_move_execution(
                job,
                observed_source_sha256="a" * 64,
                observed_source_path=f"{ROOT}/paper-7.pdf",
                observed_source_file_id="drivefile_0123456789abcdef",
                destination_exists=True,
            )

    def test_exact_destination_can_reconcile_a_crash_without_second_move(self):
        job = {
            "id": str(uuid4()),
            "status": "running",
            "lease_token": LEASE_ID,
            "paper_id": 7,
            "source_sha256": "a" * 64,
            "source_drive_file_id": "drivefile_0123456789abcdef",
            "old_drive_path": f"{ROOT}/paper-7.pdf",
            "new_drive_path": f"{ROOT}/Learning/paper-7.pdf",
            "direction": "apply",
            "idempotency_key_digest": "b" * 64,
        }
        execution = prepare_move_reconciliation(
            job,
            source_is_missing=True,
            observed_destination_sha256="a" * 64,
            observed_destination_path=f"{ROOT}/Learning/paper-7.pdf",
            observed_destination_file_id="drivefile_0123456789abcdef",
        )
        self.assertEqual(execution.paper_id, 7)
        with self.assertRaisesRegex(TaxonomyContractError, "sha_mismatch"):
            prepare_move_reconciliation(
                job,
                source_is_missing=True,
                observed_destination_sha256="c" * 64,
                observed_destination_path=f"{ROOT}/Learning/paper-7.pdf",
                observed_destination_file_id="drivefile_0123456789abcdef",
            )

    def test_review_calls_require_a_human_before_touching_database(self):
        with self.assertRaisesRegex(TaxonomyContractError, "human_required"):
            review_taxonomy_proposal(
                PROPOSAL_ID,
                decision="approve",
                actor_kind="service",
                actor_id="worker",
                expected_revision=0,
            )
        with self.assertRaisesRegex(TaxonomyContractError, "human_required"):
            review_rollback_proposal(
                "79aef857-a19d-41bb-ae42-d0f3a12cbd46",
                decision="approve",
                actor_kind="service",
                actor_id="worker",
                expected_revision=0,
            )

    def test_usage_is_sanitized_and_requires_pro(self):
        usage = TaxonomyUsage(
            provider="deepseek",
            model=DEEPSEEK_PRO_MODEL,
            prompt_tokens=100,
            completion_tokens=20,
            total_tokens=120,
        )
        self.assertEqual(usage.cost_basis, "unavailable")
        with self.assertRaisesRegex(TaxonomyContractError, "usage_model_invalid"):
            TaxonomyUsage(
                provider="deepseek",
                model="deepseek-v4-flash",
                prompt_tokens=1,
                completion_tokens=1,
                total_tokens=2,
            )


class TaxonomyPersistenceBoundaryTests(unittest.TestCase):
    @staticmethod
    def rollback_binding_row(source, *, journal_old_path=None):
        return {
            "source_move_job_id": source["id"],
            "proposal_id": source["proposal_id"],
            "paper_id": source["paper_id"],
            "source_sha256": source["source_sha256"],
            "old_drive_path": source["old_drive_path"],
            "new_drive_path": source["new_drive_path"],
            "status": "succeeded",
            "direction": "apply",
            "journal_move_job_id": source["id"],
            "journal_paper_id": source["paper_id"],
            "journal_source_sha256": source["source_sha256"],
            "journal_old_drive_path": (
                source["old_drive_path"]
                if journal_old_path is None
                else journal_old_path
            ),
            "journal_new_drive_path": source["new_drive_path"],
            "journal_direction": "apply",
        }

    def test_legacy_year_rollback_is_persisted_only_with_exact_journal_binding(self):
        source = {
            "id": str(uuid4()),
            "proposal_id": PROPOSAL_ID,
            "paper_id": 1,
            "source_sha256": "1" * 64,
            "old_drive_path": f"{ROOT}/2025/paper-1.pdf",
            "new_drive_path": f"{ROOT}/Learning/paper-1.pdf",
            "status": "succeeded",
            "direction": "apply",
        }
        rollback = build_rollback_proposal(
            [source],
            original_proposal_id=PROPOSAL_ID,
            reason="Restore the journaled legacy folder.",
        )
        database = SequenceDatabase(
            [
                Result(many=[self.rollback_binding_row(source)]),
                Result(one={"id": rollback["rollback_proposal_id"]}),
                Result(),
            ]
        )
        with patch("paper_service.taxonomy.connection", return_value=nullcontext(database)):
            stored = create_rollback_proposal(rollback, created_by="human-reviewer")
        self.assertEqual(stored["id"], rollback["rollback_proposal_id"])
        self.assertIn("paper_taxonomy_move_journal", database.calls[0][0])
        self.assertIn("FOR SHARE OF job, journal", database.calls[0][0])
        rollback_item_parameters = database.calls[2][1]
        self.assertEqual(
            rollback_item_parameters[-1], f"{ROOT}/2025/paper-1.pdf"
        )

    def test_arbitrary_legacy_year_path_cannot_pass_journal_binding(self):
        source = {
            "id": str(uuid4()),
            "proposal_id": PROPOSAL_ID,
            "paper_id": 1,
            "source_sha256": "1" * 64,
            "old_drive_path": f"{ROOT}/2025/injected.pdf",
            "new_drive_path": f"{ROOT}/Learning/paper-1.pdf",
            "status": "succeeded",
            "direction": "apply",
        }
        rollback = build_rollback_proposal(
            [source],
            original_proposal_id=PROPOSAL_ID,
            reason="This client value is not in the journal.",
        )
        binding = self.rollback_binding_row(
            source,
            journal_old_path=f"{ROOT}/2024/paper-1.pdf",
        )
        database = SequenceDatabase([Result(many=[binding])])
        with patch("paper_service.taxonomy.connection", return_value=nullcontext(database)):
            with self.assertRaisesRegex(
                TaxonomyContractError, "rollback_journal_binding_mismatch"
            ):
                create_rollback_proposal(rollback, created_by="human-reviewer")
        self.assertEqual(len(database.calls), 1)

    def test_audit_persistence_stores_content_free_snapshot_not_titles(self):
        audit = eligible_audit()
        database = SequenceDatabase([Result(one={"id": "audit"})])
        with patch("paper_service.taxonomy.connection", return_value=nullcontext(database)):
            row = persist_taxonomy_audit(
                audit,
                audit_id="34867ee8-f014-4cc0-9029-9687d7e4f2dd",
            )
        self.assertEqual(row, {"id": "audit"})
        parameters = database.calls[0][1]
        snapshot_json = parameters[3]
        self.assertNotIn("title", snapshot_json)
        self.assertNotIn("topic_terms", snapshot_json)

    def test_human_review_inserts_event_only_and_database_trigger_queues_jobs(self):
        database = SequenceDatabase(
            [Result(one={"decision": "approve", "review_revision": 1})]
        )
        with patch("paper_service.taxonomy.connection", return_value=nullcontext(database)):
            row = review_taxonomy_proposal(
                PROPOSAL_ID,
                decision="approve",
                actor_kind="human",
                actor_id="reviewer@example.invalid",
                expected_revision=0,
                comment="Reviewed assignment paths.",
            )
        self.assertEqual(row["decision"], "approve")
        sql, parameters = database.calls[0]
        self.assertIn("paper_taxonomy_review_events", sql)
        self.assertNotIn("paper_taxonomy_move_jobs", sql)
        self.assertEqual(parameters[2:5], (1, 0, "approve"))

    def test_generation_enqueue_is_bound_to_the_exact_eligible_audit(self):
        rows = paper_rows(diverse=True)
        audit = eligible_audit(rows)
        request = build_deepseek_pro_request(rows, audit, proposal_id=PROPOSAL_ID)
        audit_id = "34867ee8-f014-4cc0-9029-9687d7e4f2dd"
        request_digest = taxonomy_generation_request_digest(
            audit_id=audit_id,
            proposal_id=PROPOSAL_ID,
            provider="deepseek",
            model=DEEPSEEK_PRO_MODEL,
            request_actor_kind="human",
            request_actor_id="human-1",
            metadata_and_cost_acknowledged=True,
            provider_body_digest=request.request_sha256,
            max_output_tokens=request.request_body["max_tokens"],
        )
        audit_row = {
            "category": audit["category"],
            "category_path": audit["category_path"],
            "source_snapshot": audit["source_snapshot"],
            "source_snapshot_sha256": audit["source_snapshot_sha256"],
            "audit_policy": audit["policy"],
            "decision": "request-proposal",
        }
        stored_job = {
            "audit_id": audit_id,
            "proposal_id": PROPOSAL_ID,
            "provider": "deepseek",
            "model": DEEPSEEK_PRO_MODEL,
            "request_actor_kind": "human",
            "request_actor_id": "human-1",
            "metadata_and_cost_acknowledged": True,
            "provider_body_digest": request.request_sha256,
            "request_digest": request_digest,
            "max_output_tokens": request.request_body["max_tokens"],
        }
        database = SequenceDatabase([Result(one=audit_row), Result(one=stored_job)])
        with patch("paper_service.taxonomy.connection", return_value=nullcontext(database)):
            job = enqueue_taxonomy_generation(
                audit_id=audit_id,
                request=request,
                request_actor_kind="human",
                request_actor_id="human-1",
                metadata_and_cost_acknowledged=True,
            )
        self.assertEqual(job["proposal_id"], PROPOSAL_ID)
        self.assertIn("FOR SHARE", database.calls[0][0])
        audit_row["decision"] = "no-op"
        database = SequenceDatabase([Result(one=audit_row)])
        with patch("paper_service.taxonomy.connection", return_value=nullcontext(database)):
            with self.assertRaisesRegex(TaxonomyContractError, "audit_binding_mismatch"):
                enqueue_taxonomy_generation(
                    audit_id=audit_id,
                    request=request,
                    request_actor_kind="human",
                    request_actor_id="human-1",
                    metadata_and_cost_acknowledged=True,
                )

    def test_generation_authorization_is_human_acknowledged_and_digest_bound(self):
        request = build_deepseek_pro_request(
            paper_rows(diverse=True),
            eligible_audit(),
            proposal_id=PROPOSAL_ID,
        )
        audit_id = "34867ee8-f014-4cc0-9029-9687d7e4f2dd"
        values = {
            "audit_id": audit_id,
            "proposal_id": PROPOSAL_ID,
            "provider": "deepseek",
            "model": DEEPSEEK_PRO_MODEL,
            "request_actor_kind": "human",
            "request_actor_id": "human-1",
            "metadata_and_cost_acknowledged": True,
            "provider_body_digest": request.request_sha256,
            "max_output_tokens": request.request_body["max_tokens"],
        }
        values["request_digest"] = taxonomy_generation_request_digest(**values)
        self.assertEqual(
            validate_taxonomy_generation_job_authorization(values),
            ("human", "human-1", True),
        )
        for changed, code in (
            ({**values, "request_actor_kind": "service"}, "human_actor_required"),
            (
                {**values, "metadata_and_cost_acknowledged": False},
                "acknowledgement_required",
            ),
            ({**values, "request_actor_id": "human-2"}, "digest_mismatch"),
        ):
            with self.subTest(code=code):
                with self.assertRaisesRegex(TaxonomyContractError, code):
                    validate_taxonomy_generation_job_authorization(changed)

    def test_generation_claim_filters_and_revalidates_human_consent(self):
        job = {
            "id": str(uuid4()),
            "audit_id": "34867ee8-f014-4cc0-9029-9687d7e4f2dd",
            "proposal_id": PROPOSAL_ID,
            "provider": "deepseek",
            "model": DEEPSEEK_PRO_MODEL,
            "request_actor_kind": "human",
            "request_actor_id": "human-1",
            "metadata_and_cost_acknowledged": True,
            "provider_body_digest": "a" * 64,
            "max_output_tokens": 8192,
        }
        job["request_digest"] = taxonomy_generation_request_digest(
            **{key: value for key, value in job.items() if key != "id"}
        )
        database = SequenceDatabase([Result(), Result(one=job)])
        with patch(
            "paper_service.taxonomy.connection", return_value=nullcontext(database)
        ):
            claimed = claim_taxonomy_generation(worker_id="worker-1")
        self.assertEqual(claimed, job)
        claim_sql = " ".join(database.calls[1][0].split())
        self.assertIn("request_actor_kind = 'human'", claim_sql)
        self.assertIn("metadata_and_cost_acknowledged IS TRUE", claim_sql)

    def test_existing_generation_from_another_actor_is_an_idempotency_conflict(self):
        rows = paper_rows(diverse=True)
        audit = eligible_audit(rows)
        request = build_deepseek_pro_request(rows, audit, proposal_id=PROPOSAL_ID)
        audit_id = "34867ee8-f014-4cc0-9029-9687d7e4f2dd"
        audit_row = {
            "category": audit["category"],
            "category_path": audit["category_path"],
            "source_snapshot": audit["source_snapshot"],
            "source_snapshot_sha256": audit["source_snapshot_sha256"],
            "audit_policy": audit["policy"],
            "decision": "request-proposal",
        }
        stored = {
            "audit_id": audit_id,
            "proposal_id": PROPOSAL_ID,
            "provider": "deepseek",
            "model": DEEPSEEK_PRO_MODEL,
            "request_actor_kind": "human",
            "request_actor_id": "human-1",
            "metadata_and_cost_acknowledged": True,
            "provider_body_digest": request.request_sha256,
            "max_output_tokens": 8192,
        }
        stored["request_digest"] = taxonomy_generation_request_digest(**stored)
        database = SequenceDatabase(
            [Result(one=audit_row), Result(one=None), Result(one=stored)]
        )
        with patch(
            "paper_service.taxonomy.connection", return_value=nullcontext(database)
        ):
            with self.assertRaisesRegex(
                TaxonomyContractError, "idempotency_conflict"
            ):
                enqueue_taxonomy_generation(
                    audit_id=audit_id,
                    request=request,
                    request_actor_kind="human",
                    request_actor_id="human-2",
                    metadata_and_cost_acknowledged=True,
                )


class TaxonomyMigrationTests(unittest.TestCase):
    def test_schema_has_proposal_review_move_rollback_and_usage_boundaries(self):
        for fragment in (
            "CREATE TABLE paper_taxonomy_audits",
            "CREATE TABLE paper_taxonomy_generation_jobs",
            "model = 'deepseek-v4-pro'",
            "request_actor_kind TEXT NOT NULL CHECK (request_actor_kind = 'human')",
            "metadata_and_cost_acknowledged BOOLEAN NOT NULL",
            "provider_body_digest CHAR(64) NOT NULL",
            "CREATE TABLE paper_taxonomy_proposals",
            "CREATE TABLE paper_taxonomy_proposal_assignments",
            "CREATE TABLE paper_taxonomy_review_events",
            "actor_kind = 'human'",
            "CREATE TABLE paper_taxonomy_move_jobs",
            "CREATE TABLE paper_taxonomy_move_journal",
            "CREATE TABLE paper_taxonomy_rollback_proposals",
            "CREATE TABLE paper_taxonomy_rollback_review_events",
            "CREATE TABLE paper_taxonomy_usage_ledger",
        ):
            self.assertIn(fragment, TAXONOMY_SQL)

    def test_schema_enforces_immutable_history_and_approval_only_enqueue(self):
        for table in (
            "paper_taxonomy_audits",
            "paper_taxonomy_proposals",
            "paper_taxonomy_proposal_assignments",
            "paper_taxonomy_review_events",
            "paper_taxonomy_move_journal",
            "paper_taxonomy_rollback_proposals",
            "paper_taxonomy_rollback_items",
            "paper_taxonomy_rollback_review_events",
            "paper_taxonomy_usage_ledger",
        ):
            self.assertIn(f"BEFORE UPDATE OR DELETE ON {table}", TAXONOMY_SQL)
        self.assertIn("paper_taxonomy_validate_review_event", TAXONOMY_SQL)
        self.assertIn("paper_taxonomy_validate_generation_job", TAXONOMY_SQL)
        self.assertIn("paper_taxonomy_validate_usage", TAXONOMY_SQL)
        self.assertIn("OLD.request_actor_kind, OLD.request_actor_id", TAXONOMY_SQL)
        self.assertIn("OLD.metadata_and_cost_acknowledged", TAXONOMY_SQL)
        self.assertIn("OLD.provider_body_digest", TAXONOMY_SQL)
        self.assertIn("current_job.provider_body_digest", TAXONOMY_SQL)
        self.assertIn("NEW.decision = 'approve'", TAXONOMY_SQL)
        self.assertIn("paper.drive_path IS DISTINCT FROM assignment.old_drive_path", TAXONOMY_SQL)
        self.assertIn("source_snapshot -> 'papers'", TAXONOMY_SQL)
        self.assertIn("groups.members < 4", TAXONOMY_SQL)
        self.assertIn("other_count * 4 > assignment_count", TAXONOMY_SQL)
        self.assertIn(
            "direction = 'rollback'\n        OR new_drive_path !~",
            TAXONOMY_SQL,
        )
        self.assertIn("journal.old_drive_path <> item.new_drive_path", TAXONOMY_SQL)
        self.assertIn("journal.new_drive_path <> item.old_drive_path", TAXONOMY_SQL)
        self.assertIn("source_drive_file_id TEXT", TAXONOMY_SQL)
        self.assertIn(
            "char_length(source_drive_file_id) BETWEEN 10 AND 256",
            TAXONOMY_SQL,
        )
        self.assertNotIn("{10,256}", TAXONOMY_SQL)
        self.assertIn(
            "paper taxonomy Drive file identity is immutable once bound",
            TAXONOMY_SQL,
        )
        self.assertIn("NEW.source_drive_file_id", TAXONOMY_SQL)

    def test_schema_contains_no_prompt_raw_response_or_paper_body_log(self):
        lowered = TAXONOMY_SQL.lower()
        for forbidden_column in (
            "prompt_text",
            "raw_response",
            "paper_body",
            "abstract_text",
        ):
            self.assertNotIn(forbidden_column, lowered)
        self.assertIn("last_error_code", lowered)
        self.assertIn("request_digest", lowered)
        self.assertIn("response_digest", lowered)


if __name__ == "__main__":
    unittest.main()

import copy
import json
import unittest
from contextlib import nullcontext
from unittest.mock import patch
from uuid import uuid4

from paper_service.taxonomy import DEEPSEEK_PRO_MODEL, parse_taxonomy_proposal
from paper_service import taxonomy_queries as queries
from tests.test_paper_taxonomy_runner import (
    CATEGORY,
    CATEGORY_ROOT,
    eligible_audit,
    generation_job,
    papers,
    valid_proposal,
)


class Result:
    def __init__(self, *, one=None, many=None):
        self.one = one
        self.many = [] if many is None else many

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


def canonical_fixture():
    rows = papers()
    job, audit, request = generation_job(rows)
    canonical = parse_taxonomy_proposal(
        json.dumps(valid_proposal(request), ensure_ascii=False),
        request,
    )
    return rows, job, audit, request, canonical


def pending_audit_row(*, with_generation=False):
    audit = eligible_audit()
    row = {
        "id": str(uuid4()),
        "category": CATEGORY,
        "source_snapshot_sha256": audit["source_snapshot_sha256"],
        "paper_count": audit["metrics"]["paper_count"],
        "new_paper_count": audit["metrics"]["new_paper_count"],
        "metrics": audit["metrics"],
        "audit_policy": audit["policy"],
        "reason_codes": audit["reason_codes"],
        "evaluated_at": "2026-08-29T00:00:00Z",
        "created_at": "2026-08-29T00:00:01Z",
        "generation_job_id": None,
        "generation_status": None,
        "generation_provider": None,
        "generation_model": None,
        "request_digest": None,
        "generation_requested_at": None,
    }
    if with_generation:
        row.update(
            {
                "generation_job_id": str(uuid4()),
                "generation_status": "queued",
                "generation_provider": "deepseek",
                "generation_model": DEEPSEEK_PRO_MODEL,
                "request_digest": "a" * 64,
                "generation_requested_at": "2026-08-29T00:00:02Z",
            }
        )
    return row


def proposal_summary_row(canonical, job):
    return {
        "id": canonical["proposal_id"],
        "audit_id": job["audit_id"],
        "generation_job_id": job["id"],
        "category": canonical["category"],
        "source_snapshot_sha256": canonical["source_snapshot_sha256"],
        "canonical_payload_sha256": canonical["proposal_payload_sha256"],
        "provider": "deepseek",
        "model": DEEPSEEK_PRO_MODEL,
        "request_digest": job["request_digest"],
        "generation_status": "succeeded",
        "generation_result_proposal_id": canonical["proposal_id"],
        "generation_audit_id": job["audit_id"],
        "generation_proposal_id": canonical["proposal_id"],
        "generation_provider": "deepseek",
        "generation_model": DEEPSEEK_PRO_MODEL,
        "audit_decision": "request-proposal",
        "audit_source_snapshot_sha256": canonical[
            "source_snapshot_sha256"
        ],
        "review_revision": 0,
        "assignment_count": len(canonical["assignments"]),
        "subtheme_count": len(canonical["subthemes"]),
        "created_at": "2026-08-29T00:00:03Z",
    }


def proposal_header(canonical, job, audit, *, approved=False):
    row = proposal_summary_row(canonical, job)
    row.update(
        {
            "canonical_payload": canonical,
            "audit_policy": audit["policy"],
            "evaluated_at": audit["evaluated_at"],
            "finished_at": "2026-08-29T00:00:02Z",
            "review_updated_at": None,
            "review_event_id": None,
            "event_revision": None,
            "review_decision": None,
            "review_created_at": None,
        }
    )
    if approved:
        row.update(
            {
                "review_revision": 1,
                "review_updated_at": "2026-08-29T00:00:05Z",
                "review_event_id": str(uuid4()),
                "event_revision": 1,
                "review_decision": "approve",
                "review_created_at": "2026-08-29T00:00:05Z",
            }
        )
    return row


def assignment_rows(canonical):
    return [
        {
            "ordinal": ordinal,
            "paper_id": item["paper_id"],
            "source_sha256": item["source_sha256"],
            "old_drive_path": item["current_drive_path"],
            "new_drive_path": item["proposed_drive_path"],
            "subtheme": item["subtheme"],
            "move_job_id": item["move_job_id"],
            "idempotency_key_digest": item["idempotency_key_digest"],
        }
        for ordinal, item in enumerate(canonical["assignments"])
    ]


class PendingAuditQueryTests(unittest.TestCase):
    def test_includes_eligible_audit_before_generation_is_requested(self):
        database = SequenceDatabase(
            [Result(many=[pending_audit_row(with_generation=False)])]
        )
        with patch.object(queries, "connection", return_value=nullcontext(database)):
            result = queries.list_pending_taxonomy_audits(limit=25)
        self.assertEqual(len(result), 1)
        item = result[0]
        self.assertEqual(item["generation"], {
            "requested": False,
            "status": "not-requested",
        })
        self.assertEqual(item["paper_count"], 12)
        self.assertEqual(len(item["policy_digest"]), 64)
        self.assertEqual(database.calls[0][1], (25,))
        sql = " ".join(database.calls[0][0].split()).casefold()
        self.assertIn("decision = 'request-proposal'", sql)
        self.assertIn("proposal.id is null", sql)
        self.assertNotIn("lease_owner", sql)
        self.assertNotIn("lease_token", sql)
        self.assertNotIn("source_snapshot,", sql)

    def test_generation_projection_has_only_safe_provenance(self):
        database = SequenceDatabase(
            [Result(many=[pending_audit_row(with_generation=True)])]
        )
        with patch.object(queries, "connection", return_value=nullcontext(database)):
            item = queries.list_pending_taxonomy_audits()[0]
        self.assertEqual(item["generation"]["status"], "queued")
        self.assertEqual(item["generation"]["provider"], "deepseek")
        serialized = json.dumps(item, ensure_ascii=False)
        for forbidden in (
            "lease_owner",
            "lease_token",
            "prompt",
            "raw_response",
            CATEGORY_ROOT,
        ):
            self.assertNotIn(forbidden, serialized)

    def test_list_limit_is_bounded_before_database_access(self):
        with patch.object(queries, "connection") as connect:
            with self.assertRaisesRegex(
                queries.TaxonomyQueryError, "taxonomy_audit_limit_invalid"
            ):
                queries.list_pending_taxonomy_audits(
                    limit=queries.MAX_LIST_LIMIT + 1
                )
        connect.assert_not_called()


class PendingProposalQueryTests(unittest.TestCase):
    def test_pending_list_returns_digest_revision_and_counts_only(self):
        _rows, job, _audit, _request, canonical = canonical_fixture()
        database = SequenceDatabase(
            [Result(many=[proposal_summary_row(canonical, job)])]
        )
        with patch.object(queries, "connection", return_value=nullcontext(database)):
            result = queries.list_pending_taxonomy_proposals(limit=10)
        item = result[0]
        self.assertEqual(item["review_revision"], 0)
        self.assertEqual(item["review_status"], "pending")
        self.assertEqual(item["assignment_count"], 12)
        self.assertEqual(item["subtheme_count"], 2)
        self.assertEqual(item["provenance"]["provider"], "deepseek")
        self.assertEqual(item["provenance"]["model"], DEEPSEEK_PRO_MODEL)
        serialized = json.dumps(item, ensure_ascii=False)
        self.assertNotIn("canonical_payload", serialized)
        self.assertNotIn("lease_", serialized)
        self.assertNotIn(CATEGORY_ROOT, serialized)
        sql = " ".join(database.calls[0][0].split()).casefold()
        self.assertIn("event.id is null", sql)
        self.assertNotIn("lease_owner", sql)

    def test_bad_provider_provenance_fails_closed(self):
        _rows, job, _audit, _request, canonical = canonical_fixture()
        row = proposal_summary_row(canonical, job)
        row["provider"] = "other"
        database = SequenceDatabase([Result(many=[row])])
        with patch.object(queries, "connection", return_value=nullcontext(database)):
            with self.assertRaisesRegex(
                queries.TaxonomyQueryError,
                "taxonomy_proposal_provenance_invalid",
            ):
                queries.list_pending_taxonomy_proposals()


class ProposalDetailQueryTests(unittest.TestCase):
    def test_detail_validates_payload_and_bounds_assignments(self):
        source, job, audit, _request, canonical = canonical_fixture()
        header = proposal_header(canonical, job, audit)
        rows = assignment_rows(canonical)
        database = SequenceDatabase(
            [Result(one=header), Result(many=rows[:6])]
        )
        with patch.object(queries, "connection", return_value=nullcontext(database)):
            detail = queries.get_taxonomy_proposal_detail(
                canonical["proposal_id"], assignment_limit=5
            )
        self.assertEqual(detail["schema"], "paper-taxonomy-proposal-review")
        self.assertEqual(detail["review"], {
            "status": "pending",
            "revision": 0,
            "event_id": None,
            "decision": None,
            "updated_at": None,
        })
        self.assertEqual(len(detail["assignments"]), 5)
        self.assertFalse(detail["assignments_complete"])
        self.assertEqual(
            sum(item["assignment_count"] for item in detail["subthemes"]),
            12,
        )
        self.assertEqual(database.calls[1][1], (canonical["proposal_id"], 6))
        serialized = json.dumps(detail, ensure_ascii=False)
        for forbidden in (
            "canonical_payload",
            "old_drive_path",
            "new_drive_path",
            "lease_owner",
            "lease_token",
            "prompt",
            "raw_response",
            CATEGORY_ROOT,
            source[0]["title"],
        ):
            self.assertNotIn(forbidden, serialized)

    def test_reviewed_detail_exposes_event_revision_without_actor_data(self):
        _source, job, audit, _request, canonical = canonical_fixture()
        header = proposal_header(canonical, job, audit, approved=True)
        rows = assignment_rows(canonical)
        database = SequenceDatabase([Result(one=header), Result(many=rows)])
        with patch.object(queries, "connection", return_value=nullcontext(database)):
            detail = queries.get_taxonomy_proposal_detail(
                canonical["proposal_id"]
            )
        self.assertEqual(detail["review_status"], "approved")
        self.assertEqual(detail["review"]["revision"], 1)
        self.assertEqual(detail["review"]["decision"], "approve")
        self.assertTrue(detail["assignments_complete"])
        self.assertNotIn("actor_id", json.dumps(detail))

    def test_assignment_table_mismatch_fails_closed(self):
        _source, job, audit, _request, canonical = canonical_fixture()
        header = proposal_header(canonical, job, audit)
        rows = assignment_rows(canonical)
        rows[0] = copy.deepcopy(rows[0])
        rows[0]["new_drive_path"] = f"{CATEGORY_ROOT}/Other/tampered.pdf"
        database = SequenceDatabase([Result(one=header), Result(many=rows)])
        with patch.object(queries, "connection", return_value=nullcontext(database)):
            with self.assertRaisesRegex(
                queries.TaxonomyQueryError,
                "stored_taxonomy_assignment_identity_mismatch",
            ):
                queries.get_taxonomy_proposal_detail(canonical["proposal_id"])

    def test_not_found_and_assignment_limit_are_explicit(self):
        proposal_id = str(uuid4())
        database = SequenceDatabase([Result(one=None)])
        with patch.object(queries, "connection", return_value=nullcontext(database)):
            with self.assertRaisesRegex(
                queries.TaxonomyQueryNotFound, "taxonomy_proposal_not_found"
            ):
                queries.get_taxonomy_proposal_detail(proposal_id)
        with patch.object(queries, "connection") as connect:
            with self.assertRaisesRegex(
                queries.TaxonomyQueryError,
                "taxonomy_assignment_limit_invalid",
            ):
                queries.get_taxonomy_proposal_detail(
                    proposal_id,
                    assignment_limit=queries.MAX_ASSIGNMENT_LIMIT + 1,
                )
        connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()

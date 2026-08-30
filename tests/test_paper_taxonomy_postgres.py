"""Opt-in PostgreSQL integration probe for taxonomy migration 007.

Set PAPER_TAXONOMY_TEST_DSN, or PAPER_TAXONOMY_TEST_ENABLE=1 plus the regular
PAPER_DB_* variables, for a disposable database where migration 007 is already
applied.  Every fixture write is rolled back.
"""

import json
import os
import unittest
from uuid import uuid4

from paper_service.taxonomy import (
    build_deepseek_pro_request,
    build_rollback_proposal,
    evaluate_taxonomy_audit,
    parse_taxonomy_proposal,
    taxonomy_generation_request_digest,
)


ROOT = "paper-drive:Original/Taxonomy Probe"


def rows():
    return [
        {
            "id": 90_000 + index,
            "sha256": f"{90_000 + index:064x}",
            "drive_path": f"{ROOT}/probe-{index}.pdf",
            "category": "Taxonomy Probe",
            "title": f"Probe paper {index}",
            "topic_terms": [f"term-{index}"],
            "imported_at": "2026-08-01T00:00:00Z",
        }
        for index in range(1, 13)
    ]


def model_payload(request):
    assignments = []
    for index, paper in enumerate(request.source_snapshot["papers"]):
        subtheme = "Alpha" if index < 6 else "Beta"
        assignments.append(
            {
                "paper_id": paper["paper_id"],
                "subtheme": subtheme,
            }
        )
    return {
        "subthemes": [
            {"name": "Alpha", "description": "First bounded group."},
            {"name": "Beta", "description": "Second bounded group."},
        ],
        "assignments": assignments,
        "rationale": "Synthetic integration fixture.",
    }


def test_connection_config():
    dsn = os.environ.get("PAPER_TAXONOMY_TEST_DSN")
    if dsn:
        return dsn
    return {
        "dbname": os.environ.get("PAPER_DB_NAME", "paper_os"),
        "user": os.environ.get("PAPER_DB_USER", "paper_os"),
        "password": os.environ.get("PAPER_DB_PASSWORD"),
        "host": os.environ.get("PAPER_DB_HOST", "postgres"),
        "port": os.environ.get("PAPER_DB_PORT", "5432"),
    }


@unittest.skipUnless(
    os.environ.get("PAPER_TAXONOMY_TEST_DSN")
    or os.environ.get("PAPER_TAXONOMY_TEST_ENABLE") == "1",
    "paper taxonomy PostgreSQL integration is not configured",
)
class TaxonomyPostgresIntegrationTests(unittest.TestCase):
    def test_approval_enqueues_exact_moves_and_rollback_needs_new_approval(self):
        import psycopg
        from psycopg.rows import dict_row

        fixtures = rows()
        # Existing libraries may still have a historical year layer.  A new
        # proposal removes it, while a separately approved rollback must be
        # able to restore this exact journaled path.
        fixtures[0]["drive_path"] = f"{ROOT}/2025/probe-1.pdf"
        audit = evaluate_taxonomy_audit(
            fixtures,
            category="Taxonomy Probe",
            category_path=ROOT,
            evaluated_at="2026-08-29T00:00:00Z",
        )
        request = build_deepseek_pro_request(fixtures, audit)
        proposal = parse_taxonomy_proposal(
            json.dumps(model_payload(request)), request
        )
        audit_id = str(uuid4())
        generation_id = str(uuid4())

        config = test_connection_config()
        if isinstance(config, str):
            database_context = psycopg.connect(config, row_factory=dict_row)
        else:
            database_context = psycopg.connect(**config, row_factory=dict_row)
        with database_context as database:
            for paper in fixtures:
                database.execute(
                    """INSERT INTO papers
                       (id, sha256, size_bytes, source_path, stored_path,
                        processing_status, title, category, drive_path)
                       VALUES (%s, %s, 1, %s, %s, 'organized', %s, %s, %s)""",
                    (
                        paper["id"],
                        paper["sha256"],
                        f"probe-{paper['id']}.pdf",
                        f"probe-{paper['id']}.pdf",
                        paper["title"],
                        paper["category"],
                        paper["drive_path"],
                    ),
                )
            database.execute(
                """INSERT INTO paper_taxonomy_audits
                   (id, category, category_path, source_snapshot,
                    source_snapshot_sha256, paper_count, new_paper_count,
                    metrics, audit_policy, decision, reason_codes, evaluated_at)
                   VALUES (%s::uuid, %s, %s, %s::jsonb, %s, %s, %s,
                           %s::jsonb, %s::jsonb, %s, %s::jsonb, %s)""",
                (
                    audit_id,
                    audit["category"],
                    audit["category_path"],
                    json.dumps(audit["source_snapshot"]),
                    audit["source_snapshot_sha256"],
                    audit["metrics"]["paper_count"],
                    audit["metrics"]["new_paper_count"],
                    json.dumps(audit["metrics"]),
                    json.dumps(audit["policy"]),
                    audit["decision"],
                    json.dumps(audit["reason_codes"]),
                    audit["evaluated_at"],
                ),
            )
            database.execute(
                """INSERT INTO paper_taxonomy_generation_jobs
                   (id, audit_id, proposal_id, request_actor_kind,
                    request_actor_id, metadata_and_cost_acknowledged,
                    provider_body_digest, request_digest, max_output_tokens)
                   VALUES (%s::uuid, %s::uuid, %s::uuid, 'human',
                           'integration-requester', TRUE, %s, %s, 8192)""",
                (
                    generation_id,
                    audit_id,
                    proposal["proposal_id"],
                    request.request_sha256,
                    taxonomy_generation_request_digest(
                        audit_id=audit_id,
                        proposal_id=proposal["proposal_id"],
                        provider="deepseek",
                        model="deepseek-v4-pro",
                        request_actor_kind="human",
                        request_actor_id="integration-requester",
                        metadata_and_cost_acknowledged=True,
                        provider_body_digest=request.request_sha256,
                        max_output_tokens=8192,
                    ),
                ),
            )
            database.execute(
                """INSERT INTO paper_taxonomy_proposals
                   (id, audit_id, generation_job_id, category, category_path,
                    source_snapshot_sha256, canonical_payload,
                    canonical_payload_sha256)
                   VALUES (%s::uuid, %s::uuid, %s::uuid, %s, %s, %s,
                           %s::jsonb, %s)""",
                (
                    proposal["proposal_id"],
                    audit_id,
                    generation_id,
                    proposal["category"],
                    proposal["category_path"],
                    proposal["source_snapshot_sha256"],
                    json.dumps(proposal),
                    proposal["proposal_payload_sha256"],
                ),
            )
            for ordinal, item in enumerate(proposal["assignments"]):
                database.execute(
                    """INSERT INTO paper_taxonomy_proposal_assignments
                       (proposal_id, ordinal, paper_id, source_sha256,
                        old_drive_path, new_drive_path, subtheme, move_job_id,
                        idempotency_key_digest)
                       VALUES (%s::uuid, %s, %s, %s, %s, %s, %s, %s::uuid, %s)""",
                    (
                        proposal["proposal_id"],
                        ordinal,
                        item["paper_id"],
                        item["source_sha256"],
                        item["current_drive_path"],
                        item["proposed_drive_path"],
                        item["subtheme"],
                        item["move_job_id"],
                        item["idempotency_key_digest"],
                    ),
                )
            database.execute(
                """UPDATE paper_taxonomy_generation_jobs
                   SET status = 'succeeded', result_proposal_id = %s::uuid,
                       finished_at = NOW()
                   WHERE id = %s::uuid""",
                (proposal["proposal_id"], generation_id),
            )
            approval_id = str(uuid4())
            database.execute(
                """INSERT INTO paper_taxonomy_review_events
                   (id, proposal_id, review_revision, expected_revision,
                    decision, actor_kind, actor_id)
                   VALUES (%s::uuid, %s::uuid, 1, 0,
                           'approve', 'human', 'integration-reviewer')""",
                (approval_id, proposal["proposal_id"]),
            )
            jobs = database.execute(
                """SELECT * FROM paper_taxonomy_move_jobs
                   WHERE proposal_id = %s::uuid ORDER BY paper_id""",
                (proposal["proposal_id"],),
            ).fetchall()
            self.assertEqual(len(jobs), 12)
            self.assertTrue(all(row["status"] == "queued" for row in jobs))

            original = dict(jobs[0])
            lease_token = str(uuid4())
            source_drive_file_id = "integration_drive_file_id_001"
            database.execute(
                """UPDATE paper_taxonomy_move_jobs
                   SET status = 'running', attempt_count = 1,
                       lease_owner = 'integration-worker',
                       lease_token = %s::uuid,
                       lease_expires_at = NOW() + interval '5 minutes',
                       started_at = NOW() WHERE id = %s::uuid""",
                (lease_token, original["id"]),
            )
            # Match the runtime's two-step safety boundary: first acquire a
            # lease, then bind the immutable provider object identity while
            # that same lease is current.
            database.execute(
                """UPDATE paper_taxonomy_move_jobs
                   SET source_drive_file_id = %s
                   WHERE id = %s::uuid""",
                (source_drive_file_id, original["id"]),
            )
            database.execute(
                "UPDATE papers SET drive_path = %s WHERE id = %s",
                (original["new_drive_path"], original["paper_id"]),
            )
            database.execute(
                """INSERT INTO paper_taxonomy_move_journal
                   (id, move_job_id, paper_id, direction, source_sha256,
                    source_drive_file_id,
                    old_drive_path, new_drive_path, idempotency_key_digest,
                    worker_id)
                   VALUES (%s::uuid, %s::uuid, %s, %s, %s, %s, %s, %s, %s,
                           'integration-worker')""",
                (
                    str(uuid4()),
                    original["id"],
                    original["paper_id"],
                    original["direction"],
                    str(original["source_sha256"]).strip(),
                    source_drive_file_id,
                    original["old_drive_path"],
                    original["new_drive_path"],
                    str(original["idempotency_key_digest"]).strip(),
                ),
            )
            database.execute(
                """UPDATE paper_taxonomy_move_jobs
                   SET status = 'succeeded', lease_owner = NULL,
                       lease_token = NULL, lease_expires_at = NULL,
                       finished_at = NOW() WHERE id = %s::uuid""",
                (original["id"],),
            )
            original["status"] = "succeeded"
            original["direction"] = "apply"
            rollback = build_rollback_proposal(
                [original],
                original_proposal_id=proposal["proposal_id"],
                reason="Integration rollback verification.",
            )
            database.execute(
                """INSERT INTO paper_taxonomy_rollback_proposals
                   (id, original_proposal_id, canonical_payload,
                    canonical_payload_sha256, reason, created_by)
                   VALUES (%s::uuid, %s::uuid, %s::jsonb, %s, %s,
                           'integration-reviewer')""",
                (
                    rollback["rollback_proposal_id"],
                    rollback["original_proposal_id"],
                    json.dumps(rollback),
                    rollback["rollback_payload_sha256"],
                    rollback["reason"],
                ),
            )
            item = rollback["items"][0]
            database.execute(
                """INSERT INTO paper_taxonomy_rollback_items
                   (rollback_proposal_id, ordinal, source_move_job_id,
                    move_job_id, idempotency_key_digest, paper_id,
                    source_sha256, old_drive_path, new_drive_path)
                   VALUES (%s::uuid, 0, %s::uuid, %s::uuid, %s, %s,
                           %s, %s, %s)""",
                (
                    rollback["rollback_proposal_id"],
                    item["source_move_job_id"],
                    item["move_job_id"],
                    item["idempotency_key_digest"],
                    item["paper_id"],
                    item["source_sha256"],
                    item["current_drive_path"],
                    item["restored_drive_path"],
                ),
            )
            database.execute(
                """INSERT INTO paper_taxonomy_rollback_review_events
                   (id, rollback_proposal_id, review_revision,
                    expected_revision, decision, actor_kind, actor_id)
                   VALUES (%s::uuid, %s::uuid, 1, 0, 'approve', 'human',
                           'integration-reviewer')""",
                (str(uuid4()), rollback["rollback_proposal_id"]),
            )
            rollback_job = database.execute(
                """SELECT * FROM paper_taxonomy_move_jobs
                   WHERE rollback_proposal_id = %s::uuid""",
                (rollback["rollback_proposal_id"],),
            ).fetchone()
            self.assertEqual(rollback_job["direction"], "rollback")
            self.assertEqual(rollback_job["old_drive_path"], original["new_drive_path"])
            self.assertEqual(rollback_job["new_drive_path"], original["old_drive_path"])
            self.assertEqual(rollback_job["new_drive_path"], f"{ROOT}/2025/probe-1.pdf")
            database.rollback()


if __name__ == "__main__":
    unittest.main()

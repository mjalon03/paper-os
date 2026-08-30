from __future__ import annotations

from contextlib import nullcontext
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

from paper_service.knowledge import payload_sha256
from paper_service.knowledge_queries import (
    KnowledgeQueryError,
    KnowledgeQueryNotFound,
    get_generation_job,
    knowledge_overview,
    make_reviewed_copy,
)


NODE_ID = "n-" + "a" * 24


def payload(status="ai-draft"):
    return {
        "schema_version": 1,
        "selector_version": 1,
        "revision_id": str(uuid4()),
        "status": status,
        "paper_identity": {"paper_id": 7, "title": "Paper"},
        "source_sha256": "b" * 64,
        "article_fingerprint": "c" * 64,
        "summary": "Summary.",
        "claims": [
            {
                "text": "Claim.",
                "evidence": [{"node_id": NODE_ID, "exact_quote": "Evidence."}],
            }
        ],
        "key_methods": [],
        "key_findings": [],
        "limitations": [],
    }


def revision_row(value):
    return {
        "id": value["revision_id"],
        "paper_id": 7,
        "revision_number": 1,
        "parent_revision_id": None,
        "source_job_id": None,
        "status": value["status"],
        "canonical_payload": value,
        "canonical_payload_sha256": payload_sha256(value),
        "source_sha256": value["source_sha256"],
        "article_fingerprint": value["article_fingerprint"],
        "selector_version": 1,
        "created_at": None,
    }


class Result:
    def __init__(self, one=None):
        self.one = one

    def fetchone(self):
        return self.one


class SequenceDatabase:
    def __init__(self, rows):
        self.rows = iter(rows)
        self.calls = []

    def execute(self, sql, parameters=None):
        self.calls.append((sql, parameters))
        return Result(next(self.rows))


class KnowledgeQueryTests(unittest.TestCase):
    def test_overview_returns_validated_latest_revision_and_bounded_projection(self):
        draft = payload()
        database = SequenceDatabase(
            [
                {"id": 7, "title": "Paper", "reader_v2_status": "ready"},
                {"id": str(uuid4()), "paper_id": 7, "status": "succeeded"},
                revision_row(draft),
                {"review_revision": 0, "updated_at": None},
                None,
                None,
                {
                    "attempts": 1,
                    "total_tokens": 120,
                    "known_cost_microusd": 0,
                    "unknown_cost_attempts": 1,
                },
            ]
        )
        with patch(
            "paper_service.knowledge_queries.connection",
            return_value=nullcontext(database),
        ):
            result = knowledge_overview(7)

        self.assertEqual(result["latest_revision"]["canonical_payload"], draft)
        self.assertEqual(result["review_revision"], 0)
        self.assertEqual(result["usage"]["total_tokens"], 120)
        self.assertEqual(len(database.calls), 7)

    def test_overview_fails_closed_on_stored_payload_digest_mismatch(self):
        draft = payload()
        damaged = revision_row(draft)
        damaged["canonical_payload_sha256"] = "0" * 64
        database = SequenceDatabase(
            [
                {"id": 7, "title": "Paper", "reader_v2_status": "ready"},
                None,
                damaged,
                None,
                None,
                None,
                {
                    "attempts": 0,
                    "total_tokens": 0,
                    "known_cost_microusd": 0,
                    "unknown_cost_attempts": 0,
                },
            ]
        )
        with patch(
            "paper_service.knowledge_queries.connection",
            return_value=nullcontext(database),
        ), self.assertRaisesRegex(
            KnowledgeQueryError, "stored_revision_digest_mismatch"
        ):
            knowledge_overview(7)

    def test_reviewed_copy_changes_only_revision_identity_and_status(self):
        draft_payload = payload()
        draft = revision_row(draft_payload)
        draft["canonical_payload"] = draft_payload
        with patch(
            "paper_service.knowledge_queries.get_knowledge_revision",
            return_value=draft,
        ):
            reviewed = make_reviewed_copy(draft_payload["revision_id"])

        UUID(reviewed["revision_id"])
        self.assertNotEqual(reviewed["revision_id"], draft_payload["revision_id"])
        self.assertEqual(reviewed["status"], "reviewed")
        for key in draft_payload:
            if key not in {"revision_id", "status"}:
                self.assertEqual(reviewed[key], draft_payload[key])

    def test_job_not_found_is_distinct(self):
        database = SequenceDatabase([None])
        with patch(
            "paper_service.knowledge_queries.connection",
            return_value=nullcontext(database),
        ), self.assertRaises(KnowledgeQueryNotFound):
            get_generation_job(str(uuid4()))


if __name__ == "__main__":
    unittest.main()

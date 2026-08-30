from __future__ import annotations

from contextlib import contextmanager
import hashlib
import inspect
import json
import unittest
from unittest.mock import patch

from paper_service.grounded_search import SourceKind
from paper_service.migrations.v006_search_qa import SQL as SEARCH_QA_SQL
from paper_service.qa_contract import validate_qa_request
from paper_service.search_store import (
    QAUsage,
    QALeaseLost,
    SearchStoreError,
    _digest,
    _qa_request_digest,
    claim_qa_job,
    complete_qa_job,
    enqueue_qa_job,
    nack_qa_job,
    prepare_search_corpus,
    qa_job_identity,
    search_papers,
    validate_qa_evidence_snapshot,
)
from paper_service.selectors import SELECTOR_VERSION, build_selector_index


PAPER_SHA = "a" * 64
ANNOTATION_ID = "3d79148a-460b-4cf2-bf53-cb39900df21f"
REVISION_ID = "c2e0783a-b598-4b51-9808-5e4342bb30e0"
JOB_ID = "466f9661-1493-4bdc-8e15-b29e69bcf0ed"
LEASE_TOKEN = "871140c4-8684-463b-8917-ab69a16e1d33"


def article_fixture() -> dict:
    return {
        "schema_version": 2,
        "reader_version": 2,
        "identity": {"title": "Grounded evidence"},
        "provenance": {"source": "docling_pdf"},
        "abstract": [],
        "sections": [
            {
                "id": "results",
                "level": 1,
                "title": "Results",
                "content": [
                    {
                        "kind": "paragraph",
                        "text": "The signal increased by 12 percent.",
                        "page": 2,
                    },
                    {
                        "kind": "paragraph",
                        "text": "A second paragraph describes the method.",
                        "page": 2,
                    },
                ],
                "sections": [],
            }
        ],
        "figures": [],
        "tables": [],
        "references": [],
        "excluded": [],
        "warnings": [],
    }


def reviewed_payload(article: dict, *, status: str = "reviewed") -> dict:
    index = build_selector_index(article)
    node_id = index.nodes[0].node_id
    return {
        "schema_version": 1,
        "selector_version": SELECTOR_VERSION,
        "revision_id": REVISION_ID,
        "status": status,
        "paper_identity": {"paper_id": 7, "title": "Grounded evidence"},
        "source_sha256": PAPER_SHA,
        "article_fingerprint": index.article_fingerprint,
        "summary": "A reviewed synthesis.",
        "claims": [
            {
                "text": "The reviewed claim says the signal increased.",
                "evidence": [
                    {
                        "node_id": node_id,
                        "exact_quote": "The signal increased by 12 percent.",
                    }
                ],
            }
        ],
        "key_methods": ["Bounded extraction"],
        "key_findings": ["Signal increased"],
        "limitations": ["Single paper"],
    }


def annotation(article: dict, *, share: bool = False, trashed_at=None) -> dict:
    index = build_selector_index(article)
    node = index.nodes[0]
    return {
        "id": ANNOTATION_ID,
        "paper_id": 7,
        "kind": "note",
        "anchor": {
            "selector_version": SELECTOR_VERSION,
            "node_id": node.node_id,
            "exact_quote": "signal increased",
        },
        "body": "My interpretation of the signal.",
        "article_fingerprint": index.article_fingerprint,
        "trashed_at": trashed_at,
        "ai_share_allowed": share,
    }


def paper() -> dict:
    return {"id": 7, "sha256": PAPER_SHA}


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


def chunk_row(chunk) -> dict:
    return {
        "paper_id": 7,
        "chunk_id": chunk.chunk_id,
        "document_id": chunk.document_id,
        "selector_version": chunk.selector_version,
        "source_kind": chunk.source_kind.value,
        "source_id": chunk.source_id,
        "node_id": chunk.node_id,
        "start_offset": chunk.start,
        "end_offset": chunk.end,
        "content": chunk.text,
        "lexical_content": chunk.lexical_text,
        "authority_score": chunk.authority_score,
    }


class SearchMigrationTests(unittest.TestCase):
    def test_schema_is_extension_free_strict_and_append_only(self):
        self.assertIn("TSVECTOR GENERATED ALWAYS", SEARCH_QA_SQL)
        self.assertIn("USING GIN (search_vector)", SEARCH_QA_SQL)
        self.assertNotIn("pg_trgm", SEARCH_QA_SQL)
        self.assertIn(
            "source_kind IN ('source_text', 'reviewed_knowledge', 'user_annotation')",
            SEARCH_QA_SQL,
        )
        self.assertNotIn("ai_draft_knowledge", SEARCH_QA_SQL)
        self.assertIn("include_personal_annotations BOOLEAN NOT NULL DEFAULT FALSE", SEARCH_QA_SQL)
        self.assertIn("ai_share_allowed BOOLEAN NOT NULL DEFAULT FALSE", SEARCH_QA_SQL)
        self.assertIn("paper_search_generations_immutable", SEARCH_QA_SQL)
        self.assertIn("paper_qa_job_evidence_immutable", SEARCH_QA_SQL)
        self.assertIn("paper_qa_usage_ledger_immutable", SEARCH_QA_SQL)
        self.assertIn(
            "request_actor_kind TEXT NOT NULL CHECK (request_actor_kind = 'human')",
            SEARCH_QA_SQL,
        )
        self.assertIn("request_actor_id = btrim(request_actor_id)", SEARCH_QA_SQL)
        self.assertIn(
            "request_acknowledged BOOLEAN NOT NULL CHECK (request_acknowledged)",
            SEARCH_QA_SQL,
        )
        self.assertIn("paper_qa_jobs_identity_immutable", SEARCH_QA_SQL)
        self.assertIn("CHECK (model_eligible)", SEARCH_QA_SQL)

    def test_foreign_keys_bind_jobs_and_heads_to_exact_generation_provenance(self):
        exact = """generation_id, paper_id, source_sha256,
        article_fingerprint, selector_version"""
        self.assertGreaterEqual(SEARCH_QA_SQL.count(exact), 2)
        self.assertIn("UNIQUE (job_id, attempt_number)", SEARCH_QA_SQL)
        self.assertIn("lease_expires_at >", inspect.getsource(complete_qa_job))


class SearchCorpusTests(unittest.TestCase):
    def test_current_sources_have_stable_ids_and_annotation_consent_is_explicit(self):
        article = article_fixture()
        corpus = prepare_search_corpus(
            paper(),
            article,
            reviewed_knowledge=reviewed_payload(article),
            annotations=[annotation(article)],
        )
        kinds = [item.document.source_kind for item in corpus.documents]
        self.assertEqual(kinds.count(SourceKind.SOURCE_TEXT), 2)
        self.assertEqual(kinds.count(SourceKind.REVIEWED_KNOWLEDGE), 1)
        self.assertEqual(kinds.count(SourceKind.USER_ANNOTATION), 1)
        personal = next(
            item
            for item in corpus.documents
            if item.document.source_kind == SourceKind.USER_ANNOTATION
        )
        self.assertFalse(personal.model_eligible)
        self.assertEqual(personal.annotation_id, ANNOTATION_ID)
        reviewed = next(
            item
            for item in corpus.documents
            if item.document.source_kind == SourceKind.REVIEWED_KNOWLEDGE
        )
        self.assertEqual(reviewed.reviewed_revision_id, REVISION_ID)
        self.assertTrue(reviewed.model_eligible)
        self.assertRegex(corpus.generation_id, r"^[0-9a-f-]{36}$")
        repeated = prepare_search_corpus(
            paper(),
            article,
            reviewed_knowledge=reviewed_payload(article),
            annotations=[annotation(article)],
        )
        self.assertEqual(corpus, repeated)

    def test_ai_draft_and_stale_or_trashed_annotation_fail_closed(self):
        article = article_fixture()
        with self.assertRaisesRegex(SearchStoreError, "reviewed_knowledge_required"):
            prepare_search_corpus(
                paper(), article, reviewed_knowledge=reviewed_payload(article, status="ai-draft")
            )
        stale = annotation(article)
        stale["article_fingerprint"] = "b" * 64
        with self.assertRaisesRegex(SearchStoreError, "annotation_source_mismatch"):
            prepare_search_corpus(paper(), article, annotations=[stale])
        with self.assertRaisesRegex(SearchStoreError, "annotation_source_mismatch"):
            prepare_search_corpus(
                paper(), article, annotations=[annotation(article, trashed_at="now")]
            )

    def test_database_candidates_are_reranked_with_hard_authority(self):
        article = article_fixture()
        corpus = prepare_search_corpus(
            paper(),
            article,
            reviewed_knowledge=reviewed_payload(article),
            annotations=[annotation(article, share=True)],
        )
        matching = [chunk for chunk in corpus.chunks if "signal" in chunk.lexical_text]
        irrelevant = next(chunk for chunk in corpus.chunks if "method" in chunk.lexical_text)
        database = SequenceDatabase(
            [Result(many=[chunk_row(irrelevant), *map(chunk_row, reversed(matching))])]
        )
        with patch(
            "paper_service.search_store.connection",
            return_value=database_context(database),
        ):
            hits = search_papers("signal", paper_id=7, limit=10)
        self.assertEqual(
            [hit.chunk.source_kind for hit in hits],
            [
                SourceKind.USER_ANNOTATION,
                SourceKind.REVIEWED_KNOWLEDGE,
                SourceKind.SOURCE_TEXT,
            ],
        )
        self.assertNotIn(irrelevant.chunk_id, [hit.chunk.chunk_id for hit in hits])
        self.assertEqual({hit.paper_id for hit in hits}, {7})
        sql = database.calls[0][0]
        self.assertIn("search_vector @@ plainto_tsquery", sql)
        self.assertIn("ts_rank_cd", sql)

    def test_query_and_limit_are_rejected_before_opening_database(self):
        for query, limit in (("", 10), ("x" * 1001, 10), ("signal", 0), ("signal", True)):
            with self.subTest(query=query[:10], limit=limit), patch(
                "paper_service.search_store.connection"
            ) as connect:
                with self.assertRaises(SearchStoreError):
                    search_papers(query, limit=limit)
                connect.assert_not_called()


def qa_job(*, include_annotations: bool, nodes: list[dict]) -> dict:
    snapshot = validate_qa_request(
        {
            "schema_version": 1,
            "selector_version": SELECTOR_VERSION,
            "question": "What changed?",
            "source_nodes": nodes,
        }
    )
    return {
        "id": JOB_ID,
        "paper_id": 7,
        "question": snapshot["question"],
        "request_actor_kind": "human",
        "request_actor_id": "access-user-123",
        "request_acknowledged": True,
        "include_personal_annotations": include_annotations,
        "source_sha256": PAPER_SHA,
        "article_fingerprint": "b" * 64,
        "selector_version": SELECTOR_VERSION,
        "evidence_snapshot": snapshot,
        "evidence_snapshot_sha256": _digest(snapshot),
    }


def qa_node(kind: str, digit: str) -> dict:
    return {
        "selector_version": SELECTOR_VERSION,
        "source_kind": kind,
        "source_id": "c-" + digit * 32,
        "node_id": "n-" + digit * 24,
        "text": f"Evidence text {digit}.",
    }


def evidence_row(node: dict, ordinal: int, **overrides) -> dict:
    value = {
        "ordinal": ordinal,
        "chunk_id": node["source_id"],
        "source_kind": node["source_kind"],
        "node_id": node["node_id"],
        "content": node["text"],
        "content_sha256": hashlib.sha256(node["text"].encode()).hexdigest(),
        "model_eligible": True,
        "reviewed_status": None,
        "reviewed_approved": False,
        "reviewed_is_current": False,
        "reviewed_source_sha256": None,
        "reviewed_article_fingerprint": None,
        "reviewed_selector_version": None,
        "annotation_active": False,
        "annotation_ai_share_allowed": False,
        "annotation_article_fingerprint": None,
        "annotation_node_id": None,
        "annotation_anchor": {
            "selector_version": SELECTOR_VERSION,
            "node_id": node["node_id"],
            "exact_quote": node["text"],
        },
        "annotation_body": None,
        "start_offset": 0,
        "end_offset": len(node["text"]),
    }
    value.update(overrides)
    return value


class QAEvidenceBoundaryTests(unittest.TestCase):
    def test_default_policy_rejects_personal_annotations(self):
        node = qa_node("user_annotation", "1")
        job = qa_job(include_annotations=False, nodes=[node])
        row = evidence_row(
            node,
            0,
            annotation_active=True,
            annotation_ai_share_allowed=True,
            annotation_article_fingerprint="b" * 64,
            annotation_node_id=node["node_id"],
        )
        with self.assertRaisesRegex(SearchStoreError, "qa_annotation_not_opted_in"):
            validate_qa_evidence_snapshot(
                job,
                [row],
                current_source_sha256=PAPER_SHA,
                current_article_fingerprint="b" * 64,
                current_selector_version=SELECTOR_VERSION,
            )

    def test_opt_in_still_requires_live_per_annotation_consent(self):
        node = qa_node("user_annotation", "1")
        job = qa_job(include_annotations=True, nodes=[node])
        row = evidence_row(
            node,
            0,
            annotation_active=True,
            annotation_ai_share_allowed=False,
            annotation_article_fingerprint="b" * 64,
            annotation_node_id=node["node_id"],
        )
        with self.assertRaisesRegex(SearchStoreError, "qa_annotation_not_shareable"):
            validate_qa_evidence_snapshot(
                job,
                [row],
                current_source_sha256=PAPER_SHA,
                current_article_fingerprint="b" * 64,
                current_selector_version=SELECTOR_VERSION,
            )

    def test_reviewed_source_requires_approval_and_current_provenance(self):
        node = qa_node("reviewed_knowledge", "2")
        job = qa_job(include_annotations=False, nodes=[node])
        row = evidence_row(
            node,
            0,
            reviewed_status="reviewed",
            reviewed_approved=False,
            reviewed_is_current=True,
            reviewed_source_sha256=PAPER_SHA,
            reviewed_article_fingerprint="b" * 64,
            reviewed_selector_version=SELECTOR_VERSION,
        )
        with self.assertRaisesRegex(SearchStoreError, "qa_reviewed_knowledge_stale"):
            validate_qa_evidence_snapshot(
                job,
                [row],
                current_source_sha256=PAPER_SHA,
                current_article_fingerprint="b" * 64,
                current_selector_version=SELECTOR_VERSION,
            )
        row["reviewed_approved"] = True
        canonical = validate_qa_evidence_snapshot(
            job,
            [row],
            current_source_sha256=PAPER_SHA,
            current_article_fingerprint="b" * 64,
            current_selector_version=SELECTOR_VERSION,
        )
        self.assertEqual(canonical["source_nodes"], [node])

    def test_ai_draft_cannot_form_a_snapshot(self):
        with self.assertRaisesRegex(Exception, "ai_draft_forbidden"):
            qa_job(
                include_annotations=False,
                nodes=[qa_node("ai_draft_knowledge", "4")],
            )

    def test_stale_fingerprint_and_tampered_text_fail_without_disclosure(self):
        node = qa_node("source_text", "3")
        job = qa_job(include_annotations=False, nodes=[node])
        with self.assertRaisesRegex(SearchStoreError, "qa_evidence_stale"):
            validate_qa_evidence_snapshot(
                job,
                [evidence_row(node, 0)],
                current_source_sha256=PAPER_SHA,
                current_article_fingerprint="c" * 64,
                current_selector_version=SELECTOR_VERSION,
            )
        tampered = evidence_row(node, 0, content="PRIVATE SOURCE BODY")
        with self.assertRaises(SearchStoreError) as raised:
            validate_qa_evidence_snapshot(
                job,
                [tampered],
                current_source_sha256=PAPER_SHA,
                current_article_fingerprint="b" * 64,
                current_selector_version=SELECTOR_VERSION,
            )
        self.assertEqual(str(raised.exception), "qa_evidence_row_mismatch")
        self.assertNotIn("PRIVATE", str(raised.exception))


class QAQueueTests(unittest.TestCase):
    def test_job_identity_is_stable_and_non_reversible(self):
        first = qa_job_identity(7, "phone-request-123")
        second = qa_job_identity(7, "phone-request-123")
        self.assertEqual(first, second)
        self.assertNotIn("phone-request", first[1])
        with self.assertRaisesRegex(SearchStoreError, "idempotency_key_invalid"):
            qa_job_identity(7, "bad\nkey")

    def test_request_digest_binds_the_human_actor_and_acknowledgement(self):
        values = {
            "paper_id": 7,
            "provider": "deepseek",
            "model": "deepseek-v4-flash",
            "request_actor_kind": "human",
            "request_acknowledged": True,
            "include_personal_annotations": False,
            "evidence_limit": 32,
            "evidence_snapshot_sha256": "a" * 64,
        }
        first = _qa_request_digest(
            **values, request_actor_id="access-user-123"
        )
        second = _qa_request_digest(
            **values, request_actor_id="different-access-user"
        )
        self.assertNotEqual(first, second)

    def test_enqueue_rejects_nonhuman_or_missing_ack_before_database_access(self):
        for actor_kind, acknowledged, code in (
            ("service", True, "human_actor_required"),
            ("human", False, "acknowledgement_required"),
        ):
            with (
                self.subTest(actor_kind=actor_kind, acknowledged=acknowledged),
                patch("paper_service.search_store.connection") as connect,
                self.assertRaisesRegex(SearchStoreError, code),
            ):
                enqueue_qa_job(
                    7,
                    "What changed?",
                    idempotency_key="qa-request-authorization",
                    request_actor_kind=actor_kind,
                    request_actor_id="access-user-123",
                    request_acknowledged=acknowledged,
                )
            connect.assert_not_called()

    def test_idempotency_key_reuse_by_a_different_actor_fails_closed(self):
        snapshot = validate_qa_request(
            {
                "schema_version": 1,
                "selector_version": SELECTOR_VERSION,
                "question": "What changed?",
                "source_nodes": [],
            }
        )
        snapshot_digest = _digest(snapshot)
        existing_digest = _qa_request_digest(
            paper_id=7,
            provider="deepseek",
            model="deepseek-v4-flash",
            request_actor_kind="human",
            request_actor_id="access-user-123",
            request_acknowledged=True,
            include_personal_annotations=False,
            evidence_limit=32,
            evidence_snapshot_sha256=snapshot_digest,
        )
        database = SequenceDatabase(
            [
                Result(),
                Result(
                    one={
                        "generation_id": "92fd1154-b003-44d4-be9f-5469f0c7e115",
                        "source_sha256": PAPER_SHA,
                        "article_fingerprint": "b" * 64,
                        "selector_version": SELECTOR_VERSION,
                    }
                ),
                Result(one=None),
                Result(one={"request_digest": existing_digest}),
            ]
        )
        with (
            patch(
                "paper_service.search_store.connection",
                return_value=database_context(database),
            ),
            patch("paper_service.search_store._search_database", return_value=()),
            self.assertRaisesRegex(SearchStoreError, "qa_idempotency_conflict"),
        ):
            enqueue_qa_job(
                7,
                "What changed?",
                idempotency_key="shared-key",
                request_actor_kind="human",
                request_actor_id="different-access-user",
                request_acknowledged=True,
            )

    def test_usage_contract_is_strict(self):
        usage = QAUsage("deepseek", "deepseek-v4-flash", 10, 5, 15)
        self.assertEqual(usage.total_tokens, 15)
        with self.assertRaisesRegex(SearchStoreError, "usage_total_tokens_mismatch"):
            QAUsage("deepseek", "deepseek-v4-flash", 10, 5, 14)
        with self.assertRaisesRegex(SearchStoreError, "usage_unavailable_cost_has_value"):
            QAUsage(
                "deepseek",
                "deepseek-v4-flash",
                10,
                5,
                15,
                cost_microusd=1,
            )

    def test_invalid_answer_is_rejected_before_usage_or_job_write(self):
        node = qa_node("source_text", "3")
        stored = self._stored_running_job(node)
        database = SequenceDatabase([Result(one=stored)])
        invalid = {
            "schema_version": 1,
            "selector_version": SELECTOR_VERSION,
            "status": "answered",
            "answer": "Unsupported answer.",
            "claims": [],
        }
        usage = QAUsage("deepseek", "deepseek-v4-flash", 10, 5, 15)
        with patch(
            "paper_service.search_store.connection",
            return_value=database_context(database),
        ):
            with self.assertRaisesRegex(SearchStoreError, "qa_response_invalid"):
                complete_qa_job(
                    JOB_ID,
                    LEASE_TOKEN,
                    response=invalid,
                    usage=usage,
                )
        self.assertEqual(len(database.calls), 1)

    @staticmethod
    def _stored_running_job(node: dict, **overrides) -> dict:
        snapshot_job = qa_job(include_annotations=False, nodes=[node])
        value = {
            **snapshot_job,
            "id": JOB_ID,
            "idempotency_key_digest": "d" * 64,
            "request_digest": None,
            "provider": "deepseek",
            "model": "deepseek-v4-flash",
            "evidence_limit": 32,
            "generation_id": "92fd1154-b003-44d4-be9f-5469f0c7e115",
            "status": "running",
            "attempt_count": 1,
            "max_attempts": 3,
            "next_attempt_at": None,
            "lease_owner": "worker",
            "lease_token": LEASE_TOKEN,
            "lease_expires_at": None,
            "lease_is_current": True,
            "answer_payload": None,
            "response_digest": None,
            "last_error_code": None,
            "requested_at": None,
            "started_at": None,
            "finished_at": None,
        }
        value.update(overrides)
        if value["request_digest"] is None:
            value["request_digest"] = _qa_request_digest(
                paper_id=value["paper_id"],
                provider=value["provider"],
                model=value["model"],
                request_actor_kind=value["request_actor_kind"],
                request_actor_id=value["request_actor_id"],
                request_acknowledged=value["request_acknowledged"],
                include_personal_annotations=value[
                    "include_personal_annotations"
                ],
                evidence_limit=value["evidence_limit"],
                evidence_snapshot_sha256=value["evidence_snapshot_sha256"],
            )
        return value

    def test_claim_recovers_expired_jobs_and_uses_skip_locked(self):
        node = qa_node("source_text", "3")
        claimed = self._stored_running_job(node, attempt_count=1)
        database = SequenceDatabase([Result(), Result(one=claimed)])
        with patch(
            "paper_service.search_store.connection",
            return_value=database_context(database),
        ):
            result = claim_qa_job("qa-worker-1", lease_seconds=120)
        self.assertEqual(result, claimed)
        self.assertIn("lease_expires_at <= NOW()", database.calls[0][0])
        self.assertIn("FOR UPDATE SKIP LOCKED", database.calls[1][0])
        self.assertIn("attempt_count = j.attempt_count + 1", database.calls[1][0])
        self.assertIn("request_actor_kind = 'human'", database.calls[1][0])
        self.assertIn("request_acknowledged IS TRUE", database.calls[1][0])
        self.assertEqual(database.calls[1][1][0], "qa-worker-1")

    def test_claim_rejects_an_actor_not_bound_to_the_request_digest(self):
        node = qa_node("source_text", "3")
        claimed = self._stored_running_job(node)
        claimed["request_actor_id"] = "substituted-user"
        database = SequenceDatabase([Result(), Result(one=claimed)])
        with (
            patch(
                "paper_service.search_store.connection",
                return_value=database_context(database),
            ),
            self.assertRaisesRegex(SearchStoreError, "qa_request_digest_mismatch"),
        ):
            claim_qa_job("qa-worker-1")

    def test_terminal_nack_is_sanitized_and_bound_to_live_lease(self):
        node = qa_node("source_text", "3")
        running = self._stored_running_job(node, attempt_count=3, max_attempts=3)
        failed = dict(
            running,
            status="failed",
            lease_owner=None,
            lease_token=None,
            lease_is_current=False,
            last_error_code="provider_timeout",
        )
        database = SequenceDatabase([Result(one=running), Result(one=failed)])
        with patch(
            "paper_service.search_store.connection",
            return_value=database_context(database),
        ):
            result = nack_qa_job(
                JOB_ID,
                LEASE_TOKEN,
                error_code="provider_timeout",
                retry_delay_seconds=60,
            )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(database.calls[1][1][0], "failed")
        with patch("paper_service.search_store.connection") as connect:
            with self.assertRaisesRegex(SearchStoreError, "qa_error_code_invalid"):
                nack_qa_job(
                    JOB_ID,
                    LEASE_TOKEN,
                    error_code="private body leaked: timeout",
                )
            connect.assert_not_called()

    def test_valid_completion_appends_usage_before_canonical_answer(self):
        node = qa_node("source_text", "3")
        running = self._stored_running_job(node)
        response = {
            "schema_version": 1,
            "selector_version": SELECTOR_VERSION,
            "status": "answered",
            "answer": "The evidence says what changed.",
            "claims": [
                {
                    "text": "The evidence identifies the change.",
                    "citations": [
                        {
                            "source_kind": node["source_kind"],
                            "source_id": node["source_id"],
                            "node_id": node["node_id"],
                            "exact_quote": node["text"],
                        }
                    ],
                }
            ],
        }
        usage_row = {
            "id": "8e5e28ec-3c0d-5f93-88ed-2bb1a282a2da",
            "job_id": JOB_ID,
            "attempt_number": 1,
        }
        succeeded = dict(
            running,
            status="succeeded",
            lease_owner=None,
            lease_token=None,
            answer_payload=response,
        )
        database = SequenceDatabase(
            [Result(one=running), Result(one=usage_row), Result(one=succeeded)]
        )
        usage = QAUsage("deepseek", "deepseek-v4-flash", 10, 5, 15)
        with patch(
            "paper_service.search_store.connection",
            return_value=database_context(database),
        ):
            result = complete_qa_job(
                JOB_ID,
                LEASE_TOKEN,
                response=response,
                usage=usage,
            )
        self.assertEqual(result["status"], "succeeded")
        self.assertIn("INSERT INTO paper_qa_usage_ledger", database.calls[1][0])
        self.assertIn("SET status = 'succeeded'", database.calls[2][0])

    def test_lost_lease_never_records_usage(self):
        node = qa_node("source_text", "3")
        running = self._stored_running_job(node, lease_is_current=False)
        database = SequenceDatabase([Result(one=running)])
        usage = QAUsage("deepseek", "deepseek-v4-flash", 10, 5, 15)
        with patch(
            "paper_service.search_store.connection",
            return_value=database_context(database),
        ):
            with self.assertRaises(QALeaseLost):
                complete_qa_job(
                    JOB_ID,
                    LEASE_TOKEN,
                    response={},
                    usage=usage,
                )
        self.assertEqual(len(database.calls), 1)


if __name__ == "__main__":
    unittest.main()

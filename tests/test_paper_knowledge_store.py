import unittest
from contextlib import nullcontext
from unittest.mock import patch
from uuid import UUID, uuid5

from paper_service.knowledge import payload_sha256
from paper_service.knowledge_store import (
    KnowledgeLeaseLost,
    KnowledgeRevisionConflict,
    KnowledgeStoreError,
    _OUTBOX_NAMESPACE,
    _assert_generation_request_binding,
    _assert_outbox_eligible,
    _lock_review_head,
    _recover_expired_generation_jobs,
    _recover_expired_outbox,
    ack_outbox_item,
    approve_draft_revision,
    bind_generation_request,
    claim_generation_job,
    claim_outbox_item,
    complete_generation_job,
    enqueue_generation_job,
    generation_job_identity,
    generation_request_digest,
    nack_generation_job,
    outbox_dedupe_key,
    record_generation_usage,
)
from paper_service.knowledge_worker import (
    GenerationPolicy,
    GenerationUsage,
    build_generation_request,
)
from paper_service.migrations import MIGRATIONS
from paper_service.migrations.v005_paper_knowledge import SQL as PAPER_KNOWLEDGE_SQL
from paper_service.selectors import build_selector_index


DRAFT_ID = "cf5bb53f-abff-450a-9493-c192066f93d2"
REVIEWED_ID = "c2e0783a-b598-4b51-9808-5e4342bb30e0"
EVENT_ID = "1a94cd8b-c56e-4ee3-b229-e47495d3c494"
JOB_ID = "466f9661-1493-4bdc-8e15-b29e69bcf0ed"
LEASE_ID = "871140c4-8684-463b-8917-ab69a16e1d33"
REQUEST_ACTOR_ID = "access-user-123"


class Result:
    def __init__(self, *, one=None, rowcount=0):
        self.one = one
        self.rowcount = rowcount

    def fetchone(self):
        return self.one


class SequenceDatabase:
    def __init__(self, results):
        self.results = iter(results)
        self.calls = []

    def execute(self, sql, parameters=None):
        self.calls.append((sql, parameters))
        return next(self.results)


def article_fixture():
    return {
        "schema_version": 2,
        "reader_version": 2,
        "identity": {"title": "Reviewed evidence"},
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
                    }
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


def reviewed_payload(*, status="reviewed", revision_id=REVIEWED_ID):
    article = article_fixture()
    index = build_selector_index(article)
    node = index.nodes[0]
    return {
        "schema_version": 1,
        "selector_version": index.selector_version,
        "revision_id": revision_id,
        "status": status,
        "paper_identity": {"paper_id": 7, "title": "Reviewed evidence"},
        "source_sha256": "a" * 64,
        "article_fingerprint": index.article_fingerprint,
        "summary": "A reviewed and evidence-grounded synthesis.",
        "claims": [
            {
                "text": "The paper reports an increased signal.",
                "evidence": [
                    {
                        "node_id": node.node_id,
                        "exact_quote": "The signal increased by 12 percent.",
                    }
                ],
            }
        ],
        "key_methods": ["Bounded extraction"],
        "key_findings": ["Signal increased"],
        "limitations": ["Single paper"],
    }


def revision_row(payload=None):
    value = reviewed_payload() if payload is None else payload
    return {
        "id": value["revision_id"],
        "paper_id": 7,
        "revision_number": 2,
        "parent_revision_id": DRAFT_ID,
        "source_job_id": None,
        "status": value["status"],
        "canonical_payload": value,
        "canonical_payload_sha256": payload_sha256(value),
        "source_sha256": value["source_sha256"],
        "article_fingerprint": value["article_fingerprint"],
        "selector_version": value["selector_version"],
        "created_at": None,
    }


def paper_row():
    return {
        "paper_id": 7,
        "sha256": "a" * 64,
        "reader_v2_status": "ready",
        "reader_v2_manifest_path": "7/v2/article.json",
    }


def approval_event():
    return {
        "id": EVENT_ID,
        "paper_id": 7,
        "review_revision": 1,
        "expected_revision": 0,
        "subject_revision_id": DRAFT_ID,
        "result_revision_id": REVIEWED_ID,
        "decision": "approve",
        "actor_kind": "human",
        "actor_id": "reviewer@example.invalid",
        "comment": "Checked against the cited source node.",
        "created_at": None,
    }


def job_row(**overrides):
    value = {
        "id": JOB_ID,
        "paper_id": 7,
        "idempotency_key_digest": "c" * 64,
        "request_digest": None,
        "provider": "deepseek",
        "model": "deepseek-chat",
        "request_actor_kind": "human",
        "request_actor_id": REQUEST_ACTOR_ID,
        "paper_text_and_cost_acknowledged": True,
        "include_personal_annotations": False,
        "max_source_pages": 8,
        "max_source_characters": 30000,
        "max_output_tokens": 4096,
        "status": "running",
        "attempt_count": 1,
        "max_attempts": 3,
        "next_attempt_at": None,
        "lease_owner": "worker-1",
        "lease_token": LEASE_ID,
        "lease_expires_at": None,
        "generation_request_digest": None,
        "generation_revision_id": None,
        "generation_source_node_ids": None,
        "generation_annotation_ids": None,
        "result_revision_id": None,
        "last_error_code": None,
        "requested_at": None,
        "started_at": None,
        "finished_at": None,
    }
    value.update(overrides)
    if value["request_digest"] is None:
        value["request_digest"] = generation_request_digest(
            paper_id=value["paper_id"],
            provider=value["provider"],
            model=value["model"],
            policy=GenerationPolicy(
                max_source_pages=value["max_source_pages"],
                max_source_characters=value["max_source_characters"],
                max_output_tokens=value["max_output_tokens"],
                include_personal_annotations=value["include_personal_annotations"],
            ),
            request_actor_kind=value["request_actor_kind"],
            request_actor_id=value["request_actor_id"],
            paper_text_and_cost_acknowledged=value[
                "paper_text_and_cost_acknowledged"
            ],
        )
    return value


def outbox_row(**overrides):
    payload = reviewed_payload()
    value = {
        "id": "1ce1a3fa-78ed-4d9d-8091-ebd27136d5cc",
        "paper_id": 7,
        "revision_id": REVIEWED_ID,
        "revision_status": "reviewed",
        "revision_payload_sha256": payload_sha256(payload),
        "approval_event_id": EVENT_ID,
        "approval_decision": "approve",
        "dedupe_key": "e" * 64,
        "status": "leased",
        "attempt_count": 1,
        "max_attempts": 10,
        "available_at": None,
        "lease_owner": "vault-worker",
        "lease_token": LEASE_ID,
        "lease_expires_at": None,
        "last_error_code": None,
        "created_at": None,
        "delivered_at": None,
        "canonical_payload": payload,
    }
    value.update(overrides)
    return value


class KnowledgeMigrationTests(unittest.TestCase):
    def test_v005_is_registered_after_existing_migrations(self):
        self.assertEqual(
            [item.version for item in MIGRATIONS],
            [1, 2, 3, 4, 5, 6, 7, 8],
        )
        self.assertEqual(MIGRATIONS[4].name, "paper_knowledge")
        self.assertEqual(MIGRATIONS[4].sql, PAPER_KNOWLEDGE_SQL)

    def test_schema_has_jobs_retry_leases_and_deterministic_claim_indexes(self):
        for fragment in (
            "CREATE TABLE paper_knowledge_jobs",
            "idempotency_key_digest CHAR(64)",
            "status IN ('queued', 'running', 'retry_wait', 'succeeded', 'failed')",
            "lease_owner TEXT",
            "lease_token UUID",
            "lease_expires_at TIMESTAMPTZ",
            "attempt_count INTEGER",
            "max_attempts SMALLINT",
            "paper_knowledge_jobs_claim",
            "paper_knowledge_jobs_expired_lease",
            "request_actor_kind TEXT NOT NULL CHECK (request_actor_kind = 'human')",
            "request_actor_id = btrim(request_actor_id)",
            "paper_text_and_cost_acknowledged BOOLEAN NOT NULL",
            "paper_knowledge_jobs_identity_immutable",
        ):
            self.assertIn(fragment, PAPER_KNOWLEDGE_SQL)

    def test_revision_review_and_usage_history_are_append_only(self):
        for table in (
            "paper_knowledge_revisions",
            "paper_knowledge_review_events",
            "paper_knowledge_usage_ledger",
        ):
            self.assertIn(f"BEFORE UPDATE OR DELETE ON {table}", PAPER_KNOWLEDGE_SQL)
        self.assertIn("canonical_payload_sha256 CHAR(64)", PAPER_KNOWLEDGE_SQL)
        self.assertIn("UNIQUE (paper_id, revision_number)", PAPER_KNOWLEDGE_SQL)
        self.assertIn("UNIQUE (subject_revision_id)", PAPER_KNOWLEDGE_SQL)
        self.assertIn("review_revision = expected_revision + 1", PAPER_KNOWLEDGE_SQL)
        self.assertIn("actor_kind IN ('human', 'service')", PAPER_KNOWLEDGE_SQL)
        self.assertIn("actor_id TEXT NOT NULL", PAPER_KNOWLEDGE_SQL)
        self.assertIn("paper_knowledge_review_event_validate", PAPER_KNOWLEDGE_SQL)
        self.assertIn("subject_status IS DISTINCT FROM 'ai-draft'", PAPER_KNOWLEDGE_SQL)
        self.assertIn("result_status IS DISTINCT FROM 'reviewed'", PAPER_KNOWLEDGE_SQL)
        self.assertIn("NEW.actor_kind <> 'human'", PAPER_KNOWLEDGE_SQL)

    def test_database_itself_forbids_draft_or_unapproved_outbox_rows(self):
        self.assertIn("CHECK (revision_status = 'reviewed')", PAPER_KNOWLEDGE_SQL)
        self.assertIn("CHECK (approval_decision = 'approve')", PAPER_KNOWLEDGE_SQL)
        self.assertIn(
            "revision_id, paper_id, revision_payload_sha256, revision_status",
            PAPER_KNOWLEDGE_SQL,
        )
        self.assertIn(
            "approval_event_id, paper_id, revision_id, approval_decision",
            PAPER_KNOWLEDGE_SQL,
        )
        self.assertIn("dedupe_key CHAR(64) NOT NULL UNIQUE", PAPER_KNOWLEDGE_SQL)

    def test_usage_ledger_has_model_tokens_and_explicit_cost_provenance(self):
        for fragment in (
            "provider TEXT NOT NULL",
            "model TEXT NOT NULL",
            "prompt_tokens BIGINT NOT NULL",
            "completion_tokens BIGINT NOT NULL",
            "total_tokens BIGINT NOT NULL",
            "cost_microusd BIGINT",
            "'provider-reported'",
            "'reviewed-price-table'",
            "pricing_version TEXT",
        ):
            self.assertIn(fragment, PAPER_KNOWLEDGE_SQL)


class EligibilityTests(unittest.TestCase):
    def test_reviewed_current_grounded_approved_revision_is_eligible(self):
        canonical = _assert_outbox_eligible(
            revision=revision_row(),
            paper=paper_row(),
            approval_event=approval_event(),
            article=article_fixture(),
        )
        self.assertEqual(canonical, reviewed_payload())

    def test_ai_draft_can_never_reach_outbox(self):
        draft = reviewed_payload(status="ai-draft", revision_id=DRAFT_ID)
        event = approval_event()
        event["result_revision_id"] = DRAFT_ID
        with self.assertRaisesRegex(KnowledgeStoreError, "reviewed_required"):
            _assert_outbox_eligible(
                revision=revision_row(draft),
                paper=paper_row(),
                approval_event=event,
                article=article_fixture(),
            )

    def test_every_freshness_and_approval_boundary_fails_closed(self):
        cases = []
        stale_sha = paper_row()
        stale_sha["sha256"] = "f" * 64
        cases.append((revision_row(), stale_sha, approval_event(), article_fixture()))
        stale_article = article_fixture()
        stale_article["sections"][0]["content"][0]["text"] += " changed"
        cases.append((revision_row(), paper_row(), approval_event(), stale_article))
        missing_reader = paper_row()
        missing_reader["reader_v2_status"] = "pending"
        cases.append((revision_row(), missing_reader, approval_event(), article_fixture()))
        wrong_manifest = paper_row()
        wrong_manifest["reader_v2_manifest_path"] = "7/v1/manifest.json"
        cases.append((revision_row(), wrong_manifest, approval_event(), article_fixture()))
        rejected = approval_event()
        rejected["decision"] = "reject"
        cases.append((revision_row(), paper_row(), rejected, article_fixture()))
        wrong_revision = approval_event()
        wrong_revision["result_revision_id"] = DRAFT_ID
        cases.append((revision_row(), paper_row(), wrong_revision, article_fixture()))
        for revision, paper, event, article in cases:
            with self.subTest(paper=paper, event=event):
                with self.assertRaises(KnowledgeStoreError):
                    _assert_outbox_eligible(
                        revision=revision,
                        paper=paper,
                        approval_event=event,
                        article=article,
                    )

    def test_source_node_quote_is_revalidated_immediately_before_outbox(self):
        payload = reviewed_payload()
        payload["claims"][0]["evidence"][0]["exact_quote"] = "Invented evidence."
        revision = revision_row(payload)
        with self.assertRaisesRegex(KnowledgeStoreError, "quote_mismatch"):
            _assert_outbox_eligible(
                revision=revision,
                paper=paper_row(),
                approval_event=approval_event(),
                article=article_fixture(),
            )

    def test_stored_payload_or_digest_tampering_is_detected(self):
        revision = revision_row()
        revision["canonical_payload_sha256"] = "0" * 64
        with self.assertRaisesRegex(KnowledgeStoreError, "digest_mismatch"):
            _assert_outbox_eligible(
                revision=revision,
                paper=paper_row(),
                approval_event=approval_event(),
                article=article_fixture(),
            )

    def test_approval_creates_new_reviewed_revision_event_then_outbox(self):
        draft_payload = reviewed_payload(status="ai-draft", revision_id=DRAFT_ID)
        draft = revision_row(draft_payload)
        draft.update(
            sha256="a" * 64,
            title="Reviewed evidence",
            authors=None,
            publication_year=None,
            doi=None,
            reader_v2_status="ready",
            reader_v2_manifest_path="7/v2/article.json",
        )
        reviewed = revision_row()
        event = approval_event()
        digest = payload_sha256(reviewed_payload())
        dedupe = outbox_dedupe_key(REVIEWED_ID, digest)
        outbox = outbox_row(
            id=str(uuid5(_OUTBOX_NAMESPACE, dedupe)),
            dedupe_key=dedupe,
        )
        outbox.pop("canonical_payload")
        database = SequenceDatabase(
            [
                Result(one=draft),
                Result(),
                Result(one={"review_revision": 0}),
                Result(one={"next_revision": 3}),
                Result(one=reviewed),
                Result(one=event),
                Result(one={"review_revision": 1}),
                Result(one=outbox),
            ]
        )
        with (
            patch(
                "paper_service.knowledge_store.connection",
                return_value=nullcontext(database),
            ),
            patch(
                "paper_service.knowledge_store.read_article",
                return_value=article_fixture(),
            ),
            patch(
                "paper_service.knowledge_store.uuid4",
                return_value=UUID(EVENT_ID),
            ),
        ):
            result = approve_draft_revision(
                DRAFT_ID,
                reviewed_payload(),
                expected_review_revision=0,
                actor_kind="human",
                actor_id="reviewer@example.invalid",
                comment="Checked against the source.",
                data_dir="/not-read-in-this-test",
            )
        self.assertEqual(result["revision"], reviewed)
        self.assertEqual(result["review_event"], event)
        self.assertEqual(result["outbox"], outbox)
        self.assertIn("INSERT INTO paper_knowledge_review_events", database.calls[5][0])
        self.assertIn("UPDATE paper_knowledge_review_heads", database.calls[6][0])
        self.assertIn("INSERT INTO paper_knowledge_outbox", database.calls[7][0])

    def test_service_actor_cannot_approve_or_create_outbox(self):
        with (
            patch("paper_service.knowledge_store.connection") as connect,
            self.assertRaisesRegex(KnowledgeStoreError, "human_review_actor_required"),
        ):
            approve_draft_revision(
                DRAFT_ID,
                reviewed_payload(),
                expected_review_revision=0,
                actor_kind="service",
                actor_id="automation",
                comment=None,
                data_dir="/not-read",
            )
        connect.assert_not_called()

        event = approval_event()
        event["actor_kind"] = "service"
        with self.assertRaisesRegex(KnowledgeStoreError, "human_approval"):
            _assert_outbox_eligible(
                revision=revision_row(),
                paper=paper_row(),
                approval_event=event,
                article=article_fixture(),
            )


class IdempotencyAndLeaseTests(unittest.TestCase):
    def test_job_and_outbox_identities_are_deterministic_and_content_scoped(self):
        first = generation_job_identity(7, "mobile-review:2026-08-29")
        second = generation_job_identity(7, "mobile-review:2026-08-29")
        self.assertEqual(first, second)
        self.assertNotEqual(first, generation_job_identity(8, "mobile-review:2026-08-29"))
        self.assertNotIn("mobile-review", first[1])
        digest = payload_sha256(reviewed_payload())
        self.assertEqual(
            outbox_dedupe_key(REVIEWED_ID, digest),
            outbox_dedupe_key(REVIEWED_ID, digest),
        )
        self.assertNotEqual(
            outbox_dedupe_key(REVIEWED_ID, digest),
            outbox_dedupe_key(DRAFT_ID, digest),
        )

    def test_request_digest_covers_annotation_opt_in_and_bounds(self):
        base = generation_request_digest(
            paper_id=7,
            provider="deepseek",
            model="deepseek-chat",
            policy=GenerationPolicy(),
            request_actor_kind="human",
            request_actor_id=REQUEST_ACTOR_ID,
            paper_text_and_cost_acknowledged=True,
        )
        opted_in = generation_request_digest(
            paper_id=7,
            provider="deepseek",
            model="deepseek-chat",
            policy=GenerationPolicy(include_personal_annotations=True),
            request_actor_kind="human",
            request_actor_id=REQUEST_ACTOR_ID,
            paper_text_and_cost_acknowledged=True,
        )
        tightened = generation_request_digest(
            paper_id=7,
            provider="deepseek",
            model="deepseek-chat",
            policy=GenerationPolicy(max_source_pages=4),
            request_actor_kind="human",
            request_actor_id=REQUEST_ACTOR_ID,
            paper_text_and_cost_acknowledged=True,
        )
        different_actor = generation_request_digest(
            paper_id=7,
            provider="deepseek",
            model="deepseek-chat",
            policy=GenerationPolicy(),
            request_actor_kind="human",
            request_actor_id="different-access-user",
            paper_text_and_cost_acknowledged=True,
        )
        self.assertEqual(len({base, opted_in, tightened, different_actor}), 4)

    def test_enqueue_rejects_nonhuman_or_missing_ack_before_database_access(self):
        for actor_kind, acknowledged, code in (
            ("service", True, "human_actor_required"),
            ("human", False, "acknowledgement_required"),
        ):
            with (
                self.subTest(actor_kind=actor_kind, acknowledged=acknowledged),
                patch("paper_service.knowledge_store.connection") as connect,
                self.assertRaisesRegex(KnowledgeStoreError, code),
            ):
                enqueue_generation_job(
                    7,
                    idempotency_key="request-authorization",
                    provider="deepseek",
                    model="deepseek-chat",
                    request_actor_kind=actor_kind,
                    request_actor_id=REQUEST_ACTOR_ID,
                    paper_text_and_cost_acknowledged=acknowledged,
                )
            connect.assert_not_called()

    def test_enqueue_replays_same_request_and_rejects_key_reuse_with_new_request(self):
        policy = GenerationPolicy()
        expected_id, key_digest = generation_job_identity(7, "request-1")
        digest = generation_request_digest(
            paper_id=7,
            provider="deepseek",
            model="deepseek-chat",
            policy=policy,
            request_actor_kind="human",
            request_actor_id=REQUEST_ACTOR_ID,
            paper_text_and_cost_acknowledged=True,
        )
        current = job_row(
            id=expected_id,
            idempotency_key_digest=key_digest,
            request_digest=digest,
            status="queued",
            lease_owner=None,
            lease_token=None,
        )
        database = SequenceDatabase([Result(one=None), Result(one=current)])
        with patch(
            "paper_service.knowledge_store.connection",
            return_value=nullcontext(database),
        ):
            replay = enqueue_generation_job(
                7,
                idempotency_key="request-1",
                provider="deepseek",
                model="deepseek-chat",
                request_actor_kind="human",
                request_actor_id=REQUEST_ACTOR_ID,
                paper_text_and_cost_acknowledged=True,
            )

        database = SequenceDatabase([Result(one=None), Result(one=current)])
        with (
            patch(
                "paper_service.knowledge_store.connection",
                return_value=nullcontext(database),
            ),
            self.assertRaisesRegex(KnowledgeStoreError, "idempotency_conflict"),
        ):
            enqueue_generation_job(
                7,
                idempotency_key="request-1",
                provider="deepseek",
                model="deepseek-chat",
                request_actor_kind="human",
                request_actor_id="different-access-user",
                paper_text_and_cost_acknowledged=True,
            )
        self.assertEqual(replay["id"], expected_id)
        self.assertIn("ON CONFLICT", database.calls[0][0])

        conflict = dict(current, request_digest="0" * 64)
        database = SequenceDatabase([Result(one=None), Result(one=conflict)])
        with (
            patch(
                "paper_service.knowledge_store.connection",
                return_value=nullcontext(database),
            ),
            self.assertRaisesRegex(KnowledgeStoreError, "idempotency_conflict"),
        ):
            enqueue_generation_job(
                7,
                idempotency_key="request-1",
                provider="deepseek",
                model="deepseek-chat",
                request_actor_kind="human",
                request_actor_id=REQUEST_ACTOR_ID,
                paper_text_and_cost_acknowledged=True,
            )

    def test_generation_claim_recovers_expired_and_uses_skip_locked_lease(self):
        claimed = job_row()
        database = SequenceDatabase([Result(rowcount=2), Result(one=claimed)])
        with patch(
            "paper_service.knowledge_store.connection",
            return_value=nullcontext(database),
        ):
            result = claim_generation_job(worker_id="worker-1", lease_seconds=120)
        self.assertEqual(result, claimed)
        self.assertIn("lease_expires_at <= NOW()", database.calls[0][0])
        self.assertIn("FOR UPDATE SKIP LOCKED", database.calls[1][0])
        self.assertIn("make_interval", database.calls[1][0])
        self.assertIn("request_actor_kind = 'human'", database.calls[1][0])
        self.assertIn(
            "paper_text_and_cost_acknowledged IS TRUE", database.calls[1][0]
        )

    def test_generation_claim_rolls_back_a_consent_digest_mismatch(self):
        claimed = job_row(request_actor_id="substituted-user")
        claimed["request_digest"] = generation_request_digest(
            paper_id=7,
            provider="deepseek",
            model="deepseek-chat",
            policy=GenerationPolicy(),
            request_actor_kind="human",
            request_actor_id=REQUEST_ACTOR_ID,
            paper_text_and_cost_acknowledged=True,
        )
        database = SequenceDatabase([Result(rowcount=0), Result(one=claimed)])
        with (
            patch(
                "paper_service.knowledge_store.connection",
                return_value=nullcontext(database),
            ),
            self.assertRaisesRegex(
                KnowledgeStoreError, "generation_job_request_digest_mismatch"
            ),
        ):
            claim_generation_job(worker_id="worker-1")

    def test_generation_attempt_binds_exact_request_before_completion(self):
        paper = {
            "id": 7,
            "sha256": "a" * 64,
            "title": "Reviewed evidence",
            "authors": None,
            "publication_year": None,
            "doi": None,
        }
        request = build_generation_request(
            paper,
            article_fixture(),
            provider="deepseek",
            model="deepseek-chat",
            revision_id=DRAFT_ID,
        )
        leased = job_row(
            lease_valid=True,
            request_digest=generation_request_digest(
                paper_id=7,
                provider="deepseek",
                model="deepseek-chat",
                policy=GenerationPolicy(),
                request_actor_kind="human",
                request_actor_id=REQUEST_ACTOR_ID,
                paper_text_and_cost_acknowledged=True,
            ),
            sha256="a" * 64,
            title="Reviewed evidence",
            authors=None,
            publication_year=None,
            doi=None,
            reader_v2_status="ready",
            reader_v2_manifest_path="7/v2/article.json",
        )
        updated = dict(
            leased,
            generation_request_digest=request.request_digest,
            generation_revision_id=DRAFT_ID,
            generation_source_node_ids=list(request.source_nodes),
            generation_annotation_ids=[],
        )
        database = SequenceDatabase([Result(one=leased), Result(one=updated)])
        with (
            patch(
                "paper_service.knowledge_store.connection",
                return_value=nullcontext(database),
            ),
            patch(
                "paper_service.knowledge_store.read_article",
                return_value=article_fixture(),
            ),
        ):
            result = bind_generation_request(
                JOB_ID,
                worker_id="worker-1",
                lease_token=LEASE_ID,
                request=request,
                data_dir="/not-read",
            )
        self.assertEqual(result, updated)
        self.assertIn("generation_source_node_ids", database.calls[1][0])

    def test_unbound_generation_job_cannot_store_model_output(self):
        leased = job_row(
            lease_valid=True,
            sha256="a" * 64,
            title="Reviewed evidence",
            authors=None,
            publication_year=None,
            doi=None,
            reader_v2_status="ready",
            reader_v2_manifest_path="7/v2/article.json",
        )
        database = SequenceDatabase([Result(one=leased)])
        usage = GenerationUsage(
            provider="deepseek",
            model="deepseek-chat",
            prompt_tokens=1,
            completion_tokens=1,
            total_tokens=2,
        )
        generation_request = build_generation_request(
            {
                "id": 7,
                "sha256": "a" * 64,
                "title": "Reviewed evidence",
                "authors": None,
                "publication_year": None,
                "doi": None,
            },
            article_fixture(),
            provider="deepseek",
            model="deepseek-chat",
            revision_id=DRAFT_ID,
        )
        with (
            patch(
                "paper_service.knowledge_store.connection",
                return_value=nullcontext(database),
            ),
            self.assertRaisesRegex(KnowledgeStoreError, "request_not_bound"),
        ):
            complete_generation_job(
                JOB_ID,
                worker_id="worker-1",
                lease_token=LEASE_ID,
                payload=reviewed_payload(status="ai-draft", revision_id=DRAFT_ID),
                data_dir="/not-read",
                usage=usage,
                generation_request=generation_request,
                generation_response_digest="2" * 64,
            )

    def test_bound_attempt_rejects_unsent_evidence_and_opted_out_annotations(self):
        payload = reviewed_payload(status="ai-draft", revision_id=DRAFT_ID)
        node_id = payload["claims"][0]["evidence"][0]["node_id"]
        bound = job_row(
            generation_revision_id=DRAFT_ID,
            generation_source_node_ids=[],
            generation_annotation_ids=[],
        )
        with self.assertRaisesRegex(KnowledgeStoreError, "evidence_not_in_request"):
            _assert_generation_request_binding(payload, bound)

        bound["generation_source_node_ids"] = [node_id]
        payload["user_annotation_ids"] = [
            "ef475659-5da4-4935-9b05-5dfad749d398"
        ]
        with self.assertRaisesRegex(KnowledgeStoreError, "annotation_not_in_request"):
            _assert_generation_request_binding(payload, bound)

    def test_nack_requires_current_nonexpired_lease(self):
        database = SequenceDatabase([Result(one=None)])
        with (
            patch(
                "paper_service.knowledge_store.connection",
                return_value=nullcontext(database),
            ),
            self.assertRaisesRegex(KnowledgeLeaseLost, "lease_lost"),
        ):
            nack_generation_job(
                JOB_ID,
                worker_id="worker-1",
                lease_token=LEASE_ID,
                error_code="provider_timeout",
            )
        sql = database.calls[0][0]
        self.assertIn("lease_expires_at > NOW()", sql)
        self.assertIn("attempt_count >= max_attempts", sql)

    def test_usage_can_be_recorded_for_a_malformed_leased_response_idempotently(self):
        leased = job_row(
            lease_valid=True,
            generation_request_digest="1" * 64,
            generation_revision_id=REVIEWED_ID,
            generation_source_node_ids=[],
            generation_annotation_ids=[],
        )
        usage = GenerationUsage(
            provider="deepseek",
            model="deepseek-chat",
            prompt_tokens=100,
            completion_tokens=20,
            total_tokens=120,
        )
        usage_id = "91f48a42-7166-5d16-a956-7a9c43a0142b"
        existing = {
            "id": usage_id,
            "job_id": JOB_ID,
            "attempt_number": 1,
            "provider": "deepseek",
            "model": "deepseek-chat",
            "request_digest": "1" * 64,
            "response_digest": "2" * 64,
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
            "cost_microusd": None,
            "cost_basis": "unavailable",
            "pricing_version": None,
            "recorded_at": None,
        }
        database = SequenceDatabase(
            [Result(one=leased), Result(one=None), Result(one=existing)]
        )
        with patch(
            "paper_service.knowledge_store.connection",
            return_value=nullcontext(database),
        ):
            recorded = record_generation_usage(
                JOB_ID,
                worker_id="worker-1",
                lease_token=LEASE_ID,
                usage=usage,
                generation_request_digest="1" * 64,
                generation_response_digest="2" * 64,
            )
        self.assertEqual(recorded, existing)
        self.assertIn("ON CONFLICT (job_id, attempt_number)", database.calls[1][0])
        self.assertIn("FOR UPDATE", database.calls[0][0])

    def test_outbox_claim_returns_only_canonical_reviewed_payload(self):
        item = outbox_row()
        database = SequenceDatabase([Result(rowcount=0), Result(one=item)])
        with patch(
            "paper_service.knowledge_store.connection",
            return_value=nullcontext(database),
        ):
            result = claim_outbox_item(worker_id="vault-worker")
        self.assertEqual(result["canonical_payload"], reviewed_payload())
        self.assertIn("FOR UPDATE SKIP LOCKED", database.calls[1][0])
        self.assertIn("JOIN paper_knowledge_revisions", database.calls[1][0])

    def test_outbox_ack_is_bound_to_the_payload_digest_seen_by_the_consumer(self):
        item = outbox_row(status="delivered")
        database = SequenceDatabase([Result(one=item)])
        with patch(
            "paper_service.knowledge_store.connection",
            return_value=nullcontext(database),
        ):
            result = ack_outbox_item(
                item["id"],
                worker_id="vault-worker",
                lease_token=LEASE_ID,
                expected_payload_sha256=item["revision_payload_sha256"],
            )
        self.assertEqual(result["status"], "delivered")
        sql, parameters = database.calls[0]
        self.assertIn("revision_payload_sha256 = %s", sql)
        self.assertEqual(parameters[-1], item["revision_payload_sha256"])

    def test_outbox_claim_dead_letters_poison_and_advances_to_next_item(self):
        poison = outbox_row(revision_payload_sha256="0" * 64)
        valid = outbox_row(id="3710e622-4a1d-4fd7-837e-42a9cc71cb71")
        database = SequenceDatabase(
            [
                Result(rowcount=0),
                Result(one=poison),
                Result(rowcount=1),
                Result(one=valid),
            ]
        )
        with patch(
            "paper_service.knowledge_store.connection",
            return_value=nullcontext(database),
        ):
            result = claim_outbox_item(worker_id="vault-worker")
        self.assertEqual(result["id"], valid["id"])
        self.assertIn("SET status = 'dead'", database.calls[2][0])
        self.assertEqual(database.calls[2][1][0], "payload_digest_mismatch")

    def test_expired_recovery_is_terminal_at_attempt_limit(self):
        for recovery in (_recover_expired_generation_jobs, _recover_expired_outbox):
            database = SequenceDatabase([Result(rowcount=3)])
            self.assertEqual(recovery(database), 3)
            sql = database.calls[0][0]
            self.assertIn("attempt_count >= max_attempts", sql)
            self.assertIn("lease_expired", sql)

    def test_review_head_uses_optimistic_concurrency(self):
        database = SequenceDatabase(
            [Result(), Result(one={"review_revision": 4})]
        )
        with self.assertRaises(KnowledgeRevisionConflict) as raised:
            _lock_review_head(database, paper_id=7, expected_revision=3)
        self.assertEqual(raised.exception.current_revision, 4)
        self.assertIn("FOR UPDATE", database.calls[1][0])


if __name__ == "__main__":
    unittest.main()

"""Read-only projections and server-authored review copies for paper knowledge."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

from .database import connection
from .knowledge import (
    KnowledgeValidationError,
    payload_sha256,
    validate_knowledge_payload,
)


class KnowledgeQueryError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class KnowledgeQueryNotFound(KnowledgeQueryError):
    pass


def _paper_id(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise KnowledgeQueryError("paper_id_invalid")
    return value


def _uuid(value: Any, *, field: str) -> str:
    try:
        parsed = UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise KnowledgeQueryError(f"{field}_invalid") from exc
    if parsed.int == 0 or str(parsed) != str(value):
        raise KnowledgeQueryError(f"{field}_invalid")
    return str(parsed)


def _payload(row: dict[str, Any]) -> dict[str, Any]:
    value = row.get("canonical_payload")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, RecursionError) as exc:
            raise KnowledgeQueryError("stored_revision_payload_invalid") from exc
    try:
        canonical = validate_knowledge_payload(value)
    except KnowledgeValidationError as exc:
        raise KnowledgeQueryError("stored_revision_payload_invalid") from exc
    digest = str(row.get("canonical_payload_sha256") or "").strip()
    if payload_sha256(canonical) != digest:
        raise KnowledgeQueryError("stored_revision_digest_mismatch")
    if (
        canonical["revision_id"] != str(row.get("id"))
        or canonical["paper_identity"]["paper_id"] != row.get("paper_id")
        or canonical["status"] != row.get("status")
    ):
        raise KnowledgeQueryError("stored_revision_identity_mismatch")
    return canonical


def get_knowledge_revision(revision_id: str | UUID) -> dict[str, Any]:
    clean_id = _uuid(revision_id, field="revision_id")
    with connection() as database:
        row = database.execute(
            """SELECT id, paper_id, revision_number, parent_revision_id,
                      source_job_id, status, canonical_payload,
                      canonical_payload_sha256, source_sha256,
                      article_fingerprint, selector_version, created_at
               FROM paper_knowledge_revisions WHERE id = %s::uuid""",
            (clean_id,),
        ).fetchone()
    if row is None:
        raise KnowledgeQueryNotFound("knowledge_revision_not_found")
    result = dict(row)
    result["canonical_payload"] = _payload(result)
    return result


def make_reviewed_copy(draft_revision_id: str | UUID) -> dict[str, Any]:
    """Create an unpersisted, content-identical reviewed child payload."""

    draft = get_knowledge_revision(draft_revision_id)
    if draft["status"] != "ai-draft":
        raise KnowledgeQueryError("ai_draft_revision_required")
    canonical = json.loads(
        json.dumps(
            draft["canonical_payload"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    )
    canonical["revision_id"] = str(uuid4())
    canonical["status"] = "reviewed"
    try:
        return validate_knowledge_payload(canonical)
    except KnowledgeValidationError as exc:
        raise KnowledgeQueryError("reviewed_copy_invalid") from exc


def get_generation_job(job_id: str | UUID) -> dict[str, Any]:
    clean_id = _uuid(job_id, field="job_id")
    with connection() as database:
        row = database.execute(
            """SELECT id, paper_id, provider, model,
                      include_personal_annotations, max_source_pages,
                      max_source_characters, max_output_tokens, status,
                      attempt_count, max_attempts, next_attempt_at,
                      result_revision_id, last_error_code, requested_at,
                      started_at, finished_at
               FROM paper_knowledge_jobs WHERE id = %s::uuid""",
            (clean_id,),
        ).fetchone()
    if row is None:
        raise KnowledgeQueryNotFound("knowledge_job_not_found")
    return dict(row)


def knowledge_overview(paper_id: int) -> dict[str, Any]:
    clean_paper_id = _paper_id(paper_id)
    with connection() as database:
        paper_row = database.execute(
            """SELECT id, title, reader_v2_status
               FROM papers
               WHERE id = %s AND processing_status = 'organized'""",
            (clean_paper_id,),
        ).fetchone()
        if paper_row is None:
            raise KnowledgeQueryNotFound("paper_not_found")
        job_row = database.execute(
            """SELECT id, paper_id, provider, model,
                      include_personal_annotations, max_source_pages,
                      max_source_characters, max_output_tokens, status,
                      attempt_count, max_attempts, next_attempt_at,
                      result_revision_id, last_error_code, requested_at,
                      started_at, finished_at
               FROM paper_knowledge_jobs
               WHERE paper_id = %s
               ORDER BY requested_at DESC, id DESC LIMIT 1""",
            (clean_paper_id,),
        ).fetchone()
        revision_row = database.execute(
            """SELECT id, paper_id, revision_number, parent_revision_id,
                      source_job_id, status, canonical_payload,
                      canonical_payload_sha256, source_sha256,
                      article_fingerprint, selector_version, created_at
               FROM paper_knowledge_revisions
               WHERE paper_id = %s
               ORDER BY revision_number DESC LIMIT 1""",
            (clean_paper_id,),
        ).fetchone()
        head_row = database.execute(
            """SELECT review_revision, updated_at
               FROM paper_knowledge_review_heads WHERE paper_id = %s""",
            (clean_paper_id,),
        ).fetchone()
        review_row = database.execute(
            """SELECT id, paper_id, review_revision, expected_revision,
                      subject_revision_id, result_revision_id, decision,
                      actor_kind, actor_id, comment, created_at
               FROM paper_knowledge_review_events
               WHERE paper_id = %s
               ORDER BY review_revision DESC LIMIT 1""",
            (clean_paper_id,),
        ).fetchone()
        outbox_row = database.execute(
            """SELECT id, revision_id, status, attempt_count, max_attempts,
                      last_error_code, created_at, delivered_at
               FROM paper_knowledge_outbox
               WHERE paper_id = %s
               ORDER BY created_at DESC, id DESC LIMIT 1""",
            (clean_paper_id,),
        ).fetchone()
        usage_row = database.execute(
            """SELECT COUNT(*) AS attempts,
                      COALESCE(SUM(total_tokens), 0) AS total_tokens,
                      COALESCE(SUM(cost_microusd), 0) AS known_cost_microusd,
                      COUNT(*) FILTER (WHERE cost_microusd IS NULL)
                          AS unknown_cost_attempts
               FROM paper_knowledge_usage_ledger usage
               JOIN paper_knowledge_jobs jobs ON jobs.id = usage.job_id
               WHERE jobs.paper_id = %s""",
            (clean_paper_id,),
        ).fetchone()

    revision = dict(revision_row) if revision_row else None
    if revision is not None:
        revision["canonical_payload"] = _payload(revision)
    return {
        "paper": dict(paper_row),
        "latest_job": dict(job_row) if job_row else None,
        "latest_revision": revision,
        "review_revision": int(head_row["review_revision"]) if head_row else 0,
        "latest_review": dict(review_row) if review_row else None,
        "latest_outbox": dict(outbox_row) if outbox_row else None,
        "usage": dict(usage_row),
    }


__all__ = [
    "KnowledgeQueryError",
    "KnowledgeQueryNotFound",
    "get_generation_job",
    "get_knowledge_revision",
    "knowledge_overview",
    "make_reviewed_copy",
]

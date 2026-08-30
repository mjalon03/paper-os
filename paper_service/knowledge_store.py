"""PostgreSQL state machine for reviewed paper knowledge and its outbox.

Revisions, review events, and usage facts are append-only.  The only path that
creates an outbox row revalidates the current server paper and Article JSON,
source-node evidence, selector contract, canonical digest, and a concrete
approval event in one transaction.  This module does not call a model or write
to an Obsidian vault.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Mapping
from uuid import UUID, uuid4, uuid5

from .database import connection
from .knowledge import (
    KnowledgeValidationError,
    payload_sha256,
    validate_knowledge_payload,
)
from .knowledge_worker import (
    GenerationPolicy,
    GenerationUsage,
    KnowledgeGenerationRequest,
    article_source_nodes,
    validate_generation_request,
)
from .reader_v2 import read_article
from .selectors import SELECTOR_VERSION, build_selector_index


_JOB_NAMESPACE = UUID("65420450-3ec8-598d-8d75-c3ad8ba5f901")
_OUTBOX_NAMESPACE = UUID("87848a17-10bf-5d65-9293-0dfd4a811329")
_USAGE_NAMESPACE = UUID("249f0a55-c8e2-5676-b51d-f9c57392f664")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


JOB_COLUMNS = """
id, paper_id, idempotency_key_digest, request_digest, provider, model,
request_actor_kind, request_actor_id, paper_text_and_cost_acknowledged,
include_personal_annotations, max_source_pages, max_source_characters,
max_output_tokens, status, attempt_count, max_attempts, next_attempt_at,
lease_owner, lease_token, lease_expires_at, result_revision_id,
generation_request_digest, generation_revision_id,
generation_source_node_ids, generation_annotation_ids, last_error_code,
requested_at, started_at, finished_at
"""

REVISION_COLUMNS = """
id, paper_id, revision_number, parent_revision_id, source_job_id, status,
canonical_payload, canonical_payload_sha256, source_sha256,
article_fingerprint, selector_version, created_at
"""

REVIEW_EVENT_COLUMNS = """
id, paper_id, review_revision, expected_revision, subject_revision_id,
result_revision_id, decision, actor_kind, actor_id, comment, created_at
"""

OUTBOX_COLUMNS = """
id, paper_id, revision_id, revision_status, revision_payload_sha256,
approval_event_id, approval_decision, dedupe_key, status, attempt_count,
max_attempts, available_at, lease_owner, lease_token, lease_expires_at,
last_error_code, created_at, delivered_at
"""


class KnowledgeStoreError(RuntimeError):
    """A fail-closed knowledge storage invariant was not satisfied."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class KnowledgeRevisionConflict(KnowledgeStoreError):
    """The supplied optimistic review revision is stale."""

    def __init__(self, current_revision: int):
        super().__init__("review_revision_conflict")
        self.current_revision = current_revision


class KnowledgeLeaseLost(KnowledgeStoreError):
    """A job or outbox lease is missing, expired, or owned elsewhere."""


def _fail(code: str) -> KnowledgeStoreError:
    return KnowledgeStoreError(code)


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise _fail("value_not_canonical_json") from exc


def _json_value(value: Any) -> str:
    return _canonical_bytes(value).decode("utf-8")


def _text(value: Any, *, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise _fail(f"{field}_invalid")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum or _CONTROL_RE.search(normalized):
        raise _fail(f"{field}_invalid")
    try:
        normalized.encode("utf-8")
    except UnicodeError as exc:
        raise _fail(f"{field}_invalid") from exc
    return normalized


def _optional_comment(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _fail("review_comment_invalid")
    normalized = value.strip()
    if len(normalized) > 4_000 or _CONTROL_RE.search(normalized):
        raise _fail("review_comment_invalid")
    try:
        normalized.encode("utf-8")
    except UnicodeError as exc:
        raise _fail("review_comment_invalid") from exc
    return normalized or None


def _positive_int(value: Any, *, field: str, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise _fail(f"{field}_invalid")
    if maximum is not None and value > maximum:
        raise _fail(f"{field}_invalid")
    return value


def _nonnegative_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _fail(f"{field}_invalid")
    return value


def _uuid(value: Any, *, field: str) -> str:
    if not isinstance(value, (str, UUID)):
        raise _fail(f"{field}_invalid")
    try:
        parsed = UUID(str(value))
    except (ValueError, AttributeError) as exc:
        raise _fail(f"{field}_invalid") from exc
    if parsed.int == 0 or str(parsed) != str(value):
        raise _fail(f"{field}_invalid")
    return str(parsed)


def _sha256(value: Any, *, field: str) -> str:
    normalized = str(value).strip() if isinstance(value, str) else ""
    if not _SHA256_RE.fullmatch(normalized):
        raise _fail(f"{field}_invalid")
    return normalized


def _human_request_authorization(
    actor_kind: Any,
    actor_id: Any,
    paper_text_and_cost_acknowledged: Any,
) -> tuple[str, str, bool]:
    if actor_kind != "human":
        raise _fail("generation_request_human_actor_required")
    clean_actor = _text(actor_id, field="request_actor_id", maximum=200)
    if paper_text_and_cost_acknowledged is not True:
        raise _fail("generation_paper_text_and_cost_acknowledgement_required")
    return "human", clean_actor, True


def _job_human_request_authorization(
    job: Mapping[str, Any],
) -> tuple[str, str, bool]:
    if not isinstance(job, Mapping):
        raise _fail("generation_job_authorization_invalid")
    return _human_request_authorization(
        job.get("request_actor_kind"),
        job.get("request_actor_id"),
        job.get("paper_text_and_cost_acknowledged"),
    )


def _validate_generation_job_request_authorization(
    job: Mapping[str, Any],
) -> tuple[str, str, bool]:
    """Validate the durable human authorization and its exact request binding."""

    actor_kind, actor_id, acknowledged = _job_human_request_authorization(job)
    if type(job.get("include_personal_annotations")) is not bool:
        raise _fail("generation_job_policy_invalid")
    policy = GenerationPolicy(
        max_source_pages=_positive_int(
            job.get("max_source_pages"), field="max_source_pages", maximum=8
        ),
        max_source_characters=_positive_int(
            job.get("max_source_characters"),
            field="max_source_characters",
            maximum=30_000,
        ),
        max_output_tokens=_positive_int(
            job.get("max_output_tokens"), field="max_output_tokens", maximum=8_192
        ),
        include_personal_annotations=job["include_personal_annotations"],
    )
    expected = generation_request_digest(
        paper_id=_positive_int(job.get("paper_id"), field="paper_id"),
        provider=job.get("provider"),
        model=job.get("model"),
        policy=policy,
        request_actor_kind=actor_kind,
        request_actor_id=actor_id,
        paper_text_and_cost_acknowledged=acknowledged,
    )
    if _sha256(job.get("request_digest"), field="request_digest") != expected:
        raise _fail("generation_job_request_digest_mismatch")
    return actor_kind, actor_id, acknowledged


def _row(value: Any, *, missing_code: str) -> dict[str, Any]:
    if value is None:
        raise _fail(missing_code)
    try:
        return dict(value)
    except (TypeError, ValueError) as exc:
        raise _fail("database_row_invalid") from exc


def _payload_from_row(row: Mapping[str, Any]) -> dict[str, Any]:
    value = row.get("canonical_payload")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, RecursionError) as exc:
            raise _fail("stored_revision_payload_invalid") from exc
    if not isinstance(value, dict):
        raise _fail("stored_revision_payload_invalid")
    try:
        canonical = validate_knowledge_payload(value)
    except KnowledgeValidationError as exc:
        raise _fail("stored_revision_payload_invalid") from exc
    digest = payload_sha256(canonical)
    if digest != _sha256(
        row.get("canonical_payload_sha256"), field="stored_payload_digest"
    ):
        raise _fail("stored_revision_digest_mismatch")
    comparisons = {
        "revision_id": str(row.get("id")),
        "paper_id": row.get("paper_id"),
        "status": row.get("status"),
        "source_sha256": str(row.get("source_sha256", "")).strip(),
        "article_fingerprint": str(row.get("article_fingerprint", "")).strip(),
        "selector_version": row.get("selector_version"),
    }
    if canonical["revision_id"] != comparisons["revision_id"]:
        raise _fail("stored_revision_id_mismatch")
    if canonical["paper_identity"]["paper_id"] != comparisons["paper_id"]:
        raise _fail("stored_revision_paper_mismatch")
    for key in ("status", "source_sha256", "article_fingerprint", "selector_version"):
        if canonical[key] != comparisons[key]:
            raise _fail(f"stored_revision_{key}_mismatch")
    return canonical


def _current_paper_identity(paper: Mapping[str, Any]) -> dict[str, Any]:
    paper_id = _positive_int(paper.get("paper_id", paper.get("id")), field="paper_id")
    title = paper.get("title")
    if not isinstance(title, str):
        raise _fail("current_paper_title_invalid")
    clean_title = unicodedata.normalize("NFC", title.strip())
    if not clean_title:
        raise _fail("current_paper_title_invalid")
    identity: dict[str, Any] = {"paper_id": paper_id, "title": clean_title}
    doi = paper.get("doi")
    if doi is not None and str(doi).strip():
        identity["doi"] = unicodedata.normalize("NFC", str(doi).strip())
    authors = paper.get("authors")
    if isinstance(authors, str):
        try:
            authors = json.loads(authors)
        except (json.JSONDecodeError, RecursionError) as exc:
            raise _fail("current_paper_authors_invalid") from exc
    if authors is not None:
        if not isinstance(authors, list) or any(not isinstance(item, str) for item in authors):
            raise _fail("current_paper_authors_invalid")
        identity["authors"] = [
            unicodedata.normalize("NFC", item.strip()) for item in authors
        ]
    year = paper.get("publication_year")
    if year is not None:
        if isinstance(year, bool) or not isinstance(year, int):
            raise _fail("current_paper_year_invalid")
        identity["year"] = year
    return identity


def generation_job_identity(
    paper_id: int, idempotency_key: str
) -> tuple[str, str]:
    """Return a deterministic UUID and non-reversible stored key digest."""

    clean_paper_id = _positive_int(paper_id, field="paper_id")
    clean_key = _text(idempotency_key, field="idempotency_key", maximum=200)
    key_digest = hashlib.sha256(clean_key.encode("utf-8")).hexdigest()
    return str(uuid5(_JOB_NAMESPACE, f"{clean_paper_id}:{key_digest}")), key_digest


def generation_request_digest(
    *,
    paper_id: int,
    provider: str,
    model: str,
    policy: GenerationPolicy,
    request_actor_kind: str,
    request_actor_id: str,
    paper_text_and_cost_acknowledged: bool,
) -> str:
    if not isinstance(policy, GenerationPolicy):
        raise _fail("generation_policy_invalid")
    actor_kind, actor_id, acknowledged = _human_request_authorization(
        request_actor_kind,
        request_actor_id,
        paper_text_and_cost_acknowledged,
    )
    value = {
        "contract_version": 2,
        "paper_id": _positive_int(paper_id, field="paper_id"),
        "provider": _text(provider, field="provider", maximum=80),
        "model": _text(model, field="model", maximum=200),
        "request_actor_kind": actor_kind,
        "request_actor_id": actor_id,
        "paper_text_and_cost_acknowledged": acknowledged,
        "include_personal_annotations": policy.include_personal_annotations,
        "max_source_pages": policy.max_source_pages,
        "max_source_characters": policy.max_source_characters,
        "max_output_tokens": policy.max_output_tokens,
    }
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def outbox_dedupe_key(revision_id: str | UUID, canonical_digest: str) -> str:
    value = {
        "kind": "reviewed-paper-knowledge-v1",
        "revision_id": _uuid(revision_id, field="revision_id"),
        "canonical_payload_sha256": _sha256(
            canonical_digest, field="canonical_payload_sha256"
        ),
    }
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def enqueue_generation_job(
    paper_id: int,
    *,
    idempotency_key: str,
    provider: str,
    model: str,
    request_actor_kind: str,
    request_actor_id: str,
    paper_text_and_cost_acknowledged: bool,
    policy: GenerationPolicy | None = None,
    max_attempts: int = 3,
) -> dict[str, Any]:
    active_policy = GenerationPolicy() if policy is None else policy
    clean_provider = _text(provider, field="provider", maximum=80)
    clean_model = _text(model, field="model", maximum=200)
    actor_kind, actor_id, acknowledged = _human_request_authorization(
        request_actor_kind,
        request_actor_id,
        paper_text_and_cost_acknowledged,
    )
    job_id, key_digest = generation_job_identity(paper_id, idempotency_key)
    request_digest = generation_request_digest(
        paper_id=paper_id,
        provider=clean_provider,
        model=clean_model,
        policy=active_policy,
        request_actor_kind=actor_kind,
        request_actor_id=actor_id,
        paper_text_and_cost_acknowledged=acknowledged,
    )
    _positive_int(max_attempts, field="max_attempts", maximum=10)
    with connection() as database:
        row = database.execute(
            f"""INSERT INTO paper_knowledge_jobs
                (id, paper_id, idempotency_key_digest, request_digest,
                 provider, model, request_actor_kind, request_actor_id,
                 paper_text_and_cost_acknowledged, include_personal_annotations,
                 max_source_pages, max_source_characters, max_output_tokens,
                 max_attempts)
                VALUES (%s::uuid, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s)
                ON CONFLICT (paper_id, idempotency_key_digest) DO NOTHING
                RETURNING {JOB_COLUMNS}""",
            (
                job_id,
                paper_id,
                key_digest,
                request_digest,
                clean_provider,
                clean_model,
                actor_kind,
                actor_id,
                acknowledged,
                active_policy.include_personal_annotations,
                active_policy.max_source_pages,
                active_policy.max_source_characters,
                active_policy.max_output_tokens,
                max_attempts,
            ),
        ).fetchone()
        if row is None:
            row = database.execute(
                f"""SELECT {JOB_COLUMNS} FROM paper_knowledge_jobs
                    WHERE paper_id = %s AND idempotency_key_digest = %s""",
                (paper_id, key_digest),
            ).fetchone()
        current = _row(row, missing_code="generation_job_missing_after_enqueue")
        if (
            str(current["id"]) != job_id
            or str(current["request_digest"]).strip() != request_digest
            or _validate_generation_job_request_authorization(current)
            != (actor_kind, actor_id, acknowledged)
        ):
            raise _fail("generation_job_idempotency_conflict")
        return current


def _recover_expired_generation_jobs(database: Any) -> int:
    result = database.execute(
        """UPDATE paper_knowledge_jobs
           SET status = CASE
                   WHEN attempt_count >= max_attempts THEN 'failed'
                   ELSE 'retry_wait'
               END,
               next_attempt_at = NOW(),
               lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
               generation_request_digest = NULL,
               generation_revision_id = NULL,
               generation_source_node_ids = NULL,
               generation_annotation_ids = NULL,
               last_error_code = 'lease_expired',
               finished_at = CASE
                   WHEN attempt_count >= max_attempts THEN NOW() ELSE NULL END
           WHERE status = 'running' AND lease_expires_at <= NOW()"""
    )
    return int(getattr(result, "rowcount", 0) or 0)


def claim_generation_job(
    *, worker_id: str, lease_seconds: int = 300
) -> dict[str, Any] | None:
    clean_worker = _text(worker_id, field="worker_id", maximum=200)
    _positive_int(lease_seconds, field="lease_seconds", maximum=3_600)
    lease_token = str(uuid4())
    with connection() as database:
        _recover_expired_generation_jobs(database)
        row = database.execute(
            f"""WITH candidate AS (
                    SELECT id FROM paper_knowledge_jobs
                    WHERE status IN ('queued', 'retry_wait')
                      AND next_attempt_at <= NOW()
                      AND attempt_count < max_attempts
                      AND request_actor_kind = 'human'
                      AND paper_text_and_cost_acknowledged IS TRUE
                    ORDER BY next_attempt_at, requested_at, id
                    FOR UPDATE SKIP LOCKED LIMIT 1
                )
                UPDATE paper_knowledge_jobs AS job
                SET status = 'running',
                    attempt_count = attempt_count + 1,
                    started_at = COALESCE(started_at, NOW()),
                    lease_owner = %s, lease_token = %s::uuid,
                    lease_expires_at = NOW() + make_interval(secs => %s),
                    generation_request_digest = NULL,
                    generation_revision_id = NULL,
                    generation_source_node_ids = NULL,
                    generation_annotation_ids = NULL,
                    last_error_code = NULL
                FROM candidate WHERE job.id = candidate.id
                RETURNING {JOB_COLUMNS}""",
            (clean_worker, lease_token, lease_seconds),
        ).fetchone()
        if not row:
            return None
        claimed = dict(row)
        _validate_generation_job_request_authorization(claimed)
        return claimed


def _json_string_ids(value: Any, *, field: str) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, RecursionError) as exc:
            raise _fail(f"{field}_invalid") from exc
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise _fail(f"{field}_invalid")
    if len(value) != len(set(value)):
        raise _fail(f"{field}_invalid")
    return value


def bind_generation_request(
    job_id: str | UUID,
    *,
    worker_id: str,
    lease_token: str | UUID,
    request: KnowledgeGenerationRequest,
    data_dir: str | Path,
) -> dict[str, Any]:
    """Bind a leased attempt to the exact bounded request before transmission."""

    validate_generation_request(request)
    clean_job_id = _uuid(job_id, field="job_id")
    clean_worker = _text(worker_id, field="worker_id", maximum=200)
    clean_token = _uuid(lease_token, field="lease_token")
    with connection() as database:
        row = database.execute(
            f"""SELECT {', '.join('j.' + item.strip() for item in JOB_COLUMNS.split(','))},
                       p.sha256, p.title, p.authors, p.publication_year, p.doi,
                       p.reader_v2_status, p.reader_v2_manifest_path,
                       (j.lease_expires_at > NOW()) AS lease_valid
                FROM paper_knowledge_jobs j
                JOIN papers p ON p.id = j.paper_id
                WHERE j.id = %s::uuid
                FOR UPDATE OF j, p""",
            (clean_job_id,),
        ).fetchone()
        job = _row(row, missing_code="generation_job_not_found")
        _validate_generation_job_request_authorization(job)
        if (
            job["status"] != "running"
            or not job["lease_valid"]
            or str(job["lease_owner"]) != clean_worker
            or str(job["lease_token"]) != clean_token
        ):
            raise KnowledgeLeaseLost("generation_job_lease_lost")
        expected_policy = GenerationPolicy(
            max_source_pages=int(job["max_source_pages"]),
            max_source_characters=int(job["max_source_characters"]),
            max_output_tokens=int(job["max_output_tokens"]),
            include_personal_annotations=bool(job["include_personal_annotations"]),
        )
        if (
            request.provider != job["provider"]
            or request.model != job["model"]
            or request.policy != expected_policy
        ):
            raise _fail("generation_request_job_mismatch")
        paper = {
            "paper_id": int(job["paper_id"]),
            "sha256": str(job["sha256"]).strip(),
            "title": job["title"],
            "authors": job["authors"],
            "publication_year": job["publication_year"],
            "doi": job["doi"],
            "reader_v2_status": job["reader_v2_status"],
            "reader_v2_manifest_path": job["reader_v2_manifest_path"],
        }
        if request.input_payload["paper_identity"] != _current_paper_identity(paper):
            raise _fail("generation_request_paper_identity_mismatch")
        if request.input_payload["source_sha256"] != paper["sha256"]:
            raise _fail("generation_request_source_sha256_stale")
        if (
            paper["reader_v2_status"] != "ready"
            or str(paper["reader_v2_manifest_path"])
            != f"{int(job['paper_id'])}/v2/article.json"
        ):
            raise _fail("generation_request_reader_not_current")
        article = read_article(Path(data_dir), int(job["paper_id"]))
        selector_index = build_selector_index(article)
        if (
            request.input_payload["article_fingerprint"]
            != selector_index.article_fingerprint
            or request.input_payload["selector_version"]
            != selector_index.selector_version
        ):
            raise _fail("generation_request_article_stale")
        current_nodes = article_source_nodes(article, selector_index)
        source_node_ids = list(request.source_nodes)
        if not source_node_ids:
            raise _fail("generation_request_source_nodes_empty")
        for node_id, sent_text in request.source_nodes.items():
            current_text = current_nodes.get(node_id)
            if current_text is None or not current_text.startswith(sent_text):
                raise _fail("generation_request_source_node_mismatch")
        annotation_ids = [
            item["annotation_id"]
            for item in request.input_payload.get("personal_annotations", [])
        ]
        if not job["include_personal_annotations"] and annotation_ids:
            raise _fail("generation_request_annotations_forbidden")
        if annotation_ids:
            rows = database.execute(
                """SELECT id::text AS id FROM annotations
                   WHERE paper_id = %s AND id = ANY(%s::uuid[])
                     AND article_fingerprint = %s AND trashed_at IS NULL""",
                (
                    int(job["paper_id"]),
                    annotation_ids,
                    selector_index.article_fingerprint,
                ),
            ).fetchall()
            if {str(item["id"]) for item in rows} != set(annotation_ids):
                raise _fail("generation_request_annotation_stale")
        bound = {
            "generation_request_digest": request.request_digest,
            "generation_revision_id": request.revision_id,
            "generation_source_node_ids": source_node_ids,
            "generation_annotation_ids": annotation_ids,
        }
        if job.get("generation_request_digest") is not None:
            current = {
                "generation_request_digest": str(job["generation_request_digest"]).strip(),
                "generation_revision_id": str(job["generation_revision_id"]),
                "generation_source_node_ids": _json_string_ids(
                    job["generation_source_node_ids"], field="generation_source_node_ids"
                ),
                "generation_annotation_ids": _json_string_ids(
                    job["generation_annotation_ids"], field="generation_annotation_ids"
                ),
            }
            if current != bound:
                raise _fail("generation_request_rebind_conflict")
            return {
                field.strip(): job[field.strip()]
                for field in JOB_COLUMNS.split(",")
            }
        updated = database.execute(
            f"""UPDATE paper_knowledge_jobs
                SET generation_request_digest = %s,
                    generation_revision_id = %s::uuid,
                    generation_source_node_ids = %s::jsonb,
                    generation_annotation_ids = %s::jsonb
                WHERE id = %s::uuid AND status = 'running'
                  AND lease_owner = %s AND lease_token = %s::uuid
                  AND lease_expires_at > NOW()
                  AND generation_request_digest IS NULL
                RETURNING {JOB_COLUMNS}""",
            (
                request.request_digest,
                request.revision_id,
                _json_value(source_node_ids),
                _json_value(annotation_ids),
                clean_job_id,
                clean_worker,
                clean_token,
            ),
        ).fetchone()
        if updated is None:
            raise KnowledgeLeaseLost("generation_job_lease_lost")
        return dict(updated)


def nack_generation_job(
    job_id: str | UUID,
    *,
    worker_id: str,
    lease_token: str | UUID,
    error_code: str,
    retry_delay_seconds: int = 60,
) -> dict[str, Any]:
    clean_job_id = _uuid(job_id, field="job_id")
    clean_worker = _text(worker_id, field="worker_id", maximum=200)
    clean_token = _uuid(lease_token, field="lease_token")
    clean_error = _text(error_code, field="error_code", maximum=120)
    _nonnegative_int(retry_delay_seconds, field="retry_delay_seconds")
    if retry_delay_seconds > 86_400:
        raise _fail("retry_delay_seconds_invalid")
    with connection() as database:
        row = database.execute(
            f"""UPDATE paper_knowledge_jobs
                SET status = CASE
                        WHEN attempt_count >= max_attempts THEN 'failed'
                        ELSE 'retry_wait'
                    END,
                    next_attempt_at = NOW() + make_interval(secs => %s),
                    lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
                    generation_request_digest = NULL,
                    generation_revision_id = NULL,
                    generation_source_node_ids = NULL,
                    generation_annotation_ids = NULL,
                    last_error_code = %s,
                    finished_at = CASE
                        WHEN attempt_count >= max_attempts THEN NOW() ELSE NULL END
                WHERE id = %s::uuid AND status = 'running'
                  AND lease_owner = %s AND lease_token = %s::uuid
                  AND lease_expires_at > NOW()
                RETURNING {JOB_COLUMNS}""",
            (
                retry_delay_seconds,
                clean_error,
                clean_job_id,
                clean_worker,
                clean_token,
            ),
        ).fetchone()
        if row is None:
            raise KnowledgeLeaseLost("generation_job_lease_lost")
        return dict(row)


def _assert_current_payload(
    payload: Any,
    *,
    expected_status: str,
    paper: Mapping[str, Any],
    article: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, str]]:
    paper_id = _positive_int(paper.get("paper_id", paper.get("id")), field="paper_id")
    if paper.get("reader_v2_status") != "ready":
        raise _fail("current_reader_not_ready")
    expected_manifest = f"{paper_id}/v2/article.json"
    if str(paper.get("reader_v2_manifest_path") or "") != expected_manifest:
        raise _fail("current_reader_manifest_mismatch")
    source_sha = _sha256(paper.get("sha256"), field="current_source_sha256")
    selector_index = build_selector_index(article)
    source_nodes = article_source_nodes(article, selector_index)
    try:
        canonical = validate_knowledge_payload(payload, source_nodes=source_nodes)
    except KnowledgeValidationError as exc:
        raise _fail(f"knowledge_payload_invalid:{exc}") from exc
    if canonical["status"] != expected_status:
        raise _fail(f"knowledge_status_{expected_status}_required")
    if canonical["paper_identity"]["paper_id"] != paper_id:
        raise _fail("knowledge_paper_id_mismatch")
    if "title" in paper and canonical["paper_identity"] != _current_paper_identity(paper):
        raise _fail("knowledge_paper_identity_mismatch")
    if canonical["source_sha256"] != source_sha:
        raise _fail("knowledge_source_sha256_stale")
    if canonical["article_fingerprint"] != selector_index.article_fingerprint:
        raise _fail("knowledge_article_fingerprint_stale")
    if canonical["selector_version"] != selector_index.selector_version:
        raise _fail("knowledge_selector_version_stale")
    if selector_index.selector_version != SELECTOR_VERSION:
        raise _fail("current_selector_version_unsupported")
    return canonical, source_nodes


def _next_revision_number(database: Any, paper_id: int) -> int:
    row = database.execute(
        """SELECT COALESCE(MAX(revision_number), 0) + 1 AS next_revision
           FROM paper_knowledge_revisions WHERE paper_id = %s""",
        (paper_id,),
    ).fetchone()
    return int(_row(row, missing_code="revision_sequence_missing")["next_revision"])


def _insert_revision(
    database: Any,
    canonical: dict[str, Any],
    *,
    paper_id: int,
    parent_revision_id: str | None,
    source_job_id: str | None,
) -> dict[str, Any]:
    digest = payload_sha256(canonical)
    revision_number = _next_revision_number(database, paper_id)
    row = database.execute(
        f"""INSERT INTO paper_knowledge_revisions
            (id, paper_id, revision_number, parent_revision_id, source_job_id,
             status, canonical_payload, canonical_payload_sha256,
             source_sha256, article_fingerprint, selector_version)
            VALUES (%s::uuid, %s, %s, %s::uuid, %s::uuid, %s, %s::jsonb,
                    %s, %s, %s, %s)
            RETURNING {REVISION_COLUMNS}""",
        (
            canonical["revision_id"],
            paper_id,
            revision_number,
            parent_revision_id,
            source_job_id,
            canonical["status"],
            _json_value(canonical),
            digest,
            canonical["source_sha256"],
            canonical["article_fingerprint"],
            canonical["selector_version"],
        ),
    ).fetchone()
    inserted = _row(row, missing_code="revision_insert_failed")
    _payload_from_row(inserted)
    return inserted


def _record_usage(
    database: Any,
    *,
    job: Mapping[str, Any],
    usage: GenerationUsage,
    request_digest: str,
    response_digest: str,
) -> dict[str, Any]:
    if not isinstance(usage, GenerationUsage):
        raise _fail("generation_usage_required")
    if usage.provider != job["provider"] or usage.model != job["model"]:
        raise _fail("generation_usage_provider_mismatch")
    clean_request_digest = _sha256(request_digest, field="generation_request_digest")
    clean_response_digest = _sha256(response_digest, field="generation_response_digest")
    attempt = _positive_int(job["attempt_count"], field="attempt_count")
    usage_id = str(uuid5(_USAGE_NAMESPACE, f"{job['id']}:{attempt}"))
    columns = """id, job_id, attempt_number, provider, model,
                 request_digest, response_digest, prompt_tokens,
                 completion_tokens, total_tokens, cost_microusd,
                 cost_basis, pricing_version, recorded_at"""
    row = database.execute(
        """INSERT INTO paper_knowledge_usage_ledger
           (id, job_id, attempt_number, provider, model, request_digest,
            response_digest, prompt_tokens, completion_tokens, total_tokens,
            cost_microusd, cost_basis, pricing_version)
           VALUES (%s::uuid, %s::uuid, %s, %s, %s, %s, %s, %s, %s, %s,
                   %s, %s, %s)
           ON CONFLICT (job_id, attempt_number) DO NOTHING
           RETURNING id, job_id, attempt_number, provider, model,
                     request_digest, response_digest, prompt_tokens,
                     completion_tokens, total_tokens, cost_microusd,
                     cost_basis, pricing_version, recorded_at""",
        (
            usage_id,
            str(job["id"]),
            attempt,
            usage.provider,
            usage.model,
            clean_request_digest,
            clean_response_digest,
            usage.prompt_tokens,
            usage.completion_tokens,
            usage.total_tokens,
            usage.cost_microusd,
            usage.cost_basis,
            usage.pricing_version,
        ),
    ).fetchone()
    if row is None:
        row = database.execute(
            f"""SELECT {columns} FROM paper_knowledge_usage_ledger
                WHERE job_id = %s::uuid AND attempt_number = %s""",
            (str(job["id"]), attempt),
        ).fetchone()
    current = _row(row, missing_code="usage_ledger_insert_failed")
    expected = {
        "id": usage_id,
        "job_id": str(job["id"]),
        "attempt_number": attempt,
        "provider": usage.provider,
        "model": usage.model,
        "request_digest": clean_request_digest,
        "response_digest": clean_response_digest,
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "total_tokens": usage.total_tokens,
        "cost_microusd": usage.cost_microusd,
        "cost_basis": usage.cost_basis,
        "pricing_version": usage.pricing_version,
    }
    for key, expected_value in expected.items():
        actual = current.get(key)
        if key in {"id", "job_id"}:
            actual = str(actual)
        elif key in {"request_digest", "response_digest"}:
            actual = str(actual).strip()
        if actual != expected_value:
            raise _fail("usage_ledger_idempotency_conflict")
    return current


def record_generation_usage(
    job_id: str | UUID,
    *,
    worker_id: str,
    lease_token: str | UUID,
    usage: GenerationUsage,
    generation_request_digest: str,
    generation_response_digest: str,
) -> dict[str, Any]:
    """Append usage for a leased attempt, including malformed model replies."""

    clean_job_id = _uuid(job_id, field="job_id")
    clean_worker = _text(worker_id, field="worker_id", maximum=200)
    clean_token = _uuid(lease_token, field="lease_token")
    with connection() as database:
        row = database.execute(
            f"""SELECT {JOB_COLUMNS},
                       (lease_expires_at > NOW()) AS lease_valid
                FROM paper_knowledge_jobs
                WHERE id = %s::uuid FOR UPDATE""",
            (clean_job_id,),
        ).fetchone()
        job = _row(row, missing_code="generation_job_not_found")
        if (
            job["status"] != "running"
            or not job["lease_valid"]
            or str(job["lease_owner"]) != clean_worker
            or str(job["lease_token"]) != clean_token
        ):
            raise KnowledgeLeaseLost("generation_job_lease_lost")
        clean_request_digest = _sha256(
            generation_request_digest, field="generation_request_digest"
        )
        if (
            job.get("generation_request_digest") is None
            or str(job["generation_request_digest"]).strip()
            != clean_request_digest
        ):
            raise _fail("generation_request_digest_mismatch")
        return _record_usage(
            database,
            job=job,
            usage=usage,
            request_digest=clean_request_digest,
            response_digest=generation_response_digest,
        )


def _assert_generation_request_binding(
    canonical: Mapping[str, Any], job: Mapping[str, Any]
) -> None:
    if canonical["revision_id"] != str(job["generation_revision_id"]):
        raise _fail("generation_revision_id_mismatch")
    allowed_nodes = set(
        _json_string_ids(
            job["generation_source_node_ids"], field="generation_source_node_ids"
        )
    )
    evidence_nodes = {
        evidence["node_id"]
        for claim in canonical["claims"]
        for evidence in claim["evidence"]
    }
    if not evidence_nodes <= allowed_nodes:
        raise _fail("generation_evidence_not_in_request")
    allowed_annotations = set(
        _json_string_ids(
            job["generation_annotation_ids"], field="generation_annotation_ids"
        )
    )
    if not set(canonical.get("user_annotation_ids", [])) <= allowed_annotations:
        raise _fail("generation_annotation_not_in_request")


def complete_generation_job(
    job_id: str | UUID,
    *,
    worker_id: str,
    lease_token: str | UUID,
    payload: Any,
    data_dir: str | Path,
    usage: GenerationUsage,
    generation_request: KnowledgeGenerationRequest,
    generation_response_digest: str,
) -> dict[str, Any]:
    """Atomically store one AI draft, usage facts, and job acknowledgement."""

    validate_generation_request(generation_request)
    clean_job_id = _uuid(job_id, field="job_id")
    clean_worker = _text(worker_id, field="worker_id", maximum=200)
    clean_token = _uuid(lease_token, field="lease_token")
    clean_response_digest = _sha256(
        generation_response_digest,
        field="generation_response_digest",
    )
    with connection() as database:
        row = database.execute(
            f"""SELECT {', '.join('j.' + item.strip() for item in JOB_COLUMNS.split(','))},
                       p.sha256, p.title, p.authors, p.publication_year, p.doi,
                       p.reader_v2_status, p.reader_v2_manifest_path,
                       (j.lease_expires_at > NOW()) AS lease_valid
                FROM paper_knowledge_jobs j
                JOIN papers p ON p.id = j.paper_id
                WHERE j.id = %s::uuid
                FOR UPDATE OF j, p""",
            (clean_job_id,),
        ).fetchone()
        job = _row(row, missing_code="generation_job_not_found")
        if job["status"] != "running" or not job["lease_valid"]:
            raise KnowledgeLeaseLost("generation_job_lease_lost")
        if str(job["lease_owner"]) != clean_worker or str(job["lease_token"]) != clean_token:
            raise KnowledgeLeaseLost("generation_job_lease_lost")
        if job.get("generation_request_digest") is None:
            raise _fail("generation_request_not_bound")
        clean_generation_digest = generation_request.request_digest
        if str(job["generation_request_digest"]).strip() != clean_generation_digest:
            raise _fail("generation_request_digest_mismatch")
        if (
            generation_request.provider != job["provider"]
            or generation_request.model != job["model"]
            or generation_request.policy
            != GenerationPolicy(
                max_source_pages=int(job["max_source_pages"]),
                max_source_characters=int(job["max_source_characters"]),
                max_output_tokens=int(job["max_output_tokens"]),
                include_personal_annotations=bool(
                    job["include_personal_annotations"]
                ),
            )
        ):
            raise _fail("generation_request_job_mismatch")
        paper_id = int(job["paper_id"])
        article = read_article(Path(data_dir), paper_id)
        try:
            request_scoped_payload = validate_knowledge_payload(
                payload,
                source_nodes=generation_request.source_nodes,
            )
        except KnowledgeValidationError as exc:
            raise _fail(f"generation_payload_not_in_request:{exc}") from exc
        canonical, _source_nodes = _assert_current_payload(
            request_scoped_payload,
            expected_status="ai-draft",
            paper={
                "paper_id": paper_id,
                "sha256": str(job["sha256"]).strip(),
                "title": job["title"],
                "authors": job["authors"],
                "publication_year": job["publication_year"],
                "doi": job["doi"],
                "reader_v2_status": job["reader_v2_status"],
                "reader_v2_manifest_path": job["reader_v2_manifest_path"],
            },
            article=article,
        )
        _assert_generation_request_binding(canonical, job)
        revision = _insert_revision(
            database,
            canonical,
            paper_id=paper_id,
            parent_revision_id=None,
            source_job_id=clean_job_id,
        )
        usage_row = _record_usage(
            database,
            job=job,
            usage=usage,
            request_digest=clean_generation_digest,
            response_digest=clean_response_digest,
        )
        updated = database.execute(
            f"""UPDATE paper_knowledge_jobs
                SET status = 'succeeded', result_revision_id = %s::uuid,
                    lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
                    finished_at = NOW(), last_error_code = NULL
                WHERE id = %s::uuid AND status = 'running'
                  AND lease_owner = %s AND lease_token = %s::uuid
                  AND lease_expires_at > NOW()
                RETURNING {JOB_COLUMNS}""",
            (
                canonical["revision_id"],
                clean_job_id,
                clean_worker,
                clean_token,
            ),
        ).fetchone()
        if updated is None:
            raise KnowledgeLeaseLost("generation_job_lease_lost")
        return {
            "job": dict(updated),
            "revision": revision,
            "usage": usage_row,
        }


def _lock_review_head(
    database: Any, *, paper_id: int, expected_revision: int
) -> int:
    _nonnegative_int(expected_revision, field="expected_review_revision")
    database.execute(
        """INSERT INTO paper_knowledge_review_heads (paper_id)
           VALUES (%s) ON CONFLICT (paper_id) DO NOTHING""",
        (paper_id,),
    )
    row = database.execute(
        """SELECT review_revision FROM paper_knowledge_review_heads
           WHERE paper_id = %s FOR UPDATE""",
        (paper_id,),
    ).fetchone()
    current = int(_row(row, missing_code="review_head_missing")["review_revision"])
    if current != expected_revision:
        raise KnowledgeRevisionConflict(current)
    return current


def _review_body(canonical: dict[str, Any]) -> bytes:
    value = dict(canonical)
    value.pop("revision_id", None)
    value.pop("status", None)
    return _canonical_bytes(value)


def _insert_review_event(
    database: Any,
    *,
    paper_id: int,
    expected_revision: int,
    subject_revision_id: str,
    result_revision_id: str | None,
    decision: str,
    actor_kind: str,
    actor_id: str,
    comment: str | None,
) -> dict[str, Any]:
    event_id = str(uuid4())
    row = database.execute(
        f"""INSERT INTO paper_knowledge_review_events
            (id, paper_id, review_revision, expected_revision,
             subject_revision_id, result_revision_id, decision,
             actor_kind, actor_id, comment)
            VALUES (%s::uuid, %s, %s, %s, %s::uuid, %s::uuid, %s, %s, %s, %s)
            RETURNING {REVIEW_EVENT_COLUMNS}""",
        (
            event_id,
            paper_id,
            expected_revision + 1,
            expected_revision,
            subject_revision_id,
            result_revision_id,
            decision,
            actor_kind,
            actor_id,
            comment,
        ),
    ).fetchone()
    event = _row(row, missing_code="review_event_insert_failed")
    updated = database.execute(
        """UPDATE paper_knowledge_review_heads
           SET review_revision = %s, updated_at = NOW()
           WHERE paper_id = %s AND review_revision = %s
           RETURNING review_revision""",
        (expected_revision + 1, paper_id, expected_revision),
    ).fetchone()
    if updated is None:
        raise KnowledgeRevisionConflict(expected_revision)
    return event


def _assert_outbox_eligible(
    *,
    revision: Mapping[str, Any],
    paper: Mapping[str, Any],
    approval_event: Mapping[str, Any],
    article: dict[str, Any],
) -> dict[str, Any]:
    """Perform the complete fail-closed check immediately before INSERT."""

    stored = _payload_from_row(revision)
    canonical, _source_nodes = _assert_current_payload(
        stored,
        expected_status="reviewed",
        paper=paper,
        article=article,
    )
    if approval_event.get("decision") != "approve":
        raise _fail("approval_event_required")
    if approval_event.get("actor_kind") != "human":
        raise _fail("human_approval_event_required")
    if str(approval_event.get("result_revision_id")) != canonical["revision_id"]:
        raise _fail("approval_event_revision_mismatch")
    if approval_event.get("paper_id") != canonical["paper_identity"]["paper_id"]:
        raise _fail("approval_event_paper_mismatch")
    _uuid(approval_event.get("id"), field="approval_event_id")
    return canonical


def _insert_outbox(
    database: Any,
    *,
    canonical: dict[str, Any],
    approval_event: Mapping[str, Any],
    max_attempts: int = 10,
) -> dict[str, Any]:
    _positive_int(max_attempts, field="outbox_max_attempts", maximum=20)
    digest = payload_sha256(canonical)
    dedupe = outbox_dedupe_key(canonical["revision_id"], digest)
    outbox_id = str(uuid5(_OUTBOX_NAMESPACE, dedupe))
    paper_id = canonical["paper_identity"]["paper_id"]
    row = database.execute(
        f"""INSERT INTO paper_knowledge_outbox
            (id, paper_id, revision_id, revision_status,
             revision_payload_sha256, approval_event_id, approval_decision,
             dedupe_key, max_attempts)
            VALUES (%s::uuid, %s, %s::uuid, 'reviewed', %s,
                    %s::uuid, 'approve', %s, %s)
            ON CONFLICT (dedupe_key) DO NOTHING
            RETURNING {OUTBOX_COLUMNS}""",
        (
            outbox_id,
            paper_id,
            canonical["revision_id"],
            digest,
            str(approval_event["id"]),
            dedupe,
            max_attempts,
        ),
    ).fetchone()
    if row is None:
        row = database.execute(
            f"SELECT {OUTBOX_COLUMNS} FROM paper_knowledge_outbox WHERE dedupe_key = %s",
            (dedupe,),
        ).fetchone()
    output = _row(row, missing_code="outbox_insert_failed")
    if (
        str(output["id"]) != outbox_id
        or str(output["revision_id"]) != canonical["revision_id"]
        or str(output["revision_payload_sha256"]).strip() != digest
        or str(output["approval_event_id"]) != str(approval_event["id"])
    ):
        raise _fail("outbox_dedupe_conflict")
    return output


def approve_draft_revision(
    draft_revision_id: str | UUID,
    reviewed_payload: Any,
    *,
    expected_review_revision: int,
    actor_kind: str,
    actor_id: str,
    comment: str | None,
    data_dir: str | Path,
) -> dict[str, Any]:
    """Create a new immutable reviewed revision, approval, and outbox row."""

    clean_draft_id = _uuid(draft_revision_id, field="draft_revision_id")
    _nonnegative_int(expected_review_revision, field="expected_review_revision")
    if actor_kind != "human":
        raise _fail("human_review_actor_required")
    clean_actor = _text(actor_id, field="review_actor_id", maximum=200)
    clean_comment = _optional_comment(comment)
    with connection() as database:
        row = database.execute(
            f"""SELECT {', '.join('r.' + item.strip() for item in REVISION_COLUMNS.split(','))},
                       p.sha256, p.title, p.authors, p.publication_year, p.doi,
                       p.reader_v2_status, p.reader_v2_manifest_path
                FROM paper_knowledge_revisions r
                JOIN papers p ON p.id = r.paper_id
                WHERE r.id = %s::uuid
                FOR UPDATE OF p""",
            (clean_draft_id,),
        ).fetchone()
        draft = _row(row, missing_code="draft_revision_not_found")
        draft_payload = _payload_from_row(draft)
        if draft_payload["status"] != "ai-draft":
            raise _fail("ai_draft_revision_required")
        paper_id = int(draft["paper_id"])
        paper = {
            "paper_id": paper_id,
            "sha256": str(draft["sha256"]).strip(),
            "title": draft["title"],
            "authors": draft["authors"],
            "publication_year": draft["publication_year"],
            "doi": draft["doi"],
            "reader_v2_status": draft["reader_v2_status"],
            "reader_v2_manifest_path": draft["reader_v2_manifest_path"],
        }
        article = read_article(Path(data_dir), paper_id)
        reviewed, _source_nodes = _assert_current_payload(
            reviewed_payload,
            expected_status="reviewed",
            paper=paper,
            article=article,
        )
        if reviewed["revision_id"] == clean_draft_id:
            raise _fail("reviewed_revision_id_must_be_new")
        if _review_body(reviewed) != _review_body(draft_payload):
            raise _fail("reviewed_revision_content_changed")
        _lock_review_head(
            database,
            paper_id=paper_id,
            expected_revision=expected_review_revision,
        )
        reviewed_revision = _insert_revision(
            database,
            reviewed,
            paper_id=paper_id,
            parent_revision_id=clean_draft_id,
            source_job_id=None,
        )
        event = _insert_review_event(
            database,
            paper_id=paper_id,
            expected_revision=expected_review_revision,
            subject_revision_id=clean_draft_id,
            result_revision_id=reviewed["revision_id"],
            decision="approve",
            actor_kind=actor_kind,
            actor_id=clean_actor,
            comment=clean_comment,
        )

        # No database call belongs between this complete validation and INSERT.
        canonical = _assert_outbox_eligible(
            revision=reviewed_revision,
            paper=paper,
            approval_event=event,
            article=article,
        )
        outbox = _insert_outbox(
            database,
            canonical=canonical,
            approval_event=event,
        )
        return {"revision": reviewed_revision, "review_event": event, "outbox": outbox}


def reject_draft_revision(
    draft_revision_id: str | UUID,
    *,
    expected_review_revision: int,
    actor_kind: str,
    actor_id: str,
    comment: str | None,
) -> dict[str, Any]:
    clean_draft_id = _uuid(draft_revision_id, field="draft_revision_id")
    _nonnegative_int(expected_review_revision, field="expected_review_revision")
    if actor_kind not in {"human", "service"}:
        raise _fail("review_actor_kind_invalid")
    clean_actor = _text(actor_id, field="review_actor_id", maximum=200)
    clean_comment = _optional_comment(comment)
    with connection() as database:
        row = database.execute(
            f"""SELECT {REVISION_COLUMNS} FROM paper_knowledge_revisions
                WHERE id = %s::uuid FOR SHARE""",
            (clean_draft_id,),
        ).fetchone()
        draft = _row(row, missing_code="draft_revision_not_found")
        if _payload_from_row(draft)["status"] != "ai-draft":
            raise _fail("ai_draft_revision_required")
        paper_id = int(draft["paper_id"])
        _lock_review_head(
            database,
            paper_id=paper_id,
            expected_revision=expected_review_revision,
        )
        return _insert_review_event(
            database,
            paper_id=paper_id,
            expected_revision=expected_review_revision,
            subject_revision_id=clean_draft_id,
            result_revision_id=None,
            decision="reject",
            actor_kind=actor_kind,
            actor_id=clean_actor,
            comment=clean_comment,
        )


def publish_approved_revision(
    revision_id: str | UUID,
    *,
    data_dir: str | Path,
) -> dict[str, Any]:
    """Idempotently repair/publish an approved reviewed revision to the outbox."""

    clean_revision_id = _uuid(revision_id, field="revision_id")
    with connection() as database:
        row = database.execute(
            f"""SELECT {', '.join('r.' + item.strip() for item in REVISION_COLUMNS.split(','))},
                       p.sha256, p.title, p.authors, p.publication_year, p.doi,
                       p.reader_v2_status, p.reader_v2_manifest_path
                FROM paper_knowledge_revisions r
                JOIN papers p ON p.id = r.paper_id
                WHERE r.id = %s::uuid
                FOR UPDATE OF p""",
            (clean_revision_id,),
        ).fetchone()
        revision = _row(row, missing_code="reviewed_revision_not_found")
        event_row = database.execute(
            f"""SELECT {REVIEW_EVENT_COLUMNS}
                FROM paper_knowledge_review_events
                WHERE result_revision_id = %s::uuid AND decision = 'approve'""",
            (clean_revision_id,),
        ).fetchone()
        event = _row(event_row, missing_code="approval_event_required")
        paper_id = int(revision["paper_id"])
        paper = {
            "paper_id": paper_id,
            "sha256": str(revision["sha256"]).strip(),
            "title": revision["title"],
            "authors": revision["authors"],
            "publication_year": revision["publication_year"],
            "doi": revision["doi"],
            "reader_v2_status": revision["reader_v2_status"],
            "reader_v2_manifest_path": revision["reader_v2_manifest_path"],
        }
        article = read_article(Path(data_dir), paper_id)

        # No database call belongs between this complete validation and INSERT.
        canonical = _assert_outbox_eligible(
            revision=revision,
            paper=paper,
            approval_event=event,
            article=article,
        )
        return _insert_outbox(
            database,
            canonical=canonical,
            approval_event=event,
        )


def _recover_expired_outbox(database: Any) -> int:
    result = database.execute(
        """UPDATE paper_knowledge_outbox
           SET status = CASE
                   WHEN attempt_count >= max_attempts THEN 'dead'
                   ELSE 'pending'
               END,
               available_at = NOW(),
               lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
               last_error_code = 'lease_expired'
           WHERE status = 'leased' AND lease_expires_at <= NOW()"""
    )
    return int(getattr(result, "rowcount", 0) or 0)


def claim_outbox_item(
    *, worker_id: str, lease_seconds: int = 300
) -> dict[str, Any] | None:
    clean_worker = _text(worker_id, field="worker_id", maximum=200)
    _positive_int(lease_seconds, field="lease_seconds", maximum=3_600)
    with connection() as database:
        _recover_expired_outbox(database)
        for _candidate_count in range(100):
            lease_token = str(uuid4())
            row = database.execute(
                f"""WITH candidate AS (
                        SELECT id FROM paper_knowledge_outbox
                        WHERE status = 'pending' AND available_at <= NOW()
                          AND attempt_count < max_attempts
                        ORDER BY available_at, created_at, id
                        FOR UPDATE SKIP LOCKED LIMIT 1
                    ), leased AS (
                        UPDATE paper_knowledge_outbox AS outbox
                        SET status = 'leased', attempt_count = attempt_count + 1,
                            lease_owner = %s, lease_token = %s::uuid,
                            lease_expires_at = NOW() + make_interval(secs => %s),
                            last_error_code = NULL
                        FROM candidate WHERE outbox.id = candidate.id
                        RETURNING outbox.*
                    )
                    SELECT {
                        ', '.join(
                            'leased.' + item.strip()
                            for item in OUTBOX_COLUMNS.split(',')
                        )
                    },
                           revisions.canonical_payload
                    FROM leased JOIN paper_knowledge_revisions revisions
                      ON revisions.id = leased.revision_id""",
                (clean_worker, lease_token, lease_seconds),
            ).fetchone()
            if row is None:
                return None
            item = dict(row)
            invalid_code: str | None = None
            try:
                canonical = validate_knowledge_payload(item["canonical_payload"])
                if canonical["status"] != "reviewed":
                    invalid_code = "payload_not_reviewed"
                elif canonical["paper_identity"]["paper_id"] != item["paper_id"]:
                    invalid_code = "payload_paper_mismatch"
                elif canonical["revision_id"] != str(item["revision_id"]):
                    invalid_code = "payload_revision_mismatch"
                elif (
                    payload_sha256(canonical)
                    != str(item["revision_payload_sha256"]).strip()
                ):
                    invalid_code = "payload_digest_mismatch"
            except (KnowledgeValidationError, UnicodeError, ValueError):
                invalid_code = "payload_invalid"
            if invalid_code is None:
                item["canonical_payload"] = canonical
                return item
            database.execute(
                """UPDATE paper_knowledge_outbox
                   SET status = 'dead', lease_owner = NULL, lease_token = NULL,
                       lease_expires_at = NULL, last_error_code = %s
                   WHERE id = %s::uuid AND status = 'leased'
                     AND lease_owner = %s AND lease_token = %s::uuid""",
                (
                    invalid_code,
                    str(item["id"]),
                    clean_worker,
                    lease_token,
                ),
            )
        return None


def ack_outbox_item(
    outbox_id: str | UUID,
    *,
    worker_id: str,
    lease_token: str | UUID,
    expected_payload_sha256: str,
) -> dict[str, Any]:
    clean_id = _uuid(outbox_id, field="outbox_id")
    clean_worker = _text(worker_id, field="worker_id", maximum=200)
    clean_token = _uuid(lease_token, field="lease_token")
    clean_digest = _sha256(
        expected_payload_sha256, field="expected_payload_sha256"
    )
    with connection() as database:
        row = database.execute(
            f"""UPDATE paper_knowledge_outbox
                SET status = 'delivered', delivered_at = NOW(),
                    lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
                    last_error_code = NULL
                WHERE id = %s::uuid AND status = 'leased'
                  AND lease_owner = %s AND lease_token = %s::uuid
                  AND revision_payload_sha256 = %s
                  AND lease_expires_at > NOW()
                RETURNING {OUTBOX_COLUMNS}""",
            (clean_id, clean_worker, clean_token, clean_digest),
        ).fetchone()
        if row is None:
            raise KnowledgeLeaseLost("outbox_lease_lost")
        return dict(row)


def nack_outbox_item(
    outbox_id: str | UUID,
    *,
    worker_id: str,
    lease_token: str | UUID,
    error_code: str,
    retry_delay_seconds: int = 60,
) -> dict[str, Any]:
    clean_id = _uuid(outbox_id, field="outbox_id")
    clean_worker = _text(worker_id, field="worker_id", maximum=200)
    clean_token = _uuid(lease_token, field="lease_token")
    clean_error = _text(error_code, field="error_code", maximum=120)
    _nonnegative_int(retry_delay_seconds, field="retry_delay_seconds")
    if retry_delay_seconds > 86_400:
        raise _fail("retry_delay_seconds_invalid")
    with connection() as database:
        row = database.execute(
            f"""UPDATE paper_knowledge_outbox
                SET status = CASE
                        WHEN attempt_count >= max_attempts THEN 'dead'
                        ELSE 'pending'
                    END,
                    available_at = NOW() + make_interval(secs => %s),
                    lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
                    last_error_code = %s
                WHERE id = %s::uuid AND status = 'leased'
                  AND lease_owner = %s AND lease_token = %s::uuid
                  AND lease_expires_at > NOW()
                RETURNING {OUTBOX_COLUMNS}""",
            (
                retry_delay_seconds,
                clean_error,
                clean_id,
                clean_worker,
                clean_token,
            ),
        ).fetchone()
        if row is None:
            raise KnowledgeLeaseLost("outbox_lease_lost")
        return dict(row)


__all__ = [
    "JOB_COLUMNS",
    "OUTBOX_COLUMNS",
    "REVISION_COLUMNS",
    "REVIEW_EVENT_COLUMNS",
    "KnowledgeLeaseLost",
    "KnowledgeRevisionConflict",
    "KnowledgeStoreError",
    "ack_outbox_item",
    "approve_draft_revision",
    "bind_generation_request",
    "claim_generation_job",
    "claim_outbox_item",
    "complete_generation_job",
    "enqueue_generation_job",
    "generation_job_identity",
    "generation_request_digest",
    "nack_generation_job",
    "nack_outbox_item",
    "outbox_dedupe_key",
    "publish_approved_revision",
    "record_generation_usage",
    "reject_draft_revision",
]

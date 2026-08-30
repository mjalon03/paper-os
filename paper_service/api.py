"""Private API and mobile reader entrypoint for the paper pipeline."""

from __future__ import annotations

import os
import re
import mimetypes
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal, Optional
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
)
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from . import __version__
from .admin_queries import (
    AdminQueryError,
    get_admin_paper_source_binding,
    list_admin_paper_choices,
)
from .admin_views import render_admin_page
from .artifact_versions import (
    ArtifactVersionError,
    plan_retention,
    resolve_current_artifact,
)
from .database import (
    AnnotationConflict,
    AnnotationNotFound,
    AnnotationTrashed,
    PaperNotFound,
    ReaderIssueConflict,
    ReaderIssueNotFound,
    ReadingStateConflict,
    create_annotation,
    create_reader_issue,
    ensure_schema,
    get_reading_state,
    get_reader_paper,
    latest_sync,
    list_annotations,
    list_reader_issues,
    list_reader_papers,
    queue_sync,
    trash_annotation,
    update_annotation,
    update_reader_issue,
    update_reading_state,
)
from .reader import ReaderFailure, read_manifest, render_library, render_reader
from .reader_v2 import read_article, render_reader_v2, v2_asset_path
from .knowledge_queries import (
    KnowledgeQueryError,
    KnowledgeQueryNotFound,
    knowledge_overview,
    make_reviewed_copy,
)
from .knowledge_store import (
    KnowledgeLeaseLost,
    KnowledgeRevisionConflict,
    KnowledgeStoreError,
    ack_outbox_item,
    approve_draft_revision,
    claim_outbox_item,
    enqueue_generation_job,
    nack_outbox_item,
    reject_draft_revision,
)
from .knowledge_views import render_knowledge_page
from .knowledge_worker import GenerationPolicy
from .operations import build_selective_reprocess_request
from .ops_store import (
    OperationsApprovalRequired,
    OperationsReviewConflict,
    OperationsStoreError,
    create_reprocess_request,
    create_rollback_request,
    enqueue_approved_reprocess,
    enqueue_approved_rollback,
    get_cost_summary,
    get_operation_request_detail,
    get_operations_summary,
    get_worker_health_snapshot,
    list_expected_worker_services,
    list_operation_failures,
    list_pending_operation_reviews,
    list_recent_operation_requests,
    review_reprocess_request,
    review_rollback_request,
)
from .search_store import (
    SearchIndexNotReady,
    SearchStoreError,
    enqueue_qa_job,
    get_qa_job,
    search_papers,
)
from .search_views import render_search_page
from .taxonomy import (
    TaxonomyContractError,
    TaxonomyRevisionConflict,
    review_taxonomy_proposal,
)
from .taxonomy_queries import (
    TaxonomyQueryError,
    TaxonomyQueryNotFound,
    get_taxonomy_proposal_detail,
    list_pending_taxonomy_audits,
    list_pending_taxonomy_proposals,
)
from .taxonomy_runner import TaxonomyRuntimeError, enqueue_generation_for_audit
from .security import (
    ACCESS_JWT_HEADER,
    AccessTokenRejected,
    AccessVerificationUnavailable,
    SecurityConfigurationError,
    browser_mutation_is_valid,
    is_machine_request,
    is_public_request,
    machine_identity_is_allowed,
    security_config_from_env,
    validate_access_token,
)
from .status import readiness_status, service_status


@asynccontextmanager
async def lifespan(_app: FastAPI):
    ensure_schema()
    yield


app = FastAPI(
    title="Personal OS Paper API",
    version=__version__,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)


class ReadingStatePatch(BaseModel):
    """A revision-checked partial update from one reader tab."""

    model_config = ConfigDict(extra="forbid", strict=True)

    expected_revision: int = Field(ge=0)
    status: Optional[
        Literal["unread", "reading", "completed", "on_hold", "rereading"]
    ] = None
    progress: Optional[float] = Field(default=None, ge=0, le=1)
    anchor: Optional[dict[str, Any]] = None
    article_fingerprint: Optional[str] = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    favorite: Optional[bool] = None
    priority: Optional[int] = Field(default=None, ge=0, le=5)


class ReaderIssueCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: Optional[UUID] = None
    kind: Literal[
        "extraction_text",
        "sentence_break",
        "section_structure",
        "figure_crop",
        "table",
        "metadata",
        "other",
    ]
    description: Optional[str] = Field(default=None, max_length=4000)
    anchor: dict[str, Any]
    article_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReaderIssuePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    description: Optional[str] = Field(default=None, max_length=4000)
    status: Optional[Literal["open", "resolved", "dismissed"]] = None
    anchor: Optional[dict[str, Any]] = None
    article_fingerprint: Optional[str] = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )


class AnnotationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    expected_revision: int = Field(default=0, ge=0, le=0)
    kind: Literal["highlight", "note", "question"]
    anchor: dict[str, Any]
    article_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    body: Optional[str] = Field(default=None, max_length=20000)
    category: Optional[str] = Field(default=None, max_length=80)
    question_status: Optional[Literal["open", "answered", "dismissed"]] = None
    ai_share_allowed: StrictBool = False


class AnnotationPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    anchor: Optional[dict[str, Any]] = None
    article_fingerprint: Optional[str] = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    body: Optional[str] = Field(default=None, max_length=20000)
    category: Optional[str] = Field(default=None, max_length=80)
    question_status: Optional[Literal["open", "answered", "dismissed"]] = None
    ai_share_allowed: Optional[StrictBool] = None


class RevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_revision: int = Field(ge=1)


class KnowledgeGenerationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: UUID
    include_personal_annotations: StrictBool = False
    acknowledge_paper_text_and_cost: StrictBool


class PaperQuestionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: UUID
    question: str = Field(min_length=1, max_length=4000)
    include_personal_annotations: StrictBool = False


class KnowledgeReviewAction(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_review_revision: int = Field(ge=0)
    comment: Optional[str] = Field(default=None, max_length=4000)


class OutboxLeaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    consumer_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
    lease_seconds: int = Field(default=300, ge=30, le=3600)


class OutboxAckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    consumer_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
    lease_token: UUID
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class OutboxNackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    consumer_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
    lease_token: UUID
    error_code: str = Field(pattern=r"^[a-z0-9][a-z0-9_:-]{0,119}$")
    retry_delay_seconds: int = Field(default=300, ge=30, le=86400)


class AdminTaxonomyGenerationAction(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    acknowledge_metadata_and_cost: StrictBool


class AdminTaxonomyReviewAction(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_revision: int = Field(ge=0)
    confirm_apply: StrictBool = False
    comment: Optional[str] = Field(default=None, max_length=4000)


class AdminOperationReviewAction(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_revision: int = Field(ge=0)
    confirm_execution: StrictBool = False
    comment: Optional[str] = Field(default=None, max_length=4000)


class AdminReprocessCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    paper_ids: list[Annotated[int, Field(ge=1)]] = Field(
        min_length=1, max_length=50
    )
    reason_code: str = Field(
        default="reader_quality_fix", pattern=r"^[a-z][a-z0-9_]{0,119}$"
    )
    confirm_selected_reprocess: StrictBool


class AdminRollbackTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    paper_id: int = Field(ge=1)
    rollback_to_version_id: str = Field(pattern=r"^rv-[0-9a-f]{32}$")


class AdminRollbackCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    targets: list[AdminRollbackTarget] = Field(min_length=1, max_length=20)
    reason_code: str = Field(
        default="manual_rollback", pattern=r"^[a-z][a-z0-9_]{0,119}$"
    )
    confirm_selected_rollback: StrictBool


def _writes_enabled() -> bool:
    value = os.environ.get("PAPER_WRITES_ENABLED", "true").strip().casefold()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return False


def _require_writes() -> None:
    if not _writes_enabled():
        raise HTTPException(status_code=503, detail="paper writes are disabled")


def _explicit_updates(payload: BaseModel, *, exclude: set[str]) -> dict[str, Any]:
    return {
        name: getattr(payload, name)
        for name in payload.model_fields_set
        if name not in exclude
    }


def _default_reading_state(paper_id: int) -> dict[str, Any]:
    return {
        "paper_id": paper_id,
        "status": "unread",
        "progress": 0.0,
        "anchor": None,
        "article_fingerprint": None,
        "favorite": False,
        "priority": 0,
        "created_at": None,
        "updated_at": None,
        "completed_at": None,
        "revision": 0,
    }


def _conflict_detail(code: str, current: dict[str, Any] | None) -> dict[str, Any]:
    """Keep optimistic-lock conflicts JSON-safe, including database timestamps."""

    return {"code": code, "current": jsonable_encoder(current)}


@app.middleware("http")
async def private_response_headers(request: Request, call_next):
    response: Response
    try:
        security = security_config_from_env()
        public_request = is_public_request(request.method, request.url.path)
        machine_request = is_machine_request(request.url.path)
        identity: dict[str, Any] | None = None
        if security.access_enforced and not public_request:
            token = request.headers.get(ACCESS_JWT_HEADER, "")
            if not token:
                raise AccessTokenRejected("Access token is missing")
            identity = await run_in_threadpool(validate_access_token, token, security)
            request.state.access_identity = identity
            service_identity = identity.get("kind") == "service"
            if (service_identity and not machine_request) or (
                machine_request
                and not machine_identity_is_allowed(identity, security)
            ):
                response = JSONResponse(
                    {"detail": "request identity is not authorized"}, status_code=403
                )
            else:
                response = None
        else:
            response = None
        service_machine_request = bool(
            identity
            and identity.get("kind") == "service"
            and machine_request
        )
        browser_action_invalid = (
            response is None
            and not public_request
            and not service_machine_request
            and not browser_mutation_is_valid(
                request.method, request.headers, security
            )
        )
        if browser_action_invalid:
            response = JSONResponse(
                {"detail": "browser action verification failed"}, status_code=403
            )
        elif response is None:
            response = await call_next(request)
    except AccessTokenRejected:
        response = JSONResponse(
            {"detail": "authentication required"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
    except (AccessVerificationUnavailable, SecurityConfigurationError):
        response = JSONResponse(
            {"detail": "authentication service unavailable"}, status_code=503
        )

    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Cache-Control"] = "private, no-store"
    if response.headers.get("content-type", "").startswith("text/html"):
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'unsafe-inline'; "
            "script-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'"
        )
    return response


def _data_dir() -> Path:
    return Path(os.environ.get("PAPER_DATA_DIR", "/data"))


def _human_actor(request: Request) -> str:
    identity = getattr(request.state, "access_identity", None)
    if not isinstance(identity, dict) or identity.get("kind") != "human":
        raise HTTPException(status_code=403, detail="human review identity required")
    actor_id = identity.get("sub")
    if not isinstance(actor_id, str) or not actor_id.strip():
        raise HTTPException(status_code=403, detail="human review identity required")
    return actor_id.strip()


def _outbox_worker_id(consumer_id: str) -> str:
    return f"vault:{consumer_id}"


def _public_qa_job(job: dict[str, Any]) -> dict[str, Any]:
    """Expose answer state without leases, prompts, or evidence payloads."""

    allowed = (
        "id",
        "paper_id",
        "question",
        "provider",
        "model",
        "include_personal_annotations",
        "status",
        "attempt_count",
        "max_attempts",
        "last_error_code",
        "requested_at",
        "started_at",
        "finished_at",
        "answer_payload",
    )
    return {name: job.get(name) for name in allowed}


def _public_knowledge_job(job: dict[str, Any]) -> dict[str, Any]:
    """Expose knowledge-generation state without request identity or leases."""

    allowed = (
        "id",
        "paper_id",
        "provider",
        "model",
        "include_personal_annotations",
        "status",
        "attempt_count",
        "max_attempts",
        "last_error_code",
        "requested_at",
        "started_at",
        "finished_at",
        "result_revision_id",
    )
    return {name: job.get(name) for name in allowed}


_OPERATION_LIST_FIELDS = (
    "request_kind",
    "request_id",
    "scope",
    "reason_code",
    "requested_at",
    "target_count",
    "review_revision",
    "decision",
    "job_count",
    "active_job_count",
    "succeeded_job_count",
    "failed_job_count",
    "state",
)


def _utc_now_text() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _public_operation_item(item: dict[str, Any]) -> dict[str, Any]:
    return {field: item.get(field) for field in _OPERATION_LIST_FIELDS}


def _public_operation_failure(item: dict[str, Any]) -> dict[str, Any]:
    allowed = (
        "operation_kind",
        "operation_id",
        "paper_id",
        "status",
        "error_code",
        "attempt_count",
        "occurred_at",
    )
    return {field: item.get(field) for field in allowed}


def _public_operation_detail(item: dict[str, Any]) -> dict[str, Any]:
    target_fields = (
        "paper_id",
        "expected_source_sha256",
        "expected_current_version_id",
        "rollback_to_version_id",
    )
    state_fields = (
        "paper_id",
        "status",
        "attempt_count",
        "error_code",
        "result_version_id",
    )
    return {
        "request_kind": item.get("request_kind"),
        "request_id": item.get("request_id"),
        "scope": item.get("scope"),
        "reason_code": item.get("reason_code"),
        "requested_at": item.get("requested_at"),
        "target_count": item.get("target_count"),
        "review_revision": item.get("review_revision"),
        "decision": item.get("decision"),
        "global_confirmed": item.get("global_confirmed"),
        "targets": [
            {field: target.get(field) for field in target_fields if field in target}
            for target in item.get("targets", ())
        ],
        "target_states": [
            {field: state.get(field) for field in state_fields}
            for state in item.get("target_states", ())
        ],
    }


def _public_operations_summary(item: dict[str, Any]) -> dict[str, int | None]:
    fields = (
        "expected_workers",
        "observed_workers",
        "open_incidents",
        "pending_webhooks",
        "dead_webhooks",
        "reprocess_pending_review",
        "reprocess_active",
        "reprocess_failed",
        "rollback_pending_review",
        "rollback_active",
        "rollback_failed",
        "knowledge_active",
        "knowledge_failed",
        "qa_active",
        "qa_failed",
        "taxonomy_generation_pending",
        "taxonomy_generation_active",
        "taxonomy_generation_failed",
        "taxonomy_move_pending",
        "taxonomy_move_active",
        "taxonomy_move_failed",
    )
    return {field: item.get(field) for field in fields}


def _public_cost_summary(item: dict[str, Any]) -> dict[str, int | None]:
    fields = (
        "attempt_count",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "known_cost_microusd",
        "unknown_cost_attempts",
    )
    return {field: item.get(field) for field in fields}


def _public_taxonomy_audit(item: dict[str, Any]) -> dict[str, Any]:
    generation = item.get("generation") or {}
    generation_fields = (
        "requested",
        "status",
        "job_id",
        "provider",
        "model",
        "request_digest",
        "requested_at",
    )
    metric_fields = (
        "paper_count",
        "new_paper_count",
        "days_since_checkpoint",
        "max_leaf_size",
        "leaf_count",
        "overcrowding_ratio",
        "heterogeneity",
    )
    policy_fields = (
        "min_category_papers",
        "min_new_papers",
        "max_audit_age_days",
        "max_leaf_papers",
        "heterogeneity_threshold",
        "min_subtheme_size",
        "max_subthemes",
        "max_other_fraction",
    )
    metrics = item.get("metrics") or {}
    policy = item.get("policy") or {}
    return {
        "audit_id": item.get("audit_id"),
        "category": item.get("category"),
        "source_snapshot_sha256": item.get("source_snapshot_sha256"),
        "paper_count": item.get("paper_count"),
        "new_paper_count": item.get("new_paper_count"),
        "metrics": {field: metrics.get(field) for field in metric_fields},
        "policy": {field: policy.get(field) for field in policy_fields},
        "policy_digest": item.get("policy_digest"),
        "reason_codes": list(item.get("reason_codes") or ()),
        "evaluated_at": item.get("evaluated_at"),
        "created_at": item.get("created_at"),
        "generation": {
            field: generation.get(field)
            for field in generation_fields
            if field in generation
        },
    }


def _public_taxonomy_proposal(item: dict[str, Any]) -> dict[str, Any]:
    provenance = item.get("provenance") or {}
    provenance_fields = (
        "audit_id",
        "generation_job_id",
        "generation_status",
        "provider",
        "model",
        "source_snapshot_sha256",
        "request_digest",
        "proposal_payload_sha256",
    )
    return {
        "proposal_id": item.get("proposal_id"),
        "category": item.get("category"),
        "assignment_count": item.get("assignment_count"),
        "subtheme_count": item.get("subtheme_count"),
        "review_revision": item.get("review_revision"),
        "review_status": item.get("review_status"),
        "created_at": item.get("created_at"),
        "provenance": {
            field: provenance.get(field) for field in provenance_fields
        },
    }


def _public_taxonomy_proposal_detail(item: dict[str, Any]) -> dict[str, Any]:
    public = _public_taxonomy_proposal(item)
    review = item.get("review") or {}
    public.update(
        {
            "schema": item.get("schema"),
            "schema_version": item.get("schema_version"),
            "rationale": item.get("rationale"),
            "subthemes": [
                {
                    "name": subtheme.get("name"),
                    "description": subtheme.get("description"),
                    "assignment_count": subtheme.get("assignment_count"),
                }
                for subtheme in item.get("subthemes", ())
            ],
            "assignments": [
                {
                    "paper_id": assignment.get("paper_id"),
                    "source_sha256": assignment.get("source_sha256"),
                    "subtheme": assignment.get("subtheme"),
                    "move_required": assignment.get("move_required"),
                    "move_job_id": assignment.get("move_job_id"),
                    "idempotency_key_digest": assignment.get(
                        "idempotency_key_digest"
                    ),
                }
                for assignment in item.get("assignments", ())
            ],
            "assignments_complete": item.get("assignments_complete"),
            "review": {
                field: review.get(field)
                for field in (
                    "status",
                    "revision",
                    "event_id",
                    "decision",
                    "updated_at",
                    "reviewed_at",
                )
                if field in review
            },
            "audit_evaluated_at": item.get("audit_evaluated_at"),
            "generation_finished_at": item.get("generation_finished_at"),
        }
    )
    return public


def _public_health_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "service_id": snapshot.get("service_id"),
        "evaluated_at": snapshot.get("evaluated_at"),
        "status": snapshot.get("status"),
        "reason_codes": list(snapshot.get("reason_codes") or ()),
        "expected_worker_count": snapshot.get("expected_worker_count"),
        "observed_worker_count": snapshot.get("observed_worker_count"),
        "affected_worker_count": snapshot.get("affected_worker_count"),
        "workers": [
            {
                "worker_id": worker.get("worker_id"),
                "state": worker.get("state"),
                "age_seconds": worker.get("age_seconds"),
                "affected": worker.get("affected"),
            }
            for worker in snapshot.get("workers", ())
        ],
    }


def _public_admin_paper_choice(item: dict[str, Any]) -> dict[str, Any]:
    return {
        field: item.get(field)
        for field in (
            "paper_id",
            "title",
            "category",
            "publication_year",
            "reader_status",
        )
    }


def _admin_health_stale_seconds() -> int:
    raw = os.environ.get("PAPER_OPS_RUNTIME_STALE_AFTER_SECONDS", "120")
    if not re.fullmatch(r"0|[1-9][0-9]*", raw):
        raise ValueError("admin_health_stale_invalid")
    value = int(raw)
    if not 10 <= value <= 86_400:
        raise ValueError("admin_health_stale_invalid")
    return value


def _admin_health_overview(evaluated_at: str) -> list[dict[str, Any]]:
    stale_after = _admin_health_stale_seconds()
    output: list[dict[str, Any]] = []
    for service_id in list_expected_worker_services(limit=16):
        try:
            snapshot = get_worker_health_snapshot(
                evaluated_at=evaluated_at,
                stale_after_seconds=stale_after,
                service_id=service_id,
            )
        except Exception:
            output.append(
                {
                    "service_id": service_id,
                    "evaluated_at": evaluated_at,
                    "status": "unavailable",
                    "reason_codes": ["health_query_failed"],
                    "expected_worker_count": 0,
                    "observed_worker_count": 0,
                    "affected_worker_count": 0,
                    "workers": [],
                }
            )
        else:
            output.append(_public_health_snapshot(snapshot))
    return output


def _paper_source_binding(paper_id: int) -> dict[str, Any]:
    try:
        binding = get_admin_paper_source_binding(paper_id)
    except AdminQueryError:
        raise HTTPException(
            status_code=409, detail="paper_source_binding_invalid"
        ) from None
    if binding is None:
        raise HTTPException(status_code=404, detail="paper_not_found")
    return binding


def _reprocess_operation_target(paper_id: int) -> dict[str, Any]:
    """Bind legacy or versioned Reader v2 state for safe selective upgrade."""

    binding = _paper_source_binding(paper_id)
    try:
        current = resolve_current_artifact(
            _data_dir(), paper_id, allow_legacy_v2=True
        )
    except ArtifactVersionError:
        raise HTTPException(
            status_code=409, detail="reader_artifact_required"
        ) from None
    descriptor = current.descriptor
    source_sha = binding["source_sha256"]
    if (
        current.mode not in {"legacy_v2", "versioned"}
        or not isinstance(source_sha, str)
        or not re.fullmatch(r"[0-9a-f]{64}", source_sha)
        or (
            descriptor is not None
            and descriptor.get("source_sha256") != source_sha
        )
    ):
        raise HTTPException(
            status_code=409, detail="current_artifact_invalid"
        )
    return {
        "paper_id": paper_id,
        "expected_source_sha256": source_sha,
        "expected_current_version_id": current.version_id,
    }


def _rollback_operation_target(paper_id: int) -> dict[str, Any]:
    """Require an integrity-checked versioned current pointer for rollback."""

    target = _reprocess_operation_target(paper_id)
    if target["expected_current_version_id"] is None:
        raise HTTPException(
            status_code=409, detail="versioned_artifact_required"
        )
    return target


def _rollback_version_choices(paper_id: int) -> dict[str, Any]:
    current = _rollback_operation_target(paper_id)
    try:
        plan = plan_retention(_data_dir(), paper_id, keep=100)
    except ArtifactVersionError:
        raise HTTPException(
            status_code=409, detail="artifact_versions_unavailable"
        ) from None
    active = current["expected_current_version_id"]
    retained = plan.get("retain_version_ids")
    if not isinstance(retained, list) or any(
        not isinstance(item, str)
        or not re.fullmatch(r"rv-[0-9a-f]{32}", item)
        for item in retained
    ):
        raise HTTPException(status_code=409, detail="artifact_versions_invalid")
    return {
        "paper_id": paper_id,
        "active_version_id": active,
        "rollback_version_ids": [item for item in retained if item != active],
    }


def _public_created_operation(stored: dict[str, Any]) -> dict[str, Any]:
    request = stored.get("request") or {}
    targets = stored.get("targets") or request.get("targets") or ()
    return {
        "request_id": stored.get("request_id"),
        "scope": request.get("scope", "rollback"),
        "reason_code": request.get("reason_code"),
        "requested_at": request.get("requested_at"),
        "target_count": len(targets),
        "review_revision": stored.get("review_revision"),
        "decision": stored.get("decision"),
    }


def _public_generation_job(job: dict[str, Any]) -> dict[str, Any]:
    return {
        field: job.get(field)
        for field in (
            "id",
            "audit_id",
            "proposal_id",
            "provider",
            "model",
            "status",
            "attempt_count",
            "max_attempts",
            "requested_at",
        )
    }


@app.get("/static/reader-v2.js", include_in_schema=False)
def reader_v2_controller() -> FileResponse:
    path = Path(__file__).with_name("static") / "reader-v2.js"
    if not path.is_file():
        raise HTTPException(status_code=503)
    return FileResponse(path, media_type="text/javascript; charset=utf-8")


@app.get("/static/knowledge.js", include_in_schema=False)
def knowledge_controller() -> FileResponse:
    path = Path(__file__).with_name("static") / "knowledge.js"
    if not path.is_file():
        raise HTTPException(status_code=503)
    return FileResponse(path, media_type="text/javascript; charset=utf-8")


@app.get("/static/search.js", include_in_schema=False)
def search_controller() -> FileResponse:
    path = Path(__file__).with_name("static") / "search.js"
    if not path.is_file():
        raise HTTPException(status_code=503)
    return FileResponse(path, media_type="text/javascript; charset=utf-8")


@app.get("/static/admin.js", include_in_schema=False)
def admin_controller() -> FileResponse:
    path = Path(__file__).with_name("static") / "admin.js"
    if not path.is_file():
        raise HTTPException(status_code=503)
    return FileResponse(path, media_type="text/javascript; charset=utf-8")


def _reader_asset(paper_id: int, asset_name: str) -> Path:
    if not re.fullmatch(r"page-\d{3}\.jpg", asset_name):
        raise HTTPException(status_code=404)
    paper = get_reader_paper(paper_id)
    if not paper or paper.get("reader_status") != "ready":
        raise HTTPException(status_code=404)
    manifest_path = PurePosixPath(str(paper["reader_manifest_path"]))
    root = (_data_dir() / "library" / "mobile").resolve()
    path = (root / manifest_path.parent / "assets" / asset_name).resolve()
    if root not in path.parents or not path.is_file():
        raise HTTPException(status_code=404)
    return path


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse("/library", status_code=307)


@app.get("/library", response_class=HTMLResponse)
def library() -> HTMLResponse:
    return HTMLResponse(render_library(list_reader_papers()))


@app.get("/search", response_class=HTMLResponse)
def paper_search_page() -> HTMLResponse:
    return HTMLResponse(render_search_page())


@app.get("/admin", response_class=HTMLResponse)
def paper_admin_page() -> HTMLResponse:
    return HTMLResponse(render_admin_page())


@app.get("/read/{paper_id}", response_class=HTMLResponse)
def read_paper(paper_id: int) -> HTMLResponse:
    paper = get_reader_paper(paper_id)
    if not paper:
        raise HTTPException(status_code=404)
    if paper.get("reader_status") != "ready" or not paper.get(
        "reader_manifest_path"
    ):
        return HTMLResponse(render_library([paper]), status_code=202)
    try:
        manifest = read_manifest(_data_dir(), str(paper["reader_manifest_path"]))
    except ReaderFailure:
        raise HTTPException(status_code=503) from None
    return HTMLResponse(render_reader(paper, manifest))


@app.get("/read/{paper_id}/v2", response_class=HTMLResponse)
def read_paper_v2(paper_id: int) -> HTMLResponse:
    paper = get_reader_paper(paper_id)
    if not paper:
        raise HTTPException(status_code=404)
    try:
        article = read_article(_data_dir(), paper_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404) from None
    except (ValueError, ReaderFailure):
        raise HTTPException(status_code=503) from None
    return HTMLResponse(render_reader_v2(paper, article))


@app.get("/knowledge/{paper_id}", response_class=HTMLResponse)
def knowledge_review_page(paper_id: int) -> HTMLResponse:
    paper = get_reader_paper(paper_id)
    if not paper:
        raise HTTPException(status_code=404)
    return HTMLResponse(render_knowledge_page(paper))


@app.get("/read/{paper_id}/v2/assets/{asset_name}")
def reader_v2_asset(paper_id: int, asset_name: str) -> FileResponse:
    if not get_reader_paper(paper_id):
        raise HTTPException(status_code=404)
    try:
        path = v2_asset_path(_data_dir(), paper_id, asset_name)
    except FileNotFoundError:
        raise HTTPException(status_code=404) from None
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(path, media_type=media_type)


@app.get("/read/{paper_id}/assets/{asset_name}")
def reader_asset(paper_id: int, asset_name: str) -> FileResponse:
    return FileResponse(_reader_asset(paper_id, asset_name), media_type="image/jpeg")


@app.get("/read/{paper_id}/source.pdf")
def reader_source(paper_id: int) -> FileResponse:
    paper = get_reader_paper(paper_id)
    if not paper:
        raise HTTPException(status_code=404)
    relative = PurePosixPath(str(paper["stored_path"]))
    if relative.is_absolute() or ".." in relative.parts:
        raise HTTPException(status_code=404)
    root = (_data_dir() / "library" / "raw").resolve()
    path = (root / relative).resolve()
    if root not in path.parents or not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(
        path,
        media_type="application/pdf",
        filename="paper.pdf",
        content_disposition_type="inline",
    )


@app.get("/api/papers/{paper_id}/reading-state")
def paper_reading_state(paper_id: int) -> dict[str, Any]:
    if not get_reader_paper(paper_id):
        raise HTTPException(status_code=404)
    return get_reading_state(paper_id) or _default_reading_state(paper_id)


@app.patch("/api/papers/{paper_id}/reading-state")
def patch_paper_reading_state(
    paper_id: int, payload: ReadingStatePatch
) -> dict[str, Any]:
    _require_writes()
    updates = _explicit_updates(payload, exclude={"expected_revision"})
    if not updates:
        raise HTTPException(status_code=422, detail="at least one state field is required")
    try:
        return update_reading_state(
            paper_id,
            expected_revision=payload.expected_revision,
            **updates,
        )
    except PaperNotFound:
        raise HTTPException(status_code=404) from None
    except ReadingStateConflict as exc:
        raise HTTPException(
            status_code=409,
            detail=_conflict_detail("revision_conflict", exc.current_state),
        ) from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@app.get("/api/papers/{paper_id}/issues")
def paper_issues(
    paper_id: int,
    issue_status: Optional[Literal["open", "resolved", "dismissed"]] = None,
) -> dict[str, Any]:
    if not get_reader_paper(paper_id):
        raise HTTPException(status_code=404)
    return {"issues": list_reader_issues(paper_id, status=issue_status)}


@app.post(
    "/api/papers/{paper_id}/issues", status_code=status.HTTP_201_CREATED
)
def post_paper_issue(paper_id: int, payload: ReaderIssueCreate) -> dict[str, Any]:
    _require_writes()
    try:
        return create_reader_issue(
            paper_id,
            issue_id=payload.id,
            kind=payload.kind,
            description=payload.description,
            anchor=payload.anchor,
            article_fingerprint=payload.article_fingerprint,
            source="user",
        )
    except PaperNotFound:
        raise HTTPException(status_code=404) from None
    except ReaderIssueConflict as exc:
        raise HTTPException(
            status_code=409,
            detail=_conflict_detail("issue_conflict", exc.current_issue),
        ) from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@app.patch("/api/issues/{issue_id}")
def patch_paper_issue(issue_id: UUID, payload: ReaderIssuePatch) -> dict[str, Any]:
    _require_writes()
    updates = _explicit_updates(payload, exclude={"expected_revision"})
    if not updates:
        raise HTTPException(status_code=422, detail="at least one issue field is required")
    try:
        return update_reader_issue(
            issue_id, expected_revision=payload.expected_revision, **updates
        )
    except ReaderIssueNotFound:
        raise HTTPException(status_code=404) from None
    except ReaderIssueConflict as exc:
        raise HTTPException(
            status_code=409,
            detail=_conflict_detail("issue_conflict", exc.current_issue),
        ) from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@app.get("/api/papers/{paper_id}/annotations")
def paper_annotations(
    paper_id: int,
    kind: Optional[Literal["highlight", "note", "question"]] = None,
) -> dict[str, Any]:
    if not get_reader_paper(paper_id):
        raise HTTPException(status_code=404)
    return {"annotations": list_annotations(paper_id, kind=kind)}


@app.post(
    "/api/papers/{paper_id}/annotations", status_code=status.HTTP_201_CREATED
)
def post_paper_annotation(
    paper_id: int, payload: AnnotationCreate
) -> dict[str, Any]:
    _require_writes()
    try:
        return create_annotation(
            payload.id,
            paper_id,
            expected_revision=payload.expected_revision,
            kind=payload.kind,
            anchor=payload.anchor,
            article_fingerprint=payload.article_fingerprint,
            body=payload.body,
            category=payload.category,
            question_status=payload.question_status,
            ai_share_allowed=payload.ai_share_allowed,
        )
    except PaperNotFound:
        raise HTTPException(status_code=404) from None
    except AnnotationConflict as exc:
        raise HTTPException(
            status_code=409,
            detail=_conflict_detail("annotation_conflict", exc.current_annotation),
        ) from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@app.patch("/api/annotations/{annotation_id}")
def patch_paper_annotation(
    annotation_id: UUID, payload: AnnotationPatch
) -> dict[str, Any]:
    _require_writes()
    updates = _explicit_updates(payload, exclude={"expected_revision"})
    if not updates:
        raise HTTPException(
            status_code=422, detail="at least one annotation field is required"
        )
    try:
        return update_annotation(
            annotation_id, expected_revision=payload.expected_revision, **updates
        )
    except AnnotationNotFound:
        raise HTTPException(status_code=404) from None
    except AnnotationTrashed:
        raise HTTPException(status_code=410) from None
    except AnnotationConflict as exc:
        raise HTTPException(
            status_code=409,
            detail=_conflict_detail("annotation_conflict", exc.current_annotation),
        ) from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@app.post("/api/annotations/{annotation_id}/trash")
def post_trash_annotation(
    annotation_id: UUID, payload: RevisionRequest
) -> dict[str, Any]:
    _require_writes()
    try:
        return trash_annotation(
            annotation_id, expected_revision=payload.expected_revision
        )
    except AnnotationNotFound:
        raise HTTPException(status_code=404) from None
    except AnnotationConflict as exc:
        raise HTTPException(
            status_code=409,
            detail=_conflict_detail("annotation_conflict", exc.current_annotation),
        ) from None


@app.get("/api/search")
def search_library(
    q: str,
    paper_id: Optional[int] = None,
    limit: int = 20,
    include_personal_annotations: bool = True,
) -> dict[str, Any]:
    try:
        hits = search_papers(
            q,
            paper_id=paper_id,
            limit=limit,
            include_personal_annotations=include_personal_annotations,
        )
    except SearchStoreError as exc:
        raise HTTPException(status_code=422, detail=exc.code) from None
    titles = {
        int(paper["id"]): str(paper.get("title") or "Untitled Paper")
        for paper in list_reader_papers()
    }
    values: list[dict[str, Any]] = []
    for hit in hits:
        if hit.paper_id is None:
            raise HTTPException(status_code=503, detail="search result is incomplete")
        values.append(
            {
                **hit.to_dict(),
                "paper_id": hit.paper_id,
                "paper_title": titles.get(hit.paper_id, "Untitled Paper"),
            }
        )
    return {"query": q.strip(), "hits": values}


@app.post(
    "/api/papers/{paper_id}/questions",
    status_code=status.HTTP_202_ACCEPTED,
)
def post_paper_question(
    paper_id: int, payload: PaperQuestionCreate, request: Request
) -> dict[str, Any]:
    _require_writes()
    actor_id = _human_actor(request)
    if not get_reader_paper(paper_id):
        raise HTTPException(status_code=404)
    model = os.environ.get("PAPER_QA_MODEL", "deepseek-v4-flash").strip()
    if model != "deepseek-v4-flash":
        raise HTTPException(status_code=503, detail="question model is not configured")
    try:
        job = enqueue_qa_job(
            paper_id,
            payload.question,
            idempotency_key=str(payload.idempotency_key),
            provider="deepseek",
            model=model,
            request_actor_kind="human",
            request_actor_id=actor_id,
            request_acknowledged=True,
            include_personal_annotations=payload.include_personal_annotations,
            evidence_limit=32,
            max_attempts=3,
        )
    except SearchIndexNotReady as exc:
        raise HTTPException(status_code=409, detail=exc.code) from None
    except SearchStoreError as exc:
        raise HTTPException(status_code=422, detail=exc.code) from None
    return {"job": _public_qa_job(job)}


@app.get("/api/papers/{paper_id}/questions/{job_id}")
def paper_question(paper_id: int, job_id: UUID) -> dict[str, Any]:
    try:
        job = get_qa_job(job_id)
    except SearchStoreError as exc:
        status_code = 404 if exc.code == "qa_job_missing" else 503
        raise HTTPException(status_code=status_code, detail=exc.code) from None
    if int(job["paper_id"]) != paper_id:
        raise HTTPException(status_code=404)
    return {"job": _public_qa_job(job)}


@app.get("/api/papers/{paper_id}/knowledge")
def paper_knowledge(paper_id: int) -> dict[str, Any]:
    try:
        return knowledge_overview(paper_id)
    except KnowledgeQueryNotFound:
        raise HTTPException(status_code=404) from None
    except KnowledgeQueryError:
        raise HTTPException(status_code=503, detail="knowledge state is unavailable") from None


@app.post(
    "/api/papers/{paper_id}/knowledge/jobs",
    status_code=status.HTTP_202_ACCEPTED,
)
def post_paper_knowledge_job(
    paper_id: int, payload: KnowledgeGenerationCreate, request: Request
) -> dict[str, Any]:
    _require_writes()
    actor_id = _human_actor(request)
    if payload.acknowledge_paper_text_and_cost is not True:
        raise HTTPException(
            status_code=422,
            detail="paper_text_and_cost_acknowledgement_required",
        )
    if not get_reader_paper(paper_id):
        raise HTTPException(status_code=404)
    try:
        read_article(_data_dir(), paper_id)
    except FileNotFoundError:
        raise HTTPException(status_code=409, detail="mobile article is not ready") from None
    except (ValueError, ReaderFailure):
        raise HTTPException(status_code=503, detail="mobile article is unavailable") from None
    model = os.environ.get("PAPER_KNOWLEDGE_MODEL", "deepseek-v4-pro").strip()
    if model != "deepseek-v4-pro":
        raise HTTPException(status_code=503, detail="knowledge model is not configured")
    try:
        job = enqueue_generation_job(
            paper_id,
            idempotency_key=str(payload.idempotency_key),
            provider="deepseek",
            model=model,
            request_actor_kind="human",
            request_actor_id=actor_id,
            paper_text_and_cost_acknowledged=True,
            policy=GenerationPolicy(
                include_personal_annotations=payload.include_personal_annotations
            ),
        )
        return {"job": _public_knowledge_job(job)}
    except KnowledgeStoreError:
        raise HTTPException(status_code=409, detail="knowledge job could not be queued") from None


@app.post("/api/knowledge/revisions/{revision_id}/approve")
def post_approve_knowledge_revision(
    revision_id: UUID,
    payload: KnowledgeReviewAction,
    request: Request,
) -> dict[str, Any]:
    _require_writes()
    actor_id = _human_actor(request)
    try:
        reviewed_payload = make_reviewed_copy(revision_id)
        return approve_draft_revision(
            revision_id,
            reviewed_payload,
            expected_review_revision=payload.expected_review_revision,
            actor_kind="human",
            actor_id=actor_id,
            comment=payload.comment,
            data_dir=_data_dir(),
        )
    except KnowledgeQueryNotFound:
        raise HTTPException(status_code=404) from None
    except KnowledgeRevisionConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "review_revision_conflict",
                "current_revision": exc.current_revision,
            },
        ) from None
    except (KnowledgeQueryError, KnowledgeStoreError):
        raise HTTPException(status_code=409, detail="knowledge approval rejected") from None


@app.post("/api/knowledge/revisions/{revision_id}/reject")
def post_reject_knowledge_revision(
    revision_id: UUID,
    payload: KnowledgeReviewAction,
    request: Request,
) -> dict[str, Any]:
    _require_writes()
    actor_id = _human_actor(request)
    try:
        return reject_draft_revision(
            revision_id,
            expected_review_revision=payload.expected_review_revision,
            actor_kind="human",
            actor_id=actor_id,
            comment=payload.comment,
        )
    except KnowledgeRevisionConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "review_revision_conflict",
                "current_revision": exc.current_revision,
            },
        ) from None
    except KnowledgeStoreError:
        raise HTTPException(status_code=409, detail="knowledge rejection rejected") from None


@app.post("/api/knowledge/outbox/lease")
def post_knowledge_outbox_lease(payload: OutboxLeaseRequest) -> dict[str, Any]:
    item = claim_outbox_item(
        worker_id=_outbox_worker_id(payload.consumer_id),
        lease_seconds=payload.lease_seconds,
    )
    if item is None:
        return {"item": None}
    return {
        "item": {
            "outbox_id": item["id"],
            "paper_id": item["paper_id"],
            "revision_id": item["revision_id"],
            "payload_sha256": str(item["revision_payload_sha256"]).strip(),
            "lease_token": item["lease_token"],
            "lease_expires_at": item["lease_expires_at"],
            "payload": item["canonical_payload"],
        }
    }


@app.post("/api/knowledge/outbox/{outbox_id}/ack")
def post_knowledge_outbox_ack(
    outbox_id: UUID, payload: OutboxAckRequest
) -> dict[str, str]:
    try:
        ack_outbox_item(
            outbox_id,
            worker_id=_outbox_worker_id(payload.consumer_id),
            lease_token=payload.lease_token,
            expected_payload_sha256=payload.payload_sha256,
        )
    except KnowledgeLeaseLost:
        raise HTTPException(status_code=409, detail="outbox lease lost") from None
    return {"status": "delivered"}


@app.post("/api/knowledge/outbox/{outbox_id}/nack")
def post_knowledge_outbox_nack(
    outbox_id: UUID, payload: OutboxNackRequest
) -> dict[str, str]:
    try:
        item = nack_outbox_item(
            outbox_id,
            worker_id=_outbox_worker_id(payload.consumer_id),
            lease_token=payload.lease_token,
            error_code=payload.error_code,
            retry_delay_seconds=payload.retry_delay_seconds,
        )
    except KnowledgeLeaseLost:
        raise HTTPException(status_code=409, detail="outbox lease lost") from None
    return {"status": str(item["status"])}


@app.get("/api/admin/overview")
def admin_overview() -> dict[str, Any]:
    """Return failure-isolated, allowlisted state for the private admin page."""

    generated_at = _utc_now_text()
    section_errors: list[str] = []

    def read(name: str, function, fallback):
        try:
            return function()
        except Exception:
            section_errors.append(f"{name}_unavailable")
            return fallback

    operations = read(
        "operations",
        lambda: _public_operations_summary(get_operations_summary()),
        None,
    )
    cost = read(
        "cost", lambda: _public_cost_summary(get_cost_summary()), None
    )
    failures = read(
        "failures",
        lambda: [
            _public_operation_failure(item)
            for item in list_operation_failures(limit=30)
        ],
        [],
    )
    pending = read(
        "pending_operations",
        lambda: [
            _public_operation_item(item)
            for item in list_pending_operation_reviews(limit=30)
        ],
        [],
    )
    recent = read(
        "recent_operations",
        lambda: [
            _public_operation_item(item)
            for item in list_recent_operation_requests(limit=30)
        ],
        [],
    )
    health = read(
        "health",
        lambda: [
            _public_health_snapshot(item)
            for item in _admin_health_overview(generated_at)
        ],
        [],
    )
    audits = read(
        "taxonomy_audits",
        lambda: [
            _public_taxonomy_audit(item)
            for item in list_pending_taxonomy_audits(limit=30)
        ],
        [],
    )
    proposals = read(
        "taxonomy_proposals",
        lambda: [
            _public_taxonomy_proposal(item)
            for item in list_pending_taxonomy_proposals(limit=30)
        ],
        [],
    )
    papers = read(
        "papers",
        lambda: [
            _public_admin_paper_choice(item)
            for item in list_admin_paper_choices(limit=100)
        ],
        [],
    )
    return {
        "generated_at": generated_at,
        "operations": operations,
        "cost": cost,
        "failures": failures,
        "health": health,
        "pending_operations": pending,
        "recent_operations": recent,
        "taxonomy_audits": audits,
        "taxonomy_proposals": proposals,
        "papers": papers,
        "section_errors": section_errors,
    }


@app.get("/api/admin/operations/pending")
def admin_pending_operations(limit: int = 50) -> dict[str, Any]:
    try:
        items = list_pending_operation_reviews(limit=limit)
    except OperationsStoreError:
        raise HTTPException(
            status_code=422, detail="operation_review_query_invalid"
        ) from None
    return {"requests": [_public_operation_item(item) for item in items]}


@app.get("/api/admin/operations/recent")
def admin_recent_operations(limit: int = 50) -> dict[str, Any]:
    try:
        items = list_recent_operation_requests(limit=limit)
    except OperationsStoreError:
        raise HTTPException(
            status_code=422, detail="operation_request_query_invalid"
        ) from None
    return {"requests": [_public_operation_item(item) for item in items]}


@app.get("/api/admin/operations/{request_kind}/{request_id}")
def admin_operation_detail(
    request_kind: Literal["reprocess", "rollback"], request_id: UUID
) -> dict[str, Any]:
    try:
        item = get_operation_request_detail(request_kind, request_id)
    except OperationsStoreError as exc:
        status_code = 404 if exc.code.endswith("_not_found") else 409
        raise HTTPException(
            status_code=status_code, detail="operation_request_unavailable"
        ) from None
    return {"request": _public_operation_detail(item)}


@app.get("/api/admin/taxonomy/audits")
def admin_taxonomy_audits(limit: int = 50) -> dict[str, Any]:
    try:
        items = list_pending_taxonomy_audits(limit=limit)
    except TaxonomyQueryError:
        raise HTTPException(
            status_code=422, detail="taxonomy_audit_query_invalid"
        ) from None
    return {"audits": [_public_taxonomy_audit(item) for item in items]}


@app.get("/api/admin/taxonomy/proposals")
def admin_taxonomy_proposals(limit: int = 50) -> dict[str, Any]:
    try:
        items = list_pending_taxonomy_proposals(limit=limit)
    except TaxonomyQueryError:
        raise HTTPException(
            status_code=422, detail="taxonomy_proposal_query_invalid"
        ) from None
    return {"proposals": [_public_taxonomy_proposal(item) for item in items]}


@app.get("/api/admin/taxonomy/proposals/{proposal_id}")
def admin_taxonomy_proposal_detail(proposal_id: UUID) -> dict[str, Any]:
    try:
        item = get_taxonomy_proposal_detail(proposal_id, assignment_limit=1000)
    except TaxonomyQueryNotFound:
        raise HTTPException(status_code=404, detail="taxonomy_proposal_not_found") from None
    except TaxonomyQueryError:
        raise HTTPException(
            status_code=409, detail="taxonomy_proposal_unavailable"
        ) from None
    return {"proposal": _public_taxonomy_proposal_detail(item)}


@app.get("/api/admin/papers/{paper_id}/versions")
def admin_paper_versions(paper_id: int) -> dict[str, Any]:
    return _rollback_version_choices(paper_id)


@app.post(
    "/api/admin/taxonomy/audits/{audit_id}/generation",
    status_code=status.HTTP_202_ACCEPTED,
)
def post_admin_taxonomy_generation(
    audit_id: UUID,
    payload: AdminTaxonomyGenerationAction,
    request: Request,
) -> dict[str, Any]:
    _require_writes()
    actor_id = _human_actor(request)
    if payload.acknowledge_metadata_and_cost is not True:
        raise HTTPException(
            status_code=422, detail="metadata_and_cost_acknowledgement_required"
        )
    try:
        job = enqueue_generation_for_audit(
            audit_id,
            request_actor_kind="human",
            request_actor_id=actor_id,
            metadata_and_cost_acknowledged=True,
        )
    except (TaxonomyRuntimeError, TaxonomyContractError):
        raise HTTPException(
            status_code=409, detail="taxonomy_generation_not_eligible"
        ) from None
    except Exception:
        raise HTTPException(
            status_code=503, detail="taxonomy_generation_unavailable"
        ) from None
    return {"job": _public_generation_job(job)}


def _review_taxonomy(
    proposal_id: UUID,
    payload: AdminTaxonomyReviewAction,
    request: Request,
    *,
    decision: Literal["approve", "reject"],
) -> dict[str, Any]:
    _require_writes()
    actor_id = _human_actor(request)
    if decision == "approve" and payload.confirm_apply is not True:
        raise HTTPException(
            status_code=422, detail="taxonomy_apply_confirmation_required"
        )
    try:
        event = review_taxonomy_proposal(
            proposal_id,
            decision=decision,
            actor_kind="human",
            actor_id=actor_id,
            expected_revision=payload.expected_revision,
            comment=payload.comment,
        )
    except TaxonomyRevisionConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "taxonomy_review_revision_conflict",
                "current_revision": exc.current_revision,
            },
        ) from None
    except TaxonomyContractError:
        raise HTTPException(
            status_code=409, detail="taxonomy_review_rejected"
        ) from None
    except Exception:
        raise HTTPException(
            status_code=503, detail="taxonomy_review_unavailable"
        ) from None
    return {
        "review": {
            "event_id": event.get("id"),
            "proposal_id": event.get("proposal_id"),
            "review_revision": event.get("review_revision"),
            "decision": event.get("decision"),
            "created_at": event.get("created_at"),
        }
    }


@app.post("/api/admin/taxonomy/proposals/{proposal_id}/approve")
def post_admin_approve_taxonomy(
    proposal_id: UUID,
    payload: AdminTaxonomyReviewAction,
    request: Request,
) -> dict[str, Any]:
    return _review_taxonomy(
        proposal_id, payload, request, decision="approve"
    )


@app.post("/api/admin/taxonomy/proposals/{proposal_id}/reject")
def post_admin_reject_taxonomy(
    proposal_id: UUID,
    payload: AdminTaxonomyReviewAction,
    request: Request,
) -> dict[str, Any]:
    return _review_taxonomy(
        proposal_id, payload, request, decision="reject"
    )


@app.post(
    "/api/admin/operations/reprocess",
    status_code=status.HTTP_201_CREATED,
)
def post_admin_reprocess_request(
    payload: AdminReprocessCreate, request: Request
) -> dict[str, Any]:
    _require_writes()
    _human_actor(request)
    if payload.confirm_selected_reprocess is not True:
        raise HTTPException(
            status_code=422, detail="selected_reprocess_confirmation_required"
        )
    if len(payload.paper_ids) != len(set(payload.paper_ids)):
        raise HTTPException(status_code=422, detail="paper_ids_duplicate")
    targets = [
        _reprocess_operation_target(paper_id) for paper_id in payload.paper_ids
    ]
    try:
        operation = build_selective_reprocess_request(
            targets=targets,
            reason_code=payload.reason_code,
            requested_at=_utc_now_text(),
        )
        stored = create_reprocess_request(operation)
    except (ValueError, OperationsStoreError):
        raise HTTPException(
            status_code=409, detail="reprocess_request_rejected"
        ) from None
    return {"request": _public_created_operation(stored)}


@app.post(
    "/api/admin/operations/rollback",
    status_code=status.HTTP_201_CREATED,
)
def post_admin_rollback_request(
    payload: AdminRollbackCreate, request: Request
) -> dict[str, Any]:
    _require_writes()
    _human_actor(request)
    if payload.confirm_selected_rollback is not True:
        raise HTTPException(
            status_code=422, detail="selected_rollback_confirmation_required"
        )
    paper_ids = [target.paper_id for target in payload.targets]
    if len(paper_ids) != len(set(paper_ids)):
        raise HTTPException(status_code=422, detail="paper_ids_duplicate")
    targets: list[dict[str, Any]] = []
    for selected in payload.targets:
        current = _rollback_operation_target(selected.paper_id)
        choices = _rollback_version_choices(selected.paper_id)
        if choices["active_version_id"] != current["expected_current_version_id"]:
            raise HTTPException(
                status_code=409, detail="rollback_current_changed"
            )
        if selected.rollback_to_version_id not in choices["rollback_version_ids"]:
            raise HTTPException(
                status_code=409, detail="rollback_version_not_available"
            )
        targets.append(
            {
                **current,
                "rollback_to_version_id": selected.rollback_to_version_id,
            }
        )
    try:
        stored = create_rollback_request(
            targets=targets,
            reason_code=payload.reason_code,
            requested_at=_utc_now_text(),
        )
    except OperationsStoreError:
        raise HTTPException(
            status_code=409, detail="rollback_request_rejected"
        ) from None
    return {"request": _public_created_operation(stored)}


def _approve_operation(
    request_kind: Literal["reprocess", "rollback"],
    request_id: UUID,
    payload: AdminOperationReviewAction,
    request: Request,
) -> dict[str, Any]:
    _require_writes()
    actor_id = _human_actor(request)
    if payload.confirm_execution is not True:
        raise HTTPException(
            status_code=422, detail="operation_execution_confirmation_required"
        )
    try:
        detail = get_operation_request_detail(request_kind, request_id)
    except OperationsStoreError:
        raise HTTPException(status_code=404, detail="operation_request_not_found") from None
    if request_kind == "reprocess" and detail.get("scope") != "selected":
        raise HTTPException(
            status_code=409, detail="global_reprocess_not_supported"
        )
    review = (
        review_reprocess_request
        if request_kind == "reprocess"
        else review_rollback_request
    )
    enqueue = (
        enqueue_approved_reprocess
        if request_kind == "reprocess"
        else enqueue_approved_rollback
    )
    revision = payload.expected_revision + 1
    try:
        event = review(
            request_id,
            expected_revision=payload.expected_revision,
            decision="approve",
            actor_id=actor_id,
            comment=payload.comment,
        )
        revision = int(event["review_revision"])
    except OperationsReviewConflict as exc:
        if exc.decision != "approved":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "operation_review_revision_conflict",
                    "current_revision": exc.current_revision,
                    "decision": exc.decision,
                },
            ) from None
        revision = exc.current_revision
    except OperationsStoreError:
        raise HTTPException(
            status_code=409, detail="operation_approval_rejected"
        ) from None
    try:
        jobs = enqueue(request_id)
    except OperationsApprovalRequired:
        raise HTTPException(
            status_code=409, detail="operation_human_approval_required"
        ) from None
    except OperationsStoreError:
        raise HTTPException(
            status_code=503, detail="operation_enqueue_unavailable"
        ) from None
    status_counts: dict[str, int] = {}
    for job in jobs:
        job_status = str(job.get("status"))
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", job_status):
            raise HTTPException(
                status_code=503, detail="operation_job_state_invalid"
            )
        status_counts[job_status] = status_counts.get(job_status, 0) + 1
    return {
        "request_id": str(request_id),
        "decision": "approved",
        "review_revision": revision,
        "enqueued_job_count": len(jobs),
        "job_status_counts": status_counts,
    }


def _reject_operation(
    request_kind: Literal["reprocess", "rollback"],
    request_id: UUID,
    payload: AdminOperationReviewAction,
    request: Request,
) -> dict[str, Any]:
    _require_writes()
    actor_id = _human_actor(request)
    try:
        detail = get_operation_request_detail(request_kind, request_id)
    except OperationsStoreError:
        raise HTTPException(status_code=404, detail="operation_request_not_found") from None
    if request_kind == "reprocess" and detail.get("scope") != "selected":
        raise HTTPException(
            status_code=409, detail="global_reprocess_not_supported"
        )
    review = (
        review_reprocess_request
        if request_kind == "reprocess"
        else review_rollback_request
    )
    try:
        event = review(
            request_id,
            expected_revision=payload.expected_revision,
            decision="reject",
            actor_id=actor_id,
            comment=payload.comment,
        )
    except OperationsReviewConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "operation_review_revision_conflict",
                "current_revision": exc.current_revision,
                "decision": exc.decision,
            },
        ) from None
    except OperationsStoreError:
        raise HTTPException(
            status_code=409, detail="operation_rejection_rejected"
        ) from None
    return {
        "request_id": str(request_id),
        "decision": "rejected",
        "review_revision": int(event["review_revision"]),
    }


@app.post("/api/admin/operations/reprocess/{request_id}/approve")
def post_admin_approve_reprocess(
    request_id: UUID,
    payload: AdminOperationReviewAction,
    request: Request,
) -> dict[str, Any]:
    return _approve_operation("reprocess", request_id, payload, request)


@app.post("/api/admin/operations/reprocess/{request_id}/reject")
def post_admin_reject_reprocess(
    request_id: UUID,
    payload: AdminOperationReviewAction,
    request: Request,
) -> dict[str, Any]:
    return _reject_operation("reprocess", request_id, payload, request)


@app.post("/api/admin/operations/rollback/{request_id}/approve")
def post_admin_approve_rollback(
    request_id: UUID,
    payload: AdminOperationReviewAction,
    request: Request,
) -> dict[str, Any]:
    return _approve_operation("rollback", request_id, payload, request)


@app.post("/api/admin/operations/rollback/{request_id}/reject")
def post_admin_reject_rollback(
    request_id: UUID,
    payload: AdminOperationReviewAction,
    request: Request,
) -> dict[str, Any]:
    return _reject_operation("rollback", request_id, payload, request)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return service_status()


@app.get("/readyz")
def readyz(response: Response) -> dict[str, object]:
    payload, status_code = readiness_status()
    response.status_code = status_code
    return payload


@app.post("/jobs/sync", status_code=status.HTTP_202_ACCEPTED)
def request_sync() -> dict[str, object]:
    _require_writes()
    return {"job": queue_sync("manual")}


@app.get("/jobs/sync/latest")
def get_latest_sync() -> dict[str, object]:
    return {"job": latest_sync()}

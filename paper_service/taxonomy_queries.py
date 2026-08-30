"""Bounded, allowlisted read projections for human taxonomy review.

These functions intentionally do not directly expose source snapshots, Drive
path fields, paper-title fields, model prompts/responses, worker leases,
credentials, or raw database rows.  Digests and opaque identifiers are
retained so the UI can display and submit review actions against exact
provenance.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
import hashlib
import json
import re
from typing import Any
from uuid import UUID

from .database import connection
from .taxonomy import (
    AuditPolicy,
    DEEPSEEK_PRO_MODEL,
    TaxonomyContractError,
    validate_taxonomy_proposal,
)


MAX_LIST_LIMIT = 100
MAX_ASSIGNMENT_LIMIT = 1_000

_SHA_RE = re.compile(r"[0-9a-f]{64}")
_CODE_RE = re.compile(r"[a-z][a-z0-9_]{0,119}")
_AUDIT_POLICY_FIELDS = frozenset(
    {
        "min_category_papers",
        "min_new_papers",
        "max_audit_age_days",
        "max_leaf_papers",
        "heterogeneity_threshold",
        "min_subtheme_size",
        "max_subthemes",
        "max_other_fraction",
    }
)
_AUDIT_METRIC_FIELDS = frozenset(
    {
        "paper_count",
        "new_paper_count",
        "days_since_checkpoint",
        "max_leaf_size",
        "leaf_count",
        "overcrowding_ratio",
        "heterogeneity",
    }
)
_GENERATION_STATUSES = frozenset(
    {"queued", "running", "retry_wait", "succeeded", "failed"}
)


class TaxonomyQueryError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class TaxonomyQueryNotFound(TaxonomyQueryError):
    pass


def _fail(code: str) -> TaxonomyQueryError:
    return TaxonomyQueryError(code)


def _limit(value: Any, *, field: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise _fail(f"{field}_invalid")
    return value


def _uuid(value: Any, *, field: str) -> str:
    try:
        parsed = UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise _fail(f"{field}_invalid") from exc
    if parsed.int == 0 or str(parsed) != str(value):
        raise _fail(f"{field}_invalid")
    return str(parsed)


def _sha(value: Any, *, field: str) -> str:
    clean = str(value or "").strip()
    if not _SHA_RE.fullmatch(clean):
        raise _fail(f"{field}_invalid")
    return clean


def _text(value: Any, *, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise _fail(f"{field}_invalid")
    clean = value.strip()
    if not clean or len(clean) > maximum:
        raise _fail(f"{field}_invalid")
    if any(ord(character) < 32 or ord(character) == 127 for character in clean):
        raise _fail(f"{field}_invalid")
    return clean


def _integer(value: Any, *, field: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise _fail(f"{field}_invalid")
    return value


def _json(value: Any, *, field: str) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, RecursionError) as exc:
            raise _fail(f"{field}_invalid") from exc
    return value


def _policy(value: Any) -> tuple[AuditPolicy, dict[str, Any], str]:
    raw = _json(value, field="stored_taxonomy_policy")
    if not isinstance(raw, Mapping) or set(raw) != _AUDIT_POLICY_FIELDS:
        raise _fail("stored_taxonomy_policy_invalid")
    try:
        policy = AuditPolicy(**dict(raw))
    except (TypeError, TaxonomyContractError) as exc:
        raise _fail("stored_taxonomy_policy_invalid") from exc
    public = {field: getattr(policy, field) for field in sorted(_AUDIT_POLICY_FIELDS)}
    digest = hashlib.sha256(
        json.dumps(
            public,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    return policy, public, digest


def _metrics(value: Any) -> dict[str, Any]:
    raw = _json(value, field="stored_taxonomy_metrics")
    if not isinstance(raw, Mapping) or set(raw) != _AUDIT_METRIC_FIELDS:
        raise _fail("stored_taxonomy_metrics_invalid")
    output = dict(raw)
    for field in ("paper_count", "new_paper_count", "max_leaf_size", "leaf_count"):
        _integer(output[field], field=f"taxonomy_metric_{field}")
    for field in ("overcrowding_ratio", "heterogeneity"):
        value = output[field]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _fail(f"taxonomy_metric_{field}_invalid")
        output[field] = float(value)
    days = output["days_since_checkpoint"]
    if days is not None and (
        isinstance(days, bool) or not isinstance(days, (int, float)) or days < 0
    ):
        raise _fail("taxonomy_metric_days_since_checkpoint_invalid")
    return output


def _reason_codes(value: Any) -> list[str]:
    raw = _json(value, field="stored_taxonomy_reason_codes")
    if (
        not isinstance(raw, list)
        or len(raw) > 20
        or any(not isinstance(item, str) or not _CODE_RE.fullmatch(item) for item in raw)
    ):
        raise _fail("stored_taxonomy_reason_codes_invalid")
    return list(raw)


def _generation_projection(row: Mapping[str, Any]) -> dict[str, Any]:
    job_id = row.get("generation_job_id")
    if job_id is None:
        if any(
            row.get(field) is not None
            for field in (
                "generation_status",
                "generation_provider",
                "generation_model",
                "request_digest",
                "generation_requested_at",
            )
        ):
            raise _fail("taxonomy_generation_projection_invalid")
        return {"requested": False, "status": "not-requested"}
    status = row.get("generation_status")
    if status not in _GENERATION_STATUSES:
        raise _fail("taxonomy_generation_status_invalid")
    provider = _text(
        row.get("generation_provider"),
        field="taxonomy_generation_provider",
        maximum=80,
    )
    model = _text(
        row.get("generation_model"),
        field="taxonomy_generation_model",
        maximum=200,
    )
    if provider != "deepseek" or model != DEEPSEEK_PRO_MODEL:
        raise _fail("taxonomy_generation_provenance_invalid")
    return {
        "requested": True,
        "job_id": _uuid(job_id, field="generation_job_id"),
        "status": status,
        "provider": provider,
        "model": model,
        "request_digest": _sha(
            row.get("request_digest"), field="taxonomy_request_digest"
        ),
        "requested_at": row.get("generation_requested_at"),
    }


def list_pending_taxonomy_audits(*, limit: int = 50) -> tuple[dict[str, Any], ...]:
    """List eligible audits that do not yet have a generated proposal."""

    maximum = _limit(limit, field="taxonomy_audit_limit", maximum=MAX_LIST_LIMIT)
    with connection() as database:
        rows = database.execute(
            """SELECT audit.id, audit.category, audit.source_snapshot_sha256,
                      audit.paper_count, audit.new_paper_count, audit.metrics,
                      audit.audit_policy, audit.reason_codes,
                      audit.evaluated_at, audit.created_at,
                      generation.id AS generation_job_id,
                      generation.status AS generation_status,
                      generation.provider AS generation_provider,
                      generation.model AS generation_model,
                      generation.request_digest,
                      generation.requested_at AS generation_requested_at
               FROM paper_taxonomy_audits audit
               LEFT JOIN paper_taxonomy_generation_jobs generation
                 ON generation.audit_id = audit.id
               LEFT JOIN paper_taxonomy_proposals proposal
                 ON proposal.audit_id = audit.id
               WHERE audit.decision = 'request-proposal'
                 AND proposal.id IS NULL
               ORDER BY audit.evaluated_at ASC, audit.id ASC
               LIMIT %s""",
            (maximum,),
        ).fetchall()
    output: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        _policy_value, policy, policy_digest = _policy(row.get("audit_policy"))
        paper_count = _integer(
            row.get("paper_count"), field="taxonomy_paper_count"
        )
        new_count = _integer(
            row.get("new_paper_count"), field="taxonomy_new_paper_count"
        )
        if new_count > paper_count:
            raise _fail("taxonomy_new_paper_count_invalid")
        metrics = _metrics(row.get("metrics"))
        if (
            metrics["paper_count"] != paper_count
            or metrics["new_paper_count"] != new_count
        ):
            raise _fail("taxonomy_audit_metrics_identity_mismatch")
        output.append(
            {
                "audit_id": _uuid(row.get("id"), field="audit_id"),
                "category": _text(
                    row.get("category"), field="taxonomy_category", maximum=120
                ),
                "source_snapshot_sha256": _sha(
                    row.get("source_snapshot_sha256"),
                    field="taxonomy_source_snapshot_sha256",
                ),
                "paper_count": paper_count,
                "new_paper_count": new_count,
                "metrics": metrics,
                "policy": policy,
                "policy_digest": policy_digest,
                "reason_codes": _reason_codes(row.get("reason_codes")),
                "evaluated_at": row.get("evaluated_at"),
                "created_at": row.get("created_at"),
                "generation": _generation_projection(row),
            }
        )
    return tuple(output)


def _proposal_summary(row: Mapping[str, Any]) -> dict[str, Any]:
    revision = _integer(
        row.get("review_revision"), field="taxonomy_review_revision"
    )
    provider = _text(
        row.get("provider"), field="taxonomy_provider", maximum=80
    )
    model = _text(row.get("model"), field="taxonomy_model", maximum=200)
    if provider != "deepseek" or model != DEEPSEEK_PRO_MODEL:
        raise _fail("taxonomy_proposal_provenance_invalid")
    proposal_id = _uuid(row.get("id"), field="proposal_id")
    audit_id = _uuid(row.get("audit_id"), field="audit_id")
    generation_job_id = _uuid(
        row.get("generation_job_id"), field="generation_job_id"
    )
    source_digest = _sha(
        row.get("source_snapshot_sha256"),
        field="taxonomy_source_snapshot_sha256",
    )
    if (
        row.get("generation_status") != "succeeded"
        or str(row.get("generation_result_proposal_id")) != proposal_id
        or str(row.get("generation_audit_id")) != audit_id
        or str(row.get("generation_proposal_id")) != proposal_id
        or row.get("generation_provider") != provider
        or row.get("generation_model") != model
        or row.get("audit_decision") != "request-proposal"
        or str(row.get("audit_source_snapshot_sha256") or "").strip()
        != source_digest
    ):
        raise _fail("taxonomy_proposal_provenance_invalid")
    assignment_count = _integer(
        row.get("assignment_count"), field="taxonomy_assignment_count"
    )
    subtheme_count = _integer(
        row.get("subtheme_count"), field="taxonomy_subtheme_count"
    )
    if not 1 <= assignment_count <= MAX_ASSIGNMENT_LIMIT or not 2 <= subtheme_count <= 24:
        raise _fail("taxonomy_proposal_counts_invalid")
    return {
        "proposal_id": proposal_id,
        "category": _text(
            row.get("category"), field="taxonomy_category", maximum=120
        ),
        "assignment_count": assignment_count,
        "subtheme_count": subtheme_count,
        "review_revision": revision,
        "review_status": "pending",
        "created_at": row.get("created_at"),
        "provenance": {
            "audit_id": audit_id,
            "generation_job_id": generation_job_id,
            "generation_status": "succeeded",
            "provider": provider,
            "model": model,
            "source_snapshot_sha256": source_digest,
            "request_digest": _sha(
                row.get("request_digest"), field="taxonomy_request_digest"
            ),
            "proposal_payload_sha256": _sha(
                row.get("canonical_payload_sha256"),
                field="taxonomy_proposal_payload_sha256",
            ),
        },
    }


def list_pending_taxonomy_proposals(
    *, limit: int = 50
) -> tuple[dict[str, Any], ...]:
    """List generated proposals that have no human review event."""

    maximum = _limit(limit, field="taxonomy_proposal_limit", maximum=MAX_LIST_LIMIT)
    with connection() as database:
        rows = database.execute(
            """SELECT proposal.id, proposal.audit_id,
                      proposal.generation_job_id, proposal.category,
                      proposal.source_snapshot_sha256,
                      proposal.canonical_payload_sha256,
                      proposal.provider, proposal.model, proposal.created_at,
                      generation.request_digest,
                      generation.status AS generation_status,
                      generation.result_proposal_id
                          AS generation_result_proposal_id,
                      generation.audit_id AS generation_audit_id,
                      generation.proposal_id AS generation_proposal_id,
                      generation.provider AS generation_provider,
                      generation.model AS generation_model,
                      audit.decision AS audit_decision,
                      audit.source_snapshot_sha256
                          AS audit_source_snapshot_sha256,
                      COALESCE(head.review_revision, 0) AS review_revision,
                      (SELECT count(*)
                       FROM paper_taxonomy_proposal_assignments assignment
                       WHERE assignment.proposal_id = proposal.id)
                          AS assignment_count,
                      jsonb_array_length(
                          proposal.canonical_payload -> 'subthemes'
                      ) AS subtheme_count
               FROM paper_taxonomy_proposals proposal
               JOIN paper_taxonomy_generation_jobs generation
                 ON generation.id = proposal.generation_job_id
               JOIN paper_taxonomy_audits audit ON audit.id = proposal.audit_id
               LEFT JOIN paper_taxonomy_review_heads head
                 ON head.proposal_id = proposal.id
               LEFT JOIN paper_taxonomy_review_events event
                 ON event.proposal_id = proposal.id
               WHERE event.id IS NULL
               ORDER BY proposal.created_at ASC, proposal.id ASC
               LIMIT %s""",
            (maximum,),
        ).fetchall()
    output = tuple(_proposal_summary(dict(row)) for row in rows)
    if any(item["review_revision"] != 0 for item in output):
        raise _fail("taxonomy_pending_proposal_revision_invalid")
    return output


def _proposal_payload(row: Mapping[str, Any]) -> tuple[dict[str, Any], AuditPolicy]:
    policy, _public, _digest = _policy(row.get("audit_policy"))
    payload = _json(row.get("canonical_payload"), field="stored_taxonomy_proposal")
    try:
        canonical = validate_taxonomy_proposal(payload, policy=policy)
    except TaxonomyContractError as exc:
        raise _fail("stored_taxonomy_proposal_invalid") from exc
    if (
        canonical["proposal_id"] != str(row.get("id"))
        or canonical["category"] != row.get("category")
        or canonical["source_snapshot_sha256"]
        != str(row.get("source_snapshot_sha256") or "").strip()
        or canonical["proposal_payload_sha256"]
        != str(row.get("canonical_payload_sha256") or "").strip()
    ):
        raise _fail("stored_taxonomy_proposal_identity_mismatch")
    return canonical, policy


def get_taxonomy_proposal_detail(
    proposal_id: str | UUID,
    *,
    assignment_limit: int = MAX_ASSIGNMENT_LIMIT,
) -> dict[str, Any]:
    """Return one integrity-checked proposal and its optimistic review head."""

    clean_id = _uuid(proposal_id, field="proposal_id")
    maximum = _limit(
        assignment_limit,
        field="taxonomy_assignment_limit",
        maximum=MAX_ASSIGNMENT_LIMIT,
    )
    with connection() as database:
        header = database.execute(
            """SELECT proposal.id, proposal.audit_id,
                      proposal.generation_job_id, proposal.category,
                      proposal.source_snapshot_sha256,
                      proposal.canonical_payload,
                      proposal.canonical_payload_sha256,
                      proposal.provider, proposal.model, proposal.created_at,
                      audit.audit_policy, audit.evaluated_at,
                      audit.decision AS audit_decision,
                      audit.source_snapshot_sha256
                          AS audit_source_snapshot_sha256,
                      generation.request_digest, generation.finished_at,
                      generation.status AS generation_status,
                      generation.result_proposal_id
                          AS generation_result_proposal_id,
                      generation.audit_id AS generation_audit_id,
                      generation.proposal_id AS generation_proposal_id,
                      generation.provider AS generation_provider,
                      generation.model AS generation_model,
                      COALESCE(head.review_revision, 0) AS review_revision,
                      head.updated_at AS review_updated_at,
                      event.id AS review_event_id,
                      event.review_revision AS event_revision,
                      event.decision AS review_decision,
                      event.created_at AS review_created_at,
                      (SELECT count(*)
                       FROM paper_taxonomy_proposal_assignments counted
                       WHERE counted.proposal_id = proposal.id)
                           AS assignment_count,
                      jsonb_array_length(
                          proposal.canonical_payload -> 'subthemes'
                      ) AS subtheme_count
               FROM paper_taxonomy_proposals proposal
               JOIN paper_taxonomy_audits audit ON audit.id = proposal.audit_id
               JOIN paper_taxonomy_generation_jobs generation
                 ON generation.id = proposal.generation_job_id
               LEFT JOIN paper_taxonomy_review_heads head
                 ON head.proposal_id = proposal.id
               LEFT JOIN paper_taxonomy_review_events event
                 ON event.proposal_id = proposal.id
               WHERE proposal.id = %s::uuid""",
            (clean_id,),
        ).fetchone()
        if header is None:
            raise TaxonomyQueryNotFound("taxonomy_proposal_not_found")
        assignment_rows = database.execute(
            """SELECT ordinal, paper_id, source_sha256, old_drive_path,
                      new_drive_path, subtheme, move_job_id,
                      idempotency_key_digest
               FROM paper_taxonomy_proposal_assignments
               WHERE proposal_id = %s::uuid
               ORDER BY ordinal
               LIMIT %s""",
            (clean_id, maximum + 1),
        ).fetchall()

    row = dict(header)
    canonical, _policy_value = _proposal_payload(row)
    summary = _proposal_summary(row)
    total = summary["assignment_count"]
    if total != len(canonical["assignments"]):
        raise _fail("stored_taxonomy_assignment_count_mismatch")
    if summary["subtheme_count"] != len(canonical["subthemes"]):
        raise _fail("stored_taxonomy_subtheme_count_mismatch")
    fetched = [dict(item) for item in assignment_rows]
    if len(fetched) > maximum + 1:
        raise _fail("stored_taxonomy_assignment_limit_violated")
    if len(fetched) != min(total, maximum + 1):
        raise _fail("stored_taxonomy_assignment_rows_incomplete")
    selected = fetched[:maximum]
    assignments: list[dict[str, Any]] = []
    for expected_ordinal, assignment_row in enumerate(selected):
        if assignment_row.get("ordinal") != expected_ordinal:
            raise _fail("stored_taxonomy_assignment_order_invalid")
        canonical_item = canonical["assignments"][expected_ordinal]
        comparisons = {
            "paper_id": canonical_item["paper_id"],
            "source_sha256": canonical_item["source_sha256"],
            "old_drive_path": canonical_item["current_drive_path"],
            "new_drive_path": canonical_item["proposed_drive_path"],
            "subtheme": canonical_item["subtheme"],
            "move_job_id": canonical_item["move_job_id"],
            "idempotency_key_digest": canonical_item["idempotency_key_digest"],
        }
        for field, expected in comparisons.items():
            actual = assignment_row.get(field)
            if field in {"move_job_id"}:
                actual = str(actual)
            elif field.endswith("sha256") or field.endswith("digest"):
                actual = str(actual or "").strip()
            if actual != expected:
                raise _fail("stored_taxonomy_assignment_identity_mismatch")
        assignments.append(
            {
                "paper_id": canonical_item["paper_id"],
                "source_sha256": canonical_item["source_sha256"],
                "subtheme": canonical_item["subtheme"],
                "move_required": (
                    canonical_item["current_drive_path"]
                    != canonical_item["proposed_drive_path"]
                ),
                "move_job_id": canonical_item["move_job_id"],
                "idempotency_key_digest": canonical_item[
                    "idempotency_key_digest"
                ],
            }
        )

    revision = summary["review_revision"]
    event_id = row.get("review_event_id")
    if event_id is None:
        if row.get("review_decision") is not None or row.get("event_revision") is not None:
            raise _fail("taxonomy_review_projection_invalid")
        review = {
            "status": "pending",
            "revision": revision,
            "event_id": None,
            "decision": None,
            "updated_at": row.get("review_updated_at"),
        }
    else:
        decision = row.get("review_decision")
        event_revision = _integer(
            row.get("event_revision"), field="taxonomy_event_revision", minimum=1
        )
        if decision not in {"approve", "reject"} or event_revision != revision:
            raise _fail("taxonomy_review_projection_invalid")
        review = {
            "status": "approved" if decision == "approve" else "rejected",
            "revision": revision,
            "event_id": _uuid(event_id, field="review_event_id"),
            "decision": decision,
            "updated_at": row.get("review_updated_at"),
            "reviewed_at": row.get("review_created_at"),
        }

    counts = Counter(item["subtheme"] for item in canonical["assignments"])
    detail = {
        **summary,
        "schema": "paper-taxonomy-proposal-review",
        "schema_version": 1,
        "rationale": canonical["rationale"],
        "subthemes": [
            {
                "name": item["name"],
                "description": item["description"],
                "assignment_count": counts[item["name"]],
            }
            for item in canonical["subthemes"]
        ],
        "assignments": assignments,
        "assignments_complete": len(assignments) == total,
        "review": review,
        "audit_evaluated_at": row.get("evaluated_at"),
        "generation_finished_at": row.get("finished_at"),
    }
    detail["review_status"] = review["status"]
    return detail


__all__ = [
    "MAX_ASSIGNMENT_LIMIT",
    "MAX_LIST_LIMIT",
    "TaxonomyQueryError",
    "TaxonomyQueryNotFound",
    "get_taxonomy_proposal_detail",
    "list_pending_taxonomy_audits",
    "list_pending_taxonomy_proposals",
]

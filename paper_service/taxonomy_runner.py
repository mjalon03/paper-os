"""Sequential runtime for safe paper taxonomy audits, drafts, and moves.

The runtime intentionally keeps model generation and Drive mutation behind
separate durable leases.  It never reads a PDF, abstract, annotation, or
article body.  Results contain only opaque job identifiers, counts, and
sanitized error codes; titles, paths, filenames, credentials, provider bodies,
and command output are never logged or returned.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
import configparser
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import threading
import time
import unicodedata
from typing import Any
from uuid import UUID, uuid5

from .database import connection, ensure_schema
from .deepseek_client import (
    DeepSeekCompletion,
    DeepSeekTransportError,
    complete_deepseek_request,
)
from .metadata import deepseek_api_key
from .ops_runtime import worker_heartbeat_from_env
from .taxonomy import (
    AuditPolicy,
    DEEPSEEK_PRO_MODEL,
    TaxonomyContractError,
    TaxonomyLeaseLost,
    TaxonomyModelRequest,
    TaxonomyUsage,
    bind_taxonomy_move_source_identity,
    build_deepseek_pro_request,
    build_source_snapshot,
    claim_taxonomy_generation,
    claim_taxonomy_move_job,
    complete_taxonomy_move_job,
    enqueue_taxonomy_generation,
    evaluate_taxonomy_audit,
    nack_taxonomy_generation,
    nack_taxonomy_move_job,
    parse_taxonomy_proposal,
    persist_taxonomy_audit,
    prepare_move_execution,
    prepare_move_reconciliation,
    record_taxonomy_usage,
    source_snapshot_sha256,
    store_taxonomy_proposal,
    validate_taxonomy_generation_job_authorization,
    validate_drive_path,
    validate_drive_file_id,
    validate_model_request,
    validate_source_snapshot,
)


_AUDIT_NAMESPACE = UUID("fd27d51b-990e-5a88-96fb-031383ce63ac")
_PROPOSAL_NAMESPACE = UUID("cfb8ec6e-2331-5ea9-ab8b-4f80df236513")

_WORKER_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}")
_REMOTE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_SAFE_ERROR_RE = re.compile(r"[a-z][a-z0-9_]{0,119}")
_TOKEN_RE = re.compile(r"[^\W_]+", flags=re.UNICODE)
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

MAX_TOPIC_TERMS = 24
MAX_TOPIC_TERM_CHARACTERS = 64
MAX_RCLONE_OUTPUT_BYTES = 1024 * 1024
MAX_LOCAL_PDF_BYTES = 2 * 1024 * 1024 * 1024
GENERATION_LEASE_MARGIN_SECONDS = 60
MOVE_LEASE_MARGIN_SECONDS = 300

_TITLE_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "by",
        "for",
        "from",
        "in",
        "is",
        "of",
        "on",
        "or",
        "the",
        "to",
        "using",
        "via",
        "with",
        "study",
        "analysis",
        "paper",
        "대한",
        "위한",
        "통한",
        "연구",
    }
)


class TaxonomyRuntimeError(RuntimeError):
    """A runtime boundary failed with a sanitized operational code."""

    def __init__(self, code: str, *, retryable: bool = False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


def _fail(code: str, *, retryable: bool = False) -> TaxonomyRuntimeError:
    return TaxonomyRuntimeError(code, retryable=retryable)


@dataclass(frozen=True)
class AuditCycleResult:
    status: str
    categories_seen: int = 0
    no_op: int = 0
    enqueued: int = 0
    pending_generation: int = 0
    reused: int = 0
    failed: int = 0
    error_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class RuntimeJobResult:
    status: str
    job_id: str | None = None
    error_code: str | None = None


@dataclass(frozen=True)
class RuntimeCycleResult:
    audit: AuditCycleResult
    generation: RuntimeJobResult
    move: RuntimeJobResult


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: bytes = b""


CommandRunner = Callable[[Sequence[str], int, bool], CommandResult]


@dataclass(frozen=True)
class RemoteEntry:
    file_id: str
    name: str


@dataclass(frozen=True)
class MoveRuntimeConfig:
    drive_root: str
    rclone_binary: Path
    rclone_config: Path
    timeout_seconds: int = 900
    temporary_root: Path | None = None

    def __post_init__(self) -> None:
        root = validate_drive_path(self.drive_root, field="drive_root")
        remote, suffix = root.split(":", 1)
        if not _REMOTE_RE.fullmatch(remote) or not suffix:
            raise _fail("taxonomy_drive_root_invalid")
        object.__setattr__(self, "drive_root", root.rstrip("/"))
        binary = _executable_file(self.rclone_binary)
        config = _private_regular_file(self.rclone_config)
        _require_rclone_remote(config, remote)
        object.__setattr__(self, "rclone_binary", binary)
        object.__setattr__(self, "rclone_config", config)
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, int)
            or not 30 <= self.timeout_seconds <= 3_000
        ):
            raise _fail("taxonomy_rclone_timeout_invalid")
        if self.temporary_root is not None:
            temporary = _private_directory(self.temporary_root)
            object.__setattr__(self, "temporary_root", temporary)


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise _fail("taxonomy_runtime_json_invalid") from exc


def _json_value(value: Any, *, field: str) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, RecursionError) as exc:
            raise _fail(f"{field}_invalid") from exc
    return value


def _worker_id(value: Any) -> str:
    if not isinstance(value, str) or not _WORKER_ID_RE.fullmatch(value):
        raise _fail("taxonomy_worker_id_invalid")
    return value


def _uuid_text(value: Any, *, field: str) -> str:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise _fail(f"{field}_invalid") from exc


def _positive_seconds(
    value: Any, *, field: str, minimum: int, maximum: int
) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise _fail(f"{field}_invalid") from exc
    if isinstance(value, bool) or not minimum <= parsed <= maximum:
        raise _fail(f"{field}_invalid")
    return parsed


def _timestamp_text(value: Any, *, field: str) -> str:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise _fail(f"{field}_invalid") from exc
    else:
        raise _fail(f"{field}_invalid")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _fail(f"{field}_invalid")
    parsed = parsed.astimezone(timezone.utc)
    if parsed.microsecond:
        return parsed.isoformat(timespec="microseconds").replace("+00:00", "Z")
    return parsed.isoformat(timespec="seconds").replace("+00:00", "Z")


def _safe_error_code(exc: BaseException, *, fallback: str) -> tuple[str, bool]:
    if isinstance(exc, DeepSeekTransportError):
        return exc.code, exc.retryable
    if isinstance(exc, TaxonomyRuntimeError):
        return exc.code, exc.retryable
    if isinstance(exc, TaxonomyContractError):
        code = getattr(exc, "code", str(exc))
        if isinstance(code, str) and _SAFE_ERROR_RE.fullmatch(code):
            return code, False
    return fallback, False


def _topic_terms(title: Any) -> list[str]:
    if not isinstance(title, str):
        raise _fail("taxonomy_paper_title_invalid")
    normalized = unicodedata.normalize("NFC", title).strip()
    if not normalized or len(normalized) > 1_000 or _CONTROL_RE.search(normalized):
        raise _fail("taxonomy_paper_title_invalid")
    if any(
        unicodedata.category(character) in {"Cc", "Cf", "Cs"}
        for character in normalized
    ):
        raise _fail("taxonomy_paper_title_invalid")
    output: list[str] = []
    for match in _TOKEN_RE.findall(normalized.casefold()):
        if match in _TITLE_STOP_WORDS or match.isdecimal():
            continue
        term = match[:MAX_TOPIC_TERM_CHARACTERS]
        if term not in output:
            output.append(term)
        if len(output) >= MAX_TOPIC_TERMS:
            break
    return output


def _paper_record(row: Mapping[str, Any]) -> dict[str, Any]:
    try:
        paper_id = int(row["id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise _fail("taxonomy_paper_id_invalid") from exc
    if paper_id <= 0:
        raise _fail("taxonomy_paper_id_invalid")
    sha = str(row.get("sha256", "")).strip()
    if not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise _fail("taxonomy_paper_sha_invalid")
    category = row.get("category")
    if not isinstance(category, str) or not category.strip():
        raise _fail("taxonomy_paper_category_invalid")
    title = row.get("title")
    return {
        "id": paper_id,
        "sha256": sha,
        "title": title,
        "category": category,
        "drive_path": validate_drive_path(row.get("drive_path"), field="drive_path"),
        "imported_at": row.get("imported_at"),
        "topic_terms": _topic_terms(title),
    }


def _load_organized_papers() -> list[dict[str, Any]]:
    """Read only metadata needed by the taxonomy contract."""

    with connection() as database:
        rows = database.execute(
            """SELECT id, sha256, title, category, drive_path, imported_at
               FROM papers
               WHERE processing_status = 'organized'
               ORDER BY category, id"""
        ).fetchall()
    return [_paper_record(dict(row)) for row in rows]


def _latest_substantive_checkpoint(category: str) -> str | None:
    """Exclude no-op audits so they never postpone the 90-day deadline."""

    with connection() as database:
        row = database.execute(
            """SELECT MAX(checkpoint_at) AS checkpoint_at
               FROM (
                   SELECT evaluated_at AS checkpoint_at
                   FROM paper_taxonomy_audits
                   WHERE category = %s AND decision = 'request-proposal'
                   UNION ALL
                   SELECT event.created_at AS checkpoint_at
                   FROM paper_taxonomy_review_events event
                   JOIN paper_taxonomy_proposals proposal
                     ON proposal.id = event.proposal_id
                   WHERE proposal.category = %s
               ) checkpoints""",
            (category, category),
        ).fetchone()
    if not row or row.get("checkpoint_at") is None:
        return None
    return _timestamp_text(row["checkpoint_at"], field="taxonomy_checkpoint")


def _policy_payload(policy: AuditPolicy) -> dict[str, Any]:
    return {
        "min_category_papers": policy.min_category_papers,
        "min_new_papers": policy.min_new_papers,
        "max_audit_age_days": policy.max_audit_age_days,
        "max_leaf_papers": policy.max_leaf_papers,
        "heterogeneity_threshold": policy.heterogeneity_threshold,
        "min_subtheme_size": policy.min_subtheme_size,
        "max_subthemes": policy.max_subthemes,
        "max_other_fraction": policy.max_other_fraction,
    }


def _stored_audit(row: Mapping[str, Any]) -> dict[str, Any]:
    snapshot = validate_source_snapshot(
        _json_value(row.get("source_snapshot"), field="stored_taxonomy_snapshot")
    )
    metrics = _json_value(row.get("metrics"), field="stored_taxonomy_metrics")
    policy = _json_value(row.get("audit_policy"), field="stored_taxonomy_policy")
    reasons = _json_value(row.get("reason_codes"), field="stored_taxonomy_reasons")
    if not isinstance(metrics, Mapping) or not isinstance(policy, Mapping):
        raise _fail("stored_taxonomy_audit_invalid")
    if not isinstance(reasons, list) or any(not isinstance(item, str) for item in reasons):
        raise _fail("stored_taxonomy_audit_invalid")
    digest = str(row.get("source_snapshot_sha256", "")).strip()
    if source_snapshot_sha256(snapshot) != digest:
        raise _fail("stored_taxonomy_snapshot_digest_mismatch")
    return {
        "schema": "paper-taxonomy-audit",
        "schema_version": 1,
        "category": snapshot["category"],
        "category_path": snapshot["category_path"],
        "evaluated_at": _timestamp_text(
            row.get("evaluated_at"), field="stored_taxonomy_evaluated_at"
        ),
        "decision": row.get("decision"),
        "reason_codes": reasons,
        "metrics": dict(metrics),
        "policy": dict(policy),
        "source_snapshot": snapshot,
        "source_snapshot_sha256": digest,
    }


def _existing_eligible_audit(
    *, category: str, snapshot_sha256: str, policy: AuditPolicy
) -> tuple[str, dict[str, Any]] | None:
    with connection() as database:
        row = database.execute(
            """SELECT id, category, category_path, source_snapshot,
                      source_snapshot_sha256, metrics, audit_policy,
                      decision, reason_codes, evaluated_at
               FROM paper_taxonomy_audits
               WHERE category = %s AND source_snapshot_sha256 = %s
                 AND audit_policy = %s::jsonb
                 AND decision = 'request-proposal'
               ORDER BY evaluated_at DESC, id DESC LIMIT 1""",
            (
                category,
                snapshot_sha256,
                _canonical_bytes(_policy_payload(policy)).decode("utf-8"),
            ),
        ).fetchone()
    if not row:
        return None
    return str(row["id"]), _stored_audit(dict(row))


def _audit_identity(audit: Mapping[str, Any]) -> str:
    identity = {
        "contract": "paper-taxonomy-audit-runtime-v1",
        "category": audit["category"],
        "source_snapshot_sha256": audit["source_snapshot_sha256"],
        "policy": audit["policy"],
    }
    return str(uuid5(_AUDIT_NAMESPACE, hashlib.sha256(_canonical_bytes(identity)).hexdigest()))


def _proposal_identity(audit_id: str) -> str:
    return str(uuid5(_PROPOSAL_NAMESPACE, audit_id))


def _ensure_generation_for_audit(
    *,
    audit_id: str,
    audit: Mapping[str, Any],
    papers: Iterable[Mapping[str, Any]],
    request_actor_kind: str,
    request_actor_id: str,
    metadata_and_cost_acknowledged: bool,
) -> dict[str, Any]:
    request = build_deepseek_pro_request(
        papers,
        audit,
        proposal_id=_proposal_identity(audit_id),
    )
    return enqueue_taxonomy_generation(
        audit_id=audit_id,
        request=request,
        request_actor_kind=request_actor_kind,
        request_actor_id=request_actor_id,
        metadata_and_cost_acknowledged=metadata_and_cost_acknowledged,
    )


def enqueue_generation_for_audit(
    audit_id: str | UUID,
    *,
    request_actor_kind: str,
    request_actor_id: str,
    metadata_and_cost_acknowledged: bool,
) -> dict[str, Any]:
    """Explicitly enqueue one eligible audit after refreshing its metadata.

    This is the public boundary for a reviewed UI/API action.  It reconstructs
    the exact request from the immutable audit and the current minimal paper
    metadata; a changed SHA, path, title-derived topic set, or paper set fails
    before the idempotent generation row is created.
    """

    clean_id = _uuid_text(audit_id, field="taxonomy_audit_id")
    audit = _load_generation_audit(clean_id)
    if audit.get("decision") != "request-proposal":
        raise _fail("taxonomy_generation_audit_not_eligible")
    papers = _load_current_snapshot_papers(audit)
    return _ensure_generation_for_audit(
        audit_id=clean_id,
        audit=audit,
        papers=papers,
        request_actor_kind=request_actor_kind,
        request_actor_id=request_actor_id,
        metadata_and_cost_acknowledged=metadata_and_cost_acknowledged,
    )


def run_audit_cycle(
    *,
    evaluated_at: datetime | str | None = None,
    drive_root: str | None = None,
    policy: AuditPolicy | None = None,
) -> AuditCycleResult:
    """Audit organized categories without ever enqueueing provider work.

    Local audits are safe to schedule automatically.  An authenticated human
    must separately acknowledge metadata sharing and cost at the API boundary
    before an eligible audit can become a generation job.
    """

    active_policy = AuditPolicy() if policy is None else policy
    if not isinstance(active_policy, AuditPolicy):
        raise _fail("taxonomy_audit_policy_invalid")
    now = (
        datetime.now(timezone.utc)
        if evaluated_at is None
        else evaluated_at
    )
    now_text = _timestamp_text(now, field="taxonomy_evaluated_at")
    base = validate_drive_path(
        drive_root or os.environ.get("PAPER_DRIVE_ORIGINAL", "paper-drive:Original"),
        field="taxonomy_drive_root",
    ).rstrip("/")
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for paper in _load_organized_papers():
        grouped[paper["category"]].append(paper)

    no_op = enqueued = pending_generation = reused = failed = 0
    errors: list[str] = []
    for category in sorted(grouped):
        papers = grouped[category]
        try:
            category_path = f"{base}/{category}"
            snapshot = build_source_snapshot(
                papers,
                category=category,
                category_path=category_path,
            )
            snapshot_digest = source_snapshot_sha256(snapshot)
            previous = _existing_eligible_audit(
                category=category,
                snapshot_sha256=snapshot_digest,
                policy=active_policy,
            )
            if previous is not None:
                audit_id, audit = previous
                pending_generation += 1
                reused += 1
                continue
            checkpoint = _latest_substantive_checkpoint(category)
            audit = evaluate_taxonomy_audit(
                papers,
                category=category,
                category_path=category_path,
                evaluated_at=now_text,
                last_review_or_audit_at=checkpoint,
                policy=active_policy,
            )
            if audit["decision"] == "no-op":
                no_op += 1
                continue
            audit_id = _audit_identity(audit)
            try:
                persist_taxonomy_audit(audit, audit_id=audit_id)
            except Exception:
                # A concurrent identical cycle may have won the deterministic
                # ID.  Re-read exact source+policy before deciding this failed.
                raced = _existing_eligible_audit(
                    category=category,
                    snapshot_sha256=snapshot_digest,
                    policy=active_policy,
                )
                if raced is None or raced[0] != audit_id:
                    raise
                audit_id, audit = raced
            pending_generation += 1
        except Exception as exc:
            code, _ = _safe_error_code(exc, fallback="taxonomy_audit_failed")
            errors.append(code)
            failed += 1
    return AuditCycleResult(
        status="completed" if failed == 0 else "partial",
        categories_seen=len(grouped),
        no_op=no_op,
        enqueued=enqueued,
        pending_generation=pending_generation,
        reused=reused,
        failed=failed,
        error_codes=tuple(sorted(errors)),
    )


def _load_generation_audit(audit_id: Any) -> dict[str, Any]:
    with connection() as database:
        row = database.execute(
            """SELECT id, category, category_path, source_snapshot,
                      source_snapshot_sha256, metrics, audit_policy,
                      decision, reason_codes, evaluated_at
               FROM paper_taxonomy_audits WHERE id = %s::uuid""",
            (str(audit_id),),
        ).fetchone()
    if not row:
        raise _fail("taxonomy_generation_audit_missing")
    return _stored_audit(dict(row))


def _load_current_snapshot_papers(audit: Mapping[str, Any]) -> list[dict[str, Any]]:
    snapshot = validate_source_snapshot(audit.get("source_snapshot"))
    paper_ids = [item["paper_id"] for item in snapshot["papers"]]
    if not paper_ids:
        raise _fail("taxonomy_generation_snapshot_empty")
    with connection() as database:
        rows = database.execute(
            """SELECT id, sha256, title, category, drive_path, imported_at
               FROM papers
               WHERE id = ANY(%s) AND processing_status = 'organized'
               ORDER BY id""",
            (paper_ids,),
        ).fetchall()
    papers = [_paper_record(dict(row)) for row in rows]
    if [item["id"] for item in papers] != paper_ids:
        raise _fail("taxonomy_generation_paper_set_stale")
    return papers


def _generation_request(job: Mapping[str, Any]) -> TaxonomyModelRequest:
    validate_taxonomy_generation_job_authorization(job)
    if job.get("model") != DEEPSEEK_PRO_MODEL or job.get("provider") != "deepseek":
        raise _fail("taxonomy_generation_model_invalid")
    audit = _load_generation_audit(job.get("audit_id"))
    if audit.get("decision") != "request-proposal":
        raise _fail("taxonomy_generation_audit_not_eligible")
    papers = _load_current_snapshot_papers(audit)
    request = build_deepseek_pro_request(
        papers,
        audit,
        proposal_id=str(job.get("proposal_id")),
        max_output_tokens=int(job.get("max_output_tokens")),
    )
    validate_model_request(request)
    if request.request_sha256 != str(job.get("provider_body_digest", "")).strip():
        raise _fail("taxonomy_generation_provider_body_digest_stale")
    return request


def execute_generation_one(
    *,
    worker_id: str,
    api_key: str,
    lease_seconds: int = 300,
    endpoint: str | None = None,
    timeout_seconds: int = 120,
    complete: Callable[..., DeepSeekCompletion] = complete_deepseek_request,
) -> RuntimeJobResult:
    """Claim and execute at most one taxonomy proposal generation."""

    worker = _worker_id(worker_id)
    if not isinstance(api_key, str) or not api_key.strip():
        return RuntimeJobResult(
            status="disabled", error_code="deepseek_not_configured"
        )
    clean_api_key = api_key.strip()
    if not re.fullmatch(r"[!-~]{20,512}", clean_api_key):
        return RuntimeJobResult(
            status="disabled", error_code="deepseek_configuration_invalid"
        )
    lease = _positive_seconds(
        lease_seconds,
        field="taxonomy_generation_lease_seconds",
        minimum=30,
        maximum=3_600,
    )
    timeout = _positive_seconds(
        timeout_seconds,
        field="taxonomy_deepseek_timeout_seconds",
        minimum=1,
        maximum=300,
    )
    if lease <= timeout + GENERATION_LEASE_MARGIN_SECONDS:
        raise _fail("taxonomy_generation_lease_too_short")
    job = claim_taxonomy_generation(worker_id=worker, lease_seconds=lease)
    if job is None:
        return RuntimeJobResult(status="idle")
    job_id = str(job["id"])
    lease_token = str(job["lease_token"])
    try:
        request = _generation_request(job)
        # Revalidation is deliberately adjacent to the provider boundary.
        validate_model_request(request)
        validate_taxonomy_generation_job_authorization(job)
        if request.request_sha256 != str(job.get("provider_body_digest", "")).strip():
            raise _fail("taxonomy_generation_provider_body_digest_stale")
        completion = complete(
            request.request_body,
            api_key=clean_api_key,
            endpoint=endpoint
            or os.environ.get(
                "PAPER_DEEPSEEK_URL", "https://api.deepseek.com/chat/completions"
            ),
            timeout_seconds=timeout,
        )
        usage = TaxonomyUsage(
            provider="deepseek",
            model=DEEPSEEK_PRO_MODEL,
            prompt_tokens=completion.usage.prompt_tokens,
            completion_tokens=completion.usage.completion_tokens,
            total_tokens=completion.usage.total_tokens,
        )
        # Record opaque usage before parsing.  Invalid JSON still has a known
        # provider response digest and consumes tokens.
        record_taxonomy_usage(
            generation_job_id=job_id,
            attempt_number=int(job["attempt_count"]),
            request_digest=request.request_sha256,
            response_digest=completion.response_sha256,
            usage=usage,
        )
        if completion.request_sha256 != request.request_sha256:
            raise _fail("taxonomy_generation_transport_digest_mismatch")
        proposal = parse_taxonomy_proposal(completion.content, request)
        store_taxonomy_proposal(
            generation_job_id=job_id,
            worker_id=worker,
            lease_token=lease_token,
            proposal=proposal,
        )
        return RuntimeJobResult(status="succeeded", job_id=job_id)
    except TaxonomyLeaseLost:
        return RuntimeJobResult(
            status="lease_lost",
            job_id=job_id,
            error_code="taxonomy_generation_lease_lost",
        )
    except Exception as exc:
        code, retryable = _safe_error_code(
            exc, fallback="taxonomy_generation_failed"
        )
        try:
            updated = nack_taxonomy_generation(
                generation_job_id=job_id,
                worker_id=worker,
                lease_token=lease_token,
                error_code=code,
                retryable=retryable,
            )
        except TaxonomyLeaseLost:
            return RuntimeJobResult(
                status="lease_lost",
                job_id=job_id,
                error_code="taxonomy_generation_lease_lost",
            )
        return RuntimeJobResult(
            status=str(updated["status"]),
            job_id=job_id,
            error_code=code,
        )


def _absolute_path(value: Any, *, field: str) -> Path:
    try:
        path = Path(value)
    except (TypeError, ValueError) as exc:
        raise _fail(f"{field}_invalid") from exc
    if not path.is_absolute() or path != Path(os.path.abspath(path)):
        raise _fail(f"{field}_invalid")
    return path


def _private_regular_file(value: Any) -> Path:
    path = _absolute_path(value, field="taxonomy_rclone_config")
    try:
        details = path.lstat()
    except OSError as exc:
        raise _fail("taxonomy_rclone_config_invalid") from exc
    if (
        not stat.S_ISREG(details.st_mode)
        or stat.S_ISLNK(details.st_mode)
        or stat.S_IMODE(details.st_mode) & 0o077
        or not 1 <= details.st_size <= 1024 * 1024
    ):
        raise _fail("taxonomy_rclone_config_invalid")
    return path


def _require_rclone_remote(config_path: Path, remote: str) -> None:
    """Confirm the selected remote exists without exposing config contents."""

    parser = configparser.RawConfigParser(interpolation=None, strict=True)
    try:
        with config_path.open("r", encoding="utf-8") as handle:
            parser.read_file(handle)
    except (OSError, UnicodeError, configparser.Error) as exc:
        raise _fail("taxonomy_rclone_config_invalid") from exc
    if remote not in parser.sections():
        raise _fail("taxonomy_rclone_remote_missing")
    if parser.get(remote, "type", fallback="").strip().casefold() != "drive":
        raise _fail("taxonomy_rclone_remote_type_invalid")


def _executable_file(value: Any) -> Path:
    path = _absolute_path(value, field="taxonomy_rclone_binary")
    try:
        details = path.lstat()
    except OSError as exc:
        raise _fail("taxonomy_rclone_binary_invalid") from exc
    if (
        not stat.S_ISREG(details.st_mode)
        or stat.S_ISLNK(details.st_mode)
        or not details.st_mode & stat.S_IXUSR
    ):
        raise _fail("taxonomy_rclone_binary_invalid")
    return path


def _private_directory(value: Any) -> Path:
    path = _absolute_path(value, field="taxonomy_temporary_root")
    try:
        details = path.lstat()
    except OSError as exc:
        raise _fail("taxonomy_temporary_root_invalid") from exc
    if not stat.S_ISDIR(details.st_mode) or stat.S_ISLNK(details.st_mode):
        raise _fail("taxonomy_temporary_root_invalid")
    if stat.S_IMODE(details.st_mode) & 0o022:
        raise _fail("taxonomy_temporary_root_not_private")
    return path


def _default_command_runner(
    argv: Sequence[str], timeout_seconds: int, capture_stdout: bool
) -> CommandResult:
    """Run without a shell and retain at most a fixed amount of stdout."""

    if (
        not isinstance(argv, Sequence)
        or isinstance(argv, (str, bytes))
        or not argv
        or any(not isinstance(item, str) or not item for item in argv)
    ):
        raise _fail("taxonomy_command_invalid")
    output = bytearray()
    overflow = False
    read_failed = False
    process = subprocess.Popen(
        list(argv),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE if capture_stdout else subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=False,
        close_fds=True,
    )
    reader: threading.Thread | None = None
    if capture_stdout:
        if process.stdout is None:
            process.kill()
            raise _fail("taxonomy_command_pipe_missing")

        def drain() -> None:
            nonlocal overflow, read_failed
            try:
                while True:
                    chunk = process.stdout.read(4_096)
                    if not chunk:
                        break
                    remaining = MAX_RCLONE_OUTPUT_BYTES + 1 - len(output)
                    if remaining > 0:
                        output.extend(chunk[:remaining])
                    if (
                        len(output) > MAX_RCLONE_OUTPUT_BYTES
                        or len(chunk) > remaining
                    ):
                        overflow = True
            except OSError:
                read_failed = True

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
    try:
        returncode = process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        process.wait()
        if reader is not None:
            reader.join(timeout=5)
        raise _fail("taxonomy_rclone_timeout", retryable=True) from exc
    if reader is not None:
        reader.join(timeout=5)
        if reader.is_alive():
            raise _fail("taxonomy_rclone_output_incomplete", retryable=True)
    if overflow:
        raise _fail("taxonomy_rclone_output_too_large")
    if read_failed:
        raise _fail("taxonomy_rclone_output_incomplete", retryable=True)
    return CommandResult(returncode=returncode, stdout=bytes(output))


def _rclone(
    config: MoveRuntimeConfig,
    command: Sequence[str],
    *,
    capture: bool,
    command_runner: CommandRunner,
) -> CommandResult:
    argv = [
        str(config.rclone_binary),
        *command,
        "--config",
        str(config.rclone_config),
        "--contimeout",
        "30s",
        "--timeout",
        "5m",
    ]
    try:
        result = command_runner(argv, config.timeout_seconds, capture)
    except TaxonomyRuntimeError:
        raise
    except (OSError, subprocess.SubprocessError) as exc:
        raise _fail("taxonomy_rclone_unavailable", retryable=True) from exc
    if not isinstance(result, CommandResult):
        raise _fail("taxonomy_command_result_invalid")
    if len(result.stdout) > MAX_RCLONE_OUTPUT_BYTES:
        raise _fail("taxonomy_rclone_output_too_large")
    return result


def _remote_parent(path: str) -> tuple[str, str]:
    canonical = validate_drive_path(path, field="taxonomy_remote_path")
    if "/" not in canonical.split(":", 1)[1]:
        raise _fail("taxonomy_remote_parent_missing")
    parent, filename = canonical.rsplit("/", 1)
    if not filename:
        raise _fail("taxonomy_remote_filename_invalid")
    return parent, filename


def _under_drive_root(path: str, root: str) -> str:
    canonical = validate_drive_path(path, field="taxonomy_remote_path")
    if not canonical.startswith(root + "/"):
        raise _fail("taxonomy_remote_path_outside_root")
    if canonical.split(":", 1)[0] != root.split(":", 1)[0]:
        raise _fail("taxonomy_remote_mismatch")
    return canonical


def _remote_root(path: str) -> str:
    canonical = validate_drive_path(path, field="taxonomy_remote_path")
    return canonical.split(":", 1)[0] + ":"


def _remote_entry(
    path: str,
    *,
    config: MoveRuntimeConfig,
    command_runner: CommandRunner,
) -> RemoteEntry | None:
    parent, filename = _remote_parent(path)
    result = _rclone(
        config,
        [
            "lsjson",
            parent,
            "--files-only",
            "--max-depth",
            "1",
            "--no-modtime",
            "--drive-skip-shortcuts",
        ],
        capture=True,
        command_runner=command_runner,
    )
    if result.returncode in {3, 4}:
        return None
    if result.returncode != 0:
        raise _fail("taxonomy_rclone_list_failed", retryable=True)
    try:
        entries = json.loads(result.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise _fail("taxonomy_rclone_list_invalid") from exc
    if not isinstance(entries, list):
        raise _fail("taxonomy_rclone_list_invalid")
    matches: list[RemoteEntry] = []
    for raw in entries:
        if not isinstance(raw, Mapping) or not isinstance(raw.get("Name"), str):
            raise _fail("taxonomy_rclone_list_invalid")
        if raw["Name"] != filename:
            continue
        if (
            raw.get("IsDir") is not False
            or raw.get("MimeType") != "application/pdf"
        ):
            raise _fail("taxonomy_rclone_list_invalid")
        try:
            file_id = validate_drive_file_id(
                raw.get("ID"), field="taxonomy_remote_file_id"
            )
        except TaxonomyContractError as exc:
            raise _fail("taxonomy_rclone_file_id_invalid") from exc
        matches.append(RemoteEntry(file_id=file_id, name=filename))
    if len(matches) > 1:
        raise _fail("taxonomy_remote_path_ambiguous")
    return matches[0] if matches else None


def _download_by_id(
    remote_path: str,
    source_file_id: str,
    local_path: Path,
    *,
    config: MoveRuntimeConfig,
    command_runner: CommandRunner,
) -> None:
    try:
        file_id = validate_drive_file_id(
            source_file_id, field="taxonomy_remote_file_id"
        )
    except TaxonomyContractError as exc:
        raise _fail("taxonomy_rclone_file_id_invalid") from exc
    if local_path.exists() or local_path.is_symlink():
        raise _fail("taxonomy_download_destination_exists")
    result = _rclone(
        config,
        [
            "backend",
            "copyid",
            _remote_root(remote_path),
            file_id,
            str(local_path),
            "--immutable",
            "--checksum",
        ],
        capture=False,
        command_runner=command_runner,
    )
    if result.returncode != 0:
        raise _fail("taxonomy_rclone_download_failed", retryable=True)


def _file_sha256(path: Path) -> str:
    try:
        details = path.lstat()
    except OSError as exc:
        raise _fail("taxonomy_download_missing") from exc
    if (
        not stat.S_ISREG(details.st_mode)
        or stat.S_ISLNK(details.st_mode)
        or not 1 <= details.st_size <= MAX_LOCAL_PDF_BYTES
    ):
        raise _fail("taxonomy_download_invalid")
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise _fail("taxonomy_download_read_failed") from exc
    return digest.hexdigest()


def _move_remote_by_id(
    source: str,
    source_file_id: str,
    destination: str,
    *,
    config: MoveRuntimeConfig,
    command_runner: CommandRunner,
) -> None:
    try:
        file_id = validate_drive_file_id(
            source_file_id, field="taxonomy_remote_file_id"
        )
    except TaxonomyContractError as exc:
        raise _fail("taxonomy_rclone_file_id_invalid") from exc
    if _remote_root(source) != _remote_root(destination):
        raise _fail("taxonomy_remote_mismatch")
    result = _rclone(
        config,
        # These flags are defense in depth.  Drive backend moveid ultimately
        # resolves its own destination, so safety does not rely on the flags:
        # it relies on the exact source ID, pre/post duplicate-aware listings,
        # SHA verification, and never deleting a path during reconciliation.
        [
            "backend",
            "moveid",
            _remote_root(source),
            file_id,
            destination,
            "--immutable",
            "--checksum",
            "--ignore-existing",
        ],
        capture=False,
        command_runner=command_runner,
    )
    if result.returncode != 0:
        raise _fail("taxonomy_rclone_move_failed", retryable=True)


def _temporary_directory(config: MoveRuntimeConfig):
    root = str(config.temporary_root) if config.temporary_root is not None else None
    return tempfile.TemporaryDirectory(prefix="paper-taxonomy-", dir=root)


def _validate_claimed_move_authorization(job: Mapping[str, Any]) -> None:
    """Require the durable human-approval binding before touching Drive."""

    if not isinstance(job, Mapping) or job.get("status") != "running":
        raise _fail("taxonomy_move_job_not_running")
    direction = job.get("direction")
    if direction == "apply":
        valid = (
            job.get("proposal_id") is not None
            and job.get("approval_event_id") is not None
            and job.get("approval_decision") == "approve"
            and job.get("rollback_proposal_id") is None
            and job.get("rollback_approval_event_id") is None
            and job.get("rollback_approval_decision") is None
        )
    elif direction == "rollback":
        valid = (
            job.get("proposal_id") is None
            and job.get("approval_event_id") is None
            and job.get("approval_decision") is None
            and job.get("rollback_proposal_id") is not None
            and job.get("rollback_approval_event_id") is not None
            and job.get("rollback_approval_decision") == "approve"
        )
    else:
        raise _fail("taxonomy_move_direction_invalid")
    if not valid:
        raise _fail("taxonomy_move_human_approval_missing")
    for field in (
        "id",
        "lease_token",
        "proposal_id" if direction == "apply" else "rollback_proposal_id",
        "approval_event_id"
        if direction == "apply"
        else "rollback_approval_event_id",
    ):
        try:
            UUID(str(job.get(field)))
        except (TypeError, ValueError, AttributeError) as exc:
            raise _fail("taxonomy_move_human_approval_invalid") from exc


def _complete_reconciliation_if_moved(
    job: Mapping[str, Any],
    *,
    source: str,
    destination: str,
    destination_file: Path,
    config: MoveRuntimeConfig,
    worker_id: str,
    command_runner: CommandRunner,
) -> bool:
    """Finish only a completed move: source absent and destination exact.

    If both paths exist, even with identical bytes, there is no provider-side
    compare-and-delete primitive that can prove the source has not changed
    between verification and deletion.  Such states therefore remain a
    collision for human reconciliation.
    """

    source_entry = _remote_entry(
        source, config=config, command_runner=command_runner
    )
    destination_entry = _remote_entry(
        destination, config=config, command_runner=command_runner
    )
    raw_bound_id = job.get("source_drive_file_id")
    if source_entry is not None:
        if destination_entry is not None:
            raise _fail("destination_collision")
        if raw_bound_id is not None:
            try:
                bound_id = validate_drive_file_id(
                    raw_bound_id, field="source_drive_file_id"
                )
            except TaxonomyContractError as exc:
                raise _fail("taxonomy_rclone_file_id_invalid") from exc
            if source_entry.file_id != bound_id:
                raise _fail("source_file_id_stale")
        return False
    if destination_entry is None:
        return False
    if raw_bound_id is None:
        raise _fail("source_file_id_unbound")
    try:
        bound_id = validate_drive_file_id(
            raw_bound_id, field="source_drive_file_id"
        )
    except TaxonomyContractError as exc:
        raise _fail("taxonomy_rclone_file_id_invalid") from exc
    if destination_entry.file_id != bound_id:
        raise _fail("destination_file_id_mismatch")
    _download_by_id(
        destination,
        destination_entry.file_id,
        destination_file,
        config=config,
        command_runner=command_runner,
    )
    destination_sha = _file_sha256(destination_file)
    execution = prepare_move_reconciliation(
        job,
        source_is_missing=True,
        observed_destination_sha256=destination_sha,
        observed_destination_path=destination,
        observed_destination_file_id=destination_entry.file_id,
    )
    complete_taxonomy_move_job(execution, worker_id=worker_id)
    return True


def _execute_claimed_move(
    job: Mapping[str, Any],
    *,
    config: MoveRuntimeConfig,
    worker_id: str,
    command_runner: CommandRunner,
) -> None:
    _validate_claimed_move_authorization(job)
    source = _under_drive_root(str(job.get("old_drive_path")), config.drive_root)
    destination = _under_drive_root(
        str(job.get("new_drive_path")), config.drive_root
    )
    expected_sha = str(job.get("source_sha256", "")).strip()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha):
        raise _fail("taxonomy_move_source_sha_invalid")

    with _temporary_directory(config) as temporary_name:
        temporary = Path(temporary_name)
        temporary.chmod(0o700)
        source_file = temporary / "source.pdf"
        destination_file = temporary / "destination.pdf"

        source_entry = _remote_entry(
            source,
            config=config,
            command_runner=command_runner,
        )
        destination_entry = _remote_entry(
            destination,
            config=config,
            command_runner=command_runner,
        )
        if source_entry is None:
            if destination_entry is None:
                raise _fail("source_path_stale")
            if _complete_reconciliation_if_moved(
                job,
                source=source,
                destination=destination,
                destination_file=destination_file,
                config=config,
                worker_id=worker_id,
                command_runner=command_runner,
            ):
                return
            raise _fail("source_path_stale")
        if destination_entry is not None:
            raise _fail("destination_collision")

        already_bound = job.get("source_drive_file_id")
        if already_bound is not None:
            try:
                expected_file_id = validate_drive_file_id(
                    already_bound, field="source_drive_file_id"
                )
            except TaxonomyContractError as exc:
                raise _fail("taxonomy_rclone_file_id_invalid") from exc
            if source_entry.file_id != expected_file_id:
                raise _fail("source_file_id_stale")

        _download_by_id(
            source,
            source_entry.file_id,
            source_file,
            config=config,
            command_runner=command_runner,
        )
        observed_sha = _file_sha256(source_file)
        if observed_sha != expected_sha:
            raise _fail("source_sha_stale")
        bound_job = bind_taxonomy_move_source_identity(
            job,
            observed_source_file_id=source_entry.file_id,
            worker_id=worker_id,
        )
        live_source = _remote_entry(
            source,
            config=config,
            command_runner=command_runner,
        )
        live_destination = _remote_entry(
            destination,
            config=config,
            command_runner=command_runner,
        )
        if live_source is None:
            if live_destination is not None and _complete_reconciliation_if_moved(
                bound_job,
                source=source,
                destination=destination,
                destination_file=destination_file,
                config=config,
                worker_id=worker_id,
                command_runner=command_runner,
            ):
                return
            raise _fail("source_path_stale")
        if live_source.file_id != source_entry.file_id:
            raise _fail("source_file_id_stale")
        if live_destination is not None:
            raise _fail("destination_collision")
        execution = prepare_move_execution(
            bound_job,
            observed_source_sha256=observed_sha,
            observed_source_path=source,
            observed_source_file_id=live_source.file_id,
            destination_exists=False,
        )
        try:
            _move_remote_by_id(
                source,
                execution.source_drive_file_id,
                destination,
                config=config,
                command_runner=command_runner,
            )
        except Exception:
            # A command can lose its process/connection after the provider
            # completed the move.  Reconcile the observable state before nack.
            if _complete_reconciliation_if_moved(
                bound_job,
                source=source,
                destination=destination,
                destination_file=destination_file,
                config=config,
                worker_id=worker_id,
                command_runner=command_runner,
            ):
                return
            raise
        post_source = _remote_entry(
            source, config=config, command_runner=command_runner
        )
        post_destination = _remote_entry(
            destination, config=config, command_runner=command_runner
        )
        if post_source is not None:
            if post_destination is not None:
                raise _fail("destination_collision")
            if post_source.file_id != execution.source_drive_file_id:
                raise _fail("source_file_id_stale")
            raise _fail("taxonomy_move_source_still_present", retryable=True)
        if post_destination is None:
            raise _fail("taxonomy_move_destination_missing", retryable=True)
        if post_destination.file_id != execution.source_drive_file_id:
            raise _fail("destination_file_id_mismatch")
        _download_by_id(
            destination,
            post_destination.file_id,
            destination_file,
            config=config,
            command_runner=command_runner,
        )
        if _file_sha256(destination_file) != expected_sha:
            raise _fail("taxonomy_move_destination_sha_mismatch")
        complete_taxonomy_move_job(execution, worker_id=worker_id)


def execute_move_one(
    *,
    worker_id: str,
    config: MoveRuntimeConfig,
    lease_seconds: int = 1_800,
    command_runner: CommandRunner = _default_command_runner,
) -> RuntimeJobResult:
    """Claim and execute at most one approved Drive move."""

    worker = _worker_id(worker_id)
    if not isinstance(config, MoveRuntimeConfig):
        raise _fail("taxonomy_move_config_invalid")
    # Revalidate files and the configured Google Drive remote immediately
    # before claiming so a broken runtime configuration cannot consume a job.
    config = MoveRuntimeConfig(
        drive_root=config.drive_root,
        rclone_binary=config.rclone_binary,
        rclone_config=config.rclone_config,
        timeout_seconds=config.timeout_seconds,
        temporary_root=config.temporary_root,
    )
    lease = _positive_seconds(
        lease_seconds,
        field="taxonomy_move_lease_seconds",
        minimum=60,
        maximum=3_600,
    )
    if lease <= config.timeout_seconds + MOVE_LEASE_MARGIN_SECONDS:
        raise _fail("taxonomy_move_lease_too_short")
    job = claim_taxonomy_move_job(worker_id=worker, lease_seconds=lease)
    if job is None:
        return RuntimeJobResult(status="idle")
    job_id = str(job["id"])
    lease_token = str(job["lease_token"])
    command_deadline = time.monotonic() + lease - MOVE_LEASE_MARGIN_SECONDS

    def lease_bounded_runner(
        argv: Sequence[str], timeout_seconds: int, capture_stdout: bool
    ) -> CommandResult:
        remaining = int(command_deadline - time.monotonic())
        if remaining < 1:
            raise _fail("taxonomy_move_lease_budget_exhausted", retryable=True)
        return command_runner(argv, min(timeout_seconds, remaining), capture_stdout)

    try:
        _execute_claimed_move(
            job,
            config=config,
            worker_id=worker,
            command_runner=lease_bounded_runner,
        )
        return RuntimeJobResult(status="succeeded", job_id=job_id)
    except TaxonomyLeaseLost:
        return RuntimeJobResult(
            status="lease_lost",
            job_id=job_id,
            error_code="taxonomy_move_lease_lost",
        )
    except Exception as exc:
        code, retryable = _safe_error_code(exc, fallback="taxonomy_move_failed")
        try:
            updated = nack_taxonomy_move_job(
                move_job_id=job_id,
                worker_id=worker,
                lease_token=lease_token,
                error_code=code,
                retryable=retryable,
            )
        except TaxonomyLeaseLost:
            return RuntimeJobResult(
                status="lease_lost",
                job_id=job_id,
                error_code="taxonomy_move_lease_lost",
            )
        return RuntimeJobResult(
            status=str(updated["status"]),
            job_id=job_id,
            error_code=code,
        )


def run_once(
    *,
    worker_id: str,
    move_config: MoveRuntimeConfig,
    api_key: str | None,
    include_audit: bool = True,
    generation_lease_seconds: int = 300,
    generation_timeout_seconds: int = 120,
    move_lease_seconds: int = 1_800,
    command_runner: CommandRunner = _default_command_runner,
) -> RuntimeCycleResult:
    """Run each stage independently so one failure cannot suppress the next."""

    try:
        audit = (
            run_audit_cycle()
            if include_audit
            else AuditCycleResult("skipped")
        )
    except Exception as exc:
        code, _ = _safe_error_code(exc, fallback="taxonomy_audit_failed")
        audit = AuditCycleResult(status="failed", failed=1, error_codes=(code,))
    try:
        generation = execute_generation_one(
            worker_id=worker_id,
            api_key=api_key or "",
            lease_seconds=generation_lease_seconds,
            timeout_seconds=generation_timeout_seconds,
        )
    except Exception as exc:
        code, _ = _safe_error_code(exc, fallback="taxonomy_generation_failed")
        generation = RuntimeJobResult(status="failed", error_code=code)
    try:
        move = execute_move_one(
            worker_id=worker_id,
            config=move_config,
            lease_seconds=move_lease_seconds,
            command_runner=command_runner,
        )
    except Exception as exc:
        code, _ = _safe_error_code(exc, fallback="taxonomy_move_failed")
        move = RuntimeJobResult(status="failed", error_code=code)
    return RuntimeCycleResult(audit=audit, generation=generation, move=move)


def _move_config_from_environment() -> MoveRuntimeConfig:
    return MoveRuntimeConfig(
        drive_root=os.environ.get("PAPER_DRIVE_ORIGINAL", "paper-drive:Original"),
        rclone_binary=Path(
            os.environ.get("PAPER_RCLONE_BIN", "/usr/local/bin/rclone")
        ),
        rclone_config=Path(
            os.environ.get("PAPER_RCLONE_CONFIG", "/config/rclone/rclone.conf")
        ),
        timeout_seconds=_positive_seconds(
            os.environ.get("PAPER_TAXONOMY_RCLONE_TIMEOUT_SECONDS", "900"),
            field="taxonomy_rclone_timeout_seconds",
            minimum=30,
            maximum=3_000,
        ),
        temporary_root=(
            Path(os.environ["PAPER_TAXONOMY_TMP_DIR"])
            if os.environ.get("PAPER_TAXONOMY_TMP_DIR")
            else None
        ),
    )


@contextmanager
def _background_heartbeat(default_worker_id: str):
    """Keep liveness reporting fully isolated from taxonomy processing."""

    heartbeat = None
    candidate = None
    try:
        candidate = worker_heartbeat_from_env(default_worker_id)
        candidate.start()
        heartbeat = candidate
    except Exception:
        if candidate is not None:
            try:
                candidate.stop()
            except Exception:
                pass
    try:
        yield heartbeat
    finally:
        if heartbeat is not None:
            try:
                heartbeat.stop()
            except Exception:
                pass


def run_worker() -> None:
    """Run forever without emitting paper, credential, or command data."""

    ensure_schema()
    worker = _worker_id(
        os.environ.get("PAPER_TAXONOMY_WORKER_ID", "taxonomy-worker-1")
    )
    poll_seconds = _positive_seconds(
        os.environ.get("PAPER_TAXONOMY_POLL_SECONDS", "15"),
        field="taxonomy_poll_seconds",
        minimum=5,
        maximum=300,
    )
    audit_interval = _positive_seconds(
        os.environ.get("PAPER_TAXONOMY_AUDIT_INTERVAL_SECONDS", "43200"),
        field="taxonomy_audit_interval_seconds",
        minimum=3_600,
        maximum=604_800,
    )
    config = _move_config_from_environment()
    generation_lease_seconds = _positive_seconds(
        os.environ.get("PAPER_TAXONOMY_GENERATION_LEASE_SECONDS", "300"),
        field="taxonomy_generation_lease_seconds",
        minimum=30,
        maximum=3_600,
    )
    generation_timeout_seconds = _positive_seconds(
        os.environ.get("PAPER_DEEPSEEK_TIMEOUT_SECONDS", "120"),
        field="taxonomy_deepseek_timeout_seconds",
        minimum=1,
        maximum=300,
    )
    if (
        generation_lease_seconds
        <= generation_timeout_seconds + GENERATION_LEASE_MARGIN_SECONDS
    ):
        raise _fail("taxonomy_generation_lease_too_short")
    move_lease_seconds = _positive_seconds(
        os.environ.get("PAPER_TAXONOMY_MOVE_LEASE_SECONDS", "1800"),
        field="taxonomy_move_lease_seconds",
        minimum=60,
        maximum=3_600,
    )
    if move_lease_seconds <= config.timeout_seconds + MOVE_LEASE_MARGIN_SECONDS:
        raise _fail("taxonomy_move_lease_too_short")
    next_audit_at = 0.0
    with _background_heartbeat("taxonomy-worker-1"):
        while True:
            now = time.monotonic()
            include_audit = now >= next_audit_at
            run_once(
                worker_id=worker,
                move_config=config,
                api_key=deepseek_api_key(),
                include_audit=include_audit,
                generation_lease_seconds=generation_lease_seconds,
                generation_timeout_seconds=generation_timeout_seconds,
                move_lease_seconds=move_lease_seconds,
            )
            if include_audit:
                next_audit_at = now + audit_interval
            time.sleep(poll_seconds)


if __name__ == "__main__":
    run_worker()


__all__ = [
    "AuditCycleResult",
    "CommandResult",
    "MoveRuntimeConfig",
    "RuntimeCycleResult",
    "RuntimeJobResult",
    "TaxonomyRuntimeError",
    "enqueue_generation_for_audit",
    "execute_generation_one",
    "execute_move_one",
    "run_audit_cycle",
    "run_once",
    "run_worker",
]

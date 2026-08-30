"""Approval-gated, crash-recoverable Reader v2 reprocess/rollback worker.

The worker is deliberately sequential.  It never publishes a reprocessed
artifact until its immutable version ID has been bound to the leased job, and
it never repeats an external mutation when the durable authorization boundary
reports that a previous attempt already succeeded.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import stat
import time
from typing import Any, Optional
from uuid import UUID

from .artifact_versions import (
    MAX_FILE_BYTES,
    ArtifactVersionError,
    resolve_current_artifact,
    rollback_current_version,
    safe_relative_artifact_path,
)
from .database import connection, ensure_schema
from .ops_store import (
    OperationsLeaseLost,
    OperationsStoreError,
    authorize_reprocess_job,
    authorize_rollback_job,
    bind_reprocess_result_version,
    claim_reprocess_job,
    claim_rollback_job,
    complete_reprocess_job,
    complete_rollback_job,
    get_rollback_request,
    nack_reprocess_job,
    nack_rollback_job,
)
from .reader_v2 import (
    PreparedPaperV2Version,
    ReaderV2Failure,
    activate_prepared_paper_v2_version,
    prepare_paper_v2_version,
    resume_prepared_paper_v2_version,
)


DEFAULT_LEASE_SECONDS = 1_800
DEFAULT_POLL_SECONDS = 15
DEFAULT_RETRY_DELAY_SECONDS = 60
MAX_JOBS_PER_CYCLE = 10

_WORKER_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}")
_SAFE_CODE_RE = re.compile(r"[a-z][a-z0-9_]{0,119}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_VERSION_RE = re.compile(r"rv-[0-9a-f]{32}")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_OPERATIONS = ("reprocess", "rollback")
_RESULT_STATUSES = frozenset(
    {"idle", "succeeded", "failed", "retry_wait", "lease_lost", "error"}
)


class ReprocessRunnerError(RuntimeError):
    """A content-free, stable worker boundary failure."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class OperationResult:
    """Safe result suitable for metrics; it contains no paper content/path."""

    status: str
    operation: str | None = None
    job_id: str | None = None
    paper_id: int | None = None
    error_code: str | None = None
    recovered: bool = False

    def __post_init__(self) -> None:
        if self.status not in _RESULT_STATUSES:
            raise RuntimeError("operations_result_invalid")
        if self.operation is not None and self.operation not in _OPERATIONS:
            raise RuntimeError("operations_result_invalid")
        if self.job_id is not None:
            _uuid(self.job_id, field="job_id")
        if self.paper_id is not None and (
            isinstance(self.paper_id, bool)
            or not isinstance(self.paper_id, int)
            or self.paper_id < 1
        ):
            raise RuntimeError("operations_result_invalid")
        if self.error_code is not None and not _SAFE_CODE_RE.fullmatch(
            self.error_code
        ):
            raise RuntimeError("operations_result_invalid")
        if self.status == "idle" and any(
            value is not None
            for value in (self.operation, self.job_id, self.paper_id, self.error_code)
        ):
            raise RuntimeError("operations_result_invalid")
        if type(self.recovered) is not bool or (
            self.recovered and self.status != "succeeded"
        ):
            raise RuntimeError("operations_result_invalid")


@dataclass(frozen=True)
class OperationCycleResult:
    results: tuple[OperationResult, ...]
    claimed: int
    succeeded: int
    failed: int

    def __post_init__(self) -> None:
        values = (self.claimed, self.succeeded, self.failed)
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in values
        ):
            raise RuntimeError("operations_cycle_result_invalid")
        if self.claimed != sum(item.job_id is not None for item in self.results):
            raise RuntimeError("operations_cycle_result_invalid")
        if self.succeeded != sum(
            item.status == "succeeded" for item in self.results
        ) or self.failed != sum(
            item.status in {"failed", "retry_wait", "lease_lost", "error"}
            for item in self.results
        ):
            raise RuntimeError("operations_cycle_result_invalid")


@dataclass(frozen=True)
class _ClaimIdentity:
    operation: str
    job_id: str
    request_id: str
    paper_id: int
    lease_token: str
    planned_version_id: str | None


PrepareVersion = Callable[[dict[str, Any], Path], PreparedPaperV2Version]
ActivateVersion = Callable[
    [PreparedPaperV2Version], Optional[Mapping[str, Any]]
]
ResumeVersion = Callable[
    [dict[str, Any], Path, str, Any], PreparedPaperV2Version
]


def _worker_id(value: Any) -> str:
    if not isinstance(value, str) or not _WORKER_ID_RE.fullmatch(value):
        raise RuntimeError("PAPER_OPS_WORKER_ID_invalid")
    return value


def _bounded_integer(
    name: str,
    value: Any,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool):
        raise RuntimeError(f"{name}_invalid")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
        parsed = int(value)
    else:
        raise RuntimeError(f"{name}_invalid")
    if not minimum <= parsed <= maximum:
        raise RuntimeError(f"{name}_invalid")
    return parsed


def _data_dir(value: str | Path) -> Path:
    if not isinstance(value, (str, Path)):
        raise RuntimeError("PAPER_DATA_DIR_invalid")
    raw = os.fspath(value)
    if not raw or len(raw) > 4_096 or _CONTROL_RE.search(raw):
        raise RuntimeError("PAPER_DATA_DIR_invalid")
    path = Path(raw)
    if not path.is_absolute():
        raise RuntimeError("PAPER_DATA_DIR_invalid")
    try:
        metadata = path.lstat()
    except (OSError, ValueError) as exc:
        raise RuntimeError("PAPER_DATA_DIR_invalid") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeError("PAPER_DATA_DIR_invalid")
    if not os.access(path, os.R_OK | os.W_OK | os.X_OK):
        raise RuntimeError("PAPER_DATA_DIR_invalid")
    return path


def _uuid(value: Any, *, field: str) -> str:
    try:
        parsed = UUID(str(value))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ReprocessRunnerError(f"operations_{field}_invalid") from exc
    if parsed.int == 0 or str(parsed) != str(value):
        raise ReprocessRunnerError(f"operations_{field}_invalid")
    return str(parsed)


def _positive_paper_id(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ReprocessRunnerError("operations_paper_id_invalid")
    return value


def _version(value: Any, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not _VERSION_RE.fullmatch(value):
        raise ReprocessRunnerError("operations_version_id_invalid")
    return value


def _safe_code(value: Any, *, fallback: str) -> str:
    if isinstance(value, str) and _SAFE_CODE_RE.fullmatch(value):
        return value
    return fallback


def _safe_error(exc: BaseException, *, operation: str) -> str:
    fallback = f"{operation}_execution_failed"
    if isinstance(exc, ReprocessRunnerError):
        return _safe_code(exc.code, fallback=fallback)
    if isinstance(exc, OperationsStoreError):
        code = getattr(exc, "code", None)
        if isinstance(code, str) and code.startswith(
            ("operations_", "reprocess_", "rollback_")
        ):
            return _safe_code(code, fallback=fallback)
        return fallback
    if isinstance(exc, ReaderV2Failure):
        code = getattr(exc, "code", None)
        if isinstance(code, str) and code.startswith("reader_v2_"):
            return _safe_code(code, fallback=fallback)
        return fallback
    if isinstance(exc, ArtifactVersionError):
        return fallback
    return fallback


def _claim_identity(operation: str, job: Any) -> _ClaimIdentity:
    if operation not in _OPERATIONS or not isinstance(job, Mapping):
        raise ReprocessRunnerError("operations_claim_invalid")
    planned = job.get("planned_result_version_id")
    return _ClaimIdentity(
        operation=operation,
        job_id=_uuid(job.get("id"), field="job_id"),
        request_id=_uuid(job.get("request_id"), field="request_id"),
        paper_id=_positive_paper_id(job.get("paper_id")),
        lease_token=_uuid(job.get("lease_token"), field="lease_token"),
        planned_version_id=(
            _version(planned, nullable=True) if operation == "reprocess" else None
        ),
    )


def _load_paper(paper_id: int) -> dict[str, Any]:
    """Read only the metadata required by the converter and source verifier."""

    with connection() as database:
        row = database.execute(
            """SELECT id, title, authors, publication_year, doi, category,
                      paper_type, stored_path, sha256
               FROM papers
               WHERE id = %s AND processing_status = 'organized'""",
            (paper_id,),
        ).fetchone()
    if not isinstance(row, Mapping):
        raise ReprocessRunnerError("operations_paper_missing")
    paper = dict(row)
    if _positive_paper_id(paper.get("id")) != paper_id:
        raise ReprocessRunnerError("operations_paper_invalid")
    if not isinstance(paper.get("sha256"), str) or not _SHA256_RE.fullmatch(
        paper["sha256"]
    ):
        raise ReprocessRunnerError("operations_paper_invalid")
    try:
        paper["stored_path"] = safe_relative_artifact_path(paper.get("stored_path"))
    except ArtifactVersionError as exc:
        raise ReprocessRunnerError("operations_paper_invalid") from exc
    return paper


def _open_directory_no_follow(path: Path) -> int:
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise ReprocessRunnerError("operations_source_unsafe") from exc
    if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise ReprocessRunnerError("operations_source_unsafe")
    return descriptor


def _actual_source_sha256(data_dir: Path, paper: Mapping[str, Any]) -> str:
    """Hash the actual source through an all-no-follow directory walk."""

    try:
        relative = PurePosixPath(
            safe_relative_artifact_path(paper.get("stored_path"))
        )
    except ArtifactVersionError as exc:
        raise ReprocessRunnerError("operations_source_unsafe") from exc
    directory = _open_directory_no_follow(data_dir)
    source = -1
    try:
        for part in ("library", "raw", *relative.parts[:-1]):
            try:
                child = os.open(
                    part,
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=directory,
                )
            except FileNotFoundError as exc:
                raise ReprocessRunnerError("operations_source_missing") from exc
            except OSError as exc:
                raise ReprocessRunnerError("operations_source_unsafe") from exc
            os.close(directory)
            directory = child
        try:
            source = os.open(
                relative.parts[-1],
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory,
            )
        except FileNotFoundError as exc:
            raise ReprocessRunnerError("operations_source_missing") from exc
        except OSError as exc:
            raise ReprocessRunnerError("operations_source_unsafe") from exc
        before = os.fstat(source)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_FILE_BYTES:
            raise ReprocessRunnerError("operations_source_unsafe")
        digest = hashlib.sha256()
        consumed = 0
        while chunk := os.read(source, 1024 * 1024):
            consumed += len(chunk)
            if consumed > MAX_FILE_BYTES:
                raise ReprocessRunnerError("operations_source_unsafe")
            digest.update(chunk)
        after = os.fstat(source)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ReprocessRunnerError("operations_source_changed")
        return digest.hexdigest()
    finally:
        if source >= 0:
            os.close(source)
        os.close(directory)


def _actual_current_version(data_dir: Path, paper_id: int) -> str | None:
    try:
        resolution = resolve_current_artifact(
            data_dir,
            paper_id,
            allow_legacy_v2=True,
        )
    except ArtifactVersionError as exc:
        if str(exc) == "current_artifact_missing":
            return None
        raise ReprocessRunnerError("operations_current_artifact_invalid") from exc
    return _version(resolution.version_id, nullable=True)


def _authorization_status(
    value: Any,
    *,
    identity: _ClaimIdentity,
) -> tuple[str, str | None, str | None]:
    if not isinstance(value, Mapping):
        raise ReprocessRunnerError("operations_authorization_invalid")
    if _uuid(value.get("id"), field="job_id") != identity.job_id:
        raise ReprocessRunnerError("operations_authorization_invalid")
    status = value.get("status")
    if status not in {"running", "succeeded", "failed"}:
        raise ReprocessRunnerError("operations_authorization_invalid")
    planned = value.get("planned_result_version_id")
    if identity.operation == "reprocess":
        planned = _version(planned, nullable=True)
    else:
        planned = None
    error = value.get("last_error_code")
    if status == "failed":
        fallback = f"{identity.operation}_authorization_failed"
        error = (
            _safe_code(error, fallback=fallback)
            if isinstance(error, str)
            and error.startswith(f"{identity.operation}_")
            else fallback
        )
    elif error is not None and status != "succeeded":
        raise ReprocessRunnerError("operations_authorization_invalid")
    return status, planned, error


def _rollback_target(identity: _ClaimIdentity) -> str:
    stored = get_rollback_request(identity.request_id)
    if (
        not isinstance(stored, Mapping)
        or stored.get("request_id") != identity.request_id
        or stored.get("decision") != "approved"
    ):
        raise ReprocessRunnerError("rollback_request_invalid")
    request = stored.get("request")
    targets = request.get("targets") if isinstance(request, Mapping) else None
    if not isinstance(targets, Sequence) or isinstance(targets, (str, bytes)):
        raise ReprocessRunnerError("rollback_request_invalid")
    matches = [
        target
        for target in targets
        if isinstance(target, Mapping) and target.get("paper_id") == identity.paper_id
    ]
    if len(matches) != 1:
        raise ReprocessRunnerError("rollback_target_invalid")
    return str(_version(matches[0].get("rollback_to_version_id")))


def _default_prepare(
    paper: dict[str, Any], data_dir: Path
) -> PreparedPaperV2Version:
    return prepare_paper_v2_version(
        paper,
        data_dir,
        force_reprocess=True,
    )


def _validate_prepared(
    prepared: Any,
    *,
    paper: Mapping[str, Any],
    data_dir: Path,
    expected_current_version_id: str | None,
) -> PreparedPaperV2Version:
    if not isinstance(prepared, PreparedPaperV2Version):
        raise ReprocessRunnerError("reprocess_prepared_invalid")
    if (
        prepared.paper_id != paper["id"]
        or prepared.data_dir != data_dir
        or prepared.source_sha256 != paper["sha256"]
        or prepared.expected_current_version_id != expected_current_version_id
        or prepared.reused_current
        or prepared.staged is None
        or _version(prepared.version_id) != prepared.version_id
    ):
        raise ReprocessRunnerError("reprocess_prepared_invalid")
    return prepared


def _validate_completion(
    value: Any,
    *,
    identity: _ClaimIdentity,
    expected_version: str,
) -> None:
    if (
        not isinstance(value, Mapping)
        or value.get("status") != "succeeded"
        or _uuid(value.get("id"), field="job_id") != identity.job_id
        or _version(value.get("result_version_id")) != expected_version
    ):
        raise ReprocessRunnerError("operations_completion_invalid")


def _execute_reprocess(
    identity: _ClaimIdentity,
    *,
    worker_id: str,
    data_dir: Path,
    prepare_version: PrepareVersion,
    resume_version: ResumeVersion,
    activate_version: ActivateVersion,
) -> OperationResult:
    paper = _load_paper(identity.paper_id)
    actual_sha = _actual_source_sha256(data_dir, paper)
    before_version = _actual_current_version(data_dir, identity.paper_id)
    authorized = authorize_reprocess_job(
        identity.job_id,
        worker_id=worker_id,
        lease_token=identity.lease_token,
        actual_source_sha256=actual_sha,
        actual_current_version_id=before_version,
    )
    status, planned, error = _authorization_status(
        authorized,
        identity=identity,
    )
    if status == "succeeded":
        return OperationResult(
            "succeeded",
            "reprocess",
            identity.job_id,
            identity.paper_id,
            recovered=True,
        )
    if status == "failed":
        return OperationResult(
            "failed",
            "reprocess",
            identity.job_id,
            identity.paper_id,
            error_code=error,
        )

    prepared = (
        resume_version(
            paper,
            data_dir,
            planned,
            expected_current_version_id=before_version,
        )
        if planned is not None
        else prepare_version(paper, data_dir)
    )
    prepared = _validate_prepared(
        prepared,
        paper=paper,
        data_dir=data_dir,
        expected_current_version_id=before_version,
    )
    planned = prepared.version_id
    bound = bind_reprocess_result_version(
        identity.job_id,
        worker_id=worker_id,
        lease_token=identity.lease_token,
        planned_result_version_id=planned,
    )
    if (
        not isinstance(bound, Mapping)
        or bound.get("status") != "running"
        or _version(bound.get("planned_result_version_id")) != planned
    ):
        raise ReprocessRunnerError("reprocess_bind_invalid")
    # Refuse to overwrite a pointer that changed after authorization.
    if _actual_current_version(data_dir, identity.paper_id) != before_version:
        raise ReprocessRunnerError("reprocess_current_changed_before_publish")
    activate_version(prepared)
    actual_current = _actual_current_version(data_dir, identity.paper_id)
    if actual_current != planned:
        raise ReprocessRunnerError("reprocess_result_not_current")
    completed = complete_reprocess_job(
        identity.job_id,
        worker_id=worker_id,
        lease_token=identity.lease_token,
        actual_current_version_id=actual_current,
    )
    _validate_completion(
        completed,
        identity=identity,
        expected_version=planned,
    )
    return OperationResult(
        "succeeded",
        "reprocess",
        identity.job_id,
        identity.paper_id,
    )


def _execute_rollback(
    identity: _ClaimIdentity,
    *,
    worker_id: str,
    data_dir: Path,
) -> OperationResult:
    paper = _load_paper(identity.paper_id)
    actual_sha = _actual_source_sha256(data_dir, paper)
    before_version = _actual_current_version(data_dir, identity.paper_id)
    if before_version is None:
        raise ReprocessRunnerError("rollback_current_missing")
    authorized = authorize_rollback_job(
        identity.job_id,
        worker_id=worker_id,
        lease_token=identity.lease_token,
        actual_source_sha256=actual_sha,
        actual_current_version_id=before_version,
    )
    status, _planned, error = _authorization_status(
        authorized,
        identity=identity,
    )
    if status == "succeeded":
        return OperationResult(
            "succeeded",
            "rollback",
            identity.job_id,
            identity.paper_id,
            recovered=True,
        )
    if status == "failed":
        return OperationResult(
            "failed",
            "rollback",
            identity.job_id,
            identity.paper_id,
            error_code=error,
        )
    target = _rollback_target(identity)
    if _actual_current_version(data_dir, identity.paper_id) != before_version:
        raise ReprocessRunnerError("rollback_current_changed_before_publish")
    applied = rollback_current_version(
        data_dir,
        identity.paper_id,
        target,
        raw_source_relative=paper["stored_path"],
        dry_run=False,
        expected_current_version_id=before_version,
    )
    if (
        not isinstance(applied, Mapping)
        or applied.get("paper_id") != identity.paper_id
        or applied.get("from_version_id") != before_version
        or applied.get("to_version_id") != target
        or applied.get("dry_run") is not False
        or applied.get("changed") is not True
    ):
        raise ReprocessRunnerError("rollback_result_invalid")
    actual_current = _actual_current_version(data_dir, identity.paper_id)
    if actual_current != target:
        raise ReprocessRunnerError("rollback_result_not_current")
    completed = complete_rollback_job(
        identity.job_id,
        worker_id=worker_id,
        lease_token=identity.lease_token,
        actual_current_version_id=actual_current,
    )
    _validate_completion(
        completed,
        identity=identity,
        expected_version=target,
    )
    return OperationResult(
        "succeeded",
        "rollback",
        identity.job_id,
        identity.paper_id,
    )


def _nack(
    identity: _ClaimIdentity,
    *,
    worker_id: str,
    error_code: str,
    retry_delay_seconds: int,
) -> OperationResult:
    function = (
        nack_reprocess_job
        if identity.operation == "reprocess"
        else nack_rollback_job
    )
    updated = function(
        identity.job_id,
        worker_id=worker_id,
        lease_token=identity.lease_token,
        error_code=error_code,
        retry_delay_seconds=retry_delay_seconds,
    )
    status = updated.get("status") if isinstance(updated, Mapping) else None
    if status not in {"retry_wait", "failed"}:
        return OperationResult(
            "error",
            identity.operation,
            identity.job_id,
            identity.paper_id,
            error_code=f"{identity.operation}_nack_result_invalid",
        )
    return OperationResult(
        status,
        identity.operation,
        identity.job_id,
        identity.paper_id,
        error_code=error_code,
    )


def run_once(
    *,
    worker_id: str,
    data_dir: str | Path,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    retry_delay_seconds: int = DEFAULT_RETRY_DELAY_SECONDS,
    preferred_operation: str = "reprocess",
    prepare_version: PrepareVersion = _default_prepare,
    resume_version: ResumeVersion = resume_prepared_paper_v2_version,
    activate_version: ActivateVersion = activate_prepared_paper_v2_version,
) -> OperationResult:
    """Claim and execute at most one approved operation."""

    clean_worker = _worker_id(worker_id)
    root = _data_dir(data_dir)
    lease = _bounded_integer(
        "PAPER_OPS_LEASE_SECONDS",
        lease_seconds,
        minimum=60,
        maximum=3_600,
    )
    retry_delay = _bounded_integer(
        "PAPER_OPS_RETRY_DELAY_SECONDS",
        retry_delay_seconds,
        minimum=1,
        maximum=86_400,
    )
    if preferred_operation not in _OPERATIONS:
        raise RuntimeError("PAPER_OPS_PREFERRED_OPERATION_invalid")
    if not all(callable(item) for item in (prepare_version, resume_version, activate_version)):
        raise RuntimeError("operations_callback_invalid")
    order = (
        preferred_operation,
        _OPERATIONS[1] if preferred_operation == _OPERATIONS[0] else _OPERATIONS[0],
    )
    job: Any = None
    operation: str | None = None
    try:
        for candidate in order:
            function = (
                claim_reprocess_job if candidate == "reprocess" else claim_rollback_job
            )
            job = function(worker_id=clean_worker, lease_seconds=lease)
            if job is not None:
                operation = candidate
                break
    except Exception:
        return OperationResult("error", error_code="operations_claim_failed")
    if operation is None:
        return OperationResult("idle")
    try:
        identity = _claim_identity(operation, job)
    except Exception:
        return OperationResult("error", error_code="operations_claim_invalid")

    try:
        if operation == "reprocess":
            return _execute_reprocess(
                identity,
                worker_id=clean_worker,
                data_dir=root,
                prepare_version=prepare_version,
                resume_version=resume_version,
                activate_version=activate_version,
            )
        return _execute_rollback(
            identity,
            worker_id=clean_worker,
            data_dir=root,
        )
    except OperationsLeaseLost:
        return OperationResult(
            "lease_lost",
            operation,
            identity.job_id,
            identity.paper_id,
            error_code=f"{operation}_lease_lost",
        )
    except Exception as exc:
        error_code = _safe_error(exc, operation=operation)
        try:
            return _nack(
                identity,
                worker_id=clean_worker,
                error_code=error_code,
                retry_delay_seconds=retry_delay,
            )
        except OperationsLeaseLost:
            return OperationResult(
                "lease_lost",
                operation,
                identity.job_id,
                identity.paper_id,
                error_code=f"{operation}_lease_lost",
            )
        except Exception:
            return OperationResult(
                "error",
                operation,
                identity.job_id,
                identity.paper_id,
                error_code=f"{operation}_nack_failed",
            )


def run_cycle(
    *,
    worker_id: str,
    data_dir: str | Path,
    max_jobs: int = 1,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    retry_delay_seconds: int = DEFAULT_RETRY_DELAY_SECONDS,
    prepare_version: PrepareVersion = _default_prepare,
    resume_version: ResumeVersion = resume_prepared_paper_v2_version,
    activate_version: ActivateVersion = activate_prepared_paper_v2_version,
) -> OperationCycleResult:
    """Run a bounded sequential cycle, alternating queue preference."""

    limit = _bounded_integer(
        "PAPER_OPS_MAX_JOBS_PER_CYCLE",
        max_jobs,
        minimum=1,
        maximum=MAX_JOBS_PER_CYCLE,
    )
    results: list[OperationResult] = []
    for index in range(limit):
        result = run_once(
            worker_id=worker_id,
            data_dir=data_dir,
            lease_seconds=lease_seconds,
            retry_delay_seconds=retry_delay_seconds,
            preferred_operation=_OPERATIONS[index % len(_OPERATIONS)],
            prepare_version=prepare_version,
            resume_version=resume_version,
            activate_version=activate_version,
        )
        if result.status == "idle":
            break
        results.append(result)
        if result.status == "error" and result.job_id is None:
            break
    return OperationCycleResult(
        results=tuple(results),
        claimed=sum(item.job_id is not None for item in results),
        succeeded=sum(item.status == "succeeded" for item in results),
        failed=sum(
            item.status in {"failed", "retry_wait", "lease_lost", "error"}
            for item in results
        ),
    )


def run_worker() -> None:
    """Optional standalone loop; the Reader v2 worker can call run_cycle instead."""

    ensure_schema()
    worker = _worker_id(
        os.environ.get("PAPER_OPS_WORKER_ID", "reader-v2-ops-worker-1")
    )
    data_dir = _data_dir(Path(os.environ.get("PAPER_DATA_DIR", "/data")))
    poll_seconds = _bounded_integer(
        "PAPER_OPS_POLL_SECONDS",
        os.environ.get("PAPER_OPS_POLL_SECONDS", str(DEFAULT_POLL_SECONDS)),
        minimum=1,
        maximum=300,
    )
    lease_seconds = _bounded_integer(
        "PAPER_OPS_LEASE_SECONDS",
        os.environ.get("PAPER_OPS_LEASE_SECONDS", str(DEFAULT_LEASE_SECONDS)),
        minimum=60,
        maximum=3_600,
    )
    retry_delay_seconds = _bounded_integer(
        "PAPER_OPS_RETRY_DELAY_SECONDS",
        os.environ.get(
            "PAPER_OPS_RETRY_DELAY_SECONDS",
            str(DEFAULT_RETRY_DELAY_SECONDS),
        ),
        minimum=1,
        maximum=86_400,
    )
    max_jobs = _bounded_integer(
        "PAPER_OPS_MAX_JOBS_PER_CYCLE",
        os.environ.get("PAPER_OPS_MAX_JOBS_PER_CYCLE", "1"),
        minimum=1,
        maximum=MAX_JOBS_PER_CYCLE,
    )
    while True:
        result = run_cycle(
            worker_id=worker,
            data_dir=data_dir,
            max_jobs=max_jobs,
            lease_seconds=lease_seconds,
            retry_delay_seconds=retry_delay_seconds,
        )
        if not any(item.job_id is not None for item in result.results):
            time.sleep(poll_seconds)


if __name__ == "__main__":
    run_worker()


__all__ = [
    "DEFAULT_LEASE_SECONDS",
    "DEFAULT_POLL_SECONDS",
    "DEFAULT_RETRY_DELAY_SECONDS",
    "MAX_JOBS_PER_CYCLE",
    "OperationCycleResult",
    "OperationResult",
    "ReprocessRunnerError",
    "run_cycle",
    "run_once",
    "run_worker",
]

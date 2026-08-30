"""One-at-a-time DeepSeek worker for reviewed paper-knowledge drafts.

The worker binds the exact bounded request to its database lease before any
paper text leaves the server.  It records only opaque IDs, digests, token
counts, and sanitized error codes; prompt and response bodies are never logged.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import time
from typing import Any

from .database import connection, ensure_schema, list_annotations
from .deepseek_client import DeepSeekTransportError, complete_deepseek_request
from .knowledge_store import (
    KnowledgeLeaseLost,
    KnowledgeStoreError,
    bind_generation_request,
    claim_generation_job,
    complete_generation_job,
    nack_generation_job,
    record_generation_usage,
)
from .knowledge_worker import (
    GenerationPolicy,
    GenerationUsage,
    KnowledgeGenerationError,
    build_deepseek_compatible_request,
    build_generation_request,
    parse_generation_response,
)
from .metadata import deepseek_api_key
from .ops_runtime import worker_heartbeat_from_env
from .reader_v2 import read_article


_WORKER_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}")
_SAFE_ERROR_RE = re.compile(r"[a-z0-9][a-z0-9_:-]{0,119}")


@dataclass(frozen=True)
class KnowledgeWorkerResult:
    status: str
    job_id: str | None = None
    error_code: str | None = None


def _worker_id(value: str) -> str:
    if not isinstance(value, str) or not _WORKER_ID_RE.fullmatch(value):
        raise RuntimeError("knowledge_worker_id_invalid")
    return value


def _positive_seconds(name: str, raw: str, *, minimum: int, maximum: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{name}_invalid") from exc
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name}_invalid")
    return value


def _paper_for_generation(paper_id: int) -> dict[str, Any]:
    with connection() as database:
        row = database.execute(
            """SELECT id, sha256, title, authors, publication_year, doi,
                      reader_v2_status, reader_v2_manifest_path
               FROM papers
               WHERE id = %s AND processing_status = 'organized'""",
            (paper_id,),
        ).fetchone()
    if row is None:
        raise KnowledgeStoreError("generation_paper_not_found")
    return dict(row)


def _policy(job: dict[str, Any]) -> GenerationPolicy:
    return GenerationPolicy(
        max_source_pages=int(job["max_source_pages"]),
        max_source_characters=int(job["max_source_characters"]),
        max_output_tokens=int(job["max_output_tokens"]),
        include_personal_annotations=bool(job["include_personal_annotations"]),
    )


def _error_code(exc: BaseException) -> tuple[str, bool]:
    if isinstance(exc, DeepSeekTransportError):
        return exc.code, exc.retryable
    if isinstance(exc, KnowledgeStoreError):
        return exc.code if _SAFE_ERROR_RE.fullmatch(exc.code) else "knowledge_store_error", False
    if isinstance(exc, KnowledgeGenerationError):
        value = str(exc)
        return value if _SAFE_ERROR_RE.fullmatch(value) else "knowledge_contract_error", False
    return "knowledge_worker_failed", False


def _heartbeat_processing(heartbeat: Any | None, paper_id: Any) -> bool:
    if (
        heartbeat is None
        or isinstance(paper_id, bool)
        or not isinstance(paper_id, int)
        or paper_id < 1
    ):
        return False
    try:
        heartbeat.processing(paper_id)
        return True
    except Exception:
        return False


def _heartbeat_idle(
    heartbeat: Any | None,
    active: bool,
    *,
    successful: bool,
) -> None:
    if not active:
        return
    try:
        heartbeat.idle(successful=successful)
    except Exception:
        return


def execute_one(
    *,
    worker_id: str,
    data_dir: str | Path,
    api_key: str,
    lease_seconds: int = 300,
    retry_delay_seconds: int = 60,
    heartbeat: Any | None = None,
) -> KnowledgeWorkerResult:
    """Claim and execute at most one job, returning only sanitized state."""

    clean_worker = _worker_id(worker_id)
    if not isinstance(api_key, str) or not api_key.strip():
        raise RuntimeError("deepseek_not_configured")
    root = Path(data_dir)
    job = claim_generation_job(
        worker_id=clean_worker,
        lease_seconds=lease_seconds,
    )
    if job is None:
        return KnowledgeWorkerResult(status="idle")

    job_id = str(job["id"])
    lease_token = str(job["lease_token"])
    heartbeat_active = _heartbeat_processing(heartbeat, job.get("paper_id"))
    heartbeat_successful = False
    try:
        paper_id = int(job["paper_id"])
        paper = _paper_for_generation(paper_id)
        article = read_article(root, paper_id)
        annotations = (
            list_annotations(paper_id)
            if bool(job["include_personal_annotations"])
            else []
        )
        request = build_generation_request(
            paper,
            article,
            provider=str(job["provider"]),
            model=str(job["model"]),
            policy=_policy(job),
            annotations=annotations,
        )
        bind_generation_request(
            job_id,
            worker_id=clean_worker,
            lease_token=lease_token,
            request=request,
            data_dir=root,
        )
        body = build_deepseek_compatible_request(request)
        body["thinking"] = {
            "type": "enabled" if request.model == "deepseek-v4-pro" else "disabled"
        }
        completion = complete_deepseek_request(
            body,
            api_key=api_key,
            endpoint=os.environ.get(
                "PAPER_DEEPSEEK_URL", "https://api.deepseek.com/chat/completions"
            ),
            timeout_seconds=_positive_seconds(
                "PAPER_DEEPSEEK_TIMEOUT_SECONDS",
                os.environ.get("PAPER_DEEPSEEK_TIMEOUT_SECONDS", "120"),
                minimum=1,
                maximum=300,
            ),
        )
        usage = GenerationUsage(
            provider=request.provider,
            model=request.model,
            prompt_tokens=completion.usage.prompt_tokens,
            completion_tokens=completion.usage.completion_tokens,
            total_tokens=completion.usage.total_tokens,
        )
        record_generation_usage(
            job_id,
            worker_id=clean_worker,
            lease_token=lease_token,
            usage=usage,
            generation_request_digest=request.request_digest,
            generation_response_digest=completion.response_sha256,
        )
        canonical = parse_generation_response(completion.content, request)
        complete_generation_job(
            job_id,
            worker_id=clean_worker,
            lease_token=lease_token,
            payload=canonical,
            data_dir=root,
            usage=usage,
            generation_request=request,
            generation_response_digest=completion.response_sha256,
        )
        heartbeat_successful = True
        return KnowledgeWorkerResult(status="succeeded", job_id=job_id)
    except KnowledgeLeaseLost:
        return KnowledgeWorkerResult(
            status="lease_lost",
            job_id=job_id,
            error_code="generation_job_lease_lost",
        )
    except Exception as exc:
        code, retryable = _error_code(exc)
        try:
            updated = nack_generation_job(
                job_id,
                worker_id=clean_worker,
                lease_token=lease_token,
                error_code=code,
                retry_delay_seconds=retry_delay_seconds if retryable else 0,
            )
        except KnowledgeLeaseLost:
            return KnowledgeWorkerResult(
                status="lease_lost",
                job_id=job_id,
                error_code="generation_job_lease_lost",
            )
        return KnowledgeWorkerResult(
            status=str(updated["status"]),
            job_id=job_id,
            error_code=code,
        )
    finally:
        _heartbeat_idle(
            heartbeat,
            heartbeat_active,
            successful=heartbeat_successful,
        )


def run_worker() -> None:
    ensure_schema()
    worker_id = _worker_id(
        os.environ.get("PAPER_KNOWLEDGE_WORKER_ID", "knowledge-worker-1")
    )
    poll_seconds = _positive_seconds(
        "PAPER_KNOWLEDGE_POLL_SECONDS",
        os.environ.get("PAPER_KNOWLEDGE_POLL_SECONDS", "15"),
        minimum=5,
        maximum=300,
    )
    lease_seconds = _positive_seconds(
        "PAPER_KNOWLEDGE_LEASE_SECONDS",
        os.environ.get("PAPER_KNOWLEDGE_LEASE_SECONDS", "300"),
        minimum=30,
        maximum=3_600,
    )
    data_dir = Path(os.environ.get("PAPER_DATA_DIR", "/data"))
    with worker_heartbeat_from_env(worker_id) as heartbeat:
        while True:
            key = deepseek_api_key()
            if not key:
                time.sleep(poll_seconds)
                continue
            try:
                result = execute_one(
                    worker_id=worker_id,
                    data_dir=data_dir,
                    api_key=key,
                    lease_seconds=lease_seconds,
                    heartbeat=heartbeat,
                )
            except Exception:
                time.sleep(poll_seconds)
                continue
            if result.status == "idle":
                time.sleep(poll_seconds)


if __name__ == "__main__":
    run_worker()


__all__ = ["KnowledgeWorkerResult", "execute_one", "run_worker"]

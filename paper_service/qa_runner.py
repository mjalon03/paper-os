"""One-at-a-time DeepSeek worker for grounded paper question answering.

The database owns the job, lease, evidence snapshot, and consent decision.  A
worker must therefore call :func:`load_qa_model_request` immediately before the
provider call.  That function revalidates the current paper source, reviewed
knowledge, and any explicit annotation-sharing consent.  This module never
logs questions, evidence, answers, API keys, or provider response bodies.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import os
from pathlib import Path
import re
import time
from typing import Any
from uuid import UUID

from .database import ensure_schema
from .deepseek_client import (
    DeepSeekTransportError,
    complete_deepseek_request,
)
from .metadata import deepseek_api_key
from .ops_runtime import worker_heartbeat_from_env
from .qa_contract import (
    QAContractError,
    build_qa_prompt_payload,
    load_qa_response_json,
    safe_json_dumps,
    validate_qa_request,
)
from .search_store import (
    QALeaseLost,
    QAUsage,
    SearchStoreError,
    claim_qa_job,
    complete_qa_job,
    load_qa_model_request,
    nack_qa_job,
    record_qa_usage,
)


DEFAULT_QA_MODEL = "deepseek-v4-flash"
DEFAULT_MAX_OUTPUT_TOKENS = 4_096
MAX_OUTPUT_TOKENS = 8_192

_WORKER_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}")
_SAFE_ERROR_RE = re.compile(r"[a-z0-9][a-z0-9_:-]{0,119}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_API_KEY_RE = re.compile(r"[!-~]{20,512}")
_RESULT_STATUSES = frozenset(
    {"idle", "succeeded", "retry_wait", "failed", "lease_lost", "error"}
)


class QARunnerError(RuntimeError):
    """A sanitized worker-boundary failure."""

    def __init__(self, code: str, *, retryable: bool = False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class QAWorkerResult:
    """Safe worker result containing no question, evidence, or answer text."""

    status: str
    job_id: str | None = None
    error_code: str | None = None

    def __post_init__(self) -> None:
        if self.status not in _RESULT_STATUSES:
            raise RuntimeError("qa_worker_result_invalid")
        if self.job_id is not None:
            _uuid(self.job_id, field="job_id")
        if self.error_code is not None and not _SAFE_ERROR_RE.fullmatch(
            self.error_code
        ):
            raise RuntimeError("qa_worker_result_invalid")


@dataclass(frozen=True)
class _PreparedRequest:
    provider: str
    model: str
    request: dict[str, Any]
    prompt: dict[str, Any]


def _worker_id(value: Any) -> str:
    if not isinstance(value, str) or not _WORKER_ID_RE.fullmatch(value):
        raise RuntimeError("qa_worker_id_invalid")
    return value


def _bounded_integer(
    name: str,
    raw: Any,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(raw, bool):
        raise RuntimeError(f"{name}_invalid")
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, str) and re.fullmatch(r"[0-9]+", raw):
        value = int(raw)
    else:
        raise RuntimeError(f"{name}_invalid")
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name}_invalid")
    return value


def _api_key(value: Any) -> str:
    if not isinstance(value, str) or not _API_KEY_RE.fullmatch(value):
        raise RuntimeError("deepseek_not_configured")
    return value


def _uuid(value: Any, *, field: str) -> str:
    try:
        parsed = UUID(str(value))
    except (AttributeError, TypeError, ValueError) as exc:
        raise QARunnerError(f"qa_{field}_invalid") from exc
    if parsed.int == 0:
        raise QARunnerError(f"qa_{field}_invalid")
    return str(parsed)


def _safe_code(value: Any, *, fallback: str) -> str:
    if isinstance(value, str) and _SAFE_ERROR_RE.fullmatch(value):
        return value
    return fallback


def _error_details(exc: BaseException) -> tuple[str, bool]:
    if isinstance(exc, DeepSeekTransportError):
        return (
            _safe_code(exc.code, fallback="deepseek_transport_failed"),
            exc.retryable,
        )
    if isinstance(exc, QARunnerError):
        return _safe_code(exc.code, fallback="qa_worker_failed"), exc.retryable
    if isinstance(exc, SearchStoreError):
        return _safe_code(exc.code, fallback="qa_store_failed"), False
    if isinstance(exc, QAContractError):
        return "qa_contract_invalid", False
    return "qa_worker_failed", True


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


def _validate_loaded_request(
    loaded: Any,
    *,
    claimed_job: Mapping[str, Any],
    job_id: str,
) -> _PreparedRequest:
    if not isinstance(loaded, Mapping):
        raise QARunnerError("qa_model_request_invalid")
    if _uuid(loaded.get("job_id"), field="job_id") != job_id:
        raise QARunnerError("qa_model_request_job_mismatch")

    provider = loaded.get("provider")
    model = loaded.get("model")
    if provider != "deepseek" or claimed_job.get("provider") != provider:
        raise QARunnerError("qa_provider_not_allowed")
    if (
        not isinstance(model, str)
        or model != DEFAULT_QA_MODEL
        or claimed_job.get("model") != model
    ):
        raise QARunnerError("qa_model_not_allowed")

    include_annotations = claimed_job.get("include_personal_annotations")
    if type(include_annotations) is not bool:
        raise QARunnerError("qa_annotation_policy_invalid")
    try:
        request = validate_qa_request(loaded.get("request"))
        expected_prompt = build_qa_prompt_payload(
            request,
            include_user_annotations=include_annotations,
        )
    except QAContractError as exc:
        raise QARunnerError("qa_model_request_invalid") from exc
    if loaded.get("prompt") != expected_prompt:
        raise QARunnerError("qa_prompt_mismatch")

    request_digest = loaded.get("request_digest")
    if (
        not isinstance(request_digest, str)
        or not _SHA256_RE.fullmatch(request_digest)
        or claimed_job.get("request_digest") != request_digest
    ):
        raise QARunnerError("qa_request_digest_mismatch")
    attempt_number = loaded.get("attempt_number")
    if (
        isinstance(attempt_number, bool)
        or not isinstance(attempt_number, int)
        or attempt_number < 1
        or claimed_job.get("attempt_count") != attempt_number
    ):
        raise QARunnerError("qa_attempt_mismatch")
    return _PreparedRequest(
        provider=provider,
        model=model,
        request=request,
        prompt=expected_prompt,
    )


def _deepseek_body(
    prepared: _PreparedRequest,
    *,
    max_output_tokens: int,
) -> dict[str, Any]:
    prompt = prepared.prompt
    instructions = prompt["instructions"]
    if not isinstance(instructions, str) or not instructions:
        raise QARunnerError("qa_prompt_invalid")
    user_payload = dict(prompt)
    del user_payload["instructions"]
    try:
        user_content = safe_json_dumps(user_payload)
    except QAContractError as exc:
        raise QARunnerError("qa_prompt_invalid") from exc
    return {
        "model": prepared.model,
        "messages": [
            {"role": "system", "content": instructions},
            {"role": "user", "content": user_content},
        ],
        "thinking": {"type": "enabled"},
        "response_format": {"type": "json_object"},
        "temperature": 0,
        "max_tokens": max_output_tokens,
        "stream": False,
    }


def _usage(prepared: _PreparedRequest, completion: Any) -> QAUsage:
    try:
        return QAUsage(
            provider=prepared.provider,
            model=prepared.model,
            prompt_tokens=completion.usage.prompt_tokens,
            completion_tokens=completion.usage.completion_tokens,
            total_tokens=completion.usage.total_tokens,
        )
    except (AttributeError, SearchStoreError) as exc:
        raise QARunnerError("qa_provider_usage_invalid") from exc


def execute_one(
    *,
    worker_id: str,
    data_dir: str | Path,
    api_key: str,
    lease_seconds: int = 300,
    retry_delay_seconds: int = 60,
    timeout_seconds: int = 120,
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    heartbeat: Any | None = None,
) -> QAWorkerResult:
    """Claim and execute at most one job without exposing sensitive content."""

    clean_worker = _worker_id(worker_id)
    clean_key = _api_key(api_key)
    clean_lease = _bounded_integer(
        "PAPER_QA_LEASE_SECONDS",
        lease_seconds,
        minimum=30,
        maximum=3_600,
    )
    clean_retry_delay = _bounded_integer(
        "PAPER_QA_RETRY_DELAY_SECONDS",
        retry_delay_seconds,
        minimum=1,
        maximum=86_400,
    )
    clean_timeout = _bounded_integer(
        "PAPER_DEEPSEEK_TIMEOUT_SECONDS",
        timeout_seconds,
        minimum=1,
        maximum=300,
    )
    clean_max_tokens = _bounded_integer(
        "PAPER_QA_MAX_OUTPUT_TOKENS",
        max_output_tokens,
        minimum=1,
        maximum=MAX_OUTPUT_TOKENS,
    )
    if not isinstance(data_dir, (str, Path)) or not str(data_dir):
        raise RuntimeError("qa_data_dir_invalid")
    root = Path(data_dir)

    try:
        job = claim_qa_job(clean_worker, lease_seconds=clean_lease)
    except Exception:
        return QAWorkerResult(status="error", error_code="qa_claim_failed")
    if job is None:
        return QAWorkerResult(status="idle")
    if not isinstance(job, Mapping):
        return QAWorkerResult(status="error", error_code="qa_claim_invalid")

    try:
        job_id = _uuid(job.get("id"), field="job_id")
        lease_token = _uuid(job.get("lease_token"), field="lease_token")
    except QARunnerError as exc:
        return QAWorkerResult(status="error", error_code=exc.code)

    heartbeat_active = _heartbeat_processing(heartbeat, job.get("paper_id"))
    heartbeat_successful = False
    try:
        # This is deliberately the final source/consent boundary before egress.
        loaded = load_qa_model_request(job_id, lease_token, data_dir=root)
        prepared = _validate_loaded_request(
            loaded,
            claimed_job=job,
            job_id=job_id,
        )
        body = _deepseek_body(prepared, max_output_tokens=clean_max_tokens)
        completion = complete_deepseek_request(
            body,
            api_key=clean_key,
            endpoint=os.environ.get(
                "PAPER_DEEPSEEK_URL",
                "https://api.deepseek.com/chat/completions",
            ),
            timeout_seconds=clean_timeout,
        )
        usage = _usage(prepared, completion)
        try:
            canonical = load_qa_response_json(
                completion.content,
                request=prepared.request,
            )
        except QAContractError as exc:
            # The transport succeeded and billed tokens even though its content
            # failed our contract.  Preserve only usage and the raw envelope SHA.
            record_qa_usage(
                job_id,
                lease_token,
                response_digest=completion.response_sha256,
                usage=usage,
            )
            raise QARunnerError("qa_response_invalid", retryable=True) from exc

        # Successful usage is written only inside complete_qa_job, alongside
        # the canonical response digest.  Pre-recording the raw transport digest
        # here would create an append-only ledger conflict.
        complete_qa_job(
            job_id,
            lease_token,
            response=canonical,
            usage=usage,
        )
        heartbeat_successful = True
        return QAWorkerResult(status="succeeded", job_id=job_id)
    except QALeaseLost:
        return QAWorkerResult(
            status="lease_lost",
            job_id=job_id,
            error_code="qa_lease_lost",
        )
    except Exception as exc:
        code, retryable = _error_details(exc)
        try:
            updated = nack_qa_job(
                job_id,
                lease_token,
                error_code=code,
                retry_delay_seconds=clean_retry_delay if retryable else 1,
            )
        except QALeaseLost:
            return QAWorkerResult(
                status="lease_lost",
                job_id=job_id,
                error_code="qa_lease_lost",
            )
        except Exception:
            return QAWorkerResult(
                status="error",
                job_id=job_id,
                error_code="qa_nack_failed",
            )
        status = updated.get("status") if isinstance(updated, Mapping) else None
        if status not in {"retry_wait", "failed"}:
            return QAWorkerResult(
                status="error",
                job_id=job_id,
                error_code="qa_nack_result_invalid",
            )
        return QAWorkerResult(status=status, job_id=job_id, error_code=code)
    finally:
        _heartbeat_idle(
            heartbeat,
            heartbeat_active,
            successful=heartbeat_successful,
        )


def run_worker() -> None:
    """Run the sequential QA worker loop with fail-fast configuration checks."""

    ensure_schema()
    worker_id = _worker_id(os.environ.get("PAPER_QA_WORKER_ID", "qa-worker-1"))
    poll_seconds = _bounded_integer(
        "PAPER_QA_POLL_SECONDS",
        os.environ.get("PAPER_QA_POLL_SECONDS", "15"),
        minimum=5,
        maximum=300,
    )
    lease_seconds = _bounded_integer(
        "PAPER_QA_LEASE_SECONDS",
        os.environ.get("PAPER_QA_LEASE_SECONDS", "300"),
        minimum=30,
        maximum=3_600,
    )
    retry_delay_seconds = _bounded_integer(
        "PAPER_QA_RETRY_DELAY_SECONDS",
        os.environ.get("PAPER_QA_RETRY_DELAY_SECONDS", "60"),
        minimum=1,
        maximum=86_400,
    )
    timeout_seconds = _bounded_integer(
        "PAPER_DEEPSEEK_TIMEOUT_SECONDS",
        os.environ.get("PAPER_DEEPSEEK_TIMEOUT_SECONDS", "120"),
        minimum=1,
        maximum=300,
    )
    max_output_tokens = _bounded_integer(
        "PAPER_QA_MAX_OUTPUT_TOKENS",
        os.environ.get("PAPER_QA_MAX_OUTPUT_TOKENS", str(DEFAULT_MAX_OUTPUT_TOKENS)),
        minimum=1,
        maximum=MAX_OUTPUT_TOKENS,
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
                    retry_delay_seconds=retry_delay_seconds,
                    timeout_seconds=timeout_seconds,
                    max_output_tokens=max_output_tokens,
                    heartbeat=heartbeat,
                )
            except Exception:
                time.sleep(poll_seconds)
                continue
            if result.status in {"idle", "lease_lost", "error"}:
                time.sleep(poll_seconds)


if __name__ == "__main__":
    run_worker()


__all__ = [
    "DEFAULT_MAX_OUTPUT_TOKENS",
    "DEFAULT_QA_MODEL",
    "MAX_OUTPUT_TOKENS",
    "QARunnerError",
    "QAWorkerResult",
    "execute_one",
    "run_worker",
]

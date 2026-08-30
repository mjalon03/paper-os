"""Failure-isolated runtime helpers for Paper OS operational state.

The module is intentionally adapter-only: it reports worker state through
``ops_store``, evaluates configured services, and drains generic operational
webhooks.  It never logs or returns webhook URLs, credentials, payloads, paper
titles, paper text, filesystem paths, or exception messages.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import http.client
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import ssl
import stat
import socket
import threading
import time
from typing import Any, Callable, Mapping, Protocol
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from uuid import UUID

from .database import ensure_schema

from .operations import (
    WEBHOOK_SCHEMA,
    validate_health_snapshot,
    validate_worker_heartbeat,
)
from .ops_store import (
    ack_webhook_item,
    claim_webhook_item,
    configure_expected_workers,
    get_worker_health_snapshot,
    nack_webhook_item,
    record_health_snapshot,
    upsert_worker_heartbeat,
)


MAX_WEBHOOK_REQUEST_BYTES = 16 * 1024
DEFAULT_WEBHOOK_RESPONSE_BYTES = 4 * 1024
MAX_WEBHOOK_RESPONSE_BYTES = 64 * 1024
MAX_RUNTIME_SERVICES = 16

_IDENTIFIER_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_CODE_RE = re.compile(r"[a-z][a-z0-9_]{0,119}")
_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_INCIDENT_RE = re.compile(r"incident-[0-9a-f]{32}")
_EVENT_RE = re.compile(r"event-[0-9a-f]{32}")
_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_DNS_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_DNS_TLD_RE = re.compile(r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?")
_HEALTH_REASON_CODES = frozenset(
    {"worker_degraded", "worker_missing", "worker_stale", "worker_stopped"}
)
_RUNTIME_PREFIX = "PAPER_OPS_RUNTIME_"
_HEARTBEAT_PREFIX = "PAPER_OPS_HEARTBEAT_"
_HEARTBEAT_ENV_KEYS = frozenset(
    {
        "PAPER_OPS_HEARTBEAT_ENABLED",
        "PAPER_OPS_HEARTBEAT_SERVICE_ID",
        "PAPER_OPS_HEARTBEAT_WORKER_ID",
        "PAPER_OPS_HEARTBEAT_INTERVAL_SECONDS",
    }
)
_ENV_KEYS = frozenset(
    {
        "PAPER_OPS_RUNTIME_SERVICES_JSON",
        "PAPER_OPS_RUNTIME_MONITOR_INTERVAL_SECONDS",
        "PAPER_OPS_RUNTIME_STALE_AFTER_SECONDS",
        "PAPER_OPS_RUNTIME_STARTUP_GRACE_SECONDS",
        "PAPER_OPS_RUNTIME_WEBHOOK_URL",
        "PAPER_OPS_RUNTIME_WEBHOOK_ALLOWED_HOSTS_JSON",
        "PAPER_OPS_RUNTIME_WEBHOOK_AUTH_TOKEN_FILE",
        "PAPER_OPS_RUNTIME_WEBHOOK_WORKER_ID",
        "PAPER_OPS_RUNTIME_WEBHOOK_TIMEOUT_SECONDS",
        "PAPER_OPS_RUNTIME_WEBHOOK_RESPONSE_BYTES",
        "PAPER_OPS_RUNTIME_WEBHOOK_LEASE_SECONDS",
        "PAPER_OPS_RUNTIME_WEBHOOK_RETRY_DELAY_SECONDS",
        "PAPER_OPS_RUNTIME_WEBHOOK_MAX_PER_CYCLE",
    }
)

_logger = logging.getLogger(__name__)


class OpsRuntimeError(RuntimeError):
    """A sanitized runtime configuration or contract failure."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class WebhookTransportError(OpsRuntimeError):
    """A sanitized webhook transport failure."""


@dataclass(frozen=True)
class WorkerHeartbeatResult:
    service_id: str
    worker_id: str
    state: str
    sequence: int
    report_count: int
    failure_count: int
    result_code: str


@dataclass(frozen=True)
class ServiceMonitorResult:
    service_id: str
    expected_worker_count: int
    observed_worker_count: int
    incident_event_count: int
    result_code: str


@dataclass(frozen=True)
class WebhookItemResult:
    outbox_id: str | None
    result_code: str


@dataclass(frozen=True)
class WebhookBatchResult:
    result_code: str
    claimed_count: int
    acknowledged_count: int
    nacked_count: int
    failed_count: int
    items: tuple[WebhookItemResult, ...]


@dataclass(frozen=True)
class MonitorCycleResult:
    service_count: int
    successful_service_count: int
    failed_service_count: int
    services: tuple[ServiceMonitorResult, ...]
    webhook: WebhookBatchResult
    result_code: str


@dataclass(frozen=True)
class MonitorLoopResult:
    cycle_count: int
    failed_cycle_count: int
    result_code: str


@dataclass(frozen=True, repr=False)
class WebhookDeliveryConfig:
    url: str | None
    allowed_hosts: tuple[str, ...]
    auth_token: str | None
    worker_id: str
    timeout_seconds: int
    response_bytes: int
    lease_seconds: int
    retry_delay_seconds: int
    max_per_cycle: int

    def __post_init__(self) -> None:
        if self.url is None:
            if self.auth_token is not None:
                raise _fail("webhook_auth_without_url")
            if self.allowed_hosts != ():
                raise _fail("webhook_allowed_hosts_without_url")
        else:
            allowed_hosts = _allowed_hosts(self.allowed_hosts)
            _webhook_url(self.url, allowed_hosts=allowed_hosts)
            if self.auth_token is not None and not re.fullmatch(
                r"[!-~]{20,2048}", self.auth_token
            ):
                raise _fail("webhook_auth_token_invalid")
        _identifier(self.worker_id, field="webhook_worker_id")
        _bounded_int(
            self.timeout_seconds,
            field="webhook_timeout_seconds",
            minimum=1,
            maximum=30,
        )
        _bounded_int(
            self.response_bytes,
            field="webhook_response_bytes",
            minimum=1,
            maximum=MAX_WEBHOOK_RESPONSE_BYTES,
        )
        _bounded_int(
            self.lease_seconds,
            field="webhook_lease_seconds",
            minimum=10,
            maximum=3_600,
        )
        _bounded_int(
            self.retry_delay_seconds,
            field="webhook_retry_delay_seconds",
            minimum=0,
            maximum=86_400,
        )
        _bounded_int(
            self.max_per_cycle,
            field="webhook_max_per_cycle",
            minimum=1,
            maximum=100,
        )
        if self.lease_seconds <= self.timeout_seconds:
            raise _fail("webhook_lease_too_short")

    def __repr__(self) -> str:
        state = "enabled" if self.url is not None else "disabled"
        return f"WebhookDeliveryConfig(state={state!r})"


@dataclass(frozen=True, repr=False)
class OpsRuntimeConfig:
    services: tuple[tuple[str, tuple[str, ...]], ...]
    monitor_interval_seconds: int
    stale_after_seconds: int
    startup_grace_seconds: int
    webhook: WebhookDeliveryConfig

    def __post_init__(self) -> None:
        if not isinstance(self.services, tuple) or not 1 <= len(
            self.services
        ) <= MAX_RUNTIME_SERVICES:
            raise _fail("runtime_services_invalid")
        normalized: list[tuple[str, tuple[str, ...]]] = []
        for item in self.services:
            if not isinstance(item, tuple) or len(item) != 2:
                raise _fail("runtime_services_invalid")
            service, workers = item
            clean_service = _identifier(service, field="service_id")
            if not isinstance(workers, tuple) or not 1 <= len(workers) <= 64:
                raise _fail("runtime_expected_workers_invalid")
            clean_workers = tuple(
                _identifier(worker, field="worker_id") for worker in workers
            )
            if clean_workers != tuple(sorted(clean_workers)) or len(
                clean_workers
            ) != len(set(clean_workers)):
                raise _fail("runtime_expected_workers_invalid")
            normalized.append((clean_service, clean_workers))
        if tuple(normalized) != tuple(sorted(normalized, key=lambda value: value[0])):
            raise _fail("runtime_services_invalid")
        if len({service for service, _workers in normalized}) != len(normalized):
            raise _fail("runtime_services_invalid")
        _bounded_int(
            self.monitor_interval_seconds,
            field="monitor_interval_seconds",
            minimum=5,
            maximum=3_600,
        )
        _bounded_int(
            self.stale_after_seconds,
            field="stale_after_seconds",
            minimum=10,
            maximum=86_400,
        )
        if self.stale_after_seconds < self.monitor_interval_seconds * 2:
            raise _fail("runtime_stale_window_too_short")
        _bounded_int(
            self.startup_grace_seconds,
            field="startup_grace_seconds",
            minimum=0,
            maximum=3_600,
        )
        if 0 < self.startup_grace_seconds < 30:
            raise _fail("runtime_startup_grace_too_short")
        if not isinstance(self.webhook, WebhookDeliveryConfig):
            raise _fail("webhook_config_invalid")

    def __repr__(self) -> str:
        state = "enabled" if self.webhook.url is not None else "disabled"
        return (
            "OpsRuntimeConfig("
            f"service_count={len(self.services)}, webhook={state!r})"
        )


class _Response(Protocol):
    headers: Any
    status: int

    def read(self, amount: int = -1) -> bytes: ...

    def geturl(self) -> str: ...

    def __enter__(self) -> "_Response": ...

    def __exit__(self, *args: Any) -> None: ...


def _fail(code: str) -> OpsRuntimeError:
    return OpsRuntimeError(code)


def _identifier(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        raise _fail(f"{field}_invalid")
    return value


def _code(value: Any, *, field: str = "error_code") -> str:
    if not isinstance(value, str) or not _CODE_RE.fullmatch(value):
        raise _fail(f"{field}_invalid")
    return value


def _bounded_int(value: Any, *, field: str, minimum: int, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise _fail(f"{field}_invalid")
    return value


def _positive_paper_id(value: Any) -> int:
    return _bounded_int(
        value, field="active_paper_id", minimum=1, maximum=9_223_372_036_854_775_807
    )


def _utc_timestamp(clock: Callable[[], datetime]) -> str:
    value = clock()
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise _fail("heartbeat_clock_invalid")
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_emit(event: Mapping[str, Any]) -> None:
    """Log an allowlisted operational event without exception interpolation."""

    allowed = {"service_id", "worker_id", "result_code", "count"}
    if not isinstance(event, Mapping) or not set(event).issubset(allowed):
        return
    values: dict[str, Any] = {}
    try:
        if "service_id" in event:
            values["service_id"] = _identifier(
                event["service_id"], field="service_id"
            )
        if "worker_id" in event:
            values["worker_id"] = _identifier(event["worker_id"], field="worker_id")
        if "result_code" in event:
            values["result_code"] = _code(event["result_code"], field="result_code")
        if "count" in event:
            values["count"] = _bounded_int(
                event["count"], field="count", minimum=0, maximum=1_000_000_000
            )
    except OpsRuntimeError:
        return
    _logger.info(
        "paper_ops_runtime service_id=%s worker_id=%s result_code=%s count=%s",
        values.get("service_id", "none"),
        values.get("worker_id", "none"),
        values.get("result_code", "none"),
        values.get("count", 0),
    )


def _emit_safely(
    emit: Callable[[Mapping[str, Any]], None], event: Mapping[str, Any]
) -> None:
    try:
        emit(event)
    except Exception:
        return


class WorkerHeartbeat:
    """Non-blocking heartbeat reporter intended to wrap any long-lived worker."""

    def __init__(
        self,
        *,
        service_id: str,
        worker_id: str,
        interval_seconds: int = 30,
        report: Callable[..., Any] = upsert_worker_heartbeat,
        time_ns: Callable[[], int] = time.time_ns,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        emit: Callable[[Mapping[str, Any]], None] = _safe_emit,
    ) -> None:
        self.service_id = _identifier(service_id, field="service_id")
        self.worker_id = _identifier(worker_id, field="worker_id")
        self.interval_seconds = _bounded_int(
            interval_seconds,
            field="heartbeat_interval_seconds",
            minimum=1,
            maximum=3_600,
        )
        if not callable(report) or not callable(time_ns) or not callable(clock):
            raise _fail("heartbeat_dependency_invalid")
        if not callable(emit):
            raise _fail("heartbeat_emit_invalid")
        self._report = report
        self._time_ns = time_ns
        self._clock = clock
        self._emit = emit
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._state = "idle"
        self._active_paper_id: int | None = None
        self._last_success_at: str | None = None
        self._error_code: str | None = None
        self._state_generation = 0
        self._stop_requested = False
        self._last_sequence = 0
        self._report_count = 0
        self._failure_count = 0
        self._last_result_code = "heartbeat_not_started"

    def _next_sequence_locked(self) -> int:
        value = self._time_ns()
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 1 <= value <= 9_223_372_036_854_775_807
        ):
            raise _fail("heartbeat_time_ns_invalid")
        sequence = max(value, self._last_sequence + 1)
        if sequence > 9_223_372_036_854_775_807:
            raise _fail("heartbeat_sequence_exhausted")
        self._last_sequence = sequence
        return sequence

    def _snapshot(self) -> tuple[dict[str, Any], int, int]:
        with self._lock:
            sequence = self._next_sequence_locked()
            payload = {
                "schema": "paper-reader-worker-heartbeat",
                "schema_version": 1,
                "worker_id": self.worker_id,
                "observed_at": _utc_timestamp(self._clock),
                "sequence": sequence,
                "state": self._state,
                "active_paper_id": self._active_paper_id,
                "last_success_at": self._last_success_at,
                "error_code": self._error_code,
            }
            return payload, self._state_generation, sequence

    def _report_once(self) -> tuple[int, int]:
        generation = -1
        sequence = -1
        try:
            payload, generation, sequence = self._snapshot()
            stored = validate_worker_heartbeat(
                self._report(payload, service_id=self.service_id)
            )
            if stored != payload:
                raise _fail("heartbeat_store_ack_invalid")
        except Exception:
            with self._lock:
                self._failure_count += 1
                self._last_result_code = "heartbeat_store_unavailable"
            _emit_safely(
                self._emit,
                {
                    "service_id": self.service_id,
                    "worker_id": self.worker_id,
                    "result_code": "heartbeat_store_unavailable",
                    "count": 1,
                },
            )
            if generation < 0:
                with self._lock:
                    generation = self._state_generation
            return generation, sequence
        with self._lock:
            self._report_count += 1
            self._last_result_code = "heartbeat_reported"
        return generation, sequence

    def _run(self) -> None:
        while True:
            self._wake.clear()
            generation, _sequence = self._report_once()
            with self._lock:
                stop = self._stop_requested
                current_generation = self._state_generation
            if stop and generation == current_generation:
                return
            self._wake.wait(self.interval_seconds)

    def start(self) -> WorkerHeartbeatResult:
        with self._lock:
            if self._thread is not None:
                raise _fail("heartbeat_already_started")
            if self._stop_requested:
                raise _fail("heartbeat_already_stopped")
            self._thread = threading.Thread(
                target=self._run,
                name=f"paper-heartbeat-{self.worker_id}",
                daemon=True,
            )
            thread = self._thread
            thread.start()
        return self.snapshot()

    def _transition(
        self,
        *,
        state: str,
        active_paper_id: int | None,
        error_code: str | None,
        mark_success: bool,
    ) -> WorkerHeartbeatResult:
        if state not in {"idle", "processing", "degraded", "stopped"}:
            raise _fail("heartbeat_state_invalid")
        if active_paper_id is not None:
            active_paper_id = _positive_paper_id(active_paper_id)
        if state == "processing" and active_paper_id is None:
            raise _fail("heartbeat_processing_requires_paper")
        if state in {"idle", "stopped"} and active_paper_id is not None:
            raise _fail("heartbeat_inactive_has_paper")
        if error_code is not None:
            error_code = _code(error_code)
        if state == "degraded" and error_code is None:
            raise _fail("heartbeat_degraded_requires_code")
        if state in {"idle", "processing"} and error_code is not None:
            raise _fail("heartbeat_healthy_has_error")
        with self._lock:
            if self._stop_requested and state != "stopped":
                raise _fail("heartbeat_already_stopped")
            self._state = state
            self._active_paper_id = active_paper_id
            self._error_code = error_code
            if mark_success:
                self._last_success_at = _utc_timestamp(self._clock)
            self._state_generation += 1
            if state == "stopped":
                self._stop_requested = True
        self._wake.set()
        return self.snapshot()

    def processing(self, paper_id: int) -> WorkerHeartbeatResult:
        return self._transition(
            state="processing",
            active_paper_id=paper_id,
            error_code=None,
            mark_success=False,
        )

    def idle(self, *, successful: bool = False) -> WorkerHeartbeatResult:
        if not isinstance(successful, bool):
            raise _fail("heartbeat_successful_invalid")
        return self._transition(
            state="idle",
            active_paper_id=None,
            error_code=None,
            mark_success=successful,
        )

    def degraded(
        self, error_code: str, *, active_paper_id: int | None = None
    ) -> WorkerHeartbeatResult:
        return self._transition(
            state="degraded",
            active_paper_id=active_paper_id,
            error_code=error_code,
            mark_success=False,
        )

    def stop(self, *, timeout_seconds: int = 5) -> WorkerHeartbeatResult:
        timeout = _bounded_int(
            timeout_seconds, field="heartbeat_stop_timeout", minimum=1, maximum=60
        )
        with self._lock:
            already_stopped = self._stop_requested
        if already_stopped:
            return self.snapshot()
        self._transition(
            state="stopped",
            active_paper_id=None,
            error_code=None,
            mark_success=False,
        )
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
            if thread.is_alive():
                with self._lock:
                    self._last_result_code = "heartbeat_stop_timeout"
        return self.snapshot()

    def snapshot(self) -> WorkerHeartbeatResult:
        with self._lock:
            return WorkerHeartbeatResult(
                service_id=self.service_id,
                worker_id=self.worker_id,
                state=self._state,
                sequence=self._last_sequence,
                report_count=self._report_count,
                failure_count=self._failure_count,
                result_code=self._last_result_code,
            )

    def tick(self) -> WorkerHeartbeatResult:
        """Attempt one report synchronously and return only sanitized state."""

        self._report_once()
        return self.snapshot()

    def __enter__(self) -> "WorkerHeartbeat":
        self.start()
        return self

    def __exit__(self, _kind: Any, _value: Any, _traceback: Any) -> None:
        self.stop()


class DisabledWorkerHeartbeat:
    """Thread-free heartbeat adapter used when local reporting is disabled."""

    def __init__(self, *, service_id: str, worker_id: str) -> None:
        self.service_id = _identifier(service_id, field="service_id")
        self.worker_id = _identifier(worker_id, field="worker_id")
        self._state = "idle"

    def _result(self) -> WorkerHeartbeatResult:
        return WorkerHeartbeatResult(
            service_id=self.service_id,
            worker_id=self.worker_id,
            state=self._state,
            sequence=0,
            report_count=0,
            failure_count=0,
            result_code="heartbeat_disabled",
        )

    def start(self) -> WorkerHeartbeatResult:
        return self._result()

    def processing(self, paper_id: int) -> WorkerHeartbeatResult:
        _positive_paper_id(paper_id)
        self._state = "processing"
        return self._result()

    def idle(self, *, successful: bool = False) -> WorkerHeartbeatResult:
        if not isinstance(successful, bool):
            raise _fail("heartbeat_successful_invalid")
        self._state = "idle"
        return self._result()

    def degraded(
        self, error_code: str, *, active_paper_id: int | None = None
    ) -> WorkerHeartbeatResult:
        _code(error_code)
        if active_paper_id is not None:
            _positive_paper_id(active_paper_id)
        self._state = "degraded"
        return self._result()

    def stop(self, *, timeout_seconds: int = 5) -> WorkerHeartbeatResult:
        _bounded_int(
            timeout_seconds,
            field="heartbeat_stop_timeout",
            minimum=1,
            maximum=60,
        )
        self._state = "stopped"
        return self._result()

    def snapshot(self) -> WorkerHeartbeatResult:
        return self._result()

    def tick(self) -> WorkerHeartbeatResult:
        return self._result()

    def __enter__(self) -> "DisabledWorkerHeartbeat":
        self.start()
        return self

    def __exit__(self, _kind: Any, _value: Any, _traceback: Any) -> None:
        self.stop()


def worker_heartbeat_from_env(
    default_worker_id: str,
    *,
    environment: Mapping[str, str] | None = None,
    report: Callable[..., Any] = upsert_worker_heartbeat,
    time_ns: Callable[[], int] = time.time_ns,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    emit: Callable[[Mapping[str, Any]], None] = _safe_emit,
) -> WorkerHeartbeat | DisabledWorkerHeartbeat:
    """Build a context-ready reporter; reporting is disabled unless explicitly on."""

    env = os.environ if environment is None else environment
    if not isinstance(env, Mapping):
        raise _fail("heartbeat_environment_invalid")
    unknown = sorted(
        key
        for key in env
        if key.startswith(_HEARTBEAT_PREFIX) and key not in _HEARTBEAT_ENV_KEYS
    )
    if unknown:
        raise _fail("heartbeat_environment_unknown_key")
    raw_enabled = env.get("PAPER_OPS_HEARTBEAT_ENABLED", "false")
    if raw_enabled not in {"true", "false"}:
        raise _fail("heartbeat_enabled_invalid")
    service_id = _identifier(
        env.get("PAPER_OPS_HEARTBEAT_SERVICE_ID", "paper-reader-v2"),
        field="service_id",
    )
    worker_id = _identifier(
        env.get(
            "PAPER_OPS_HEARTBEAT_WORKER_ID",
            _identifier(default_worker_id, field="worker_id"),
        ),
        field="worker_id",
    )
    interval_seconds = _env_integer(
        env,
        "PAPER_OPS_HEARTBEAT_INTERVAL_SECONDS",
        default=30,
        minimum=1,
        maximum=3_600,
    )
    if raw_enabled == "false":
        return DisabledWorkerHeartbeat(service_id=service_id, worker_id=worker_id)
    return WorkerHeartbeat(
        service_id=service_id,
        worker_id=worker_id,
        interval_seconds=interval_seconds,
        report=report,
        time_ns=time_ns,
        clock=clock,
        emit=emit,
    )


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise _fail("runtime_config_duplicate_key")
        output[key] = value
    return output


def _services_json(value: Any) -> tuple[tuple[str, tuple[str, ...]], ...]:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 32_768:
        raise _fail("runtime_services_json_invalid")
    try:
        parsed = json.loads(
            value,
            object_pairs_hook=_reject_duplicate_keys,
            parse_float=lambda _item: (_ for _ in ()).throw(
                _fail("runtime_services_float_invalid")
            ),
            parse_constant=lambda _item: (_ for _ in ()).throw(
                _fail("runtime_services_constant_invalid")
            ),
        )
    except OpsRuntimeError:
        raise
    except (json.JSONDecodeError, UnicodeError, RecursionError) as exc:
        raise _fail("runtime_services_json_invalid") from exc
    if not isinstance(parsed, dict) or not 1 <= len(parsed) <= MAX_RUNTIME_SERVICES:
        raise _fail("runtime_services_invalid")
    output: list[tuple[str, tuple[str, ...]]] = []
    for raw_service, raw_workers in parsed.items():
        service = _identifier(raw_service, field="service_id")
        if not isinstance(raw_workers, list) or not 1 <= len(raw_workers) <= 64:
            raise _fail("runtime_expected_workers_invalid")
        workers = tuple(
            sorted(_identifier(worker, field="worker_id") for worker in raw_workers)
        )
        if len(workers) != len(set(workers)):
            raise _fail("runtime_expected_workers_duplicate")
        output.append((service, workers))
    return tuple(sorted(output, key=lambda item: item[0]))


def _env_integer(
    environment: Mapping[str, str],
    key: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw = environment.get(key)
    if raw is None:
        return default
    if not isinstance(raw, str) or not re.fullmatch(r"0|[1-9][0-9]*", raw):
        raise _fail(f"{key.lower()}_invalid")
    return _bounded_int(
        int(raw), field=key.lower(), minimum=minimum, maximum=maximum
    )


def _public_webhook_hostname(value: Any) -> str:
    if not isinstance(value, str) or value != value.lower() or len(value) > 253:
        raise _fail("webhook_allowed_host_invalid")
    if value.endswith(".") or "." not in value:
        raise _fail("webhook_allowed_host_invalid")
    labels = value.split(".")
    if (
        any(not _DNS_LABEL_RE.fullmatch(label) for label in labels[:-1])
        or not _DNS_TLD_RE.fullmatch(labels[-1])
    ):
        raise _fail("webhook_allowed_host_invalid")
    if value in {"localhost", "localhost.localdomain"} or value.endswith(
        (
            ".localhost",
            ".localdomain",
            ".local",
            ".internal",
            ".lan",
            ".home",
            ".home.arpa",
        )
    ):
        raise _fail("webhook_allowed_host_invalid")
    try:
        address = ipaddress.ip_address(value.strip("[]"))
    except ValueError:
        address = None
    if address is not None:
        raise _fail("webhook_allowed_host_invalid")
    return value


def _allowed_hosts(value: Any) -> tuple[str, ...]:
    if not isinstance(value, tuple) or not 1 <= len(value) <= 16:
        raise _fail("webhook_allowed_hosts_invalid")
    hosts = tuple(_public_webhook_hostname(item) for item in value)
    if hosts != tuple(sorted(hosts)) or len(hosts) != len(set(hosts)):
        raise _fail("webhook_allowed_hosts_invalid")
    return hosts


def _allowed_hosts_json(value: Any) -> tuple[str, ...]:
    if not isinstance(value, str) or not value:
        raise _fail("webhook_allowed_hosts_json_invalid")
    try:
        if len(value.encode("utf-8")) > 4_096:
            raise _fail("webhook_allowed_hosts_json_invalid")
        parsed = json.loads(value)
    except OpsRuntimeError:
        raise
    except (json.JSONDecodeError, UnicodeError, RecursionError) as exc:
        raise _fail("webhook_allowed_hosts_json_invalid") from exc
    if not isinstance(parsed, list) or not 1 <= len(parsed) <= 16:
        raise _fail("webhook_allowed_hosts_invalid")
    hosts = tuple(sorted(_public_webhook_hostname(item) for item in parsed))
    if len(hosts) != len(set(hosts)):
        raise _fail("webhook_allowed_hosts_invalid")
    return hosts


def _webhook_url(value: Any, *, allowed_hosts: tuple[str, ...]) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 2_048
        or _CONTROL_RE.search(value)
        or not value.startswith("https://")
        or "#" in value
    ):
        raise _fail("webhook_url_invalid")
    try:
        value.encode("ascii")
        parsed = urlsplit(value)
        port = parsed.port
    except (UnicodeError, ValueError) as exc:
        raise _fail("webhook_url_invalid") from exc
    hostname = parsed.hostname
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.fragment
        or parsed.path == ""
    ):
        raise _fail("webhook_url_invalid")
    lowered = hostname.lower()
    try:
        lowered = _public_webhook_hostname(lowered)
    except OpsRuntimeError as exc:
        raise _fail("webhook_url_invalid") from exc
    if lowered not in allowed_hosts:
        raise _fail("webhook_url_invalid")
    return value


def _private_auth_token(path_value: Any) -> str:
    if not isinstance(path_value, str) or not path_value:
        raise _fail("webhook_auth_token_file_invalid")
    path = Path(path_value)
    if not path.is_absolute() or path.is_symlink():
        raise _fail("webhook_auth_token_file_invalid")
    descriptor = -1
    try:
        details = path.lstat()
        if (
            not stat.S_ISREG(details.st_mode)
            or stat.S_IMODE(details.st_mode) & 0o077
            or not 20 <= details.st_size <= 2_048
        ):
            raise _fail("webhook_auth_token_file_invalid")
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != details.st_dev
            or opened.st_ino != details.st_ino
            or not stat.S_ISREG(opened.st_mode)
            or stat.S_IMODE(opened.st_mode) & 0o077
            or not 20 <= opened.st_size <= 2_048
        ):
            raise _fail("webhook_auth_token_file_changed")
        chunks: list[bytes] = []
        remaining = opened.st_size
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) != opened.st_size:
            raise _fail("webhook_auth_token_file_changed")
        token = raw.decode("utf-8").strip()
    except OpsRuntimeError:
        raise
    except (OSError, UnicodeError) as exc:
        raise _fail("webhook_auth_token_file_invalid") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not re.fullmatch(r"[!-~]{20,2048}", token):
        raise _fail("webhook_auth_token_invalid")
    return token


def load_runtime_config(
    environment: Mapping[str, str] | None = None,
) -> OpsRuntimeConfig:
    """Load a closed, strictly typed runtime configuration from environment."""

    env = os.environ if environment is None else environment
    if not isinstance(env, Mapping):
        raise _fail("runtime_environment_invalid")
    unknown = sorted(
        key for key in env if key.startswith(_RUNTIME_PREFIX) and key not in _ENV_KEYS
    )
    if unknown:
        raise _fail("runtime_environment_unknown_key")
    services = _services_json(env.get("PAPER_OPS_RUNTIME_SERVICES_JSON"))
    monitor_interval = _env_integer(
        env,
        "PAPER_OPS_RUNTIME_MONITOR_INTERVAL_SECONDS",
        default=30,
        minimum=5,
        maximum=3_600,
    )
    stale_after = _env_integer(
        env,
        "PAPER_OPS_RUNTIME_STALE_AFTER_SECONDS",
        default=120,
        minimum=10,
        maximum=86_400,
    )
    if stale_after < monitor_interval * 2:
        raise _fail("runtime_stale_window_too_short")
    startup_grace = _env_integer(
        env,
        "PAPER_OPS_RUNTIME_STARTUP_GRACE_SECONDS",
        default=90,
        minimum=0,
        maximum=3_600,
    )
    if 0 < startup_grace < 30:
        raise _fail("runtime_startup_grace_too_short")
    raw_url = env.get("PAPER_OPS_RUNTIME_WEBHOOK_URL")
    raw_allowed_hosts = env.get("PAPER_OPS_RUNTIME_WEBHOOK_ALLOWED_HOSTS_JSON")
    if raw_url is None or raw_url == "":
        url = None
        if raw_allowed_hosts not in {None, ""}:
            raise _fail("webhook_allowed_hosts_without_url")
        allowed_hosts: tuple[str, ...] = ()
    else:
        if raw_allowed_hosts in {None, ""}:
            raise _fail("webhook_allowed_hosts_required")
        allowed_hosts = _allowed_hosts_json(raw_allowed_hosts)
        url = _webhook_url(raw_url, allowed_hosts=allowed_hosts)
    token_path = env.get("PAPER_OPS_RUNTIME_WEBHOOK_AUTH_TOKEN_FILE")
    if url is None and token_path not in {None, ""}:
        raise _fail("webhook_auth_without_url")
    auth_token = (
        _private_auth_token(token_path)
        if url is not None and token_path not in {None, ""}
        else None
    )
    webhook = WebhookDeliveryConfig(
        url=url,
        allowed_hosts=allowed_hosts,
        auth_token=auth_token,
        worker_id=_identifier(
            env.get("PAPER_OPS_RUNTIME_WEBHOOK_WORKER_ID", "ops-webhook"),
            field="webhook_worker_id",
        ),
        timeout_seconds=_env_integer(
            env,
            "PAPER_OPS_RUNTIME_WEBHOOK_TIMEOUT_SECONDS",
            default=10,
            minimum=1,
            maximum=30,
        ),
        response_bytes=_env_integer(
            env,
            "PAPER_OPS_RUNTIME_WEBHOOK_RESPONSE_BYTES",
            default=DEFAULT_WEBHOOK_RESPONSE_BYTES,
            minimum=1,
            maximum=MAX_WEBHOOK_RESPONSE_BYTES,
        ),
        lease_seconds=_env_integer(
            env,
            "PAPER_OPS_RUNTIME_WEBHOOK_LEASE_SECONDS",
            default=60,
            minimum=10,
            maximum=3_600,
        ),
        retry_delay_seconds=_env_integer(
            env,
            "PAPER_OPS_RUNTIME_WEBHOOK_RETRY_DELAY_SECONDS",
            default=60,
            minimum=0,
            maximum=86_400,
        ),
        max_per_cycle=_env_integer(
            env,
            "PAPER_OPS_RUNTIME_WEBHOOK_MAX_PER_CYCLE",
            default=10,
            minimum=1,
            maximum=100,
        ),
    )
    if webhook.lease_seconds <= webhook.timeout_seconds:
        raise _fail("webhook_lease_too_short")
    return OpsRuntimeConfig(
        services=services,
        monitor_interval_seconds=monitor_interval,
        stale_after_seconds=stale_after,
        startup_grace_seconds=startup_grace,
        webhook=webhook,
    )


def _canonical_webhook_body(payload: Any) -> bytes:
    expected = {
        "schema",
        "schema_version",
        "event",
        "incident_id",
        "dedupe_key",
        "occurred_at",
        "severity",
        "summary",
        "reason_codes",
        "metrics",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        raise _fail("webhook_payload_invalid")
    if payload["schema"] != WEBHOOK_SCHEMA or payload["schema_version"] != 1:
        raise _fail("webhook_payload_invalid")
    event = payload["event"]
    if event not in {"incident_opened", "incident_recovered"}:
        raise _fail("webhook_payload_invalid")
    if not isinstance(payload["incident_id"], str) or not _INCIDENT_RE.fullmatch(
        payload["incident_id"]
    ):
        raise _fail("webhook_payload_invalid")
    if not isinstance(payload["dedupe_key"], str) or not _EVENT_RE.fullmatch(
        payload["dedupe_key"]
    ):
        raise _fail("webhook_payload_invalid")
    if not isinstance(payload["occurred_at"], str) or not _TIMESTAMP_RE.fullmatch(
        payload["occurred_at"]
    ):
        raise _fail("webhook_payload_invalid")
    try:
        parsed_time = datetime.strptime(
            payload["occurred_at"], "%Y-%m-%dT%H:%M:%SZ"
        )
    except ValueError as exc:
        raise _fail("webhook_payload_invalid") from exc
    if parsed_time.strftime("%Y-%m-%dT%H:%M:%SZ") != payload["occurred_at"]:
        raise _fail("webhook_payload_invalid")
    expected_severity = "warning" if event == "incident_opened" else "info"
    expected_summary = (
        "Paper reader worker health incident opened"
        if event == "incident_opened"
        else "Paper reader worker health recovered"
    )
    if (
        payload["severity"] != expected_severity
        or payload["summary"] != expected_summary
    ):
        raise _fail("webhook_payload_invalid")
    reasons = payload["reason_codes"]
    if (
        not isinstance(reasons, list)
        or not 1 <= len(reasons) <= 32
        or any(not isinstance(item, str) for item in reasons)
        or len(reasons) != len(set(reasons))
        or reasons != sorted(reasons)
        or not set(reasons).issubset(_HEALTH_REASON_CODES)
    ):
        raise _fail("webhook_payload_invalid")
    metrics = payload["metrics"]
    if not isinstance(metrics, dict) or set(metrics) != {"affected_workers"}:
        raise _fail("webhook_payload_invalid")
    _bounded_int(
        metrics["affected_workers"],
        field="affected_workers",
        minimum=1,
        maximum=64,
    )
    try:
        body = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise _fail("webhook_payload_invalid") from exc
    if not body or len(body) > MAX_WEBHOOK_REQUEST_BYTES:
        raise _fail("webhook_payload_size_invalid")
    return body


def _is_public_peer_address(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    return bool(
        address.is_global
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_multicast
        and not address.is_private
        and not address.is_reserved
        and not address.is_unspecified
    )


def _resolve_public_webhook_endpoints(
    hostname: str,
    port: int,
    *,
    resolver: Callable[..., Any] = socket.getaddrinfo,
) -> tuple[tuple[int, int, int, tuple[Any, ...]], ...]:
    """Resolve once and reject the whole answer set if any peer is non-public."""

    if not callable(resolver):
        raise WebhookTransportError("webhook_resolver_invalid")
    try:
        answers = resolver(
            hostname,
            port,
            socket.AF_UNSPEC,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
        )
    except (OSError, ValueError) as exc:
        raise WebhookTransportError("webhook_dns_unavailable") from exc
    if not isinstance(answers, (list, tuple)) or not 1 <= len(answers) <= 32:
        raise WebhookTransportError("webhook_dns_invalid")
    endpoints: list[tuple[int, int, int, tuple[Any, ...]]] = []
    seen: set[tuple[int, tuple[Any, ...]]] = set()
    for answer in answers:
        if not isinstance(answer, tuple) or len(answer) != 5:
            raise WebhookTransportError("webhook_dns_invalid")
        family, socket_type, protocol, _canonical_name, socket_address = answer
        if (
            family not in {socket.AF_INET, socket.AF_INET6}
            or socket_type != socket.SOCK_STREAM
            or protocol not in {0, socket.IPPROTO_TCP}
            or not isinstance(socket_address, tuple)
            or len(socket_address) < 2
            or not _is_public_peer_address(socket_address[0])
            or socket_address[1] != port
        ):
            raise WebhookTransportError("webhook_destination_not_public")
        key = (family, socket_address)
        if key not in seen:
            seen.add(key)
            endpoints.append((family, socket_type, protocol, socket_address))
    if not endpoints or len(endpoints) > 16:
        raise WebhookTransportError("webhook_dns_invalid")
    return tuple(endpoints)


def _connect_pinned_public_endpoint(
    endpoints: tuple[tuple[int, int, int, tuple[Any, ...]], ...],
    *,
    timeout: int,
    socket_factory: Callable[..., Any] = socket.socket,
    monotonic: Callable[[], float] = time.monotonic,
) -> Any:
    """Connect by numeric address and verify the peer before TLS or HTTP data."""

    if (
        not endpoints
        or isinstance(timeout, bool)
        or not isinstance(timeout, int)
        or timeout <= 0
        or not callable(socket_factory)
        or not callable(monotonic)
    ):
        raise WebhookTransportError("webhook_connection_invalid")
    deadline = monotonic() + timeout
    for family, socket_type, protocol, socket_address in endpoints:
        connection = None
        try:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError
            connection = socket_factory(family, socket_type, protocol)
            connection.settimeout(remaining)
            connection.connect(socket_address)
            peer = connection.getpeername()
            if (
                not isinstance(peer, tuple)
                or not peer
                or not _is_public_peer_address(peer[0])
                or ipaddress.ip_address(peer[0].split("%", 1)[0])
                != ipaddress.ip_address(socket_address[0].split("%", 1)[0])
            ):
                raise WebhookTransportError("webhook_destination_not_public")
            return connection
        except WebhookTransportError:
            if connection is not None:
                connection.close()
            raise
        except (OSError, TimeoutError, ValueError):
            if connection is not None:
                connection.close()
    raise WebhookTransportError("webhook_unavailable")


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(
        self,
        hostname: str,
        port: int,
        *,
        timeout: int,
        context: ssl.SSLContext,
        endpoints: tuple[tuple[int, int, int, tuple[Any, ...]], ...],
        socket_factory: Callable[..., Any],
    ):
        super().__init__(hostname, port=port, timeout=timeout, context=context)
        self._pinned_endpoints = endpoints
        self._socket_factory = socket_factory

    def connect(self) -> None:
        raw_socket = _connect_pinned_public_endpoint(
            self._pinned_endpoints,
            timeout=int(self.timeout),
            socket_factory=self._socket_factory,
        )
        try:
            self.sock = self._context.wrap_socket(
                raw_socket,
                server_hostname=self.host,
            )
        except BaseException:
            raw_socket.close()
            raise


class _PinnedResponse:
    def __init__(
        self,
        *,
        response: http.client.HTTPResponse,
        connection: http.client.HTTPSConnection,
        url: str,
    ):
        self.status = response.status
        self.headers = response.headers
        self._response = response
        self._connection = connection
        self._url = url

    def read(self, amount: int = -1) -> bytes:
        return self._response.read(amount)

    def geturl(self) -> str:
        return self._url

    def close(self) -> None:
        try:
            self._response.close()
        finally:
            self._connection.close()

    def __enter__(self) -> "_PinnedResponse":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()


def _default_open(
    request: urllib.request.Request,
    timeout: int,
    *,
    resolver: Callable[..., Any] = socket.getaddrinfo,
    socket_factory: Callable[..., Any] = socket.socket,
) -> _Response:
    """Send directly to a DNS-pinned public peer with redirects disabled."""

    if not isinstance(request, urllib.request.Request):
        raise WebhookTransportError("webhook_request_invalid")
    parsed = urlsplit(request.full_url)
    hostname = parsed.hostname
    try:
        port = parsed.port or 443
    except ValueError as exc:
        raise WebhookTransportError("webhook_url_invalid") from exc
    if (
        parsed.scheme != "https"
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or port != 443
        or parsed.fragment
    ):
        raise WebhookTransportError("webhook_url_invalid")
    endpoints = _resolve_public_webhook_endpoints(
        hostname, port, resolver=resolver
    )
    context = ssl.create_default_context()
    connection = _PinnedHTTPSConnection(
        hostname,
        port,
        timeout=timeout,
        context=context,
        endpoints=endpoints,
        socket_factory=socket_factory,
    )
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    try:
        connection.request(
            request.get_method(),
            path,
            body=request.data,
            headers=dict(request.header_items()),
        )
        response = connection.getresponse()
    except WebhookTransportError:
        connection.close()
        raise
    except (OSError, TimeoutError, ssl.SSLError, http.client.HTTPException) as exc:
        connection.close()
        raise WebhookTransportError("webhook_unavailable") from exc
    return _PinnedResponse(
        response=response,
        connection=connection,
        url=request.full_url,
    )


def _post_operational_webhook(
    payload: Any,
    *,
    config: WebhookDeliveryConfig,
    open_request: Callable[[urllib.request.Request, int], _Response],
) -> None:
    if not isinstance(config, WebhookDeliveryConfig) or config.url is None:
        raise _fail("webhook_not_configured")
    body = _canonical_webhook_body(payload)
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "paper-os-operations/1",
    }
    if config.auth_token is not None:
        headers["Authorization"] = f"Bearer {config.auth_token}"
    request = urllib.request.Request(
        config.url,
        data=body,
        headers=headers,
        method="POST",
    )
    try:
        with open_request(request, config.timeout_seconds) as response:
            status = getattr(response, "status", None)
            if isinstance(status, bool) or not isinstance(status, int):
                raise WebhookTransportError("webhook_response_invalid")
            if 300 <= status <= 399:
                raise WebhookTransportError("webhook_redirect_rejected")
            if not 200 <= status <= 299:
                raise WebhookTransportError("webhook_http_failure")
            if response.geturl() != config.url:
                raise WebhookTransportError("webhook_redirect_rejected")
            length_header = response.headers.get("Content-Length")
            if length_header is not None:
                if not isinstance(length_header, str) or not re.fullmatch(
                    r"0|[1-9][0-9]*", length_header
                ):
                    raise WebhookTransportError("webhook_response_size_invalid")
                if int(length_header) > config.response_bytes:
                    raise WebhookTransportError("webhook_response_size_invalid")
            response_body = response.read(config.response_bytes + 1)
            if not isinstance(response_body, bytes) or len(response_body) > config.response_bytes:
                raise WebhookTransportError("webhook_response_size_invalid")
    except WebhookTransportError:
        raise
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code <= 399:
            raise WebhookTransportError("webhook_redirect_rejected") from exc
        raise WebhookTransportError(
            "webhook_http_server_error"
            if 500 <= exc.code <= 599
            else "webhook_http_client_error"
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise WebhookTransportError("webhook_unavailable") from exc


def _lease_contract(value: Any) -> tuple[str, str, str, dict[str, Any]]:
    if not isinstance(value, Mapping):
        raise _fail("webhook_lease_invalid")
    try:
        outbox_id = str(value["id"])
        lease_token = str(value["lease_token"])
        payload_digest = str(value["payload_sha256"]).strip()
        payload = value["payload"]
    except KeyError as exc:
        raise _fail("webhook_lease_invalid") from exc
    if not _UUID_RE.fullmatch(outbox_id) or str(UUID(outbox_id)) != outbox_id:
        raise _fail("webhook_lease_invalid")
    if not _UUID_RE.fullmatch(lease_token) or str(UUID(lease_token)) != lease_token:
        raise _fail("webhook_lease_invalid")
    if not _SHA256_RE.fullmatch(payload_digest):
        raise _fail("webhook_lease_invalid")
    body = _canonical_webhook_body(payload)
    if hashlib.sha256(body).hexdigest() != payload_digest:
        raise _fail("webhook_lease_invalid")
    return outbox_id, lease_token, payload_digest, payload


def dispatch_webhook_outbox(
    config: WebhookDeliveryConfig,
    *,
    claim: Callable[..., Any] | None = None,
    ack: Callable[..., Any] | None = None,
    nack: Callable[..., Any] | None = None,
    open_request: Callable[[urllib.request.Request, int], _Response] = _default_open,
) -> WebhookBatchResult:
    """Drain a bounded batch; every claimed item is isolated from the next."""

    if not isinstance(config, WebhookDeliveryConfig):
        raise _fail("webhook_config_invalid")
    if config.url is None:
        return WebhookBatchResult("webhook_disabled", 0, 0, 0, 0, ())
    claim_item = claim_webhook_item if claim is None else claim
    ack_item = ack_webhook_item if ack is None else ack
    nack_item = nack_webhook_item if nack is None else nack
    if not all(callable(item) for item in (claim_item, ack_item, nack_item, open_request)):
        raise _fail("webhook_dependency_invalid")
    claimed = acknowledged = nacked = failed = 0
    items: list[WebhookItemResult] = []
    for _index in range(config.max_per_cycle):
        try:
            raw_item = claim_item(
                worker_id=config.worker_id,
                lease_seconds=config.lease_seconds,
            )
        except Exception:
            failed += 1
            items.append(WebhookItemResult(None, "webhook_claim_failed"))
            break
        if raw_item is None:
            break
        claimed += 1
        try:
            outbox_id, lease_token, digest, payload = _lease_contract(raw_item)
        except Exception:
            failed += 1
            items.append(WebhookItemResult(None, "webhook_lease_invalid"))
            continue
        try:
            _post_operational_webhook(
                payload,
                config=config,
                open_request=open_request,
            )
        except WebhookTransportError as exc:
            transport_code = exc.code
        except Exception:
            transport_code = "webhook_unavailable"
        else:
            transport_code = None
        if transport_code is not None:
            try:
                nack_item(
                    outbox_id,
                    worker_id=config.worker_id,
                    lease_token=lease_token,
                    error_code=transport_code,
                    retry_delay_seconds=config.retry_delay_seconds,
                )
            except Exception:
                failed += 1
                items.append(WebhookItemResult(outbox_id, "webhook_nack_failed"))
            else:
                nacked += 1
                items.append(WebhookItemResult(outbox_id, transport_code))
            continue
        try:
            ack_item(
                outbox_id,
                worker_id=config.worker_id,
                lease_token=lease_token,
                expected_payload_sha256=digest,
            )
        except Exception:
            failed += 1
            items.append(WebhookItemResult(outbox_id, "webhook_ack_failed"))
            continue
        acknowledged += 1
        items.append(WebhookItemResult(outbox_id, "webhook_delivered"))
    result_code = (
        "webhook_batch_failed"
        if failed
        else "webhook_batch_partial"
        if nacked
        else "webhook_batch_ok"
    )
    return WebhookBatchResult(
        result_code=result_code,
        claimed_count=claimed,
        acknowledged_count=acknowledged,
        nacked_count=nacked,
        failed_count=failed,
        items=tuple(items),
    )


def configure_monitor_expected_workers(
    config: OpsRuntimeConfig,
    *,
    configure: Callable[..., Any] | None = None,
    emit: Callable[[Mapping[str, Any]], None] = _safe_emit,
) -> tuple[ServiceMonitorResult, ...]:
    """Persist expected sets once at monitor startup or a config change."""

    if not isinstance(config, OpsRuntimeConfig):
        raise _fail("runtime_config_invalid")
    configure_workers = configure_expected_workers if configure is None else configure
    if not callable(configure_workers) or not callable(emit):
        raise _fail("runtime_dependency_invalid")
    results: list[ServiceMonitorResult] = []
    for service_id, worker_ids in config.services:
        try:
            configure_workers(worker_ids, service_id=service_id)
        except Exception:
            results.append(
                ServiceMonitorResult(
                    service_id=service_id,
                    expected_worker_count=len(worker_ids),
                    observed_worker_count=0,
                    incident_event_count=0,
                    result_code="monitor_service_prime_failed",
                )
            )
            _emit_safely(
                emit,
                {
                    "service_id": service_id,
                    "result_code": "monitor_service_prime_failed",
                    "count": 1,
                },
            )
        else:
            results.append(
                ServiceMonitorResult(
                    service_id=service_id,
                    expected_worker_count=len(worker_ids),
                    observed_worker_count=0,
                    incident_event_count=0,
                    result_code="monitor_service_primed",
                )
            )
    return tuple(results)


def run_monitor_cycle(
    config: OpsRuntimeConfig,
    *,
    evaluated_at: str | None = None,
    health: Callable[..., Any] | None = None,
    record: Callable[..., Any] | None = None,
    webhook_dispatch: Callable[[WebhookDeliveryConfig], WebhookBatchResult] | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    emit: Callable[[Mapping[str, Any]], None] = _safe_emit,
) -> MonitorCycleResult:
    """Evaluate every service independently, then drain the webhook outbox."""

    if not isinstance(config, OpsRuntimeConfig):
        raise _fail("runtime_config_invalid")
    if not callable(clock) or not callable(emit):
        raise _fail("runtime_dependency_invalid")
    read_health = get_worker_health_snapshot if health is None else health
    persist_incident = record_health_snapshot if record is None else record
    deliver = dispatch_webhook_outbox if webhook_dispatch is None else webhook_dispatch
    if not all(
        callable(item)
        for item in (read_health, persist_incident, deliver)
    ):
        raise _fail("runtime_dependency_invalid")
    observed_at = (
        _utc_timestamp(clock)
        if evaluated_at is None
        else _strict_timestamp(evaluated_at, field="evaluated_at")
    )
    services: list[ServiceMonitorResult] = []
    successful = failed = 0
    for service_id, worker_ids in config.services:
        try:
            snapshot = validate_health_snapshot(
                read_health(
                    evaluated_at=observed_at,
                    stale_after_seconds=config.stale_after_seconds,
                    service_id=service_id,
                )
            )
            if snapshot["service_id"] != service_id:
                raise _fail("monitor_service_mismatch")
            snapshot_worker_ids = tuple(
                sorted(str(worker["worker_id"]) for worker in snapshot["workers"])
            )
            if (
                snapshot_worker_ids != worker_ids
                or snapshot["expected_worker_count"] != len(worker_ids)
            ):
                raise _fail("monitor_expected_workers_mismatch")
            transition = persist_incident(snapshot)
            if not isinstance(transition, Mapping):
                raise _fail("monitor_transition_invalid")
            events = transition.get("events")
            if not isinstance(events, list) or len(events) > 2:
                raise _fail("monitor_transition_invalid")
            result_code = f"monitor_{snapshot['status']}"
            services.append(
                ServiceMonitorResult(
                    service_id=service_id,
                    expected_worker_count=int(snapshot["expected_worker_count"]),
                    observed_worker_count=int(snapshot["observed_worker_count"]),
                    incident_event_count=len(events),
                    result_code=result_code,
                )
            )
            successful += 1
        except Exception:
            services.append(
                ServiceMonitorResult(
                    service_id=service_id,
                    expected_worker_count=len(worker_ids),
                    observed_worker_count=0,
                    incident_event_count=0,
                    result_code="monitor_service_failed",
                )
            )
            failed += 1
            _emit_safely(
                emit,
                {
                    "service_id": service_id,
                    "result_code": "monitor_service_failed",
                    "count": 1,
                },
            )
    try:
        webhook = deliver(config.webhook)
        if not isinstance(webhook, WebhookBatchResult):
            raise _fail("webhook_result_invalid")
    except Exception:
        webhook = WebhookBatchResult(
            "webhook_dispatch_failed", 0, 0, 0, 1, ()
        )
        _emit_safely(
            emit, {"result_code": "webhook_dispatch_failed", "count": 1}
        )
    result_code = "monitor_cycle_partial" if failed or webhook.failed_count else "monitor_cycle_ok"
    return MonitorCycleResult(
        service_count=len(config.services),
        successful_service_count=successful,
        failed_service_count=failed,
        services=tuple(services),
        webhook=webhook,
        result_code=result_code,
    )


def run_monitor_loop(
    config: OpsRuntimeConfig,
    *,
    stop_event: threading.Event | None = None,
    cycle: Callable[[OpsRuntimeConfig], MonitorCycleResult] = run_monitor_cycle,
    configure: Callable[..., Any] | None = None,
    emit: Callable[[Mapping[str, Any]], None] = _safe_emit,
) -> MonitorLoopResult:
    """Prime every expected set, honor grace, then run bounded cycles.

    A transient database error during bootstrap is retried before any health
    evaluation.  Only services that have not yet been primed are retried, so a
    healthy service does not receive repeated configuration writes.
    """

    if not isinstance(config, OpsRuntimeConfig):
        raise _fail("runtime_config_invalid")
    stopper = threading.Event() if stop_event is None else stop_event
    configure_workers = configure_expected_workers if configure is None else configure
    if (
        not isinstance(stopper, threading.Event)
        or not callable(cycle)
        or not callable(configure_workers)
        or not callable(emit)
    ):
        raise _fail("runtime_loop_dependency_invalid")
    cycles = failures = 0
    if stopper.is_set():
        return MonitorLoopResult(
            cycle_count=0,
            failed_cycle_count=0,
            result_code="monitor_loop_stopped",
        )
    pending_services = config.services
    retry_delay = 1
    maximum_retry_delay = min(config.monitor_interval_seconds, 30)
    while pending_services and not stopper.is_set():
        pending_config = OpsRuntimeConfig(
            services=pending_services,
            monitor_interval_seconds=config.monitor_interval_seconds,
            stale_after_seconds=config.stale_after_seconds,
            startup_grace_seconds=config.startup_grace_seconds,
            webhook=config.webhook,
        )
        prime_results = configure_monitor_expected_workers(
            pending_config,
            configure=configure_workers,
            emit=emit,
        )
        failed_service_ids = {
            result.service_id
            for result in prime_results
            if result.result_code == "monitor_service_prime_failed"
        }
        pending_services = tuple(
            item for item in pending_services if item[0] in failed_service_ids
        )
        if pending_services:
            if stopper.wait(retry_delay):
                return MonitorLoopResult(
                    cycle_count=0,
                    failed_cycle_count=0,
                    result_code="monitor_loop_stopped",
                )
            retry_delay = min(retry_delay * 2, maximum_retry_delay)
    if config.startup_grace_seconds:
        if stopper.wait(config.startup_grace_seconds):
            return MonitorLoopResult(
                cycle_count=0,
                failed_cycle_count=0,
                result_code="monitor_loop_stopped",
            )
    while not stopper.is_set():
        try:
            result = cycle(config)
            if not isinstance(result, MonitorCycleResult):
                raise _fail("runtime_cycle_result_invalid")
            failures += int(result.result_code != "monitor_cycle_ok")
        except Exception:
            failures += 1
            _emit_safely(
                emit, {"result_code": "monitor_cycle_failed", "count": 1}
            )
        cycles += 1
        if stopper.wait(config.monitor_interval_seconds):
            break
    return MonitorLoopResult(
        cycle_count=cycles,
        failed_cycle_count=failures,
        result_code="monitor_loop_stopped",
    )


def run_runtime() -> None:
    """Validate configuration, migrate safely, then run the monitor forever."""

    ensure_schema()
    run_monitor_loop(load_runtime_config())


def _strict_timestamp(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not _TIMESTAMP_RE.fullmatch(value):
        raise _fail(f"{field}_invalid")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise _fail(f"{field}_invalid") from exc
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise _fail(f"{field}_invalid")
    return value


__all__ = [
    "configure_monitor_expected_workers",
    "DisabledWorkerHeartbeat",
    "MonitorCycleResult",
    "MonitorLoopResult",
    "OpsRuntimeConfig",
    "OpsRuntimeError",
    "ServiceMonitorResult",
    "WebhookBatchResult",
    "WebhookDeliveryConfig",
    "WebhookItemResult",
    "WebhookTransportError",
    "WorkerHeartbeat",
    "WorkerHeartbeatResult",
    "dispatch_webhook_outbox",
    "load_runtime_config",
    "run_monitor_cycle",
    "run_monitor_loop",
    "run_runtime",
    "worker_heartbeat_from_env",
]


if __name__ == "__main__":
    run_runtime()

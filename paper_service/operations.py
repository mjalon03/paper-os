"""Provider-neutral operational contracts for the paper reader pipeline.

The module contains only bounded state transitions and payload construction. It
does not update the database, schedule reprocessing, or send a webhook.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any


OPERATIONS_SCHEMA_VERSION = 1
HEARTBEAT_SCHEMA = "paper-reader-worker-heartbeat"
HEALTH_SCHEMA = "paper-reader-worker-health"
INCIDENT_STATE_SCHEMA = "paper-reader-incident-state"
INCIDENT_EVENT_SCHEMA = "paper-reader-incident-event"
WEBHOOK_SCHEMA = "paper-reader-generic-webhook"
REPROCESS_SCHEMA = "paper-reader-reprocess-request"

MAX_WORKERS = 64
MAX_REPROCESS_TARGETS = 50
MAX_OPERATIONS_JSON_BYTES = 256 * 1024
GLOBAL_REPROCESS_CONFIRMATION = "REPROCESS_ALL_PAPERS"

__all__ = [
    "GLOBAL_REPROCESS_CONFIRMATION",
    "HEALTH_SCHEMA",
    "HEARTBEAT_SCHEMA",
    "INCIDENT_EVENT_SCHEMA",
    "INCIDENT_STATE_SCHEMA",
    "MAX_OPERATIONS_JSON_BYTES",
    "MAX_REPROCESS_TARGETS",
    "MAX_WORKERS",
    "OPERATIONS_SCHEMA_VERSION",
    "REPROCESS_SCHEMA",
    "WEBHOOK_SCHEMA",
    "OperationsContractError",
    "advance_incident_state",
    "build_generic_webhook_payload",
    "build_selective_reprocess_request",
    "build_worker_health_snapshot",
    "classify_reprocess_idempotency",
    "derive_reprocess_idempotency_key",
    "initial_incident_state",
    "load_reprocess_request_json",
    "reprocess_request_fingerprint",
    "validate_health_snapshot",
    "validate_incident_event",
    "validate_incident_state",
    "validate_reprocess_request",
    "validate_reprocess_target_source",
    "validate_worker_heartbeat",
    "worker_heartbeat",
]

_IDENTIFIER_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_CODE_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_VERSION_ID_RE = re.compile(r"rv-[0-9a-f]{32}")
_IDEMPOTENCY_RE = re.compile(r"rr-[0-9a-f]{32}")
_EVENT_DEDUPE_RE = re.compile(r"event-[0-9a-f]{32}")
_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_HEALTH_REASON_CODES = frozenset(
    {"worker_degraded", "worker_missing", "worker_stale", "worker_stopped"}
)

_HEARTBEAT_REQUIRED = frozenset(
    {
        "schema",
        "schema_version",
        "worker_id",
        "observed_at",
        "sequence",
        "state",
        "active_paper_id",
        "last_success_at",
        "error_code",
    }
)
_HEALTH_REQUIRED = frozenset(
    {
        "schema",
        "schema_version",
        "service_id",
        "evaluated_at",
        "status",
        "reason_codes",
        "expected_worker_count",
        "observed_worker_count",
        "affected_worker_count",
        "workers",
        "dedupe_key",
    }
)
_WORKER_HEALTH_REQUIRED = frozenset(
    {"worker_id", "state", "age_seconds", "affected"}
)
_STATE_REQUIRED = frozenset(
    {"schema", "schema_version", "service_id", "generation", "incident"}
)
_INCIDENT_REQUIRED = frozenset(
    {
        "incident_id",
        "status",
        "dedupe_key",
        "opened_at",
        "last_seen_at",
        "recovered_at",
        "reason_codes",
        "affected_worker_count",
    }
)
_EVENT_REQUIRED = frozenset(
    {
        "schema",
        "schema_version",
        "service_id",
        "event_type",
        "incident_id",
        "dedupe_key",
        "occurred_at",
        "severity",
        "reason_codes",
        "affected_worker_count",
    }
)
_REPROCESS_REQUIRED = frozenset(
    {
        "schema",
        "schema_version",
        "scope",
        "reason_code",
        "requested_at",
        "targets",
        "idempotency_key",
    }
)
_REPROCESS_OPTIONAL = frozenset({"global_confirmation"})
_TARGET_REQUIRED = frozenset(
    {"paper_id", "expected_source_sha256", "expected_current_version_id"}
)


class OperationsContractError(ValueError):
    """An operational payload or state transition is invalid or unsafe."""


def _fail(code: str) -> OperationsContractError:
    return OperationsContractError(code)


def _object(
    value: Any,
    *,
    field: str,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _fail(f"{field}_must_be_object")
    if any(not isinstance(key, str) for key in value):
        raise _fail(f"{field}_key_invalid")
    missing = required - set(value)
    unknown = set(value) - required - optional
    if missing:
        raise _fail(f"{field}_missing_{sorted(missing)[0]}")
    if unknown:
        raise _fail(f"{field}_unknown_field")
    return value


def _identifier(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        raise _fail(f"{field}_invalid")
    return value


def _code(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not _CODE_RE.fullmatch(value):
        raise _fail(f"{field}_invalid")
    return value


def _paper_id(value: Any) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= 9_223_372_036_854_775_807
    ):
        raise _fail("paper_id_invalid")
    return value


def _timestamp(value: Any, *, field: str) -> tuple[str, datetime]:
    if not isinstance(value, str) or not _TIMESTAMP_RE.fullmatch(value):
        raise _fail(f"{field}_invalid")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError as exc:
        raise _fail(f"{field}_invalid") from exc
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise _fail(f"{field}_invalid")
    return value, parsed


def _nullable_timestamp(value: Any, *, field: str) -> tuple[str | None, datetime | None]:
    if value is None:
        return None, None
    return _timestamp(value, field=field)


def _bounded_integer(
    value: Any,
    *,
    field: str,
    minimum: int,
    maximum: int,
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise _fail(f"{field}_invalid")
    return value


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise _fail("json_serialization_invalid") from exc


def _reason_codes(value: Any, *, allow_empty: bool) -> list[str]:
    if not isinstance(value, list) or len(value) > 32:
        raise _fail("reason_codes_invalid")
    output = [_code(item, field="reason_code") for item in value]
    if not allow_empty and not output:
        raise _fail("reason_codes_empty")
    if len(output) != len(set(output)):
        raise _fail("reason_codes_duplicate")
    return sorted(output)


def _health_reason_codes(value: Any, *, allow_empty: bool) -> list[str]:
    output = _reason_codes(value, allow_empty=allow_empty)
    if not set(output).issubset(_HEALTH_REASON_CODES):
        raise _fail("health_reason_code_invalid")
    return output


def _schema_matches(payload: Mapping[str, Any], expected: str) -> bool:
    version = payload["schema_version"]
    return (
        payload["schema"] == expected
        and isinstance(version, int)
        and not isinstance(version, bool)
        and version == OPERATIONS_SCHEMA_VERSION
    )


def validate_worker_heartbeat(payload: Any) -> dict[str, Any]:
    """Validate a bounded heartbeat containing no title or paper content."""

    heartbeat = _object(
        payload,
        field="heartbeat",
        required=_HEARTBEAT_REQUIRED,
    )
    if not _schema_matches(heartbeat, HEARTBEAT_SCHEMA):
        raise _fail("heartbeat_schema_invalid")
    observed_at, observed_time = _timestamp(
        heartbeat["observed_at"], field="observed_at"
    )
    last_success_at, last_success_time = _nullable_timestamp(
        heartbeat["last_success_at"], field="last_success_at"
    )
    if last_success_time is not None and last_success_time > observed_time:
        raise _fail("last_success_after_observed")
    state = heartbeat["state"]
    if not isinstance(state, str) or state not in {
        "idle",
        "processing",
        "degraded",
        "stopped",
    }:
        raise _fail("worker_state_invalid")
    active = heartbeat["active_paper_id"]
    if active is not None:
        active = _paper_id(active)
    if state == "processing" and active is None:
        raise _fail("processing_requires_active_paper")
    if state in {"idle", "stopped"} and active is not None:
        raise _fail("inactive_worker_has_active_paper")
    error = heartbeat["error_code"]
    if error is not None:
        error = _code(error, field="error_code")
    if state == "degraded" and error is None:
        raise _fail("degraded_requires_error_code")
    if state in {"idle", "processing"} and error is not None:
        raise _fail("healthy_worker_has_error_code")
    return {
        "schema": HEARTBEAT_SCHEMA,
        "schema_version": 1,
        "worker_id": _identifier(heartbeat["worker_id"], field="worker_id"),
        "observed_at": observed_at,
        "sequence": _bounded_integer(
            heartbeat["sequence"],
            field="sequence",
            minimum=0,
            maximum=9_223_372_036_854_775_807,
        ),
        "state": state,
        "active_paper_id": active,
        "last_success_at": last_success_at,
        "error_code": error,
    }


def worker_heartbeat(
    *,
    worker_id: str,
    observed_at: str,
    sequence: int,
    state: str,
    active_paper_id: int | None = None,
    last_success_at: str | None = None,
    error_code: str | None = None,
) -> dict[str, Any]:
    return validate_worker_heartbeat(
        {
            "schema": HEARTBEAT_SCHEMA,
            "schema_version": 1,
            "worker_id": worker_id,
            "observed_at": observed_at,
            "sequence": sequence,
            "state": state,
            "active_paper_id": active_paper_id,
            "last_success_at": last_success_at,
            "error_code": error_code,
        }
    )


def _canonical_expected_workers(value: Any) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise _fail("expected_workers_invalid")
    if not 1 <= len(value) <= MAX_WORKERS:
        raise _fail("expected_workers_count_invalid")
    output = [_identifier(item, field="worker_id") for item in value]
    if len(output) != len(set(output)):
        raise _fail("expected_workers_duplicate")
    return sorted(output)


def build_worker_health_snapshot(
    heartbeats: Iterable[Mapping[str, Any]],
    *,
    expected_worker_ids: Sequence[str],
    evaluated_at: str,
    stale_after_seconds: int = 120,
    service_id: str = "paper-reader-v2",
) -> dict[str, Any]:
    """Aggregate current heartbeats into a deterministic health snapshot."""

    if not isinstance(heartbeats, Iterable) or isinstance(
        heartbeats, (str, bytes, Mapping)
    ):
        raise _fail("heartbeats_invalid")
    expected = _canonical_expected_workers(expected_worker_ids)
    service = _identifier(service_id, field="service_id")
    evaluated_text, evaluated_time = _timestamp(evaluated_at, field="evaluated_at")
    stale_after = _bounded_integer(
        stale_after_seconds,
        field="stale_after_seconds",
        minimum=10,
        maximum=86_400,
    )
    by_worker: dict[str, dict[str, Any]] = {}
    for raw in heartbeats:
        if len(by_worker) >= MAX_WORKERS:
            raise _fail("heartbeats_too_many_items")
        heartbeat = validate_worker_heartbeat(raw)
        worker_id = heartbeat["worker_id"]
        if worker_id not in expected:
            raise _fail("unexpected_worker")
        if worker_id in by_worker:
            raise _fail("heartbeat_duplicate_worker")
        by_worker[worker_id] = heartbeat

    reason_codes: set[str] = set()
    workers: list[dict[str, Any]] = []
    affected_count = 0
    has_unhealthy = False
    has_degraded = False
    for worker_id in expected:
        heartbeat = by_worker.get(worker_id)
        if heartbeat is None:
            reason_codes.add("worker_missing")
            workers.append(
                {
                    "worker_id": worker_id,
                    "state": "missing",
                    "age_seconds": None,
                    "affected": True,
                }
            )
            affected_count += 1
            has_unhealthy = True
            continue
        _text, observed_time = _timestamp(
            heartbeat["observed_at"], field="observed_at"
        )
        age = int((evaluated_time - observed_time).total_seconds())
        if age < -5:
            raise _fail("heartbeat_from_future")
        age = max(0, age)
        affected = False
        if age > stale_after:
            reason_codes.add("worker_stale")
            affected = True
            has_unhealthy = True
        if heartbeat["state"] == "stopped":
            reason_codes.add("worker_stopped")
            affected = True
            has_unhealthy = True
        elif heartbeat["state"] == "degraded":
            reason_codes.add("worker_degraded")
            affected = True
            has_degraded = True
        if affected:
            affected_count += 1
        workers.append(
            {
                "worker_id": worker_id,
                "state": heartbeat["state"],
                "age_seconds": age,
                "affected": affected,
            }
        )
    status = "unhealthy" if has_unhealthy else "degraded" if has_degraded else "healthy"
    reasons = sorted(reason_codes)
    dedupe_identity = {
        "service_id": service,
        "status": status,
        "reason_codes": reasons,
        "affected_worker_count": affected_count,
    }
    dedupe_key = "health-" + hashlib.sha256(
        _canonical_json_bytes(dedupe_identity)
    ).hexdigest()[:32]
    return {
        "schema": HEALTH_SCHEMA,
        "schema_version": 1,
        "service_id": service,
        "evaluated_at": evaluated_text,
        "status": status,
        "reason_codes": reasons,
        "expected_worker_count": len(expected),
        "observed_worker_count": len(by_worker),
        "affected_worker_count": affected_count,
        "workers": workers,
        "dedupe_key": dedupe_key,
    }


def validate_health_snapshot(payload: Any) -> dict[str, Any]:
    snapshot = _object(payload, field="health", required=_HEALTH_REQUIRED)
    if not _schema_matches(snapshot, HEALTH_SCHEMA):
        raise _fail("health_schema_invalid")
    service = _identifier(snapshot["service_id"], field="service_id")
    evaluated_at, _evaluated = _timestamp(
        snapshot["evaluated_at"], field="evaluated_at"
    )
    status = snapshot["status"]
    if not isinstance(status, str) or status not in {
        "healthy",
        "degraded",
        "unhealthy",
    }:
        raise _fail("health_status_invalid")
    reasons = _health_reason_codes(
        snapshot["reason_codes"], allow_empty=status == "healthy"
    )
    expected_count = _bounded_integer(
        snapshot["expected_worker_count"],
        field="expected_worker_count",
        minimum=1,
        maximum=MAX_WORKERS,
    )
    observed_count = _bounded_integer(
        snapshot["observed_worker_count"],
        field="observed_worker_count",
        minimum=0,
        maximum=expected_count,
    )
    affected_count = _bounded_integer(
        snapshot["affected_worker_count"],
        field="affected_worker_count",
        minimum=0,
        maximum=expected_count,
    )
    workers_value = snapshot["workers"]
    if not isinstance(workers_value, list) or len(workers_value) != expected_count:
        raise _fail("health_workers_invalid")
    workers: list[dict[str, Any]] = []
    seen: set[str] = set()
    calculated_affected = 0
    calculated_observed = 0
    worker_state_reasons: set[str] = set()
    for raw in workers_value:
        worker = _object(raw, field="health_worker", required=_WORKER_HEALTH_REQUIRED)
        worker_id = _identifier(worker["worker_id"], field="worker_id")
        if worker_id in seen:
            raise _fail("health_worker_duplicate")
        seen.add(worker_id)
        worker_state = worker["state"]
        if not isinstance(worker_state, str) or worker_state not in {
            "idle",
            "processing",
            "degraded",
            "stopped",
            "missing",
        }:
            raise _fail("health_worker_state_invalid")
        age = worker["age_seconds"]
        if worker_state == "missing":
            if age is not None:
                raise _fail("missing_worker_has_age")
        else:
            age = _bounded_integer(
                age,
                field="age_seconds",
                minimum=0,
                maximum=31_536_000,
            )
            calculated_observed += 1
        if not isinstance(worker["affected"], bool):
            raise _fail("worker_affected_invalid")
        if worker_state in {"missing", "stopped", "degraded"}:
            if not worker["affected"]:
                raise _fail("unhealthy_worker_not_affected")
            worker_state_reasons.add(f"worker_{worker_state}")
        elif worker["affected"]:
            worker_state_reasons.add("worker_stale")
        calculated_affected += int(worker["affected"])
        workers.append(
            {
                "worker_id": worker_id,
                "state": worker_state,
                "age_seconds": age,
                "affected": worker["affected"],
            }
        )
    if calculated_affected != affected_count or calculated_observed != observed_count:
        raise _fail("health_counts_mismatch")
    if not worker_state_reasons.issubset(reasons):
        raise _fail("health_reasons_mismatch")
    dedupe = snapshot["dedupe_key"]
    if not isinstance(dedupe, str) or not re.fullmatch(r"health-[0-9a-f]{32}", dedupe):
        raise _fail("health_dedupe_key_invalid")
    expected_dedupe = "health-" + hashlib.sha256(
        _canonical_json_bytes(
            {
                "service_id": service,
                "status": status,
                "reason_codes": reasons,
                "affected_worker_count": affected_count,
            }
        )
    ).hexdigest()[:32]
    if dedupe != expected_dedupe:
        raise _fail("health_dedupe_key_mismatch")
    derived_status = (
        "unhealthy"
        if set(reasons) & {"worker_missing", "worker_stale", "worker_stopped"}
        else "degraded"
        if "worker_degraded" in reasons
        else "healthy"
    )
    if status != derived_status:
        raise _fail("health_status_mismatch")
    if status == "healthy" and (reasons or affected_count):
        raise _fail("healthy_snapshot_has_incident")
    if status != "healthy" and (not reasons or affected_count == 0):
        raise _fail("unhealthy_snapshot_missing_incident")
    return {
        "schema": HEALTH_SCHEMA,
        "schema_version": 1,
        "service_id": service,
        "evaluated_at": evaluated_at,
        "status": status,
        "reason_codes": reasons,
        "expected_worker_count": expected_count,
        "observed_worker_count": observed_count,
        "affected_worker_count": affected_count,
        "workers": sorted(workers, key=lambda item: item["worker_id"]),
        "dedupe_key": dedupe,
    }


def initial_incident_state(service_id: str = "paper-reader-v2") -> dict[str, Any]:
    return {
        "schema": INCIDENT_STATE_SCHEMA,
        "schema_version": 1,
        "service_id": _identifier(service_id, field="service_id"),
        "generation": 0,
        "incident": None,
    }


def _canonical_incident(value: Any, *, generation: int) -> dict[str, Any] | None:
    if value is None:
        return None
    incident = _object(value, field="incident", required=_INCIDENT_REQUIRED)
    incident_id = incident["incident_id"]
    if not isinstance(incident_id, str) or not re.fullmatch(
        r"incident-[0-9a-f]{32}", incident_id
    ):
        raise _fail("incident_id_invalid")
    status = incident["status"]
    if not isinstance(status, str) or status not in {"open", "recovered"}:
        raise _fail("incident_status_invalid")
    opened_at, opened = _timestamp(incident["opened_at"], field="opened_at")
    last_seen_at, last_seen = _timestamp(
        incident["last_seen_at"], field="last_seen_at"
    )
    recovered_at, recovered = _nullable_timestamp(
        incident["recovered_at"], field="recovered_at"
    )
    if last_seen < opened:
        raise _fail("incident_time_order_invalid")
    if status == "open" and recovered is not None:
        raise _fail("open_incident_has_recovery")
    if status == "recovered" and (recovered is None or recovered < last_seen):
        raise _fail("recovered_incident_time_invalid")
    dedupe = incident["dedupe_key"]
    if not isinstance(dedupe, str) or not re.fullmatch(r"health-[0-9a-f]{32}", dedupe):
        raise _fail("incident_dedupe_key_invalid")
    return {
        "incident_id": incident_id,
        "status": status,
        "dedupe_key": dedupe,
        "opened_at": opened_at,
        "last_seen_at": last_seen_at,
        "recovered_at": recovered_at,
        "reason_codes": _health_reason_codes(
            incident["reason_codes"], allow_empty=False
        ),
        "affected_worker_count": _bounded_integer(
            incident["affected_worker_count"],
            field="affected_worker_count",
            minimum=1,
            maximum=MAX_WORKERS,
        ),
    }


def validate_incident_state(payload: Any) -> dict[str, Any]:
    state = _object(payload, field="incident_state", required=_STATE_REQUIRED)
    if not _schema_matches(state, INCIDENT_STATE_SCHEMA):
        raise _fail("incident_state_schema_invalid")
    generation = _bounded_integer(
        state["generation"],
        field="generation",
        minimum=0,
        maximum=9_223_372_036_854_775_807,
    )
    incident = _canonical_incident(state["incident"], generation=generation)
    if generation == 0 and incident is not None:
        raise _fail("incident_generation_mismatch")
    if generation > 0 and incident is None:
        raise _fail("incident_generation_mismatch")
    return {
        "schema": INCIDENT_STATE_SCHEMA,
        "schema_version": 1,
        "service_id": _identifier(state["service_id"], field="service_id"),
        "generation": generation,
        "incident": incident,
    }


def _incident_event(
    *,
    state: Mapping[str, Any],
    incident: Mapping[str, Any],
    event_type: str,
    occurred_at: str,
) -> dict[str, Any]:
    dedupe_identity = {
        "service_id": state["service_id"],
        "event_type": event_type,
        "incident_id": incident["incident_id"],
    }
    return {
        "schema": INCIDENT_EVENT_SCHEMA,
        "schema_version": 1,
        "service_id": state["service_id"],
        "event_type": event_type,
        "incident_id": incident["incident_id"],
        "dedupe_key": "event-"
        + hashlib.sha256(_canonical_json_bytes(dedupe_identity)).hexdigest()[:32],
        "occurred_at": occurred_at,
        "severity": "warning" if event_type == "incident_opened" else "info",
        "reason_codes": list(incident["reason_codes"]),
        "affected_worker_count": incident["affected_worker_count"],
    }


def advance_incident_state(
    state: Any,
    health_snapshot: Any,
) -> tuple[dict[str, Any], tuple[dict[str, Any], ...]]:
    """Open once, dedupe while unhealthy, and emit recovery exactly once."""

    canonical_state = validate_incident_state(state)
    health = validate_health_snapshot(health_snapshot)
    if canonical_state["service_id"] != health["service_id"]:
        raise _fail("incident_service_mismatch")
    occurred_at = health["evaluated_at"]
    _text, transition_time = _timestamp(occurred_at, field="evaluated_at")
    incident = canonical_state["incident"]
    if incident is not None:
        _seen_text, last_seen = _timestamp(
            incident["last_seen_at"], field="last_seen_at"
        )
        if transition_time < last_seen:
            raise _fail("incident_transition_time_regressed")

    if health["status"] != "healthy":
        if incident is not None and incident["status"] == "open":
            updated = dict(
                incident,
                last_seen_at=occurred_at,
                reason_codes=health["reason_codes"],
                affected_worker_count=health["affected_worker_count"],
            )
            return dict(canonical_state, incident=updated), ()
        generation = canonical_state["generation"] + 1
        identity = {
            "service_id": canonical_state["service_id"],
            "generation": generation,
            "opened_at": occurred_at,
            "dedupe_key": health["dedupe_key"],
        }
        opened = {
            "incident_id": "incident-"
            + hashlib.sha256(_canonical_json_bytes(identity)).hexdigest()[:32],
            "status": "open",
            "dedupe_key": health["dedupe_key"],
            "opened_at": occurred_at,
            "last_seen_at": occurred_at,
            "recovered_at": None,
            "reason_codes": health["reason_codes"],
            "affected_worker_count": health["affected_worker_count"],
        }
        next_state = dict(
            canonical_state,
            generation=generation,
            incident=opened,
        )
        event = _incident_event(
            state=next_state,
            incident=opened,
            event_type="incident_opened",
            occurred_at=occurred_at,
        )
        return next_state, (event,)

    if incident is not None and incident["status"] == "open":
        recovered = dict(
            incident,
            status="recovered",
            recovered_at=occurred_at,
        )
        next_state = dict(canonical_state, incident=recovered)
        event = _incident_event(
            state=next_state,
            incident=recovered,
            event_type="incident_recovered",
            occurred_at=occurred_at,
        )
        return next_state, (event,)
    return canonical_state, ()


def validate_incident_event(payload: Any) -> dict[str, Any]:
    event = _object(payload, field="incident_event", required=_EVENT_REQUIRED)
    if not _schema_matches(event, INCIDENT_EVENT_SCHEMA):
        raise _fail("incident_event_schema_invalid")
    event_type = event["event_type"]
    if not isinstance(event_type, str) or event_type not in {
        "incident_opened",
        "incident_recovered",
    }:
        raise _fail("incident_event_type_invalid")
    severity = event["severity"]
    expected_severity = "warning" if event_type == "incident_opened" else "info"
    if severity != expected_severity:
        raise _fail("incident_event_severity_invalid")
    incident_id = event["incident_id"]
    if not isinstance(incident_id, str) or not re.fullmatch(
        r"incident-[0-9a-f]{32}", incident_id
    ):
        raise _fail("incident_id_invalid")
    dedupe = event["dedupe_key"]
    if not isinstance(dedupe, str) or not _EVENT_DEDUPE_RE.fullmatch(dedupe):
        raise _fail("incident_dedupe_key_invalid")
    occurred_at, _time = _timestamp(event["occurred_at"], field="occurred_at")
    expected_dedupe = "event-" + hashlib.sha256(
        _canonical_json_bytes(
            {
                "service_id": event["service_id"],
                "event_type": event_type,
                "incident_id": incident_id,
            }
        )
    ).hexdigest()[:32]
    if dedupe != expected_dedupe:
        raise _fail("incident_dedupe_key_mismatch")
    return {
        "schema": INCIDENT_EVENT_SCHEMA,
        "schema_version": 1,
        "service_id": _identifier(event["service_id"], field="service_id"),
        "event_type": event_type,
        "incident_id": incident_id,
        "dedupe_key": dedupe,
        "occurred_at": occurred_at,
        "severity": severity,
        "reason_codes": _health_reason_codes(
            event["reason_codes"], allow_empty=False
        ),
        "affected_worker_count": _bounded_integer(
            event["affected_worker_count"],
            field="affected_worker_count",
            minimum=1,
            maximum=MAX_WORKERS,
        ),
    }


def build_generic_webhook_payload(event: Any) -> dict[str, Any]:
    """Build generic notification data; this function performs no transmission."""

    canonical = validate_incident_event(event)
    opened = canonical["event_type"] == "incident_opened"
    return {
        "schema": WEBHOOK_SCHEMA,
        "schema_version": 1,
        "event": canonical["event_type"],
        "incident_id": canonical["incident_id"],
        "dedupe_key": canonical["dedupe_key"],
        "occurred_at": canonical["occurred_at"],
        "severity": canonical["severity"],
        "summary": (
            "Paper reader worker health incident opened"
            if opened
            else "Paper reader worker health recovered"
        ),
        "reason_codes": canonical["reason_codes"],
        "metrics": {"affected_workers": canonical["affected_worker_count"]},
    }


def _canonical_reprocess_targets(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > MAX_REPROCESS_TARGETS:
        raise _fail("reprocess_targets_invalid")
    output: list[dict[str, Any]] = []
    seen: set[int] = set()
    for raw in value:
        target = _object(raw, field="reprocess_target", required=_TARGET_REQUIRED)
        paper_id = _paper_id(target["paper_id"])
        if paper_id in seen:
            raise _fail("reprocess_target_duplicate")
        seen.add(paper_id)
        source_sha = target["expected_source_sha256"]
        if not isinstance(source_sha, str) or not _SHA256_RE.fullmatch(source_sha):
            raise _fail("expected_source_sha256_invalid")
        version_id = target["expected_current_version_id"]
        if version_id is not None and (
            not isinstance(version_id, str) or not _VERSION_ID_RE.fullmatch(version_id)
        ):
            raise _fail("expected_current_version_id_invalid")
        output.append(
            {
                "paper_id": paper_id,
                "expected_source_sha256": source_sha,
                "expected_current_version_id": version_id,
            }
        )
    return sorted(output, key=lambda item: item["paper_id"])


def derive_reprocess_idempotency_key(
    *,
    scope: str,
    reason_code: str,
    requested_at: str,
    targets: Sequence[Mapping[str, Any]],
) -> str:
    if not isinstance(targets, Sequence) or isinstance(targets, (str, bytes)):
        raise _fail("reprocess_targets_invalid")
    if len(targets) > MAX_REPROCESS_TARGETS:
        raise _fail("reprocess_targets_invalid")
    canonical_targets = _canonical_reprocess_targets(list(targets))
    if not isinstance(scope, str) or scope not in {"selected", "all"}:
        raise _fail("reprocess_scope_invalid")
    reason = _code(reason_code, field="reason_code")
    requested, _requested_time = _timestamp(requested_at, field="requested_at")
    identity = {
        "scope": scope,
        "reason_code": reason,
        "requested_at": requested,
        "targets": canonical_targets,
    }
    return "rr-" + hashlib.sha256(_canonical_json_bytes(identity)).hexdigest()[:32]


def validate_reprocess_request(
    payload: Any,
    *,
    allow_global: bool = False,
) -> dict[str, Any]:
    """Validate a bounded selective request; global scope is denied by default."""

    if not isinstance(allow_global, bool):
        raise _fail("allow_global_invalid")
    request = _object(
        payload,
        field="reprocess_request",
        required=_REPROCESS_REQUIRED,
        optional=_REPROCESS_OPTIONAL,
    )
    if not _schema_matches(request, REPROCESS_SCHEMA):
        raise _fail("reprocess_schema_invalid")
    scope = request["scope"]
    if not isinstance(scope, str) or scope not in {"selected", "all"}:
        raise _fail("reprocess_scope_invalid")
    reason = _code(request["reason_code"], field="reason_code")
    requested_at, _requested_time = _timestamp(
        request["requested_at"], field="requested_at"
    )
    targets = _canonical_reprocess_targets(request["targets"])
    if scope == "selected":
        if not targets:
            raise _fail("selective_reprocess_targets_required")
        if "global_confirmation" in request:
            raise _fail("selective_reprocess_has_global_confirmation")
    else:
        if not allow_global:
            raise _fail("global_reprocess_forbidden")
        if targets:
            raise _fail("global_reprocess_targets_must_be_empty")
        if request.get("global_confirmation") != GLOBAL_REPROCESS_CONFIRMATION:
            raise _fail("global_reprocess_confirmation_required")
    key = request["idempotency_key"]
    if not isinstance(key, str) or not _IDEMPOTENCY_RE.fullmatch(key):
        raise _fail("reprocess_idempotency_key_invalid")
    expected_key = derive_reprocess_idempotency_key(
        scope=scope,
        reason_code=reason,
        requested_at=requested_at,
        targets=targets,
    )
    if key != expected_key:
        raise _fail("reprocess_idempotency_key_mismatch")
    canonical = {
        "schema": REPROCESS_SCHEMA,
        "schema_version": 1,
        "scope": scope,
        "reason_code": reason,
        "requested_at": requested_at,
        "targets": targets,
        "idempotency_key": key,
    }
    if scope == "all":
        canonical["global_confirmation"] = GLOBAL_REPROCESS_CONFIRMATION
    return canonical


def build_selective_reprocess_request(
    *,
    targets: Sequence[Mapping[str, Any]],
    reason_code: str,
    requested_at: str,
) -> dict[str, Any]:
    if not isinstance(targets, Sequence) or isinstance(targets, (str, bytes)):
        raise _fail("reprocess_targets_invalid")
    if len(targets) > MAX_REPROCESS_TARGETS:
        raise _fail("reprocess_targets_invalid")
    canonical_targets = _canonical_reprocess_targets(list(targets))
    key = derive_reprocess_idempotency_key(
        scope="selected",
        reason_code=reason_code,
        requested_at=requested_at,
        targets=canonical_targets,
    )
    return validate_reprocess_request(
        {
            "schema": REPROCESS_SCHEMA,
            "schema_version": 1,
            "scope": "selected",
            "reason_code": reason_code,
            "requested_at": requested_at,
            "targets": canonical_targets,
            "idempotency_key": key,
        }
    )


def validate_reprocess_target_source(
    request: Any,
    *,
    paper_id: int,
    actual_source_sha256: str,
    actual_current_version_id: str | None,
) -> dict[str, Any]:
    """Reject a queued target when source or active version became stale."""

    canonical = validate_reprocess_request(request)
    target_id = _paper_id(paper_id)
    if not isinstance(actual_source_sha256, str) or not _SHA256_RE.fullmatch(
        actual_source_sha256
    ):
        raise _fail("actual_source_sha256_invalid")
    if actual_current_version_id is not None and (
        not isinstance(actual_current_version_id, str)
        or not _VERSION_ID_RE.fullmatch(actual_current_version_id)
    ):
        raise _fail("actual_current_version_id_invalid")
    target = next(
        (item for item in canonical["targets"] if item["paper_id"] == target_id),
        None,
    )
    if target is None:
        raise _fail("reprocess_target_not_requested")
    if target["expected_source_sha256"] != actual_source_sha256:
        raise _fail("reprocess_source_sha256_stale")
    if target["expected_current_version_id"] != actual_current_version_id:
        raise _fail("reprocess_current_version_stale")
    return target


def reprocess_request_fingerprint(
    request: Any,
    *,
    allow_global: bool = False,
) -> str:
    canonical = validate_reprocess_request(request, allow_global=allow_global)
    return hashlib.sha256(_canonical_json_bytes(canonical)).hexdigest()


def classify_reprocess_idempotency(
    request: Any,
    known_requests: Mapping[str, str],
    *,
    allow_global: bool = False,
) -> str:
    """Return ``new`` or ``duplicate``; reject an impossible key collision."""

    if not isinstance(known_requests, Mapping):
        raise _fail("known_requests_invalid")
    canonical = validate_reprocess_request(request, allow_global=allow_global)
    key = canonical["idempotency_key"]
    fingerprint = reprocess_request_fingerprint(
        canonical, allow_global=allow_global
    )
    existing = known_requests.get(key)
    if existing is None:
        return "new"
    if existing != fingerprint:
        raise _fail("reprocess_idempotency_conflict")
    return "duplicate"


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise _fail("json_duplicate_key")
        output[key] = value
    return output


def load_reprocess_request_json(
    value: str | bytes,
    *,
    allow_global: bool = False,
) -> dict[str, Any]:
    if isinstance(value, bytes):
        encoded = value
        try:
            text = value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _fail("json_utf8_invalid") from exc
    elif isinstance(value, str):
        try:
            encoded = value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise _fail("json_utf8_invalid") from exc
        text = value
    else:
        raise _fail("json_input_must_be_text")
    if len(encoded) > MAX_OPERATIONS_JSON_BYTES:
        raise _fail("json_too_large")
    try:
        payload = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_float=lambda _value: (_ for _ in ()).throw(_fail("json_float_invalid")),
            parse_constant=lambda _value: (_ for _ in ()).throw(_fail("json_constant_invalid")),
        )
    except OperationsContractError:
        raise
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise _fail("json_invalid") from exc
    return validate_reprocess_request(payload, allow_global=allow_global)

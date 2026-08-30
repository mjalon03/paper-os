"""PostgreSQL persistence for observable and approval-gated paper operations.

This module deliberately stops at durable state transitions.  It does not send
webhooks, run an extractor, publish an artifact, or move an artifact pointer.
Callers must resolve the current artifact version immediately before authorizing
an execution job, and must persist a planned result before publishing it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any
from uuid import UUID, uuid4, uuid5

from .database import connection
from .operations import (
    MAX_REPROCESS_TARGETS,
    advance_incident_state,
    build_generic_webhook_payload,
    build_worker_health_snapshot,
    initial_incident_state,
    reprocess_request_fingerprint,
    validate_health_snapshot,
    validate_incident_event,
    validate_incident_state,
    validate_reprocess_request,
    validate_worker_heartbeat,
)


_REPROCESS_NAMESPACE = UUID("87520957-c7ab-53d5-af0b-5eca19ba98ca")
_REPROCESS_JOB_NAMESPACE = UUID("b2bf3301-455c-5612-97c8-4cc39ad537b7")
_ROLLBACK_NAMESPACE = UUID("0cc32832-eb7a-550a-b2d0-f299a7d6c0c2")
_ROLLBACK_JOB_NAMESPACE = UUID("f11ddc13-8352-5bf1-a7e6-e4d8c03e46a8")
_REVIEW_NAMESPACE = UUID("42536d1f-b103-5b70-8bbc-119f1e9a19b2")
_INCIDENT_EVENT_NAMESPACE = UUID("d1c73bf3-9b16-54c3-a755-55922d464206")
_WEBHOOK_NAMESPACE = UUID("176cda77-f2cd-559f-9028-eec316ab9ffd")

_IDENTIFIER_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_CODE_RE = re.compile(r"[a-z][a-z0-9_]{0,119}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_VERSION_ID_RE = re.compile(r"rv-[0-9a-f]{32}")
_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

WEBHOOK_COLUMNS = """
id, incident_event_id, event_dedupe_key, payload, payload_sha256, status,
attempt_count, max_attempts, available_at, lease_owner, lease_token,
lease_expires_at, last_error_code, created_at, delivered_at
"""

REPROCESS_JOB_COLUMNS = """
id, request_id, paper_id, status, attempt_count, max_attempts, available_at,
lease_owner, lease_token, lease_expires_at, planned_result_version_id,
result_version_id, last_error_code, created_at, started_at, finished_at
"""

ROLLBACK_JOB_COLUMNS = """
id, request_id, paper_id, status, attempt_count, max_attempts, available_at,
lease_owner, lease_token, lease_expires_at, result_version_id,
last_error_code, created_at, started_at, finished_at
"""

__all__ = [
    "OperationsApprovalRequired",
    "OperationsLeaseLost",
    "OperationsReviewConflict",
    "OperationsStoreError",
    "ack_webhook_item",
    "authorize_reprocess_job",
    "authorize_rollback_job",
    "bind_reprocess_result_version",
    "claim_reprocess_job",
    "claim_rollback_job",
    "claim_webhook_item",
    "complete_reprocess_job",
    "complete_rollback_job",
    "configure_expected_workers",
    "create_reprocess_request",
    "create_rollback_request",
    "enqueue_approved_reprocess",
    "enqueue_approved_rollback",
    "evaluate_and_record_worker_health",
    "get_cost_summary",
    "get_current_incident_state",
    "get_operation_request_detail",
    "get_operations_summary",
    "get_reprocess_request",
    "get_rollback_request",
    "get_worker_health_snapshot",
    "list_operation_failures",
    "list_expected_worker_services",
    "list_pending_operation_reviews",
    "list_recent_operation_requests",
    "list_reprocess_target_states",
    "list_rollback_target_states",
    "nack_reprocess_job",
    "nack_rollback_job",
    "nack_webhook_item",
    "record_health_snapshot",
    "review_reprocess_request",
    "review_rollback_request",
    "upsert_worker_heartbeat",
]


class OperationsStoreError(RuntimeError):
    """A durable operations invariant could not be proven."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class OperationsReviewConflict(OperationsStoreError):
    """The review head differs from the caller's optimistic revision."""

    def __init__(self, current_revision: int, decision: str):
        super().__init__("operations_review_revision_conflict")
        self.current_revision = current_revision
        self.decision = decision


class OperationsApprovalRequired(OperationsStoreError):
    """Execution was requested before a human approval event existed."""


class OperationsLeaseLost(OperationsStoreError):
    """A job/outbox lease is absent, expired, or owned by another worker."""


def _fail(code: str) -> OperationsStoreError:
    return OperationsStoreError(code)


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
        raise _fail("value_not_canonical_json") from exc


def _json(value: Any) -> str:
    return _canonical_bytes(value).decode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _row(value: Any, *, missing_code: str) -> dict[str, Any]:
    if value is None:
        raise _fail(missing_code)
    try:
        return dict(value)
    except (TypeError, ValueError) as exc:
        raise _fail("database_row_invalid") from exc


def _rows(value: Any) -> list[dict[str, Any]]:
    try:
        return [dict(item) for item in value]
    except (TypeError, ValueError) as exc:
        raise _fail("database_row_invalid") from exc


def _identifier(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        raise _fail(f"{field}_invalid")
    return value


def _code(value: Any, *, field: str = "error_code") -> str:
    if not isinstance(value, str) or not _CODE_RE.fullmatch(value):
        raise _fail(f"{field}_invalid")
    return value


def _text(value: Any, *, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise _fail(f"{field}_invalid")
    clean = value.strip()
    if not clean or len(clean) > maximum or _CONTROL_RE.search(clean):
        raise _fail(f"{field}_invalid")
    try:
        clean.encode("utf-8")
    except UnicodeError as exc:
        raise _fail(f"{field}_invalid") from exc
    return clean


def _comment(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _fail("review_comment_invalid")
    clean = value.strip()
    if len(clean) > 4_000 or _CONTROL_RE.search(clean):
        raise _fail("review_comment_invalid")
    return clean or None


def _positive_int(value: Any, *, field: str, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise _fail(f"{field}_invalid")
    if maximum is not None and value > maximum:
        raise _fail(f"{field}_invalid")
    return value


def _nonnegative_int(value: Any, *, field: str, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _fail(f"{field}_invalid")
    if maximum is not None and value > maximum:
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
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise _fail(f"{field}_invalid")
    return value


def _version(value: Any, *, field: str, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not _VERSION_ID_RE.fullmatch(value):
        raise _fail(f"{field}_invalid")
    return value


def _timestamp(value: Any, *, field: str) -> str:
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
    return value


def _utc_text(value: Any, *, field: str, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise _fail(f"stored_{field}_invalid")
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, str):
        text = value
        if text.endswith("+00:00"):
            text = text[:-6] + "Z"
        return _timestamp(text, field=f"stored_{field}")
    raise _fail(f"stored_{field}_invalid")


def _json_object(value: Any, *, field: str) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, RecursionError) as exc:
            raise _fail(f"stored_{field}_invalid") from exc
    if not isinstance(value, dict):
        raise _fail(f"stored_{field}_invalid")
    return value


def _heartbeat_from_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return validate_worker_heartbeat(
        {
            "schema": "paper-reader-worker-heartbeat",
            "schema_version": 1,
            "worker_id": row["worker_id"],
            "observed_at": _utc_text(row["observed_at"], field="observed_at"),
            "sequence": int(row["sequence"]),
            "state": row["state"],
            "active_paper_id": row.get("active_paper_id"),
            "last_success_at": _utc_text(
                row.get("last_success_at"), field="last_success_at", nullable=True
            ),
            "error_code": row.get("error_code"),
        }
    )


def configure_expected_workers(
    worker_ids: Sequence[str], *, service_id: str = "paper-reader-v2"
) -> tuple[str, ...]:
    """Replace the enabled worker set without deleting heartbeat history."""

    service = _identifier(service_id, field="service_id")
    if not isinstance(worker_ids, Sequence) or isinstance(worker_ids, (str, bytes)):
        raise _fail("expected_workers_invalid")
    workers = sorted(_identifier(item, field="worker_id") for item in worker_ids)
    if not 1 <= len(workers) <= 64 or len(workers) != len(set(workers)):
        raise _fail("expected_workers_invalid")
    with connection() as database:
        database.execute(
            """UPDATE paper_expected_workers SET enabled = FALSE, updated_at = NOW()
               WHERE service_id = %s AND enabled""",
            (service,),
        )
        for worker in workers:
            database.execute(
                """INSERT INTO paper_expected_workers
                       (service_id, worker_id, enabled)
                   VALUES (%s, %s, TRUE)
                   ON CONFLICT (service_id, worker_id) DO UPDATE
                   SET enabled = TRUE, updated_at = NOW()""",
                (service, worker),
            )
    return tuple(workers)


def upsert_worker_heartbeat(
    payload: Any, *, service_id: str = "paper-reader-v2"
) -> dict[str, Any]:
    """Persist a monotonic heartbeat; same-sequence conflicts fail closed."""

    heartbeat = validate_worker_heartbeat(payload)
    service = _identifier(service_id, field="service_id")
    parameters = (
        service,
        heartbeat["worker_id"],
        heartbeat["observed_at"],
        heartbeat["sequence"],
        heartbeat["state"],
        heartbeat["active_paper_id"],
        heartbeat["last_success_at"],
        heartbeat["error_code"],
    )
    with connection() as database:
        expected = database.execute(
            """SELECT enabled FROM paper_expected_workers
               WHERE service_id = %s AND worker_id = %s FOR SHARE""",
            (service, heartbeat["worker_id"]),
        ).fetchone()
        if expected is None or not bool(expected["enabled"]):
            raise _fail("worker_not_expected")
        row = database.execute(
            """INSERT INTO paper_worker_heartbeats
                   (service_id, worker_id, observed_at, sequence, state,
                    active_paper_id, last_success_at, error_code)
               VALUES (%s, %s, %s::timestamptz, %s, %s, %s,
                       %s::timestamptz, %s)
               ON CONFLICT (service_id, worker_id) DO UPDATE
               SET observed_at = EXCLUDED.observed_at,
                   sequence = EXCLUDED.sequence,
                   state = EXCLUDED.state,
                   active_paper_id = EXCLUDED.active_paper_id,
                   last_success_at = EXCLUDED.last_success_at,
                   error_code = EXCLUDED.error_code,
                   updated_at = NOW()
               WHERE EXCLUDED.sequence > paper_worker_heartbeats.sequence
                 AND EXCLUDED.observed_at >= paper_worker_heartbeats.observed_at
               RETURNING worker_id, observed_at, sequence, state,
                         active_paper_id, last_success_at, error_code""",
            parameters,
        ).fetchone()
        if row is None:
            stored_row = database.execute(
                """SELECT worker_id, observed_at, sequence, state,
                          active_paper_id, last_success_at, error_code
                   FROM paper_worker_heartbeats
                   WHERE service_id = %s AND worker_id = %s""",
                (service, heartbeat["worker_id"]),
            ).fetchone()
            stored = _heartbeat_from_row(
                _row(stored_row, missing_code="worker_heartbeat_not_expected")
            )
            if stored != heartbeat:
                raise _fail("worker_heartbeat_sequence_conflict")
            return stored
        return _heartbeat_from_row(dict(row))


def get_worker_health_snapshot(
    *,
    evaluated_at: str,
    stale_after_seconds: int = 120,
    service_id: str = "paper-reader-v2",
) -> dict[str, Any]:
    """Read only opaque worker state/counts and build a validated snapshot."""

    service = _identifier(service_id, field="service_id")
    evaluated = _timestamp(evaluated_at, field="evaluated_at")
    with connection() as database:
        expected_rows = database.execute(
            """SELECT worker_id FROM paper_expected_workers
               WHERE service_id = %s AND enabled ORDER BY worker_id""",
            (service,),
        ).fetchall()
        expected = [str(row["worker_id"]) for row in expected_rows]
        if not expected:
            raise _fail("expected_workers_not_configured")
        heartbeat_rows = database.execute(
            """SELECT worker_id, observed_at, sequence, state,
                      active_paper_id, last_success_at, error_code
               FROM paper_worker_heartbeats
               WHERE service_id = %s AND worker_id = ANY(%s)
               ORDER BY worker_id""",
            (service, expected),
        ).fetchall()
    heartbeats = [_heartbeat_from_row(row) for row in heartbeat_rows]
    return build_worker_health_snapshot(
        heartbeats,
        expected_worker_ids=expected,
        evaluated_at=evaluated,
        stale_after_seconds=stale_after_seconds,
        service_id=service,
    )


def list_expected_worker_services(*, limit: int = 16) -> tuple[str, ...]:
    """Return a bounded set of opaque service IDs for health administration."""

    maximum = _positive_int(limit, field="limit", maximum=64)
    with connection() as database:
        rows = database.execute(
            """SELECT DISTINCT service_id
               FROM paper_expected_workers
               WHERE enabled
               ORDER BY service_id
               LIMIT %s""",
            (maximum + 1,),
        ).fetchall()
    if len(rows) > maximum:
        raise _fail("expected_worker_service_limit_exceeded")
    return tuple(
        _identifier(row["service_id"], field="service_id") for row in rows
    )


def _incident_state_locked(database: Any, service_id: str) -> dict[str, Any]:
    database.execute(
        """INSERT INTO paper_incident_heads (service_id)
           VALUES (%s) ON CONFLICT (service_id) DO NOTHING""",
        (service_id,),
    )
    row = database.execute(
        """SELECT h.service_id, h.generation, h.current_incident_id,
                  i.status, i.health_dedupe_key, i.opened_at, i.last_seen_at,
                  i.recovered_at, i.reason_codes, i.affected_worker_count
           FROM paper_incident_heads h
           LEFT JOIN paper_incidents i
             ON i.incident_id = h.current_incident_id
           WHERE h.service_id = %s FOR UPDATE OF h""",
        (service_id,),
    ).fetchone()
    current = _row(row, missing_code="incident_head_missing")
    if current["current_incident_id"] is None:
        return initial_incident_state(service_id)
    return validate_incident_state({
        "schema": "paper-reader-incident-state",
        "schema_version": 1,
        "service_id": service_id,
        "generation": int(current["generation"]),
        "incident": {
            "incident_id": str(current["current_incident_id"]),
            "status": str(current["status"]),
            "dedupe_key": str(current["health_dedupe_key"]),
            "opened_at": _utc_text(current["opened_at"], field="opened_at"),
            "last_seen_at": _utc_text(
                current["last_seen_at"], field="last_seen_at"
            ),
            "recovered_at": _utc_text(
                current["recovered_at"], field="recovered_at", nullable=True
            ),
            "reason_codes": list(current["reason_codes"]),
            "affected_worker_count": int(current["affected_worker_count"]),
        },
    })


def get_current_incident_state(
    *, service_id: str = "paper-reader-v2"
) -> dict[str, Any]:
    """Read current incident state without creating or mutating it."""

    service = _identifier(service_id, field="service_id")
    with connection() as database:
        row = database.execute(
            """SELECT h.service_id, h.generation, h.current_incident_id,
                      i.status, i.health_dedupe_key, i.opened_at,
                      i.last_seen_at, i.recovered_at, i.reason_codes,
                      i.affected_worker_count
               FROM paper_incident_heads h
               LEFT JOIN paper_incidents i
                 ON i.incident_id = h.current_incident_id
               WHERE h.service_id = %s""",
            (service,),
        ).fetchone()
    if row is None or row["current_incident_id"] is None:
        return initial_incident_state(service)
    current = dict(row)
    return validate_incident_state({
        "schema": "paper-reader-incident-state",
        "schema_version": 1,
        "service_id": service,
        "generation": int(current["generation"]),
        "incident": {
            "incident_id": str(current["current_incident_id"]),
            "status": str(current["status"]),
            "dedupe_key": str(current["health_dedupe_key"]),
            "opened_at": _utc_text(current["opened_at"], field="opened_at"),
            "last_seen_at": _utc_text(
                current["last_seen_at"], field="last_seen_at"
            ),
            "recovered_at": _utc_text(
                current["recovered_at"], field="recovered_at", nullable=True
            ),
            "reason_codes": list(current["reason_codes"]),
            "affected_worker_count": int(current["affected_worker_count"]),
        },
    })


def _persist_incident_event(database: Any, event: Mapping[str, Any]) -> None:
    canonical = validate_incident_event(event)
    event_id = str(uuid5(_INCIDENT_EVENT_NAMESPACE, canonical["dedupe_key"]))
    event_digest = _digest(canonical)
    payload = build_generic_webhook_payload(canonical)
    payload_digest = _digest(payload)
    outbox_id = str(uuid5(_WEBHOOK_NAMESPACE, canonical["dedupe_key"]))
    database.execute(
        """INSERT INTO paper_incident_events
               (id, service_id, incident_id, event_type, dedupe_key,
                occurred_at, severity, reason_codes, affected_worker_count,
                canonical_event, canonical_event_sha256)
           VALUES (%s::uuid, %s, %s, %s, %s, %s::timestamptz, %s, %s,
                   %s, %s::jsonb, %s)
           ON CONFLICT (dedupe_key) DO NOTHING""",
        (
            event_id,
            canonical["service_id"],
            canonical["incident_id"],
            canonical["event_type"],
            canonical["dedupe_key"],
            canonical["occurred_at"],
            canonical["severity"],
            canonical["reason_codes"],
            canonical["affected_worker_count"],
            _json(canonical),
            event_digest,
        ),
    )
    database.execute(
        """INSERT INTO paper_webhook_outbox
               (id, incident_event_id, event_dedupe_key, payload,
                payload_sha256)
           VALUES (%s::uuid, %s::uuid, %s, %s::jsonb, %s)
           ON CONFLICT (incident_event_id) DO NOTHING""",
        (
            outbox_id,
            event_id,
            canonical["dedupe_key"],
            _json(payload),
            payload_digest,
        ),
    )


def record_health_snapshot(snapshot: Any) -> dict[str, Any]:
    """Advance current incident state and enqueue each transition once."""

    health = validate_health_snapshot(snapshot)
    service = health["service_id"]
    with connection() as database:
        previous = _incident_state_locked(database, service)
        current, events = advance_incident_state(previous, health)
        prior_incident = previous["incident"]
        incident = current["incident"]
        if incident is not None and (
            prior_incident is None
            or prior_incident["incident_id"] != incident["incident_id"]
        ):
            database.execute(
                """INSERT INTO paper_incidents
                       (incident_id, service_id, generation, status,
                        health_dedupe_key, opened_at, last_seen_at,
                        recovered_at, reason_codes, affected_worker_count)
                   VALUES (%s, %s, %s, %s, %s, %s::timestamptz,
                           %s::timestamptz, %s::timestamptz, %s, %s)""",
                (
                    incident["incident_id"],
                    service,
                    current["generation"],
                    incident["status"],
                    incident["dedupe_key"],
                    incident["opened_at"],
                    incident["last_seen_at"],
                    incident["recovered_at"],
                    incident["reason_codes"],
                    incident["affected_worker_count"],
                ),
            )
        elif incident is not None and prior_incident is not None:
            database.execute(
                """UPDATE paper_incidents
                   SET status = %s, health_dedupe_key = %s,
                       last_seen_at = %s::timestamptz,
                       recovered_at = %s::timestamptz,
                       reason_codes = %s, affected_worker_count = %s
                   WHERE incident_id = %s AND service_id = %s""",
                (
                    incident["status"],
                    incident["dedupe_key"],
                    incident["last_seen_at"],
                    incident["recovered_at"],
                    incident["reason_codes"],
                    incident["affected_worker_count"],
                    incident["incident_id"],
                    service,
                ),
            )
        if incident is not None:
            database.execute(
                """UPDATE paper_incident_heads
                   SET generation = %s, current_incident_id = %s,
                       updated_at = NOW()
                   WHERE service_id = %s""",
                (current["generation"], incident["incident_id"], service),
            )
        for event in events:
            _persist_incident_event(database, event)
    return {"state": current, "events": list(events)}


def evaluate_and_record_worker_health(
    *,
    evaluated_at: str,
    stale_after_seconds: int = 120,
    service_id: str = "paper-reader-v2",
) -> dict[str, Any]:
    snapshot = get_worker_health_snapshot(
        evaluated_at=evaluated_at,
        stale_after_seconds=stale_after_seconds,
        service_id=service_id,
    )
    transition = record_health_snapshot(snapshot)
    return {"health": snapshot, **transition}


def _recover_expired_webhooks(database: Any) -> int:
    result = database.execute(
        """UPDATE paper_webhook_outbox
           SET status = CASE WHEN attempt_count >= max_attempts
                             THEN 'dead' ELSE 'pending' END,
               available_at = NOW(), lease_owner = NULL, lease_token = NULL,
               lease_expires_at = NULL, last_error_code = 'lease_expired'
           WHERE status = 'leased' AND lease_expires_at <= NOW()"""
    )
    return int(getattr(result, "rowcount", 0) or 0)


def claim_webhook_item(
    *, worker_id: str, lease_seconds: int = 120
) -> dict[str, Any] | None:
    worker = _text(worker_id, field="worker_id", maximum=200)
    seconds = _positive_int(lease_seconds, field="lease_seconds", maximum=3_600)
    token = str(uuid4())
    with connection() as database:
        _recover_expired_webhooks(database)
        row = database.execute(
            f"""WITH candidate AS (
                    SELECT id FROM paper_webhook_outbox
                    WHERE status = 'pending' AND available_at <= NOW()
                      AND attempt_count < max_attempts
                    ORDER BY available_at, created_at, id
                    FOR UPDATE SKIP LOCKED LIMIT 1
                ), leased AS (
                    UPDATE paper_webhook_outbox AS item
                    SET status = 'leased', attempt_count = attempt_count + 1,
                        lease_owner = %s, lease_token = %s::uuid,
                        lease_expires_at = NOW() + make_interval(secs => %s),
                        last_error_code = NULL
                    FROM candidate WHERE item.id = candidate.id
                    RETURNING item.*
                )
                SELECT {
                    ', '.join(
                        'leased.' + part.strip()
                        for part in WEBHOOK_COLUMNS.split(',')
                    )
                }, event.canonical_event,
                   event.canonical_event_sha256 AS event_sha256
                FROM leased JOIN paper_incident_events event
                  ON event.id = leased.incident_event_id""",
            (worker, token, seconds),
        ).fetchone()
        if row is None:
            return None
        item = dict(row)
        payload: dict[str, Any] | None = None
        invalid = True
        try:
            payload = _json_object(item["payload"], field="webhook_payload")
            event = validate_incident_event(
                _json_object(item["canonical_event"], field="incident_event")
            )
            expected_payload = build_generic_webhook_payload(event)
            invalid = (
                payload != expected_payload
                or _digest(payload) != str(item["payload_sha256"]).strip()
                or _digest(event) != str(item["event_sha256"]).strip()
                or event["dedupe_key"] != item["event_dedupe_key"]
            )
        except (ValueError, OperationsStoreError):
            invalid = True
        if invalid:
            database.execute(
                """UPDATE paper_webhook_outbox
                   SET status = 'dead', lease_owner = NULL, lease_token = NULL,
                       lease_expires_at = NULL,
                       last_error_code = 'payload_digest_mismatch'
                   WHERE id = %s::uuid AND lease_token = %s::uuid""",
                (str(item["id"]), token),
            )
            return None
        assert payload is not None
        item["payload"] = payload
        item.pop("canonical_event", None)
        item.pop("event_sha256", None)
        return item


def ack_webhook_item(
    outbox_id: str | UUID,
    *,
    worker_id: str,
    lease_token: str | UUID,
    expected_payload_sha256: str,
) -> dict[str, Any]:
    item_id = _uuid(outbox_id, field="outbox_id")
    worker = _text(worker_id, field="worker_id", maximum=200)
    token = _uuid(lease_token, field="lease_token")
    digest = _sha256(expected_payload_sha256, field="expected_payload_sha256")
    with connection() as database:
        row = database.execute(
            f"""UPDATE paper_webhook_outbox
                SET status = 'delivered', delivered_at = NOW(),
                    lease_owner = NULL, lease_token = NULL,
                    lease_expires_at = NULL, last_error_code = NULL
                WHERE id = %s::uuid AND status = 'leased'
                  AND lease_owner = %s AND lease_token = %s::uuid
                  AND lease_expires_at > NOW() AND payload_sha256 = %s
                RETURNING {WEBHOOK_COLUMNS}""",
            (item_id, worker, token, digest),
        ).fetchone()
        if row is None:
            raise OperationsLeaseLost("webhook_lease_lost")
        return dict(row)


def nack_webhook_item(
    outbox_id: str | UUID,
    *,
    worker_id: str,
    lease_token: str | UUID,
    error_code: str,
    retry_delay_seconds: int = 60,
) -> dict[str, Any]:
    item_id = _uuid(outbox_id, field="outbox_id")
    worker = _text(worker_id, field="worker_id", maximum=200)
    token = _uuid(lease_token, field="lease_token")
    error = _code(error_code)
    delay = _nonnegative_int(
        retry_delay_seconds, field="retry_delay_seconds", maximum=86_400
    )
    with connection() as database:
        row = database.execute(
            f"""UPDATE paper_webhook_outbox
                SET status = CASE WHEN attempt_count >= max_attempts
                                  THEN 'dead' ELSE 'pending' END,
                    available_at = NOW() + make_interval(secs => %s),
                    lease_owner = NULL, lease_token = NULL,
                    lease_expires_at = NULL, last_error_code = %s
                WHERE id = %s::uuid AND status = 'leased'
                  AND lease_owner = %s AND lease_token = %s::uuid
                  AND lease_expires_at > NOW()
                RETURNING {WEBHOOK_COLUMNS}""",
            (delay, error, item_id, worker, token),
        ).fetchone()
        if row is None:
            raise OperationsLeaseLost("webhook_lease_lost")
        return dict(row)


def _canonical_target_snapshot(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise _fail("operation_targets_invalid")
    if not 1 <= len(value) <= MAX_REPROCESS_TARGETS:
        raise _fail("operation_targets_invalid")
    output: list[dict[str, Any]] = []
    seen: set[int] = set()
    for raw in value:
        if not isinstance(raw, Mapping) or set(raw) != {
            "paper_id",
            "expected_source_sha256",
            "expected_current_version_id",
        }:
            raise _fail("operation_target_invalid")
        paper_id = _positive_int(raw["paper_id"], field="paper_id")
        if paper_id in seen:
            raise _fail("operation_target_duplicate")
        seen.add(paper_id)
        output.append(
            {
                "paper_id": paper_id,
                "expected_source_sha256": _sha256(
                    raw["expected_source_sha256"],
                    field="expected_source_sha256",
                ),
                "expected_current_version_id": _version(
                    raw["expected_current_version_id"],
                    field="expected_current_version_id",
                    nullable=True,
                ),
            }
        )
    return sorted(output, key=lambda item: item["paper_id"])


def _reprocess_request_id(idempotency_key: str) -> str:
    return str(uuid5(_REPROCESS_NAMESPACE, idempotency_key))


def _insert_verified_reprocess_target(
    database: Any, request_id: str, ordinal: int, target: Mapping[str, Any]
) -> None:
    row = database.execute(
        """INSERT INTO paper_reprocess_targets
               (request_id, ordinal, paper_id, expected_source_sha256,
                expected_current_version_id)
           SELECT %s::uuid, %s, p.id, %s, %s
           FROM papers p WHERE p.id = %s AND p.sha256 = %s
           RETURNING paper_id""",
        (
            request_id,
            ordinal,
            target["expected_source_sha256"],
            target["expected_current_version_id"],
            target["paper_id"],
            target["expected_source_sha256"],
        ),
    ).fetchone()
    if row is None:
        raise _fail("reprocess_target_source_stale_or_missing")


def _validated_review_evidence(
    row: Mapping[str, Any], *, kind: str
) -> str | None:
    revision = int(row["review_revision"])
    decision = str(row["decision"])
    event_id = row.get("approval_event_id")
    event_revision = row.get("event_review_revision")
    event_decision = row.get("event_decision")
    event_actor = row.get("event_actor_kind")
    if decision == "pending":
        if revision != 0 or any(
            value is not None
            for value in (event_id, event_revision, event_decision, event_actor)
        ):
            raise _fail(f"stored_{kind}_review_invalid")
        return None
    expected_event_decision = "approve" if decision == "approved" else "reject"
    if (
        event_id is None
        or isinstance(event_revision, bool)
        or not isinstance(event_revision, int)
        or event_revision != revision
        or event_decision != expected_event_decision
        or event_actor != "human"
    ):
        raise _fail(f"stored_{kind}_review_invalid")
    return _uuid(event_id, field="approval_event_id")


def create_reprocess_request(
    payload: Any,
    *,
    allow_global: bool = False,
    global_target_snapshot: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Store an immutable request; global scope needs both explicit gates.

    ``allow_global`` must be true, the core payload must contain its exact
    confirmation phrase, and a bounded source/version snapshot must be given.
    """

    canonical = validate_reprocess_request(payload, allow_global=allow_global)
    if canonical["scope"] == "all":
        if global_target_snapshot is None:
            raise _fail("global_reprocess_target_snapshot_required")
        targets = _canonical_target_snapshot(global_target_snapshot)
    else:
        if global_target_snapshot is not None:
            raise _fail("selective_reprocess_global_snapshot_forbidden")
        targets = _canonical_target_snapshot(canonical["targets"])
    fingerprint = reprocess_request_fingerprint(
        canonical, allow_global=canonical["scope"] == "all"
    )
    target_digest = _digest(targets)
    request_id = _reprocess_request_id(canonical["idempotency_key"])
    with connection() as database:
        row = database.execute(
            """INSERT INTO paper_reprocess_requests
                   (id, idempotency_key, request_fingerprint,
                    target_snapshot_sha256, scope, reason_code, requested_at,
                    global_confirmed, target_count, canonical_request)
               VALUES (%s::uuid, %s, %s, %s, %s, %s, %s::timestamptz,
                       %s, %s, %s::jsonb)
               ON CONFLICT (idempotency_key) DO NOTHING
               RETURNING id""",
            (
                request_id,
                canonical["idempotency_key"],
                fingerprint,
                target_digest,
                canonical["scope"],
                canonical["reason_code"],
                canonical["requested_at"],
                canonical["scope"] == "all",
                len(targets),
                _json(canonical),
            ),
        ).fetchone()
        if row is None:
            existing = _row(
                database.execute(
                    """SELECT id, request_fingerprint, target_snapshot_sha256,
                              target_count
                       FROM paper_reprocess_requests
                       WHERE idempotency_key = %s""",
                    (canonical["idempotency_key"],),
                ).fetchone(),
                missing_code="reprocess_request_missing_after_conflict",
            )
            if (
                str(existing["id"]) != request_id
                or str(existing["request_fingerprint"]).strip() != fingerprint
                or str(existing["target_snapshot_sha256"]).strip() != target_digest
                or int(existing["target_count"]) != len(targets)
            ):
                raise _fail("reprocess_request_idempotency_conflict")
            return get_reprocess_request(request_id, database=database)
        for ordinal, target in enumerate(targets):
            _insert_verified_reprocess_target(database, request_id, ordinal, target)
        database.execute(
            """INSERT INTO paper_reprocess_review_heads (request_id)
               VALUES (%s::uuid)""",
            (request_id,),
        )
        return {
            "request_id": request_id,
            "request": canonical,
            "targets": targets,
            "target_snapshot_sha256": target_digest,
            "review_revision": 0,
            "decision": "pending",
            "approval_event_id": None,
        }


def get_reprocess_request(
    request_id: str | UUID, *, database: Any | None = None
) -> dict[str, Any]:
    clean_id = _uuid(request_id, field="request_id")

    def read(active_database: Any) -> dict[str, Any]:
        request = _row(
            active_database.execute(
                """SELECT r.id, r.idempotency_key, r.request_fingerprint,
                          r.target_snapshot_sha256, r.scope, r.reason_code,
                          r.requested_at, r.global_confirmed, r.target_count,
                          r.canonical_request, h.review_revision, h.decision,
                          event.id AS approval_event_id,
                          event.review_revision AS event_review_revision,
                          event.decision AS event_decision,
                          event.actor_kind AS event_actor_kind
                   FROM paper_reprocess_requests r
                   JOIN paper_reprocess_review_heads h ON h.request_id = r.id
                   LEFT JOIN paper_reprocess_review_events event
                     ON event.request_id = r.id
                   WHERE r.id = %s::uuid""",
                (clean_id,),
            ).fetchone(),
            missing_code="reprocess_request_not_found",
        )
        target_rows = _rows(
            active_database.execute(
                """SELECT paper_id, expected_source_sha256,
                          expected_current_version_id
                   FROM paper_reprocess_targets WHERE request_id = %s::uuid
                   ORDER BY ordinal""",
                (clean_id,),
            ).fetchall()
        )
        targets = _canonical_target_snapshot(
            [
                {
                    "paper_id": int(item["paper_id"]),
                    "expected_source_sha256": str(
                        item["expected_source_sha256"]
                    ).strip(),
                    "expected_current_version_id": item[
                        "expected_current_version_id"
                    ],
                }
                for item in target_rows
            ]
        )
        canonical = _json_object(
            request["canonical_request"], field="reprocess_request"
        )
        canonical = validate_reprocess_request(
            canonical, allow_global=bool(request["global_confirmed"])
        )
        fingerprint = reprocess_request_fingerprint(
            canonical, allow_global=bool(request["global_confirmed"])
        )
        if (
            canonical["idempotency_key"] != request["idempotency_key"]
            or fingerprint != str(request["request_fingerprint"]).strip()
            or _digest(targets)
            != str(request["target_snapshot_sha256"]).strip()
            or len(targets) != int(request["target_count"])
            or (
                canonical["scope"] == "selected"
                and canonical["targets"] != targets
            )
        ):
            raise _fail("stored_reprocess_request_invalid")
        approval_event_id = _validated_review_evidence(
            request, kind="reprocess"
        )
        return {
            "request_id": clean_id,
            "request": canonical,
            "targets": targets,
            "target_snapshot_sha256": _digest(targets),
            "review_revision": int(request["review_revision"]),
            "decision": str(request["decision"]),
            "approval_event_id": approval_event_id,
        }

    if database is not None:
        return read(database)
    with connection() as active_database:
        return read(active_database)


def list_reprocess_target_states(
    request_id: str | UUID,
) -> tuple[dict[str, Any], ...]:
    """Return one opaque execution state per immutable request target."""

    clean_id = _uuid(request_id, field="request_id")
    with connection() as database:
        exists = database.execute(
            "SELECT 1 FROM paper_reprocess_requests WHERE id = %s::uuid",
            (clean_id,),
        ).fetchone()
        if exists is None:
            raise _fail("reprocess_request_not_found")
        rows = database.execute(
            """SELECT target.paper_id,
                      COALESCE(job.status, 'awaiting_approval') AS status,
                      COALESCE(job.attempt_count, 0) AS attempt_count,
                      job.last_error_code, job.result_version_id
               FROM paper_reprocess_targets target
               LEFT JOIN paper_reprocess_execution_jobs job
                 ON job.request_id = target.request_id
                AND job.paper_id = target.paper_id
               WHERE target.request_id = %s::uuid
               ORDER BY target.ordinal""",
            (clean_id,),
        ).fetchall()
    return tuple(
        {
            "paper_id": int(row["paper_id"]),
            "status": str(row["status"]),
            "attempt_count": int(row["attempt_count"]),
            "error_code": (
                str(row["last_error_code"]) if row["last_error_code"] else None
            ),
            "result_version_id": row["result_version_id"],
        }
        for row in rows
    )


def _review_request(
    *,
    request_id: str | UUID,
    expected_revision: int,
    decision: str,
    actor_id: str,
    comment: str | None,
    head_table: str,
    event_table: str,
    kind: str,
) -> dict[str, Any]:
    clean_id = _uuid(request_id, field="request_id")
    expected = _nonnegative_int(expected_revision, field="expected_revision")
    if decision not in {"approve", "reject"}:
        raise _fail("review_decision_invalid")
    actor = _text(actor_id, field="actor_id", maximum=200)
    clean_comment = _comment(comment)
    with connection() as database:
        head = _row(
            database.execute(
                f"""SELECT review_revision, decision FROM {head_table}
                    WHERE request_id = %s::uuid FOR UPDATE""",
                (clean_id,),
            ).fetchone(),
            missing_code=f"{kind}_request_not_found",
        )
        current_revision = int(head["review_revision"])
        current_decision = str(head["decision"])
        if current_revision != expected or current_decision != "pending":
            raise OperationsReviewConflict(current_revision, current_decision)
        review_revision = expected + 1
        event_id = str(
            uuid5(
                _REVIEW_NAMESPACE,
                f"{kind}:{clean_id}:{review_revision}:{decision}:{actor}",
            )
        )
        row = database.execute(
            f"""INSERT INTO {event_table}
                    (id, request_id, review_revision, expected_revision,
                     decision, actor_kind, actor_id, comment)
                VALUES (%s::uuid, %s::uuid, %s, %s, %s, 'human', %s, %s)
                RETURNING id, request_id, review_revision, expected_revision,
                          decision, actor_kind, actor_id, comment, created_at""",
            (
                event_id,
                clean_id,
                review_revision,
                expected,
                decision,
                actor,
                clean_comment,
            ),
        ).fetchone()
        return _row(row, missing_code="review_event_not_created")


def review_reprocess_request(
    request_id: str | UUID,
    *,
    expected_revision: int,
    decision: str,
    actor_id: str,
    comment: str | None = None,
) -> dict[str, Any]:
    return _review_request(
        request_id=request_id,
        expected_revision=expected_revision,
        decision=decision,
        actor_id=actor_id,
        comment=comment,
        head_table="paper_reprocess_review_heads",
        event_table="paper_reprocess_review_events",
        kind="reprocess",
    )


def enqueue_approved_reprocess(
    request_id: str | UUID, *, max_attempts: int = 3
) -> tuple[dict[str, Any], ...]:
    clean_id = _uuid(request_id, field="request_id")
    attempts = _positive_int(max_attempts, field="max_attempts", maximum=10)
    with connection() as database:
        request = get_reprocess_request(clean_id, database=database)
        if request["decision"] != "approved":
            raise OperationsApprovalRequired("reprocess_human_approval_required")
        approval_event_id = request["approval_event_id"]
        if approval_event_id is None:
            raise OperationsApprovalRequired("reprocess_human_approval_required")
        jobs: list[dict[str, Any]] = []
        for target in request["targets"]:
            job_id = str(
                uuid5(
                    _REPROCESS_JOB_NAMESPACE,
                    f"{clean_id}:{target['paper_id']}",
                )
            )
            row = database.execute(
                f"""INSERT INTO paper_reprocess_execution_jobs
                        (id, request_id, paper_id, approval_event_id,
                         approval_decision, approval_actor_kind, max_attempts)
                    VALUES (%s::uuid, %s::uuid, %s, %s::uuid,
                            'approve', 'human', %s)
                    ON CONFLICT (request_id, paper_id) DO NOTHING
                    RETURNING {REPROCESS_JOB_COLUMNS}""",
                (
                    job_id,
                    clean_id,
                    target["paper_id"],
                    approval_event_id,
                    attempts,
                ),
            ).fetchone()
            if row is None:
                row = database.execute(
                    f"""SELECT {REPROCESS_JOB_COLUMNS}
                        FROM paper_reprocess_execution_jobs
                        WHERE request_id = %s::uuid AND paper_id = %s""",
                    (clean_id, target["paper_id"]),
                ).fetchone()
            job = _row(row, missing_code="reprocess_job_missing_after_enqueue")
            if str(job["id"]) != job_id or int(job["max_attempts"]) != attempts:
                raise _fail("reprocess_job_idempotency_conflict")
            jobs.append(job)
        return tuple(jobs)


def _recover_expired_reprocess_jobs(database: Any) -> int:
    result = database.execute(
        """UPDATE paper_reprocess_execution_jobs
           SET status = CASE
                   WHEN attempt_count < max_attempts THEN 'retry_wait'
                   WHEN status = 'running' AND planned_result_version_id IS NOT NULL
                        AND attempt_count = max_attempts THEN 'retry_wait'
                   ELSE 'failed'
               END,
               available_at = NOW(), lease_owner = NULL, lease_token = NULL,
               lease_expires_at = NULL,
               last_error_code = CASE
                   WHEN status = 'running' AND planned_result_version_id IS NOT NULL
                        THEN 'lease_expired_after_publish_intent'
                   ELSE 'lease_expired'
               END,
               finished_at = CASE
                   WHEN attempt_count < max_attempts THEN NULL
                   WHEN status = 'running' AND planned_result_version_id IS NOT NULL
                        AND attempt_count = max_attempts THEN NULL
                   ELSE NOW()
               END
           WHERE status IN ('leased', 'running')
             AND lease_expires_at <= NOW()"""
    )
    return int(getattr(result, "rowcount", 0) or 0)


def claim_reprocess_job(
    *, worker_id: str, lease_seconds: int = 600
) -> dict[str, Any] | None:
    worker = _text(worker_id, field="worker_id", maximum=200)
    seconds = _positive_int(lease_seconds, field="lease_seconds", maximum=3_600)
    token = str(uuid4())
    with connection() as database:
        _recover_expired_reprocess_jobs(database)
        row = database.execute(
            f"""WITH candidate AS (
                    SELECT j.id
                    FROM paper_reprocess_execution_jobs j
                    JOIN paper_reprocess_review_heads h
                      ON h.request_id = j.request_id
                    JOIN paper_reprocess_review_events approval
                      ON approval.request_id = j.request_id
                     AND approval.id = j.approval_event_id
                    WHERE j.status IN ('queued', 'retry_wait')
                      AND j.available_at <= NOW()
                      AND (
                          j.attempt_count < j.max_attempts
                          OR (j.attempt_count = j.max_attempts
                              AND j.last_error_code
                                  = 'lease_expired_after_publish_intent')
                      )
                      AND h.decision = 'approved'
                      AND approval.decision = 'approve'
                      AND approval.actor_kind = 'human'
                      AND approval.review_revision = h.review_revision
                    ORDER BY j.available_at, j.created_at, j.id
                    FOR UPDATE OF j SKIP LOCKED LIMIT 1
                )
                UPDATE paper_reprocess_execution_jobs AS job
                SET status = 'leased', attempt_count = attempt_count + 1,
                    started_at = COALESCE(started_at, NOW()),
                    lease_owner = %s, lease_token = %s::uuid,
                    lease_expires_at = NOW() + make_interval(secs => %s),
                    last_error_code = NULL
                FROM candidate WHERE job.id = candidate.id
                RETURNING {REPROCESS_JOB_COLUMNS}""",
            (worker, token, seconds),
        ).fetchone()
        return dict(row) if row else None


def _reprocess_job_locked(
    database: Any, job_id: str, worker: str, token: str
) -> dict[str, Any]:
    row = database.execute(
        f"""SELECT {
            ', '.join('j.' + part.strip() for part in REPROCESS_JOB_COLUMNS.split(','))
        }, t.expected_source_sha256, t.expected_current_version_id,
           p.sha256 AS current_source_sha256,
           (j.lease_expires_at > NOW()) AS lease_valid
        FROM paper_reprocess_execution_jobs j
        JOIN paper_reprocess_targets t
          ON t.request_id = j.request_id AND t.paper_id = j.paper_id
        JOIN papers p ON p.id = j.paper_id
        WHERE j.id = %s::uuid FOR UPDATE OF j, p""",
        (job_id,),
    ).fetchone()
    job = _row(row, missing_code="reprocess_job_not_found")
    if (
        job["status"] not in {"leased", "running"}
        or not bool(job["lease_valid"])
        or str(job["lease_owner"]) != worker
        or str(job["lease_token"]) != token
    ):
        raise OperationsLeaseLost("reprocess_job_lease_lost")
    return job


def _fail_reprocess_locked(
    database: Any, job_id: str, error_code: str
) -> dict[str, Any]:
    row = database.execute(
        f"""UPDATE paper_reprocess_execution_jobs
            SET status = 'failed', finished_at = NOW(), result_version_id = NULL,
                lease_owner = NULL, lease_token = NULL,
                lease_expires_at = NULL, last_error_code = %s
            WHERE id = %s::uuid RETURNING {REPROCESS_JOB_COLUMNS}""",
        (error_code, job_id),
    ).fetchone()
    return _row(row, missing_code="reprocess_job_not_found")


def authorize_reprocess_job(
    job_id: str | UUID,
    *,
    worker_id: str,
    lease_token: str | UUID,
    actual_source_sha256: str,
    actual_current_version_id: str | None,
) -> dict[str, Any]:
    """Fail closed on stale source/version before any external mutation.

    If a prior attempt persisted a planned version and the pointer already
    equals it, this call records crash recovery as success without republishing.
    """

    clean_id = _uuid(job_id, field="job_id")
    worker = _text(worker_id, field="worker_id", maximum=200)
    token = _uuid(lease_token, field="lease_token")
    source_sha = _sha256(actual_source_sha256, field="actual_source_sha256")
    current_version = _version(
        actual_current_version_id,
        field="actual_current_version_id",
        nullable=True,
    )
    with connection() as database:
        job = _reprocess_job_locked(database, clean_id, worker, token)
        expected_sha = str(job["expected_source_sha256"]).strip()
        database_sha = str(job["current_source_sha256"]).strip()
        expected_version = job["expected_current_version_id"]
        planned = job["planned_result_version_id"]
        if database_sha != expected_sha or source_sha != expected_sha:
            return _fail_reprocess_locked(
                database, clean_id, "reprocess_source_sha256_stale"
            )
        if planned is not None and current_version == planned:
            row = database.execute(
                f"""UPDATE paper_reprocess_execution_jobs
                    SET status = 'succeeded', result_version_id = %s,
                        finished_at = NOW(), lease_owner = NULL,
                        lease_token = NULL, lease_expires_at = NULL,
                        last_error_code = 'recovered_after_publish'
                    WHERE id = %s::uuid RETURNING {REPROCESS_JOB_COLUMNS}""",
                (planned, clean_id),
            ).fetchone()
            return _row(row, missing_code="reprocess_job_not_found")
        if current_version != expected_version:
            return _fail_reprocess_locked(
                database, clean_id, "reprocess_current_version_stale"
            )
        row = database.execute(
            f"""UPDATE paper_reprocess_execution_jobs
                SET status = 'running', last_error_code = NULL
                WHERE id = %s::uuid RETURNING {REPROCESS_JOB_COLUMNS}""",
            (clean_id,),
        ).fetchone()
        return _row(row, missing_code="reprocess_job_not_found")


def bind_reprocess_result_version(
    job_id: str | UUID,
    *,
    worker_id: str,
    lease_token: str | UUID,
    planned_result_version_id: str,
) -> dict[str, Any]:
    """Persist output identity before the caller atomically publishes it."""

    clean_id = _uuid(job_id, field="job_id")
    worker = _text(worker_id, field="worker_id", maximum=200)
    token = _uuid(lease_token, field="lease_token")
    planned = _version(
        planned_result_version_id, field="planned_result_version_id"
    )
    with connection() as database:
        job = _reprocess_job_locked(database, clean_id, worker, token)
        if job["status"] != "running":
            raise OperationsLeaseLost("reprocess_job_not_running")
        existing = job["planned_result_version_id"]
        if existing is not None and existing != planned:
            raise _fail("reprocess_result_version_conflict")
        row = database.execute(
            f"""UPDATE paper_reprocess_execution_jobs
                SET planned_result_version_id = COALESCE(
                        planned_result_version_id, %s)
                WHERE id = %s::uuid RETURNING {REPROCESS_JOB_COLUMNS}""",
            (planned, clean_id),
        ).fetchone()
        return _row(row, missing_code="reprocess_job_not_found")


def complete_reprocess_job(
    job_id: str | UUID,
    *,
    worker_id: str,
    lease_token: str | UUID,
    actual_current_version_id: str,
) -> dict[str, Any]:
    clean_id = _uuid(job_id, field="job_id")
    worker = _text(worker_id, field="worker_id", maximum=200)
    token = _uuid(lease_token, field="lease_token")
    actual = _version(actual_current_version_id, field="actual_current_version_id")
    with connection() as database:
        existing = _row(
            database.execute(
                f"""SELECT {REPROCESS_JOB_COLUMNS},
                           (lease_expires_at > NOW()) AS lease_valid
                    FROM paper_reprocess_execution_jobs
                    WHERE id = %s::uuid FOR UPDATE""",
                (clean_id,),
            ).fetchone(),
            missing_code="reprocess_job_not_found",
        )
        if existing["status"] == "succeeded":
            if existing["result_version_id"] != actual:
                raise _fail("reprocess_result_version_conflict")
            return existing
        if (
            existing["status"] != "running"
            or not bool(existing["lease_valid"])
            or str(existing["lease_owner"]) != worker
            or str(existing["lease_token"]) != token
        ):
            raise OperationsLeaseLost("reprocess_job_lease_lost")
        if existing["planned_result_version_id"] != actual:
            return _fail_reprocess_locked(
                database, clean_id, "reprocess_result_version_not_current"
            )
        row = database.execute(
            f"""UPDATE paper_reprocess_execution_jobs
                SET status = 'succeeded', result_version_id = %s,
                    finished_at = NOW(), lease_owner = NULL,
                    lease_token = NULL, lease_expires_at = NULL,
                    last_error_code = NULL
                WHERE id = %s::uuid AND lease_expires_at > NOW()
                RETURNING {REPROCESS_JOB_COLUMNS}""",
            (actual, clean_id),
        ).fetchone()
        if row is None:
            raise OperationsLeaseLost("reprocess_job_lease_lost")
        return dict(row)


def nack_reprocess_job(
    job_id: str | UUID,
    *,
    worker_id: str,
    lease_token: str | UUID,
    error_code: str,
    retry_delay_seconds: int = 60,
) -> dict[str, Any]:
    clean_id = _uuid(job_id, field="job_id")
    worker = _text(worker_id, field="worker_id", maximum=200)
    token = _uuid(lease_token, field="lease_token")
    error = _code(error_code)
    delay = _nonnegative_int(
        retry_delay_seconds, field="retry_delay_seconds", maximum=86_400
    )
    with connection() as database:
        row = database.execute(
            f"""UPDATE paper_reprocess_execution_jobs
                SET status = CASE WHEN attempt_count >= max_attempts
                                  THEN 'failed' ELSE 'retry_wait' END,
                    available_at = NOW() + make_interval(secs => %s),
                    finished_at = CASE WHEN attempt_count >= max_attempts
                                       THEN NOW() ELSE NULL END,
                    lease_owner = NULL, lease_token = NULL,
                    lease_expires_at = NULL, last_error_code = %s
                WHERE id = %s::uuid AND status IN ('leased', 'running')
                  AND lease_owner = %s AND lease_token = %s::uuid
                  AND lease_expires_at > NOW()
                RETURNING {REPROCESS_JOB_COLUMNS}""",
            (delay, error, clean_id, worker, token),
        ).fetchone()
        if row is None:
            raise OperationsLeaseLost("reprocess_job_lease_lost")
        return dict(row)


def _canonical_rollback_targets(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise _fail("rollback_targets_invalid")
    if not 1 <= len(value) <= MAX_REPROCESS_TARGETS:
        raise _fail("rollback_targets_invalid")
    output: list[dict[str, Any]] = []
    seen: set[int] = set()
    required = {
        "paper_id",
        "expected_source_sha256",
        "expected_current_version_id",
        "rollback_to_version_id",
    }
    for raw in value:
        if not isinstance(raw, Mapping) or set(raw) != required:
            raise _fail("rollback_target_invalid")
        paper_id = _positive_int(raw["paper_id"], field="paper_id")
        if paper_id in seen:
            raise _fail("rollback_target_duplicate")
        seen.add(paper_id)
        current = _version(
            raw["expected_current_version_id"],
            field="expected_current_version_id",
        )
        destination = _version(
            raw["rollback_to_version_id"], field="rollback_to_version_id"
        )
        if current == destination:
            raise _fail("rollback_versions_must_differ")
        output.append(
            {
                "paper_id": paper_id,
                "expected_source_sha256": _sha256(
                    raw["expected_source_sha256"],
                    field="expected_source_sha256",
                ),
                "expected_current_version_id": current,
                "rollback_to_version_id": destination,
            }
        )
    return sorted(output, key=lambda item: item["paper_id"])


def _rollback_request(
    *, targets: Sequence[Mapping[str, Any]], reason_code: str, requested_at: str
) -> dict[str, Any]:
    canonical_targets = _canonical_rollback_targets(targets)
    reason = _code(reason_code, field="reason_code")
    requested = _timestamp(requested_at, field="requested_at")
    identity = {
        "schema_version": 1,
        "reason_code": reason,
        "requested_at": requested,
        "targets": canonical_targets,
    }
    key = "rb-" + _digest(identity)[:32]
    return {**identity, "idempotency_key": key}


def create_rollback_request(
    *,
    targets: Sequence[Mapping[str, Any]],
    reason_code: str,
    requested_at: str,
) -> dict[str, Any]:
    """Create an immutable rollback proposal; this never moves a pointer."""

    canonical = _rollback_request(
        targets=targets, reason_code=reason_code, requested_at=requested_at
    )
    fingerprint = _digest(canonical)
    request_id = str(uuid5(_ROLLBACK_NAMESPACE, canonical["idempotency_key"]))
    with connection() as database:
        row = database.execute(
            """INSERT INTO paper_rollback_requests
                   (id, idempotency_key, request_fingerprint, reason_code,
                    requested_at, target_count, canonical_request)
               VALUES (%s::uuid, %s, %s, %s, %s::timestamptz, %s, %s::jsonb)
               ON CONFLICT (idempotency_key) DO NOTHING RETURNING id""",
            (
                request_id,
                canonical["idempotency_key"],
                fingerprint,
                canonical["reason_code"],
                canonical["requested_at"],
                len(canonical["targets"]),
                _json(canonical),
            ),
        ).fetchone()
        if row is None:
            existing = _row(
                database.execute(
                    """SELECT id, request_fingerprint, target_count
                       FROM paper_rollback_requests
                       WHERE idempotency_key = %s""",
                    (canonical["idempotency_key"],),
                ).fetchone(),
                missing_code="rollback_request_missing_after_conflict",
            )
            if (
                str(existing["id"]) != request_id
                or str(existing["request_fingerprint"]).strip() != fingerprint
                or int(existing["target_count"]) != len(canonical["targets"])
            ):
                raise _fail("rollback_request_idempotency_conflict")
            return get_rollback_request(request_id, database=database)
        for ordinal, target in enumerate(canonical["targets"]):
            inserted = database.execute(
                """INSERT INTO paper_rollback_targets
                       (request_id, ordinal, paper_id, expected_source_sha256,
                        expected_current_version_id, rollback_to_version_id)
                   SELECT %s::uuid, %s, p.id, %s, %s, %s
                   FROM papers p WHERE p.id = %s AND p.sha256 = %s
                   RETURNING paper_id""",
                (
                    request_id,
                    ordinal,
                    target["expected_source_sha256"],
                    target["expected_current_version_id"],
                    target["rollback_to_version_id"],
                    target["paper_id"],
                    target["expected_source_sha256"],
                ),
            ).fetchone()
            if inserted is None:
                raise _fail("rollback_target_source_stale_or_missing")
        database.execute(
            """INSERT INTO paper_rollback_review_heads (request_id)
               VALUES (%s::uuid)""",
            (request_id,),
        )
        return {
            "request_id": request_id,
            "request": canonical,
            "review_revision": 0,
            "decision": "pending",
            "approval_event_id": None,
        }


def get_rollback_request(
    request_id: str | UUID, *, database: Any | None = None
) -> dict[str, Any]:
    clean_id = _uuid(request_id, field="request_id")

    def read(active_database: Any) -> dict[str, Any]:
        row = _row(
            active_database.execute(
                """SELECT r.id, r.idempotency_key, r.request_fingerprint,
                          r.reason_code, r.requested_at, r.target_count,
                          r.canonical_request, h.review_revision, h.decision,
                          event.id AS approval_event_id,
                          event.review_revision AS event_review_revision,
                          event.decision AS event_decision,
                          event.actor_kind AS event_actor_kind
                   FROM paper_rollback_requests r
                   JOIN paper_rollback_review_heads h ON h.request_id = r.id
                   LEFT JOIN paper_rollback_review_events event
                     ON event.request_id = r.id
                   WHERE r.id = %s::uuid""",
                (clean_id,),
            ).fetchone(),
            missing_code="rollback_request_not_found",
        )
        canonical = _json_object(
            row["canonical_request"], field="rollback_request"
        )
        expected = _rollback_request(
            targets=canonical.get("targets"),
            reason_code=canonical.get("reason_code"),
            requested_at=canonical.get("requested_at"),
        )
        if (
            set(canonical) != set(expected)
            or canonical != expected
            or canonical["idempotency_key"] != row["idempotency_key"]
            or _digest(canonical) != str(row["request_fingerprint"]).strip()
            or len(canonical["targets"]) != int(row["target_count"])
        ):
            raise _fail("stored_rollback_request_invalid")
        targets = _rows(
            active_database.execute(
                """SELECT paper_id, expected_source_sha256,
                          expected_current_version_id, rollback_to_version_id
                   FROM paper_rollback_targets WHERE request_id = %s::uuid
                   ORDER BY ordinal""",
                (clean_id,),
            ).fetchall()
        )
        stored_targets = _canonical_rollback_targets(
            [
                {
                    "paper_id": int(item["paper_id"]),
                    "expected_source_sha256": str(
                        item["expected_source_sha256"]
                    ).strip(),
                    "expected_current_version_id": item[
                        "expected_current_version_id"
                    ],
                    "rollback_to_version_id": item["rollback_to_version_id"],
                }
                for item in targets
            ]
        )
        if stored_targets != canonical["targets"]:
            raise _fail("stored_rollback_targets_invalid")
        approval_event_id = _validated_review_evidence(row, kind="rollback")
        return {
            "request_id": clean_id,
            "request": canonical,
            "review_revision": int(row["review_revision"]),
            "decision": str(row["decision"]),
            "approval_event_id": approval_event_id,
        }

    if database is not None:
        return read(database)
    with connection() as active_database:
        return read(active_database)


def list_rollback_target_states(
    request_id: str | UUID,
) -> tuple[dict[str, Any], ...]:
    """Return one opaque execution state per immutable rollback target."""

    clean_id = _uuid(request_id, field="request_id")
    with connection() as database:
        exists = database.execute(
            "SELECT 1 FROM paper_rollback_requests WHERE id = %s::uuid",
            (clean_id,),
        ).fetchone()
        if exists is None:
            raise _fail("rollback_request_not_found")
        rows = database.execute(
            """SELECT target.paper_id,
                      COALESCE(job.status, 'awaiting_approval') AS status,
                      COALESCE(job.attempt_count, 0) AS attempt_count,
                      job.last_error_code, job.result_version_id
               FROM paper_rollback_targets target
               LEFT JOIN paper_rollback_execution_jobs job
                 ON job.request_id = target.request_id
                AND job.paper_id = target.paper_id
               WHERE target.request_id = %s::uuid
               ORDER BY target.ordinal""",
            (clean_id,),
        ).fetchall()
    return tuple(
        {
            "paper_id": int(row["paper_id"]),
            "status": str(row["status"]),
            "attempt_count": int(row["attempt_count"]),
            "error_code": (
                str(row["last_error_code"]) if row["last_error_code"] else None
            ),
            "result_version_id": row["result_version_id"],
        }
        for row in rows
    )


def review_rollback_request(
    request_id: str | UUID,
    *,
    expected_revision: int,
    decision: str,
    actor_id: str,
    comment: str | None = None,
) -> dict[str, Any]:
    return _review_request(
        request_id=request_id,
        expected_revision=expected_revision,
        decision=decision,
        actor_id=actor_id,
        comment=comment,
        head_table="paper_rollback_review_heads",
        event_table="paper_rollback_review_events",
        kind="rollback",
    )


def enqueue_approved_rollback(
    request_id: str | UUID, *, max_attempts: int = 3
) -> tuple[dict[str, Any], ...]:
    clean_id = _uuid(request_id, field="request_id")
    attempts = _positive_int(max_attempts, field="max_attempts", maximum=10)
    with connection() as database:
        request = get_rollback_request(clean_id, database=database)
        if request["decision"] != "approved":
            raise OperationsApprovalRequired("rollback_human_approval_required")
        approval_event_id = request["approval_event_id"]
        if approval_event_id is None:
            raise OperationsApprovalRequired("rollback_human_approval_required")
        jobs: list[dict[str, Any]] = []
        for target in request["request"]["targets"]:
            job_id = str(
                uuid5(
                    _ROLLBACK_JOB_NAMESPACE,
                    f"{clean_id}:{target['paper_id']}",
                )
            )
            row = database.execute(
                f"""INSERT INTO paper_rollback_execution_jobs
                        (id, request_id, paper_id, approval_event_id,
                         approval_decision, approval_actor_kind, max_attempts)
                    VALUES (%s::uuid, %s::uuid, %s, %s::uuid,
                            'approve', 'human', %s)
                    ON CONFLICT (request_id, paper_id) DO NOTHING
                    RETURNING {ROLLBACK_JOB_COLUMNS}""",
                (
                    job_id,
                    clean_id,
                    target["paper_id"],
                    approval_event_id,
                    attempts,
                ),
            ).fetchone()
            if row is None:
                row = database.execute(
                    f"""SELECT {ROLLBACK_JOB_COLUMNS}
                        FROM paper_rollback_execution_jobs
                        WHERE request_id = %s::uuid AND paper_id = %s""",
                    (clean_id, target["paper_id"]),
                ).fetchone()
            job = _row(row, missing_code="rollback_job_missing_after_enqueue")
            if str(job["id"]) != job_id or int(job["max_attempts"]) != attempts:
                raise _fail("rollback_job_idempotency_conflict")
            jobs.append(job)
        return tuple(jobs)


def _recover_expired_rollback_jobs(database: Any) -> int:
    result = database.execute(
        """UPDATE paper_rollback_execution_jobs
           SET status = CASE
                   WHEN attempt_count < max_attempts THEN 'retry_wait'
                   WHEN status = 'running' AND attempt_count = max_attempts
                        THEN 'retry_wait'
                   ELSE 'failed'
               END,
               available_at = NOW(), lease_owner = NULL, lease_token = NULL,
               lease_expires_at = NULL,
               last_error_code = CASE WHEN status = 'running'
                   THEN 'lease_expired_after_rollback_start'
                   ELSE 'lease_expired' END,
               finished_at = CASE
                   WHEN attempt_count < max_attempts THEN NULL
                   WHEN status = 'running' AND attempt_count = max_attempts
                        THEN NULL
                   ELSE NOW()
               END
           WHERE status IN ('leased', 'running')
             AND lease_expires_at <= NOW()"""
    )
    return int(getattr(result, "rowcount", 0) or 0)


def claim_rollback_job(
    *, worker_id: str, lease_seconds: int = 300
) -> dict[str, Any] | None:
    worker = _text(worker_id, field="worker_id", maximum=200)
    seconds = _positive_int(lease_seconds, field="lease_seconds", maximum=3_600)
    token = str(uuid4())
    with connection() as database:
        _recover_expired_rollback_jobs(database)
        row = database.execute(
            f"""WITH candidate AS (
                    SELECT j.id
                    FROM paper_rollback_execution_jobs j
                    JOIN paper_rollback_review_heads h
                      ON h.request_id = j.request_id
                    JOIN paper_rollback_review_events approval
                      ON approval.request_id = j.request_id
                     AND approval.id = j.approval_event_id
                    WHERE j.status IN ('queued', 'retry_wait')
                      AND j.available_at <= NOW()
                      AND (
                          j.attempt_count < j.max_attempts
                          OR (j.attempt_count = j.max_attempts
                              AND j.last_error_code
                                  = 'lease_expired_after_rollback_start')
                      )
                      AND h.decision = 'approved'
                      AND approval.decision = 'approve'
                      AND approval.actor_kind = 'human'
                      AND approval.review_revision = h.review_revision
                    ORDER BY j.available_at, j.created_at, j.id
                    FOR UPDATE OF j SKIP LOCKED LIMIT 1
                )
                UPDATE paper_rollback_execution_jobs AS job
                SET status = 'leased', attempt_count = attempt_count + 1,
                    started_at = COALESCE(started_at, NOW()),
                    lease_owner = %s, lease_token = %s::uuid,
                    lease_expires_at = NOW() + make_interval(secs => %s),
                    last_error_code = NULL
                FROM candidate WHERE job.id = candidate.id
                RETURNING {ROLLBACK_JOB_COLUMNS}""",
            (worker, token, seconds),
        ).fetchone()
        return dict(row) if row else None


def _rollback_job_locked(
    database: Any, job_id: str, worker: str, token: str
) -> dict[str, Any]:
    row = database.execute(
        f"""SELECT {
            ', '.join('j.' + part.strip() for part in ROLLBACK_JOB_COLUMNS.split(','))
        }, t.expected_source_sha256, t.expected_current_version_id,
           t.rollback_to_version_id, p.sha256 AS current_source_sha256,
           (j.lease_expires_at > NOW()) AS lease_valid
        FROM paper_rollback_execution_jobs j
        JOIN paper_rollback_targets t
          ON t.request_id = j.request_id AND t.paper_id = j.paper_id
        JOIN papers p ON p.id = j.paper_id
        WHERE j.id = %s::uuid FOR UPDATE OF j, p""",
        (job_id,),
    ).fetchone()
    job = _row(row, missing_code="rollback_job_not_found")
    if (
        job["status"] not in {"leased", "running"}
        or not bool(job["lease_valid"])
        or str(job["lease_owner"]) != worker
        or str(job["lease_token"]) != token
    ):
        raise OperationsLeaseLost("rollback_job_lease_lost")
    return job


def _fail_rollback_locked(
    database: Any, job_id: str, error_code: str
) -> dict[str, Any]:
    row = database.execute(
        f"""UPDATE paper_rollback_execution_jobs
            SET status = 'failed', finished_at = NOW(), result_version_id = NULL,
                lease_owner = NULL, lease_token = NULL,
                lease_expires_at = NULL, last_error_code = %s
            WHERE id = %s::uuid RETURNING {ROLLBACK_JOB_COLUMNS}""",
        (error_code, job_id),
    ).fetchone()
    return _row(row, missing_code="rollback_job_not_found")


def authorize_rollback_job(
    job_id: str | UUID,
    *,
    worker_id: str,
    lease_token: str | UUID,
    actual_source_sha256: str,
    actual_current_version_id: str,
) -> dict[str, Any]:
    """Authorize a rollback only if the approved source/pointer is current."""

    clean_id = _uuid(job_id, field="job_id")
    worker = _text(worker_id, field="worker_id", maximum=200)
    token = _uuid(lease_token, field="lease_token")
    source_sha = _sha256(actual_source_sha256, field="actual_source_sha256")
    current = _version(
        actual_current_version_id, field="actual_current_version_id"
    )
    with connection() as database:
        job = _rollback_job_locked(database, clean_id, worker, token)
        expected_sha = str(job["expected_source_sha256"]).strip()
        if (
            str(job["current_source_sha256"]).strip() != expected_sha
            or source_sha != expected_sha
        ):
            return _fail_rollback_locked(
                database, clean_id, "rollback_source_sha256_stale"
            )
        if current == job["rollback_to_version_id"]:
            row = database.execute(
                f"""UPDATE paper_rollback_execution_jobs
                    SET status = 'succeeded', result_version_id = %s,
                        finished_at = NOW(), lease_owner = NULL,
                        lease_token = NULL, lease_expires_at = NULL,
                        last_error_code = 'recovered_after_rollback'
                    WHERE id = %s::uuid RETURNING {ROLLBACK_JOB_COLUMNS}""",
                (current, clean_id),
            ).fetchone()
            return _row(row, missing_code="rollback_job_not_found")
        if current != job["expected_current_version_id"]:
            return _fail_rollback_locked(
                database, clean_id, "rollback_current_version_stale"
            )
        row = database.execute(
            f"""UPDATE paper_rollback_execution_jobs
                SET status = 'running', last_error_code = NULL
                WHERE id = %s::uuid RETURNING {ROLLBACK_JOB_COLUMNS}""",
            (clean_id,),
        ).fetchone()
        return _row(row, missing_code="rollback_job_not_found")


def complete_rollback_job(
    job_id: str | UUID,
    *,
    worker_id: str,
    lease_token: str | UUID,
    actual_current_version_id: str,
) -> dict[str, Any]:
    clean_id = _uuid(job_id, field="job_id")
    worker = _text(worker_id, field="worker_id", maximum=200)
    token = _uuid(lease_token, field="lease_token")
    actual = _version(actual_current_version_id, field="actual_current_version_id")
    with connection() as database:
        row = database.execute(
            f"""SELECT {
                ', '.join('j.' + part.strip() for part in ROLLBACK_JOB_COLUMNS.split(','))
            }, t.rollback_to_version_id,
               (j.lease_expires_at > NOW()) AS lease_valid
            FROM paper_rollback_execution_jobs j
            JOIN paper_rollback_targets t
              ON t.request_id = j.request_id AND t.paper_id = j.paper_id
            WHERE j.id = %s::uuid FOR UPDATE OF j""",
            (clean_id,),
        ).fetchone()
        job = _row(row, missing_code="rollback_job_not_found")
        if job["status"] == "succeeded":
            if job["result_version_id"] != actual:
                raise _fail("rollback_result_version_conflict")
            return job
        if (
            job["status"] != "running"
            or not bool(job["lease_valid"])
            or str(job["lease_owner"]) != worker
            or str(job["lease_token"]) != token
        ):
            raise OperationsLeaseLost("rollback_job_lease_lost")
        if actual != job["rollback_to_version_id"]:
            return _fail_rollback_locked(
                database, clean_id, "rollback_result_version_not_current"
            )
        row = database.execute(
            f"""UPDATE paper_rollback_execution_jobs
                SET status = 'succeeded', result_version_id = %s,
                    finished_at = NOW(), lease_owner = NULL,
                    lease_token = NULL, lease_expires_at = NULL,
                    last_error_code = NULL
                WHERE id = %s::uuid RETURNING {ROLLBACK_JOB_COLUMNS}""",
            (actual, clean_id),
        ).fetchone()
        return _row(row, missing_code="rollback_job_not_found")


def nack_rollback_job(
    job_id: str | UUID,
    *,
    worker_id: str,
    lease_token: str | UUID,
    error_code: str,
    retry_delay_seconds: int = 60,
) -> dict[str, Any]:
    clean_id = _uuid(job_id, field="job_id")
    worker = _text(worker_id, field="worker_id", maximum=200)
    token = _uuid(lease_token, field="lease_token")
    error = _code(error_code)
    delay = _nonnegative_int(
        retry_delay_seconds, field="retry_delay_seconds", maximum=86_400
    )
    with connection() as database:
        row = database.execute(
            f"""UPDATE paper_rollback_execution_jobs
                SET status = CASE WHEN attempt_count >= max_attempts
                                  THEN 'failed' ELSE 'retry_wait' END,
                    available_at = NOW() + make_interval(secs => %s),
                    finished_at = CASE WHEN attempt_count >= max_attempts
                                       THEN NOW() ELSE NULL END,
                    lease_owner = NULL, lease_token = NULL,
                    lease_expires_at = NULL, last_error_code = %s
                WHERE id = %s::uuid AND status IN ('leased', 'running')
                  AND lease_owner = %s AND lease_token = %s::uuid
                  AND lease_expires_at > NOW()
                RETURNING {ROLLBACK_JOB_COLUMNS}""",
            (delay, error, clean_id, worker, token),
        ).fetchone()
        if row is None:
            raise OperationsLeaseLost("rollback_job_lease_lost")
        return dict(row)


def _operation_request_summary(row: Any) -> dict[str, Any]:
    item = _row(row, missing_code="operation_request_summary_missing")
    kind = str(item.get("request_kind"))
    if kind not in {"reprocess", "rollback"}:
        raise _fail("operation_request_kind_invalid")
    decision = str(item.get("decision"))
    if decision not in {"pending", "approved", "rejected"}:
        raise _fail("operation_request_decision_invalid")
    scope = str(item.get("scope"))
    if scope not in {"selected", "all", "rollback"}:
        raise _fail("operation_request_scope_invalid")
    target_count = _positive_int(
        int(item.get("target_count")),
        field="target_count",
        maximum=MAX_REPROCESS_TARGETS,
    )
    review_revision = _nonnegative_int(
        int(item.get("review_revision")), field="review_revision"
    )
    counts = {
        name: _nonnegative_int(int(item.get(name, 0)), field=name)
        for name in (
            "job_count",
            "active_job_count",
            "succeeded_job_count",
            "failed_job_count",
        )
    }
    if sum(
        counts[name]
        for name in (
            "active_job_count",
            "succeeded_job_count",
            "failed_job_count",
        )
    ) > counts["job_count"]:
        raise _fail("operation_request_job_counts_invalid")
    if decision == "pending":
        state = "pending_review"
    elif decision == "rejected":
        state = "rejected"
    elif counts["failed_job_count"]:
        state = "failed"
    elif counts["active_job_count"]:
        state = "active"
    elif counts["job_count"] and counts["succeeded_job_count"] == counts["job_count"]:
        state = "succeeded"
    else:
        state = "approved_not_enqueued"
    return {
        "request_kind": kind,
        "request_id": _uuid(item.get("request_id"), field="request_id"),
        "scope": scope,
        "reason_code": _code(item.get("reason_code"), field="reason_code"),
        "requested_at": item.get("requested_at"),
        "target_count": target_count,
        "review_revision": review_revision,
        "decision": decision,
        **counts,
        "state": state,
    }


def list_pending_operation_reviews(
    *, limit: int = 50
) -> tuple[dict[str, Any], ...]:
    """List bounded human review work without paper content or storage paths."""

    maximum = _positive_int(limit, field="limit", maximum=200)
    with connection() as database:
        rows = database.execute(
            """SELECT request_kind, request_id, scope, reason_code,
                      requested_at, target_count, review_revision, decision,
                      0::bigint AS job_count, 0::bigint AS active_job_count,
                      0::bigint AS succeeded_job_count,
                      0::bigint AS failed_job_count
               FROM (
                   SELECT 'reprocess'::text AS request_kind,
                          request.id::text AS request_id, request.scope,
                          request.reason_code, request.requested_at,
                          request.target_count, head.review_revision, head.decision
                   FROM paper_reprocess_requests request
                   JOIN paper_reprocess_review_heads head
                     ON head.request_id = request.id
                   WHERE head.decision = 'pending'
                   UNION ALL
                   SELECT 'rollback', request.id::text, 'rollback'::text,
                          request.reason_code, request.requested_at,
                          request.target_count, head.review_revision, head.decision
                   FROM paper_rollback_requests request
                   JOIN paper_rollback_review_heads head
                     ON head.request_id = request.id
                   WHERE head.decision = 'pending'
               ) pending
               ORDER BY requested_at, request_kind, request_id
               LIMIT %s""",
            (maximum,),
        ).fetchall()
    return tuple(_operation_request_summary(row) for row in rows)


def list_recent_operation_requests(
    *, limit: int = 50
) -> tuple[dict[str, Any], ...]:
    """List recent approval/execution state using an allowlisted projection."""

    maximum = _positive_int(limit, field="limit", maximum=200)
    with connection() as database:
        rows = database.execute(
            """SELECT request_kind, request_id, scope, reason_code,
                      requested_at, target_count, review_revision, decision,
                      job_count, active_job_count, succeeded_job_count,
                      failed_job_count
               FROM (
                   SELECT 'reprocess'::text AS request_kind,
                          request.id::text AS request_id, request.scope,
                          request.reason_code, request.requested_at,
                          request.target_count, head.review_revision, head.decision,
                          COUNT(job.id) AS job_count,
                          COUNT(job.id) FILTER (WHERE job.status IN
                              ('queued', 'retry_wait', 'leased', 'running'))
                              AS active_job_count,
                          COUNT(job.id) FILTER (WHERE job.status = 'succeeded')
                              AS succeeded_job_count,
                          COUNT(job.id) FILTER (WHERE job.status = 'failed')
                              AS failed_job_count
                   FROM paper_reprocess_requests request
                   JOIN paper_reprocess_review_heads head
                     ON head.request_id = request.id
                   LEFT JOIN paper_reprocess_execution_jobs job
                     ON job.request_id = request.id
                   GROUP BY request.id, head.review_revision, head.decision
                   UNION ALL
                   SELECT 'rollback', request.id::text, 'rollback'::text,
                          request.reason_code, request.requested_at,
                          request.target_count, head.review_revision, head.decision,
                          COUNT(job.id),
                          COUNT(job.id) FILTER (WHERE job.status IN
                              ('queued', 'retry_wait', 'leased', 'running')),
                          COUNT(job.id) FILTER (WHERE job.status = 'succeeded'),
                          COUNT(job.id) FILTER (WHERE job.status = 'failed')
                   FROM paper_rollback_requests request
                   JOIN paper_rollback_review_heads head
                     ON head.request_id = request.id
                   LEFT JOIN paper_rollback_execution_jobs job
                     ON job.request_id = request.id
                   GROUP BY request.id, head.review_revision, head.decision
               ) recent
               ORDER BY requested_at DESC, request_kind, request_id
               LIMIT %s""",
            (maximum,),
        ).fetchall()
    return tuple(_operation_request_summary(row) for row in rows)


def _admin_target_state(value: Any) -> dict[str, Any]:
    item = _row(value, missing_code="operation_target_state_missing")
    status = str(item.get("status"))
    if status not in {
        "awaiting_approval",
        "queued",
        "retry_wait",
        "leased",
        "running",
        "succeeded",
        "failed",
    }:
        raise _fail("operation_target_status_invalid")
    raw_error = item.get("error_code")
    error_code = None if raw_error is None else _code(raw_error)
    return {
        "paper_id": _positive_int(item.get("paper_id"), field="paper_id"),
        "status": status,
        "attempt_count": _nonnegative_int(
            item.get("attempt_count"), field="attempt_count"
        ),
        "error_code": error_code,
        "result_version_id": _version(
            item.get("result_version_id"),
            field="result_version_id",
            nullable=True,
        ),
    }


def get_operation_request_detail(
    request_kind: str, request_id: str | UUID
) -> dict[str, Any]:
    """Read one immutable request plus opaque target execution state."""

    if request_kind not in {"reprocess", "rollback"}:
        raise _fail("operation_request_kind_invalid")
    clean_id = _uuid(request_id, field="request_id")
    if request_kind == "reprocess":
        stored = get_reprocess_request(clean_id)
        request = stored["request"]
        raw_targets = stored["targets"]
        states = list_reprocess_target_states(clean_id)
        scope = str(request["scope"])
        global_confirmed = bool(request.get("global_confirmation"))
    else:
        stored = get_rollback_request(clean_id)
        request = stored["request"]
        raw_targets = request["targets"]
        states = list_rollback_target_states(clean_id)
        scope = "rollback"
        global_confirmed = False
    targets = tuple(
        {
            "paper_id": int(target["paper_id"]),
            "expected_source_sha256": str(target["expected_source_sha256"]),
            "expected_current_version_id": target["expected_current_version_id"],
            **(
                {"rollback_to_version_id": target["rollback_to_version_id"]}
                if request_kind == "rollback"
                else {}
            ),
        }
        for target in raw_targets
    )
    target_states = tuple(_admin_target_state(state) for state in states)
    if {target["paper_id"] for target in targets} != {
        state["paper_id"] for state in target_states
    }:
        raise _fail("operation_request_target_state_mismatch")
    return {
        "request_kind": request_kind,
        "request_id": clean_id,
        "idempotency_key": str(request["idempotency_key"]),
        "scope": scope,
        "reason_code": str(request["reason_code"]),
        "requested_at": request["requested_at"],
        "target_count": len(targets),
        "review_revision": int(stored["review_revision"]),
        "decision": str(stored["decision"]),
        "approval_event_id": stored["approval_event_id"],
        "global_confirmed": global_confirmed,
        "targets": targets,
        "target_states": target_states,
    }


def get_operations_summary() -> dict[str, int]:
    """Return only aggregate operational counts, never content or paths."""

    with connection() as database:
        row = _row(
            database.execute(
                """SELECT
                    (SELECT COUNT(*) FROM paper_expected_workers
                     WHERE enabled) AS expected_workers,
                    (SELECT COUNT(*) FROM paper_worker_heartbeats heartbeat
                     JOIN paper_expected_workers expected
                       ON expected.service_id = heartbeat.service_id
                      AND expected.worker_id = heartbeat.worker_id
                     WHERE expected.enabled) AS observed_workers,
                    (SELECT COUNT(*) FROM paper_incidents
                     WHERE status = 'open') AS open_incidents,
                    (SELECT COUNT(*) FROM paper_webhook_outbox
                     WHERE status = 'pending') AS pending_webhooks,
                    (SELECT COUNT(*) FROM paper_webhook_outbox
                     WHERE status = 'dead') AS dead_webhooks,
                    (SELECT COUNT(*) FROM paper_reprocess_review_heads
                     WHERE decision = 'pending') AS reprocess_pending_review,
                    (SELECT COUNT(*) FROM paper_reprocess_execution_jobs
                     WHERE status IN ('queued', 'retry_wait', 'leased', 'running'))
                        AS reprocess_active,
                    (SELECT COUNT(*) FROM paper_reprocess_execution_jobs
                     WHERE status = 'failed') AS reprocess_failed,
                    (SELECT COUNT(*) FROM paper_rollback_review_heads
                     WHERE decision = 'pending') AS rollback_pending_review,
                    (SELECT COUNT(*) FROM paper_rollback_execution_jobs
                     WHERE status IN ('queued', 'retry_wait', 'leased', 'running'))
                        AS rollback_active,
                    (SELECT COUNT(*) FROM paper_rollback_execution_jobs
                     WHERE status = 'failed') AS rollback_failed,
                    (SELECT COUNT(*) FROM paper_knowledge_jobs
                     WHERE status IN ('queued', 'retry_wait', 'running'))
                        AS knowledge_active,
                    (SELECT COUNT(*) FROM paper_knowledge_jobs
                     WHERE status = 'failed') AS knowledge_failed,
                    (SELECT COUNT(*) FROM paper_qa_jobs
                     WHERE status IN ('queued', 'retry_wait', 'running'))
                        AS qa_active,
                    (SELECT COUNT(*) FROM paper_qa_jobs
                     WHERE status = 'failed') AS qa_failed,
                    (SELECT COUNT(*) FROM paper_taxonomy_generation_jobs
                     WHERE status IN ('queued', 'retry_wait'))
                        AS taxonomy_generation_pending,
                    (SELECT COUNT(*) FROM paper_taxonomy_generation_jobs
                     WHERE status = 'running') AS taxonomy_generation_active,
                    (SELECT COUNT(*) FROM paper_taxonomy_generation_jobs
                     WHERE status = 'failed') AS taxonomy_generation_failed,
                    (SELECT COUNT(*) FROM paper_taxonomy_move_jobs
                     WHERE status IN ('queued', 'retry_wait'))
                        AS taxonomy_move_pending,
                    (SELECT COUNT(*) FROM paper_taxonomy_move_jobs
                     WHERE status = 'running') AS taxonomy_move_active,
                    (SELECT COUNT(*) FROM paper_taxonomy_move_jobs
                     WHERE status = 'failed') AS taxonomy_move_failed"""
            ).fetchone(),
            missing_code="operations_summary_missing",
        )
    allowed = {
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
    }
    if set(row) != allowed:
        raise _fail("operations_summary_row_invalid")
    return {key: int(row[key]) for key in sorted(allowed)}


def list_operation_failures(*, limit: int = 50) -> tuple[dict[str, Any], ...]:
    """Return an allowlisted opaque failure projection for the admin UI."""

    maximum = _positive_int(limit, field="limit", maximum=200)
    with connection() as database:
        rows = database.execute(
            """SELECT operation_kind, operation_id, paper_id, status,
                      error_code, attempt_count, occurred_at
               FROM (
                   SELECT 'reprocess'::text AS operation_kind, id::text AS operation_id,
                          paper_id, status, last_error_code AS error_code,
                          attempt_count, finished_at AS occurred_at
                   FROM paper_reprocess_execution_jobs WHERE status = 'failed'
                   UNION ALL
                   SELECT 'rollback', id::text, paper_id, status,
                          last_error_code, attempt_count, finished_at
                   FROM paper_rollback_execution_jobs WHERE status = 'failed'
                   UNION ALL
                   SELECT 'webhook', id::text, NULL::bigint, status,
                          last_error_code, attempt_count, created_at
                   FROM paper_webhook_outbox WHERE status = 'dead'
                   UNION ALL
                   SELECT 'knowledge', id::text, paper_id, status,
                          last_error_code, attempt_count, finished_at
                   FROM paper_knowledge_jobs WHERE status = 'failed'
                   UNION ALL
                   SELECT 'qa', id::text, paper_id, status,
                          last_error_code, attempt_count, finished_at
                   FROM paper_qa_jobs WHERE status = 'failed'
                   UNION ALL
                   SELECT 'taxonomy_generation', id::text, NULL::bigint, status,
                          last_error_code, attempt_count, finished_at
                   FROM paper_taxonomy_generation_jobs WHERE status = 'failed'
                   UNION ALL
                   SELECT 'taxonomy_move', id::text, paper_id, status,
                          last_error_code, attempt_count, finished_at
                   FROM paper_taxonomy_move_jobs WHERE status = 'failed'
               ) failures
               ORDER BY occurred_at DESC NULLS LAST, operation_kind, operation_id
               LIMIT %s""",
            (maximum,),
        ).fetchall()
    output: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        output.append(
            {
                "operation_kind": str(item["operation_kind"]),
                "operation_id": str(item["operation_id"]),
                "paper_id": (
                    int(item["paper_id"]) if item["paper_id"] is not None else None
                ),
                "status": str(item["status"]),
                "error_code": (
                    str(item["error_code"]) if item["error_code"] else None
                ),
                "attempt_count": int(item["attempt_count"]),
                "occurred_at": item["occurred_at"],
            }
        )
    return tuple(output)


def get_cost_summary() -> dict[str, int]:
    """Aggregate model usage without exposing prompts, answers, or identifiers."""

    with connection() as database:
        row = _row(
            database.execute(
                """SELECT COUNT(*) AS attempt_count,
                          COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                          COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                          COALESCE(SUM(total_tokens), 0) AS total_tokens,
                          COALESCE(SUM(cost_microusd), 0) AS known_cost_microusd,
                          COUNT(*) FILTER (WHERE cost_microusd IS NULL)
                              AS unknown_cost_attempts
                   FROM (
                       SELECT prompt_tokens, completion_tokens, total_tokens,
                              cost_microusd
                       FROM paper_knowledge_usage_ledger
                       UNION ALL
                       SELECT prompt_tokens, completion_tokens, total_tokens,
                              cost_microusd
                       FROM paper_qa_usage_ledger
                       UNION ALL
                       SELECT prompt_tokens, completion_tokens, total_tokens,
                              cost_microusd
                       FROM paper_taxonomy_usage_ledger
                   ) usage"""
            ).fetchone(),
            missing_code="cost_summary_missing",
        )
    allowed = {
        "attempt_count",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "known_cost_microusd",
        "unknown_cost_attempts",
    }
    if set(row) != allowed:
        raise _fail("cost_summary_row_invalid")
    return {key: int(row[key]) for key in sorted(allowed)}

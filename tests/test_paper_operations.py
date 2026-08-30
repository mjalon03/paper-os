from __future__ import annotations

import copy
import json
import unittest

from paper_service.operations import (
    GLOBAL_REPROCESS_CONFIRMATION,
    REPROCESS_SCHEMA,
    OperationsContractError,
    advance_incident_state,
    build_generic_webhook_payload,
    build_selective_reprocess_request,
    build_worker_health_snapshot,
    classify_reprocess_idempotency,
    derive_reprocess_idempotency_key,
    initial_incident_state,
    load_reprocess_request_json,
    reprocess_request_fingerprint,
    validate_health_snapshot,
    validate_incident_state,
    validate_reprocess_request,
    validate_reprocess_target_source,
    validate_worker_heartbeat,
    worker_heartbeat,
)


SOURCE_SHA = "a" * 64
VERSION_ID = "rv-" + "b" * 32


def heartbeat(
    worker_id: str = "reader-1",
    *,
    observed_at: str = "2026-08-29T01:00:00Z",
    sequence: int = 1,
    state: str = "idle",
    active_paper_id=None,
    error_code=None,
) -> dict:
    return worker_heartbeat(
        worker_id=worker_id,
        observed_at=observed_at,
        sequence=sequence,
        state=state,
        active_paper_id=active_paper_id,
        last_success_at="2026-08-28T00:00:00Z",
        error_code=error_code,
    )


def target(
    paper_id: int = 7,
    *,
    source_sha: str = SOURCE_SHA,
    version_id: str | None = VERSION_ID,
) -> dict:
    return {
        "paper_id": paper_id,
        "expected_source_sha256": source_sha,
        "expected_current_version_id": version_id,
    }


class WorkerHeartbeatTests(unittest.TestCase):
    def test_heartbeat_contract_is_strict_bounded_and_content_free(self):
        item = heartbeat(
            state="processing",
            active_paper_id=7,
        )
        self.assertEqual(validate_worker_heartbeat(item), item)
        self.assertEqual(item["active_paper_id"], 7)
        serialized = json.dumps(item)
        self.assertNotIn("title", serialized)
        self.assertNotIn("content", serialized)
        self.assertNotIn("doi", serialized)

        unknown = dict(item, paper_title="Sensitive title")
        with self.assertRaisesRegex(OperationsContractError, "unknown_field"):
            validate_worker_heartbeat(unknown)
        with self.assertRaisesRegex(OperationsContractError, "processing_requires"):
            heartbeat(state="processing")
        with self.assertRaisesRegex(OperationsContractError, "inactive_worker_has_active"):
            heartbeat(state="idle", active_paper_id=7)
        with self.assertRaisesRegex(OperationsContractError, "degraded_requires"):
            heartbeat(state="degraded")

    def test_heartbeat_timestamps_sequences_and_error_states_fail_closed(self):
        future_success = heartbeat()
        future_success["last_success_at"] = "2026-08-29T02:00:00Z"
        boolean_sequence = heartbeat()
        boolean_sequence["sequence"] = True
        healthy_error = heartbeat()
        healthy_error["error_code"] = "worker_failed"
        for payload, code in (
            (future_success, "last_success_after"),
            (boolean_sequence, "sequence_invalid"),
            (healthy_error, "healthy_worker_has_error"),
        ):
            with self.subTest(code=code):
                with self.assertRaisesRegex(OperationsContractError, code):
                    validate_worker_heartbeat(payload)

        invalid_schema = heartbeat()
        invalid_schema["schema_version"] = True
        invalid_state = heartbeat()
        invalid_state["state"] = {"idle": True}
        with self.assertRaisesRegex(OperationsContractError, "schema_invalid"):
            validate_worker_heartbeat(invalid_schema)
        with self.assertRaisesRegex(OperationsContractError, "worker_state_invalid"):
            validate_worker_heartbeat(invalid_state)


class WorkerHealthTests(unittest.TestCase):
    def test_healthy_snapshot_is_deterministic_and_canonical(self):
        values = [
            heartbeat("reader-2", observed_at="2026-08-29T01:00:05Z"),
            heartbeat("reader-1", observed_at="2026-08-29T01:00:00Z"),
        ]
        first = build_worker_health_snapshot(
            values,
            expected_worker_ids=["reader-1", "reader-2"],
            evaluated_at="2026-08-29T01:00:10Z",
        )
        second = build_worker_health_snapshot(
            reversed(values),
            expected_worker_ids=["reader-2", "reader-1"],
            evaluated_at="2026-08-29T01:00:10Z",
        )
        self.assertEqual(first, second)
        self.assertEqual(first["status"], "healthy")
        self.assertEqual(first["reason_codes"], [])
        self.assertEqual(first, validate_health_snapshot(first))
        self.assertRegex(first["dedupe_key"], r"^health-[0-9a-f]{32}$")

    def test_missing_stale_stopped_and_degraded_workers_are_generic(self):
        snapshot = build_worker_health_snapshot(
            [
                heartbeat(
                    "reader-1",
                    observed_at="2026-08-29T00:55:00Z",
                ),
                heartbeat(
                    "reader-2",
                    state="degraded",
                    error_code="conversion_failed",
                ),
                heartbeat(
                    "reader-3",
                    state="stopped",
                ),
            ],
            expected_worker_ids=["reader-1", "reader-2", "reader-3", "reader-4"],
            evaluated_at="2026-08-29T01:00:00Z",
            stale_after_seconds=120,
        )
        self.assertEqual(snapshot["status"], "unhealthy")
        self.assertEqual(
            snapshot["reason_codes"],
            ["worker_degraded", "worker_missing", "worker_stale", "worker_stopped"],
        )
        self.assertEqual(snapshot["affected_worker_count"], 4)
        serialized = json.dumps(snapshot)
        self.assertNotIn("paper_id", serialized)
        self.assertNotIn("conversion_failed", serialized)

    def test_future_duplicate_unexpected_and_malformed_snapshots_fail_closed(self):
        with self.assertRaisesRegex(OperationsContractError, "heartbeat_from_future"):
            build_worker_health_snapshot(
                [heartbeat(observed_at="2026-08-29T02:00:00Z")],
                expected_worker_ids=["reader-1"],
                evaluated_at="2026-08-29T01:00:00Z",
            )
        with self.assertRaisesRegex(OperationsContractError, "duplicate_worker"):
            build_worker_health_snapshot(
                [heartbeat(), heartbeat()],
                expected_worker_ids=["reader-1"],
                evaluated_at="2026-08-29T01:00:00Z",
            )
        with self.assertRaisesRegex(OperationsContractError, "unexpected_worker"):
            build_worker_health_snapshot(
                [heartbeat("reader-2")],
                expected_worker_ids=["reader-1"],
                evaluated_at="2026-08-29T01:00:00Z",
            )
        canonical = build_worker_health_snapshot(
            [heartbeat()],
            expected_worker_ids=["reader-1"],
            evaluated_at="2026-08-29T01:00:10Z",
        )
        valid = copy.deepcopy(canonical)
        valid["dedupe_key"] = "health-" + "0" * 32
        with self.assertRaisesRegex(OperationsContractError, "dedupe_key_mismatch"):
            validate_health_snapshot(valid)

        boolean_schema = copy.deepcopy(canonical)
        boolean_schema["schema_version"] = True
        with self.assertRaisesRegex(OperationsContractError, "schema_invalid"):
            validate_health_snapshot(boolean_schema)
        invalid_status = copy.deepcopy(canonical)
        invalid_status["status"] = {"healthy": True}
        with self.assertRaisesRegex(OperationsContractError, "status_invalid"):
            validate_health_snapshot(invalid_status)
        forged_reason = copy.deepcopy(canonical)
        forged_reason["status"] = "unhealthy"
        forged_reason["reason_codes"] = ["paper_title_secret"]
        forged_reason["affected_worker_count"] = 1
        forged_reason["workers"][0]["affected"] = True
        with self.assertRaisesRegex(OperationsContractError, "reason_code_invalid"):
            validate_health_snapshot(forged_reason)


class IncidentStateMachineTests(unittest.TestCase):
    def unhealthy(self, evaluated_at: str):
        return build_worker_health_snapshot(
            [],
            expected_worker_ids=["reader-1"],
            evaluated_at=evaluated_at,
        )

    def healthy(self, evaluated_at: str):
        return build_worker_health_snapshot(
            [heartbeat(observed_at=evaluated_at)],
            expected_worker_ids=["reader-1"],
            evaluated_at=evaluated_at,
        )

    def test_incident_opens_once_dedupes_and_recovers_once(self):
        state = initial_incident_state()
        state, opened = advance_incident_state(
            state, self.unhealthy("2026-08-29T01:00:00Z")
        )
        self.assertEqual(len(opened), 1)
        self.assertEqual(opened[0]["event_type"], "incident_opened")
        incident_id = state["incident"]["incident_id"]

        state, duplicate = advance_incident_state(
            state, self.unhealthy("2026-08-29T01:01:00Z")
        )
        self.assertEqual(duplicate, ())
        self.assertEqual(state["incident"]["incident_id"], incident_id)
        self.assertEqual(state["incident"]["last_seen_at"], "2026-08-29T01:01:00Z")

        state, recovered = advance_incident_state(
            state, self.healthy("2026-08-29T01:02:00Z")
        )
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0]["event_type"], "incident_recovered")
        self.assertNotEqual(recovered[0]["dedupe_key"], opened[0]["dedupe_key"])
        state, repeated_recovery = advance_incident_state(
            state, self.healthy("2026-08-29T01:03:00Z")
        )
        self.assertEqual(repeated_recovery, ())
        self.assertEqual(validate_incident_state(state), state)

        state, reopened = advance_incident_state(
            state, self.unhealthy("2026-08-29T01:04:00Z")
        )
        self.assertEqual(len(reopened), 1)
        self.assertNotEqual(state["incident"]["incident_id"], incident_id)
        self.assertEqual(state["generation"], 2)

    def test_state_machine_rejects_service_and_time_regression(self):
        state, _events = advance_incident_state(
            initial_incident_state(), self.unhealthy("2026-08-29T01:00:00Z")
        )
        other = build_worker_health_snapshot(
            service_id="other-reader",
            evaluated_at="2026-08-29T01:01:00Z",
            expected_worker_ids=("reader-1",),
            heartbeats=(heartbeat(observed_at="2026-08-29T01:01:00Z"),),
            stale_after_seconds=60,
        )
        with self.assertRaisesRegex(OperationsContractError, "service_mismatch"):
            advance_incident_state(state, other)
        with self.assertRaisesRegex(OperationsContractError, "time_regressed"):
            advance_incident_state(
                state, self.unhealthy("2026-08-29T00:59:00Z")
            )

    def test_webhook_payload_is_generic_and_never_transmitted(self):
        state, events = advance_incident_state(
            initial_incident_state(), self.unhealthy("2026-08-29T01:00:00Z")
        )
        payload = build_generic_webhook_payload(events[0])
        self.assertEqual(payload["event"], "incident_opened")
        self.assertEqual(payload["metrics"], {"affected_workers": 1})
        serialized = json.dumps(payload)
        for forbidden in (
            "paper_id",
            "title",
            "content",
            "doi",
            "webhook_url",
            "endpoint",
        ):
            self.assertNotIn(forbidden, serialized.casefold())
        self.assertEqual(state["incident"]["status"], "open")


class SelectiveReprocessTests(unittest.TestCase):
    def test_selective_request_is_sorted_bounded_and_idempotent(self):
        request = build_selective_reprocess_request(
            targets=[target(9, version_id=None), target(7)],
            reason_code="extractor_upgrade",
            requested_at="2026-08-29T01:00:00Z",
        )
        self.assertEqual([item["paper_id"] for item in request["targets"]], [7, 9])
        self.assertRegex(request["idempotency_key"], r"^rr-[0-9a-f]{32}$")
        reordered = build_selective_reprocess_request(
            targets=[target(7), target(9, version_id=None)],
            reason_code="extractor_upgrade",
            requested_at="2026-08-29T01:00:00Z",
        )
        self.assertEqual(request, reordered)
        later = build_selective_reprocess_request(
            targets=[target(7), target(9, version_id=None)],
            reason_code="extractor_upgrade",
            requested_at="2026-08-29T02:00:00Z",
        )
        self.assertNotEqual(request["idempotency_key"], later["idempotency_key"])
        fingerprint = reprocess_request_fingerprint(request)
        self.assertEqual(
            classify_reprocess_idempotency(request, {request["idempotency_key"]: fingerprint}),
            "duplicate",
        )
        self.assertEqual(classify_reprocess_idempotency(request, {}), "new")

    def test_idempotency_key_is_derived_and_tampering_fails_closed(self):
        request = build_selective_reprocess_request(
            targets=[target()],
            reason_code="manual_repair",
            requested_at="2026-08-29T01:00:00Z",
        )
        expected = derive_reprocess_idempotency_key(
            scope="selected",
            reason_code="manual_repair",
            requested_at="2026-08-29T01:00:00Z",
            targets=[target()],
        )
        self.assertEqual(request["idempotency_key"], expected)
        request["idempotency_key"] = "rr-" + "0" * 32
        with self.assertRaisesRegex(OperationsContractError, "key_mismatch"):
            validate_reprocess_request(request)
        with self.assertRaisesRegex(OperationsContractError, "targets_invalid"):
            derive_reprocess_idempotency_key(
                scope="selected",
                reason_code="operator_requested",
                requested_at="2026-08-29T01:00:00Z",
                targets=None,
            )
        invalid_scope = copy.deepcopy(request)
        invalid_scope["scope"] = {"selected": True}
        with self.assertRaisesRegex(OperationsContractError, "scope_invalid"):
            validate_reprocess_request(invalid_scope)

    def test_global_reprocessing_is_forbidden_by_default_and_requires_confirmation(self):
        key = derive_reprocess_idempotency_key(
            scope="all",
            reason_code="extractor_upgrade",
            requested_at="2026-08-29T01:00:00Z",
            targets=[],
        )
        request = {
            "schema": REPROCESS_SCHEMA,
            "schema_version": 1,
            "scope": "all",
            "reason_code": "extractor_upgrade",
            "requested_at": "2026-08-29T01:00:00Z",
            "targets": [],
            "idempotency_key": key,
            "global_confirmation": GLOBAL_REPROCESS_CONFIRMATION,
        }
        with self.assertRaisesRegex(OperationsContractError, "global_reprocess_forbidden"):
            validate_reprocess_request(request)
        with self.assertRaisesRegex(OperationsContractError, "global_reprocess_forbidden"):
            reprocess_request_fingerprint(request)
        self.assertEqual(
            validate_reprocess_request(request, allow_global=True)["scope"], "all"
        )
        self.assertRegex(
            reprocess_request_fingerprint(request, allow_global=True),
            r"^[0-9a-f]{64}$",
        )
        missing_confirmation = dict(request)
        missing_confirmation.pop("global_confirmation")
        with self.assertRaisesRegex(OperationsContractError, "confirmation_required"):
            validate_reprocess_request(missing_confirmation, allow_global=True)

    def test_duplicate_empty_oversized_and_unknown_target_data_fail_closed(self):
        with self.assertRaisesRegex(OperationsContractError, "targets_required"):
            build_selective_reprocess_request(
                targets=[],
                reason_code="manual_repair",
                requested_at="2026-08-29T01:00:00Z",
            )
        with self.assertRaisesRegex(OperationsContractError, "target_duplicate"):
            build_selective_reprocess_request(
                targets=[target(), target()],
                reason_code="manual_repair",
                requested_at="2026-08-29T01:00:00Z",
            )
        with self.assertRaisesRegex(OperationsContractError, "targets_invalid"):
            build_selective_reprocess_request(
                targets=[target(index + 1, version_id=None) for index in range(51)],
                reason_code="manual_repair",
                requested_at="2026-08-29T01:00:00Z",
            )
        unknown = target()
        unknown["title"] = "Sensitive title"
        with self.assertRaisesRegex(OperationsContractError, "unknown_field"):
            build_selective_reprocess_request(
                targets=[unknown],
                reason_code="manual_repair",
                requested_at="2026-08-29T01:00:00Z",
            )

    def test_target_source_and_current_version_must_still_match(self):
        request = build_selective_reprocess_request(
            targets=[target()],
            reason_code="manual_repair",
            requested_at="2026-08-29T01:00:00Z",
        )
        self.assertEqual(
            validate_reprocess_target_source(
                request,
                paper_id=7,
                actual_source_sha256=SOURCE_SHA,
                actual_current_version_id=VERSION_ID,
            )["paper_id"],
            7,
        )
        with self.assertRaisesRegex(OperationsContractError, "source_sha256_stale"):
            validate_reprocess_target_source(
                request,
                paper_id=7,
                actual_source_sha256="c" * 64,
                actual_current_version_id=VERSION_ID,
            )
        with self.assertRaisesRegex(OperationsContractError, "current_version_stale"):
            validate_reprocess_target_source(
                request,
                paper_id=7,
                actual_source_sha256=SOURCE_SHA,
                actual_current_version_id="rv-" + "c" * 32,
            )

    def test_json_loader_rejects_duplicate_keys_and_round_trips(self):
        request = build_selective_reprocess_request(
            targets=[target()],
            reason_code="manual_repair",
            requested_at="2026-08-29T01:00:00Z",
        )
        self.assertEqual(load_reprocess_request_json(json.dumps(request)), request)
        with self.assertRaisesRegex(OperationsContractError, "duplicate_key"):
            load_reprocess_request_json('{"schema":1,"schema":2}')


if __name__ == "__main__":
    unittest.main()

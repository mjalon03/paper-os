from __future__ import annotations

import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

from paper_service.artifact_versions import ArtifactResolution
from paper_service.security import ACCESS_JWT_HEADER, ACTION_HEADER, SecurityConfig

try:
    from fastapi.testclient import TestClient
    from paper_service import api

    FASTAPI_AVAILABLE = True
except ModuleNotFoundError:  # pragma: no cover - deployment dependency
    TestClient = None
    api = None
    FASTAPI_AVAILABLE = False


SOURCE_SHA = "a" * 64
CURRENT_VERSION = "rv-" + "1" * 32
OLD_VERSION = "rv-" + "2" * 32
REQUEST_ID = UUID("7b2acb2a-0707-5c12-a05b-e46292cd6b21")
AUDIT_ID = UUID("f7347b69-0443-54d6-9a3e-f37c995af301")
PROPOSAL_ID = UUID("7c407f66-1ac3-4f54-a784-bcfb004572df")
PUBLIC_ORIGIN = "https://papers.example.com"


def operation_item(**updates):
    item = {
        "request_kind": "reprocess",
        "request_id": str(REQUEST_ID),
        "scope": "selected",
        "reason_code": "reader_quality_fix",
        "requested_at": "2026-08-29T12:00:00Z",
        "target_count": 1,
        "review_revision": 0,
        "decision": "pending",
        "job_count": 0,
        "active_job_count": 0,
        "succeeded_job_count": 0,
        "failed_job_count": 0,
        "state": "pending_review",
    }
    item.update(updates)
    return item


def operation_detail(*, kind="reprocess", scope="selected"):
    target = {
        "paper_id": 7,
        "expected_source_sha256": SOURCE_SHA,
        "expected_current_version_id": None,
        "private_path": "/private/paper.pdf",
    }
    if kind == "rollback":
        target["expected_current_version_id"] = CURRENT_VERSION
        target["rollback_to_version_id"] = OLD_VERSION
    return {
        **operation_item(request_kind=kind, scope=scope),
        "approval_event_id": "evidence-marker",
        "idempotency_key": "private-idempotency-marker",
        "targets": [target],
        "target_states": [
            {
                "paper_id": 7,
                "status": "awaiting_approval",
                "attempt_count": 0,
                "error_code": None,
                "result_version_id": None,
                "lease_token": "lease-marker",
            }
        ],
    }


@unittest.skipUnless(FASTAPI_AVAILABLE, "FastAPI test dependency is not installed")
class AdminApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(api.app)

    def test_admin_page_and_external_controller_are_served(self):
        with patch.dict(os.environ, {}, clear=True):
            page = self.client.get("/admin")
            controller = self.client.get("/static/admin.js")

        self.assertEqual(page.status_code, 200)
        self.assertIn(b'/static/admin.js" defer', page.content)
        self.assertEqual(controller.status_code, 200)
        self.assertTrue(
            controller.headers["content-type"].startswith("text/javascript")
        )

    def test_reprocess_target_accepts_integrity_bound_legacy_but_rollback_does_not(self):
        legacy = ArtifactResolution(
            mode="legacy_v2",
            paper_id=7,
            root=Path("/safe/legacy"),
            version_id=None,
            descriptor=None,
        )
        with (
            patch(
                "paper_service.api.get_admin_paper_source_binding",
                return_value={"paper_id": 7, "source_sha256": SOURCE_SHA},
            ),
            patch("paper_service.api.resolve_current_artifact", return_value=legacy) as resolve,
        ):
            target = api._reprocess_operation_target(7)
            with self.assertRaises(api.HTTPException) as raised:
                api._rollback_operation_target(7)

        self.assertEqual(
            target,
            {
                "paper_id": 7,
                "expected_source_sha256": SOURCE_SHA,
                "expected_current_version_id": None,
            },
        )
        self.assertEqual(raised.exception.detail, "versioned_artifact_required")
        self.assertTrue(resolve.call_args.kwargs["allow_legacy_v2"])

    def test_overview_is_failure_isolated_and_all_projections_are_allowlisted(self):
        secret = "must-not-escape-any-admin-response"
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("paper_service.api._utc_now_text", return_value="2026-08-29T12:00:00Z"),
            patch(
                "paper_service.api.get_operations_summary",
                return_value={"open_incidents": 2, "prompt": secret},
            ),
            patch(
                "paper_service.api.get_cost_summary",
                return_value={"attempt_count": 3, "raw_response": secret},
            ),
            patch(
                "paper_service.api.list_operation_failures",
                return_value=(
                    {
                        "operation_kind": "reprocess",
                        "operation_id": str(REQUEST_ID),
                        "paper_id": 7,
                        "status": "failed",
                        "error_code": "reprocess_failed",
                        "attempt_count": 3,
                        "occurred_at": "2026-08-29T11:00:00Z",
                        "stored_path": secret,
                    },
                ),
            ),
            patch(
                "paper_service.api.list_pending_operation_reviews",
                return_value=(operation_item(evidence=secret),),
            ),
            patch(
                "paper_service.api.list_recent_operation_requests",
                return_value=(operation_item(annotation_text=secret),),
            ),
            patch(
                "paper_service.api._admin_health_overview",
                return_value=[
                    {
                        "service_id": "reader-v2",
                        "evaluated_at": "2026-08-29T12:00:00Z",
                        "status": "healthy",
                        "reason_codes": [],
                        "expected_worker_count": 1,
                        "observed_worker_count": 1,
                        "affected_worker_count": 0,
                        "workers": [],
                        "secret": secret,
                    }
                ],
            ),
            patch(
                "paper_service.api.list_pending_taxonomy_audits",
                return_value=(
                    {
                        "audit_id": str(AUDIT_ID),
                        "category": "Neuro AI",
                        "paper_count": 12,
                        "new_paper_count": 4,
                        "generation": {"requested": False, "prompt": secret},
                        "raw_response": secret,
                    },
                ),
            ),
            patch(
                "paper_service.api.list_pending_taxonomy_proposals",
                return_value=(
                    {
                        "proposal_id": str(PROPOSAL_ID),
                        "category": "Neuro AI",
                        "assignment_count": 12,
                        "subtheme_count": 3,
                        "review_revision": 0,
                        "review_status": "pending",
                        "lease_token": secret,
                    },
                ),
            ),
            patch(
                "paper_service.api.list_admin_paper_choices",
                return_value=(
                    {
                        "paper_id": 7,
                        "title": "Safe display title",
                        "category": "Neuro AI",
                        "publication_year": 2026,
                        "reader_status": "ready",
                        "stored_path": secret,
                    },
                ),
            ),
        ):
            response = self.client.get("/api/admin/overview")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["operations"]["open_incidents"], 2)
        self.assertEqual(payload["cost"]["attempt_count"], 3)
        self.assertEqual(payload["papers"][0]["title"], "Safe display title")
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn(secret, serialized)
        for forbidden_key in (
            '"prompt"',
            '"raw_response"',
            '"stored_path"',
            '"lease_token"',
            '"annotation_text"',
            '"evidence"',
        ):
            self.assertNotIn(forbidden_key, serialized)

    def test_overview_preserves_other_sections_when_one_query_fails(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "paper_service.api.get_operations_summary",
                side_effect=RuntimeError("private exception detail"),
            ),
            patch(
                "paper_service.api.get_cost_summary",
                return_value={"attempt_count": 4},
            ),
            patch("paper_service.api.list_operation_failures", return_value=()),
            patch("paper_service.api.list_pending_operation_reviews", return_value=()),
            patch("paper_service.api.list_recent_operation_requests", return_value=()),
            patch("paper_service.api._admin_health_overview", return_value=[]),
            patch("paper_service.api.list_pending_taxonomy_audits", return_value=()),
            patch("paper_service.api.list_pending_taxonomy_proposals", return_value=()),
            patch("paper_service.api.list_admin_paper_choices", return_value=()),
        ):
            response = self.client.get("/api/admin/overview")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIsNone(payload["operations"])
        self.assertEqual(payload["cost"]["attempt_count"], 4)
        self.assertIn("operations_unavailable", payload["section_errors"])
        self.assertNotIn("private exception detail", response.text)

    def test_operation_detail_excludes_evidence_idempotency_path_and_lease(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "paper_service.api.get_operation_request_detail",
                return_value=operation_detail(),
            ),
        ):
            response = self.client.get(
                f"/api/admin/operations/reprocess/{REQUEST_ID}"
            )

        self.assertEqual(response.status_code, 200)
        serialized = response.text
        for forbidden in (
            "evidence-marker",
            "private-idempotency-marker",
            "/private/paper.pdf",
            "lease-marker",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_taxonomy_detail_excludes_paths_raw_response_and_prompt(self):
        marker = "private-taxonomy-marker"
        detail = {
            "proposal_id": str(PROPOSAL_ID),
            "category": "Neuro AI",
            "assignment_count": 1,
            "subtheme_count": 2,
            "review_revision": 0,
            "review_status": "pending",
            "rationale": "Safe rationale",
            "subthemes": [
                {"name": "BCI", "description": "Safe", "assignment_count": 1}
            ],
            "assignments": [
                {
                    "paper_id": 7,
                    "source_sha256": SOURCE_SHA,
                    "subtheme": "BCI",
                    "move_required": True,
                    "old_drive_path": marker,
                    "new_drive_path": marker,
                    "annotation_text": marker,
                }
            ],
            "prompt": marker,
            "raw_response": marker,
        }
        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "paper_service.api.get_taxonomy_proposal_detail",
                return_value=detail,
            ),
        ):
            response = self.client.get(
                f"/api/admin/taxonomy/proposals/{PROPOSAL_ID}"
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["proposal"]["rationale"], "Safe rationale")
        self.assertNotIn(marker, response.text)

    def test_legacy_reader_can_create_selective_reprocess_upgrade_request(self):
        stored_id = str(REQUEST_ID)

        def store(request):
            return {
                "request_id": stored_id,
                "request": request,
                "targets": request["targets"],
                "review_revision": 0,
                "decision": "pending",
            }

        with (
            patch.dict(os.environ, {"PAPER_WRITES_ENABLED": "true"}, clear=True),
            patch("paper_service.api._human_actor", return_value="human-1"),
            patch(
                "paper_service.api._reprocess_operation_target",
                return_value={
                    "paper_id": 7,
                    "expected_source_sha256": SOURCE_SHA,
                    "expected_current_version_id": None,
                },
            ),
            patch("paper_service.api._utc_now_text", return_value="2026-08-29T12:00:00Z"),
            patch("paper_service.api.create_reprocess_request", side_effect=store) as create,
        ):
            response = self.client.post(
                "/api/admin/operations/reprocess",
                headers={ACTION_HEADER: "1"},
                json={
                    "paper_ids": [7],
                    "reason_code": "extractor_upgrade",
                    "confirm_selected_reprocess": True,
                },
            )

        self.assertEqual(response.status_code, 201)
        request = create.call_args.args[0]
        self.assertEqual(request["scope"], "selected")
        self.assertIsNone(request["targets"][0]["expected_current_version_id"])
        self.assertEqual(response.json()["request"]["decision"], "pending")

    def test_reprocess_rejects_global_or_unconfirmed_input_before_store(self):
        with (
            patch.dict(os.environ, {"PAPER_WRITES_ENABLED": "true"}, clear=True),
            patch("paper_service.api._human_actor", return_value="human-1"),
            patch("paper_service.api._reprocess_operation_target") as target,
            patch("paper_service.api.create_reprocess_request") as create,
        ):
            global_response = self.client.post(
                "/api/admin/operations/reprocess",
                headers={ACTION_HEADER: "1"},
                json={
                    "paper_ids": [7],
                    "scope": "all",
                    "confirm_selected_reprocess": True,
                },
            )
            unconfirmed = self.client.post(
                "/api/admin/operations/reprocess",
                headers={ACTION_HEADER: "1"},
                json={
                    "paper_ids": [7],
                    "confirm_selected_reprocess": False,
                },
            )
            invalid_id = self.client.post(
                "/api/admin/operations/reprocess",
                headers={ACTION_HEADER: "1"},
                json={
                    "paper_ids": [0],
                    "confirm_selected_reprocess": True,
                },
            )

        self.assertEqual(global_response.status_code, 422)
        self.assertEqual(unconfirmed.status_code, 422)
        self.assertEqual(invalid_id.status_code, 422)
        target.assert_not_called()
        create.assert_not_called()

    def test_rollback_version_endpoint_only_plans_and_returns_opaque_ids(self):
        with (
            patch.dict(
                os.environ,
                {"PAPER_DATA_DIR": "/safe/data"},
                clear=True,
            ),
            patch(
                "paper_service.api._rollback_operation_target",
                return_value={
                    "paper_id": 7,
                    "expected_source_sha256": SOURCE_SHA,
                    "expected_current_version_id": CURRENT_VERSION,
                },
            ),
            patch(
                "paper_service.api.plan_retention",
                return_value={
                    "retain_version_ids": [CURRENT_VERSION, OLD_VERSION],
                    "delete_version_ids": ["rv-" + "3" * 32],
                    "skipped_unowned": ["private-name"],
                },
            ) as plan,
        ):
            response = self.client.get("/api/admin/papers/7/versions")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["rollback_version_ids"], [OLD_VERSION])
        self.assertNotIn("delete_version_ids", response.json())
        self.assertNotIn("private-name", response.text)
        plan.assert_called_once_with(Path("/safe/data"), 7, keep=100)

    def test_rollback_rejects_client_version_outside_server_choices(self):
        with (
            patch.dict(os.environ, {"PAPER_WRITES_ENABLED": "true"}, clear=True),
            patch("paper_service.api._human_actor", return_value="human-1"),
            patch(
                "paper_service.api._rollback_operation_target",
                return_value={
                    "paper_id": 7,
                    "expected_source_sha256": SOURCE_SHA,
                    "expected_current_version_id": CURRENT_VERSION,
                },
            ),
            patch(
                "paper_service.api._rollback_version_choices",
                return_value={
                    "paper_id": 7,
                    "active_version_id": CURRENT_VERSION,
                    "rollback_version_ids": [OLD_VERSION],
                },
            ),
            patch("paper_service.api.create_rollback_request") as create,
        ):
            response = self.client.post(
                "/api/admin/operations/rollback",
                headers={ACTION_HEADER: "1"},
                json={
                    "targets": [
                        {
                            "paper_id": 7,
                            "rollback_to_version_id": "rv-" + "9" * 32,
                        }
                    ],
                    "confirm_selected_rollback": True,
                },
            )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"], "rollback_version_not_available")
        create.assert_not_called()

    def test_valid_rollback_request_captures_server_current_before_review(self):
        stored = {
            "request_id": str(REQUEST_ID),
            "request": {
                "reason_code": "manual_rollback",
                "requested_at": "2026-08-29T12:00:00Z",
                "targets": [
                    {
                        "paper_id": 7,
                        "expected_source_sha256": SOURCE_SHA,
                        "expected_current_version_id": CURRENT_VERSION,
                        "rollback_to_version_id": OLD_VERSION,
                    }
                ],
            },
            "review_revision": 0,
            "decision": "pending",
            "private_path": "/private/paper.pdf",
        }
        with (
            patch.dict(os.environ, {"PAPER_WRITES_ENABLED": "true"}, clear=True),
            patch("paper_service.api._human_actor", return_value="human-1"),
            patch(
                "paper_service.api._rollback_operation_target",
                return_value={
                    "paper_id": 7,
                    "expected_source_sha256": SOURCE_SHA,
                    "expected_current_version_id": CURRENT_VERSION,
                },
            ),
            patch(
                "paper_service.api._rollback_version_choices",
                return_value={
                    "paper_id": 7,
                    "active_version_id": CURRENT_VERSION,
                    "rollback_version_ids": [OLD_VERSION],
                },
            ),
            patch("paper_service.api._utc_now_text", return_value="2026-08-29T12:00:00Z"),
            patch(
                "paper_service.api.create_rollback_request",
                return_value=stored,
            ) as create,
        ):
            response = self.client.post(
                "/api/admin/operations/rollback",
                headers={ACTION_HEADER: "1"},
                json={
                    "targets": [
                        {
                            "paper_id": 7,
                            "rollback_to_version_id": OLD_VERSION,
                        }
                    ],
                    "reason_code": "manual_rollback",
                    "confirm_selected_rollback": True,
                },
            )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["request"]["decision"], "pending")
        self.assertNotIn("/private/paper.pdf", response.text)
        self.assertEqual(
            create.call_args.kwargs["targets"][0]["expected_current_version_id"],
            CURRENT_VERSION,
        )
        self.assertEqual(
            create.call_args.kwargs["targets"][0]["rollback_to_version_id"],
            OLD_VERSION,
        )

    def test_operation_approval_is_human_confirmed_then_enqueued_without_leaks(self):
        review_event = {"review_revision": 1, "comment": "private comment"}
        jobs = (
            {
                "status": "queued",
                "lease_token": "private-lease",
                "stored_path": "/private/paper.pdf",
            },
        )
        with (
            patch.dict(os.environ, {"PAPER_WRITES_ENABLED": "true"}, clear=True),
            patch("paper_service.api._human_actor", return_value="human-1"),
            patch(
                "paper_service.api.get_operation_request_detail",
                return_value=operation_detail(),
            ),
            patch(
                "paper_service.api.review_reprocess_request",
                return_value=review_event,
            ) as review,
            patch(
                "paper_service.api.enqueue_approved_reprocess", return_value=jobs
            ) as enqueue,
        ):
            denied = self.client.post(
                f"/api/admin/operations/reprocess/{REQUEST_ID}/approve",
                headers={ACTION_HEADER: "1"},
                json={"expected_revision": 0, "confirm_execution": False},
            )
            response = self.client.post(
                f"/api/admin/operations/reprocess/{REQUEST_ID}/approve",
                headers={ACTION_HEADER: "1"},
                json={"expected_revision": 0, "confirm_execution": True},
            )

        self.assertEqual(denied.status_code, 422)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["enqueued_job_count"], 1)
        self.assertEqual(response.json()["job_status_counts"], {"queued": 1})
        self.assertNotIn("private-lease", response.text)
        self.assertNotIn("/private/paper.pdf", response.text)
        self.assertEqual(review.call_args.kwargs["actor_id"], "human-1")
        enqueue.assert_called_once_with(REQUEST_ID)

    def test_each_rejection_endpoint_records_human_decision_without_enqueue(self):
        with (
            patch.dict(os.environ, {"PAPER_WRITES_ENABLED": "true"}, clear=True),
            patch("paper_service.api._human_actor", return_value="human-1"),
            patch(
                "paper_service.api.get_operation_request_detail",
                side_effect=lambda kind, _request_id: operation_detail(
                    kind=kind,
                    scope="selected" if kind == "reprocess" else "rollback",
                ),
            ),
            patch(
                "paper_service.api.review_reprocess_request",
                return_value={"review_revision": 1},
            ) as reprocess_review,
            patch(
                "paper_service.api.review_rollback_request",
                return_value={"review_revision": 1},
            ) as rollback_review,
            patch("paper_service.api.enqueue_approved_reprocess") as reprocess_enqueue,
            patch("paper_service.api.enqueue_approved_rollback") as rollback_enqueue,
        ):
            reprocess = self.client.post(
                f"/api/admin/operations/reprocess/{REQUEST_ID}/reject",
                headers={ACTION_HEADER: "1"},
                json={"expected_revision": 0, "confirm_execution": False},
            )
            rollback = self.client.post(
                f"/api/admin/operations/rollback/{REQUEST_ID}/reject",
                headers={ACTION_HEADER: "1"},
                json={"expected_revision": 0, "confirm_execution": False},
            )

        self.assertEqual(reprocess.status_code, 200)
        self.assertEqual(rollback.status_code, 200)
        self.assertEqual(reprocess.json()["decision"], "rejected")
        self.assertEqual(rollback.json()["decision"], "rejected")
        self.assertEqual(reprocess_review.call_args.kwargs["decision"], "reject")
        self.assertEqual(rollback_review.call_args.kwargs["decision"], "reject")
        reprocess_enqueue.assert_not_called()
        rollback_enqueue.assert_not_called()

    def test_global_reprocess_cannot_be_approved_or_rejected_from_admin(self):
        with (
            patch.dict(os.environ, {"PAPER_WRITES_ENABLED": "true"}, clear=True),
            patch("paper_service.api._human_actor", return_value="human-1"),
            patch(
                "paper_service.api.get_operation_request_detail",
                return_value=operation_detail(scope="all"),
            ),
            patch("paper_service.api.review_reprocess_request") as review,
            patch("paper_service.api.enqueue_approved_reprocess") as enqueue,
        ):
            approve = self.client.post(
                f"/api/admin/operations/reprocess/{REQUEST_ID}/approve",
                headers={ACTION_HEADER: "1"},
                json={"expected_revision": 0, "confirm_execution": True},
            )
            reject = self.client.post(
                f"/api/admin/operations/reprocess/{REQUEST_ID}/reject",
                headers={ACTION_HEADER: "1"},
                json={"expected_revision": 0, "confirm_execution": False},
            )

        self.assertEqual(approve.status_code, 409)
        self.assertEqual(reject.status_code, 409)
        review.assert_not_called()
        enqueue.assert_not_called()

    def test_rollback_approval_uses_separate_human_review_and_enqueue(self):
        with (
            patch.dict(os.environ, {"PAPER_WRITES_ENABLED": "true"}, clear=True),
            patch("paper_service.api._human_actor", return_value="human-1"),
            patch(
                "paper_service.api.get_operation_request_detail",
                return_value=operation_detail(kind="rollback", scope="rollback"),
            ),
            patch(
                "paper_service.api.review_rollback_request",
                return_value={"review_revision": 1},
            ) as review,
            patch(
                "paper_service.api.enqueue_approved_rollback",
                return_value=({"status": "queued"},),
            ) as enqueue,
        ):
            response = self.client.post(
                f"/api/admin/operations/rollback/{REQUEST_ID}/approve",
                headers={ACTION_HEADER: "1"},
                json={"expected_revision": 0, "confirm_execution": True},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["decision"], "approved")
        review.assert_called_once()
        enqueue.assert_called_once_with(REQUEST_ID)

    def test_taxonomy_generation_requires_cost_ack_and_redacts_job_internals(self):
        with (
            patch.dict(os.environ, {"PAPER_WRITES_ENABLED": "true"}, clear=True),
            patch("paper_service.api._human_actor", return_value="human-1"),
            patch(
                "paper_service.api.enqueue_generation_for_audit",
                return_value={
                    "id": str(uuid4()),
                    "audit_id": str(AUDIT_ID),
                    "provider": "deepseek",
                    "model": "deepseek-reasoner",
                    "status": "queued",
                    "prompt": "private-prompt",
                    "lease_token": "private-lease",
                    "request_actor_id": "private-human-id",
                    "request_digest": "a" * 64,
                    "provider_body_digest": "b" * 64,
                },
            ) as enqueue,
        ):
            denied = self.client.post(
                f"/api/admin/taxonomy/audits/{AUDIT_ID}/generation",
                headers={ACTION_HEADER: "1"},
                json={"acknowledge_metadata_and_cost": False},
            )
            accepted = self.client.post(
                f"/api/admin/taxonomy/audits/{AUDIT_ID}/generation",
                headers={ACTION_HEADER: "1"},
                json={"acknowledge_metadata_and_cost": True},
            )

        self.assertEqual(denied.status_code, 422)
        self.assertEqual(accepted.status_code, 202)
        self.assertNotIn("private-prompt", accepted.text)
        self.assertNotIn("private-lease", accepted.text)
        self.assertNotIn("private-human-id", accepted.text)
        self.assertNotIn("request_digest", accepted.text)
        self.assertNotIn("provider_body_digest", accepted.text)
        enqueue.assert_called_once_with(
            AUDIT_ID,
            request_actor_kind="human",
            request_actor_id="human-1",
            metadata_and_cost_acknowledged=True,
        )

    def test_taxonomy_approval_requires_destructive_confirmation_and_human_actor(self):
        event = {
            "id": str(uuid4()),
            "proposal_id": str(PROPOSAL_ID),
            "review_revision": 1,
            "decision": "approve",
            "created_at": "2026-08-29T12:00:00Z",
            "actor_id": "private-actor",
            "comment": "private-comment",
        }
        with (
            patch.dict(os.environ, {"PAPER_WRITES_ENABLED": "true"}, clear=True),
            patch("paper_service.api._human_actor", return_value="human-1"),
            patch(
                "paper_service.api.review_taxonomy_proposal", return_value=event
            ) as review,
        ):
            denied = self.client.post(
                f"/api/admin/taxonomy/proposals/{PROPOSAL_ID}/approve",
                headers={ACTION_HEADER: "1"},
                json={"expected_revision": 0, "confirm_apply": False},
            )
            accepted = self.client.post(
                f"/api/admin/taxonomy/proposals/{PROPOSAL_ID}/approve",
                headers={ACTION_HEADER: "1"},
                json={"expected_revision": 0, "confirm_apply": True},
            )

        self.assertEqual(denied.status_code, 422)
        self.assertEqual(accepted.status_code, 200)
        self.assertNotIn("private-comment", accepted.text)
        self.assertEqual(review.call_args.kwargs["actor_kind"], "human")
        self.assertEqual(review.call_args.kwargs["actor_id"], "human-1")

    def test_taxonomy_rejection_is_human_attributed_and_needs_no_apply_confirmation(self):
        event = {
            "id": str(uuid4()),
            "proposal_id": str(PROPOSAL_ID),
            "review_revision": 1,
            "decision": "reject",
            "created_at": "2026-08-29T12:00:00Z",
        }
        with (
            patch.dict(os.environ, {"PAPER_WRITES_ENABLED": "true"}, clear=True),
            patch("paper_service.api._human_actor", return_value="human-1"),
            patch(
                "paper_service.api.review_taxonomy_proposal", return_value=event
            ) as review,
        ):
            response = self.client.post(
                f"/api/admin/taxonomy/proposals/{PROPOSAL_ID}/reject",
                headers={ACTION_HEADER: "1"},
                json={"expected_revision": 0, "confirm_apply": False},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["review"]["decision"], "reject")
        self.assertEqual(review.call_args.kwargs["decision"], "reject")
        self.assertEqual(review.call_args.kwargs["actor_kind"], "human")

    def test_writes_disabled_and_missing_human_identity_fail_before_mutation(self):
        with (
            patch.dict(os.environ, {"PAPER_WRITES_ENABLED": "false"}, clear=True),
            patch("paper_service.api.enqueue_generation_for_audit") as enqueue,
        ):
            disabled = self.client.post(
                f"/api/admin/taxonomy/audits/{AUDIT_ID}/generation",
                headers={ACTION_HEADER: "1"},
                json={"acknowledge_metadata_and_cost": True},
            )
        with (
            patch.dict(os.environ, {"PAPER_WRITES_ENABLED": "true"}, clear=True),
            patch("paper_service.api.enqueue_generation_for_audit") as enqueue_human,
        ):
            no_human = self.client.post(
                f"/api/admin/taxonomy/audits/{AUDIT_ID}/generation",
                headers={ACTION_HEADER: "1"},
                json={"acknowledge_metadata_and_cost": True},
            )

        self.assertEqual(disabled.status_code, 503)
        self.assertEqual(no_human.status_code, 403)
        enqueue.assert_not_called()
        enqueue_human.assert_not_called()


@unittest.skipUnless(FASTAPI_AVAILABLE, "FastAPI test dependency is not installed")
class AdminMiddlewareSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(api.app)

    def config(self):
        return SecurityConfig(
            access_enforced=True,
            mutation_protection=True,
            issuer="https://team.cloudflareaccess.com",
            audience="a" * 64,
            public_origin=PUBLIC_ORIGIN,
            clock_skew_seconds=0,
        )

    def test_service_identity_cannot_read_human_admin_namespace(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("paper_service.api.security_config_from_env", return_value=self.config()),
            patch(
                "paper_service.api.validate_access_token",
                return_value={
                    "kind": "service",
                    "service_id": "service-client.access",
                },
            ),
            patch("paper_service.api.get_operations_summary") as overview,
        ):
            response = self.client.get(
                "/api/admin/overview", headers={ACCESS_JWT_HEADER: "opaque-token"}
            )

        self.assertEqual(response.status_code, 403)
        overview.assert_not_called()

    def test_human_mutation_requires_origin_and_action_header(self):
        identity = {"kind": "human", "sub": "human-1", "email": "a@example.com"}
        with (
            patch.dict(os.environ, {"PAPER_WRITES_ENABLED": "true"}, clear=True),
            patch("paper_service.api.security_config_from_env", return_value=self.config()),
            patch("paper_service.api.validate_access_token", return_value=identity),
            patch(
                "paper_service.api.enqueue_generation_for_audit",
                return_value={"id": str(uuid4()), "status": "queued"},
            ) as enqueue,
        ):
            missing_action = self.client.post(
                f"/api/admin/taxonomy/audits/{AUDIT_ID}/generation",
                headers={
                    ACCESS_JWT_HEADER: "opaque-token",
                    "Origin": PUBLIC_ORIGIN,
                },
                json={"acknowledge_metadata_and_cost": True},
            )
            wrong_origin = self.client.post(
                f"/api/admin/taxonomy/audits/{AUDIT_ID}/generation",
                headers={
                    ACCESS_JWT_HEADER: "opaque-token",
                    ACTION_HEADER: "1",
                    "Origin": "https://attacker.example",
                },
                json={"acknowledge_metadata_and_cost": True},
            )
            accepted = self.client.post(
                f"/api/admin/taxonomy/audits/{AUDIT_ID}/generation",
                headers={
                    ACCESS_JWT_HEADER: "opaque-token",
                    ACTION_HEADER: "1",
                    "Origin": PUBLIC_ORIGIN,
                },
                json={"acknowledge_metadata_and_cost": True},
            )

        self.assertEqual(missing_action.status_code, 403)
        self.assertEqual(wrong_origin.status_code, 403)
        self.assertEqual(accepted.status_code, 202)
        enqueue.assert_called_once_with(
            AUDIT_ID,
            request_actor_kind="human",
            request_actor_id="human-1",
            metadata_and_cost_acknowledged=True,
        )


if __name__ == "__main__":
    unittest.main()

import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4


HUMAN_ACCESS_ENVIRONMENT = {
    "PAPER_ACCESS_ENFORCE": "true",
    "PAPER_BROWSER_MUTATION_PROTECTION": "true",
    "PAPER_ACCESS_TEAM_DOMAIN": "https://rapid-king-ed86.cloudflareaccess.com",
    "PAPER_ACCESS_AUDIENCE": "a" * 64,
    "PAPER_PUBLIC_ORIGIN": "https://papers.minjae-jo.com",
}
HUMAN_ACCESS_HEADERS = {
    "Cf-Access-Jwt-Assertion": "signed-token",
    "Origin": "https://papers.minjae-jo.com",
    "X-Paper-Action": "1",
}
HUMAN_ACCESS_IDENTITY = {
    "kind": "human",
    "email": "student@example.com",
    "sub": "access-user-123",
}


class PaperApiReadingStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from paper_service.api import app

        cls.client = TestClient(app)

    def test_get_returns_logical_revision_zero_before_first_write(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("paper_service.api.get_reader_paper", return_value={"id": 4}),
            patch("paper_service.api.get_reading_state", return_value=None),
        ):
            response = self.client.get("/api/papers/4/reading-state")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["revision"], 0)
        self.assertEqual(response.json()["status"], "unread")

    def test_patch_forwards_only_explicit_fields(self):
        stored = {
            "paper_id": 4,
            "status": "reading",
            "progress": 0.3,
            "revision": 2,
        }
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("paper_service.api.update_reading_state", return_value=stored) as update,
        ):
            response = self.client.patch(
                "/api/papers/4/reading-state",
                json={"expected_revision": 1, "progress": 0.3},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), stored)
        update.assert_called_once_with(4, expected_revision=1, progress=0.3)

    def test_patch_reports_revision_conflict_without_overwrite(self):
        from paper_service.database import ReadingStateConflict

        updated_at = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)
        current = {
            "paper_id": 4,
            "progress": 0.6,
            "revision": 8,
            "updated_at": updated_at,
        }
        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "paper_service.api.update_reading_state",
                side_effect=ReadingStateConflict(current),
            ),
        ):
            response = self.client.patch(
                "/api/papers/4/reading-state",
                json={"expected_revision": 7, "progress": 0.7},
            )

        self.assertEqual(response.status_code, 409)
        payload = response.json()["detail"]["current"]
        self.assertEqual(payload["paper_id"], 4)
        self.assertEqual(payload["updated_at"], updated_at.isoformat())

    def test_writes_can_be_disabled_during_rollout(self):
        with patch.dict(
            os.environ,
            {"PAPER_WRITES_ENABLED": "false"},
            clear=True,
        ):
            response = self.client.patch(
                "/api/papers/4/reading-state",
                json={"expected_revision": 0, "progress": 0.1},
            )

        self.assertEqual(response.status_code, 503)

        with patch.dict(
            os.environ,
            {"PAPER_WRITES_ENABLED": "false"},
            clear=True,
        ), patch("paper_service.api.queue_sync") as queue:
            sync_response = self.client.post("/jobs/sync")

        self.assertEqual(sync_response.status_code, 503)
        queue.assert_not_called()

    def test_source_pdf_does_not_depend_on_legacy_reader_status(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            source = data_dir / "library" / "raw" / "ab" / "paper.pdf"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"%PDF-1.4\n%%EOF\n")
            paper = {
                "id": 4,
                "stored_path": "ab/paper.pdf",
                "reader_status": "failed",
            }
            with (
                patch.dict(
                    os.environ,
                    {"PAPER_DATA_DIR": str(data_dir)},
                    clear=True,
                ),
                patch("paper_service.api.get_reader_paper", return_value=paper),
            ):
                response = self.client.get("/read/4/source.pdf")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content.startswith(b"%PDF"))

    def test_reader_controller_is_served_as_javascript(self):
        with patch.dict(os.environ, {}, clear=True):
            response = self.client.get("/static/reader-v2.js")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/javascript"))
        self.assertIn(b'"X-Paper-Action": "1"', response.content)

    def test_knowledge_review_page_is_mobile_safe_and_escapes_the_title(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "paper_service.api.get_reader_paper",
                return_value={"id": 4, "title": "Brain <script>alert(1)</script>"},
            ),
        ):
            response = self.client.get("/knowledge/4")
            controller = self.client.get("/static/knowledge.js")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"/static/knowledge.js", response.content)
        self.assertIn("30,000".encode(), response.content)
        self.assertIn("240,000".encode(), response.content)
        self.assertIn(b"DeepSeek Flash", response.content)
        self.assertIn(b'id="knowledge-consent"', response.content)
        self.assertIn(b"acknowledge_paper_text_and_cost: true", controller.content)
        self.assertNotIn(b"<script>alert(1)</script>", response.content)

    def test_search_page_and_controller_are_available(self):
        with patch.dict(os.environ, {}, clear=True):
            page = self.client.get("/search")
            controller = self.client.get("/static/search.js")

        self.assertEqual(page.status_code, 200)
        self.assertIn(b"/static/search.js", page.content)
        self.assertIn("DeepSeek".encode(), page.content)
        self.assertEqual(controller.status_code, 200)
        self.assertTrue(
            controller.headers["content-type"].startswith("text/javascript")
        )

    def test_search_results_include_paper_identity_without_model_call(self):
        hit = MagicMock()
        hit.paper_id = 4
        hit.to_dict.return_value = {
            "paper_id": 4,
            "chunk": {
                "source_kind": "source_text",
                "node_id": "n-" + "a" * 24,
            },
            "text": "Grounded result.",
        }
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("paper_service.api.search_papers", return_value=(hit,)) as search,
            patch(
                "paper_service.api.list_reader_papers",
                return_value=[{"id": 4, "title": "Brain paper"}],
            ),
        ):
            response = self.client.get(
                "/api/search?q=signal&include_personal_annotations=false"
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["hits"][0]["paper_title"], "Brain paper")
        self.assertFalse(search.call_args.kwargs["include_personal_annotations"])

    def test_question_job_is_fixed_to_flash_and_never_exposes_evidence_or_lease(self):
        job_id = uuid4()
        created = {
            "id": job_id,
            "paper_id": 4,
            "question": "What changed?",
            "provider": "deepseek",
            "model": "deepseek-v4-flash",
            "include_personal_annotations": False,
            "status": "queued",
            "attempt_count": 0,
            "max_attempts": 3,
            "request_actor_kind": "human",
            "request_actor_id": "access-user-123",
            "request_acknowledged": True,
            "lease_token": uuid4(),
            "evidence_snapshot": {"source_nodes": ["private"]},
        }
        with (
            patch.dict(os.environ, HUMAN_ACCESS_ENVIRONMENT, clear=True),
            patch(
                "paper_service.api.validate_access_token",
                return_value=HUMAN_ACCESS_IDENTITY,
            ),
            patch("paper_service.api.get_reader_paper", return_value={"id": 4}),
            patch("paper_service.api.enqueue_qa_job", return_value=created) as enqueue,
        ):
            response = self.client.post(
                "/api/papers/4/questions",
                headers=HUMAN_ACCESS_HEADERS,
                json={
                    "idempotency_key": str(uuid4()),
                    "question": "What changed?",
                    "include_personal_annotations": False,
                },
            )

        self.assertEqual(response.status_code, 202)
        public_job = response.json()["job"]
        self.assertNotIn("lease_token", public_job)
        self.assertNotIn("evidence_snapshot", public_job)
        self.assertNotIn("request_actor_id", public_job)
        self.assertEqual(enqueue.call_args.kwargs["model"], "deepseek-v4-flash")
        self.assertEqual(enqueue.call_args.kwargs["request_actor_kind"], "human")
        self.assertEqual(
            enqueue.call_args.kwargs["request_actor_id"], "access-user-123"
        )
        self.assertIs(enqueue.call_args.kwargs["request_acknowledged"], True)
        self.assertFalse(enqueue.call_args.kwargs["include_personal_annotations"])
        self.assertEqual(enqueue.call_args.kwargs["evidence_limit"], 32)

    def test_question_status_never_exposes_internal_prompt_or_lease(self):
        job_id = uuid4()
        stored = {
            "id": job_id,
            "paper_id": 4,
            "question": "What changed?",
            "provider": "deepseek",
            "model": "deepseek-v4-flash",
            "include_personal_annotations": False,
            "status": "running",
            "attempt_count": 1,
            "max_attempts": 3,
            "lease_token": uuid4(),
            "evidence_snapshot": {"source_nodes": ["private"]},
        }
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("paper_service.api.get_qa_job", return_value=stored),
        ):
            response = self.client.get(f"/api/papers/4/questions/{job_id}")

        self.assertEqual(response.status_code, 200)
        public_job = response.json()["job"]
        self.assertNotIn("lease_token", public_job)
        self.assertNotIn("evidence_snapshot", public_job)

    def test_knowledge_job_is_always_deepseek_pro_and_annotation_opt_in_is_explicit(self):
        created = {
            "id": str(uuid4()),
            "paper_id": 4,
            "status": "queued",
            "request_actor_kind": "human",
            "request_actor_id": "access-user-123",
            "paper_text_and_cost_acknowledged": True,
            "lease_token": str(uuid4()),
        }
        with (
            patch.dict(os.environ, HUMAN_ACCESS_ENVIRONMENT, clear=True),
            patch(
                "paper_service.api.validate_access_token",
                return_value=HUMAN_ACCESS_IDENTITY,
            ),
            patch("paper_service.api.get_reader_paper", return_value={"id": 4}),
            patch("paper_service.api.read_article", return_value={}),
            patch(
                "paper_service.api.enqueue_generation_job", return_value=created
            ) as enqueue,
        ):
            response = self.client.post(
                "/api/papers/4/knowledge/jobs",
                headers=HUMAN_ACCESS_HEADERS,
                json={
                    "idempotency_key": str(uuid4()),
                    "include_personal_annotations": False,
                    "acknowledge_paper_text_and_cost": True,
                },
            )

        self.assertEqual(response.status_code, 202)
        self.assertNotIn("request_actor_id", response.json()["job"])
        self.assertNotIn("lease_token", response.json()["job"])
        self.assertEqual(enqueue.call_args.kwargs["provider"], "deepseek")
        self.assertEqual(enqueue.call_args.kwargs["model"], "deepseek-v4-pro")
        self.assertEqual(enqueue.call_args.kwargs["request_actor_kind"], "human")
        self.assertEqual(
            enqueue.call_args.kwargs["request_actor_id"], "access-user-123"
        )
        self.assertIs(
            enqueue.call_args.kwargs["paper_text_and_cost_acknowledged"], True
        )
        self.assertFalse(
            enqueue.call_args.kwargs["policy"].include_personal_annotations
        )

    def test_knowledge_job_requires_explicit_paper_text_and_cost_acknowledgement(self):
        with (
            patch.dict(os.environ, HUMAN_ACCESS_ENVIRONMENT, clear=True),
            patch(
                "paper_service.api.validate_access_token",
                return_value=HUMAN_ACCESS_IDENTITY,
            ),
            patch("paper_service.api.enqueue_generation_job") as enqueue,
        ):
            response = self.client.post(
                "/api/papers/4/knowledge/jobs",
                headers=HUMAN_ACCESS_HEADERS,
                json={
                    "idempotency_key": str(uuid4()),
                    "acknowledge_paper_text_and_cost": False,
                },
            )
            coerced = self.client.post(
                "/api/papers/4/knowledge/jobs",
                headers=HUMAN_ACCESS_HEADERS,
                json={
                    "idempotency_key": str(uuid4()),
                    "acknowledge_paper_text_and_cost": "true",
                },
            )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            response.json()["detail"],
            "paper_text_and_cost_acknowledgement_required",
        )
        self.assertEqual(coerced.status_code, 422)
        enqueue.assert_not_called()

    def test_model_jobs_reject_service_identity_before_enqueue(self):
        service_identity = {"kind": "service", "service_id": "svc.access"}
        with (
            patch.dict(os.environ, HUMAN_ACCESS_ENVIRONMENT, clear=True),
            patch(
                "paper_service.api.validate_access_token",
                return_value=service_identity,
            ),
            patch("paper_service.api.enqueue_qa_job") as enqueue,
        ):
            response = self.client.post(
                "/api/papers/4/questions",
                headers=HUMAN_ACCESS_HEADERS,
                json={
                    "idempotency_key": str(uuid4()),
                    "question": "What changed?",
                },
            )

        self.assertEqual(response.status_code, 403)
        enqueue.assert_not_called()

    def test_human_approval_uses_verified_access_subject_not_request_body(self):
        revision_id = uuid4()
        reviewed = {"revision_id": str(uuid4()), "status": "reviewed"}
        access_environment = {
            "PAPER_ACCESS_ENFORCE": "true",
            "PAPER_BROWSER_MUTATION_PROTECTION": "true",
            "PAPER_ACCESS_TEAM_DOMAIN": "https://rapid-king-ed86.cloudflareaccess.com",
            "PAPER_ACCESS_AUDIENCE": "a" * 64,
            "PAPER_PUBLIC_ORIGIN": "https://papers.minjae-jo.com",
        }
        identity = {
            "kind": "human",
            "email": "student@example.com",
            "sub": "access-user-123",
        }
        with (
            patch.dict(os.environ, access_environment, clear=True),
            patch("paper_service.api.validate_access_token", return_value=identity),
            patch("paper_service.api.make_reviewed_copy", return_value=reviewed),
            patch(
                "paper_service.api.approve_draft_revision",
                return_value={"revision": reviewed, "review_event": {}, "outbox": {}},
            ) as approve,
        ):
            response = self.client.post(
                f"/api/knowledge/revisions/{revision_id}/approve",
                headers={
                    "Cf-Access-Jwt-Assertion": "signed-token",
                    "Origin": "https://papers.minjae-jo.com",
                    "X-Paper-Action": "1",
                },
                json={"expected_review_revision": 0, "comment": None},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(approve.call_args.kwargs["actor_kind"], "human")
        self.assertEqual(approve.call_args.kwargs["actor_id"], "access-user-123")

    def test_outbox_ack_binds_consumer_lease_and_payload_digest(self):
        outbox_id = uuid4()
        lease_token = uuid4()
        service_id = "a" * 32 + ".access"
        access_environment = {
            "PAPER_ACCESS_ENFORCE": "true",
            "PAPER_BROWSER_MUTATION_PROTECTION": "true",
            "PAPER_ACCESS_TEAM_DOMAIN": "https://rapid-king-ed86.cloudflareaccess.com",
            "PAPER_ACCESS_AUDIENCE": "a" * 64,
            "PAPER_PUBLIC_ORIGIN": "https://papers.minjae-jo.com",
            "PAPER_VAULT_SYNC_SERVICE_ID": service_id,
        }
        with (
            patch.dict(os.environ, access_environment, clear=True),
            patch(
                "paper_service.api.validate_access_token",
                return_value={"kind": "service", "service_id": service_id},
            ),
            patch(
                "paper_service.api.ack_outbox_item",
                return_value={"status": "delivered"},
            ) as ack,
        ):
            response = self.client.post(
                f"/api/knowledge/outbox/{outbox_id}/ack",
                headers={"Cf-Access-Jwt-Assertion": "signed-token"},
                json={
                    "consumer_id": "mac-mini",
                    "lease_token": str(lease_token),
                    "payload_sha256": "b" * 64,
                },
            )

        self.assertEqual(response.status_code, 200)
        ack.assert_called_once_with(
            outbox_id,
            worker_id="vault:mac-mini",
            lease_token=lease_token,
            expected_payload_sha256="b" * 64,
        )

    def test_outbox_lease_response_matches_the_mac_bridge_contract(self):
        from paper_service.knowledge import payload_sha256
        from paper_service.vault_bridge_client import validate_outbox_lease

        revision_id = str(uuid4())
        outbox_id = uuid4()
        lease_token = uuid4()
        reviewed = {
            "schema_version": 1,
            "selector_version": 1,
            "revision_id": revision_id,
            "status": "reviewed",
            "paper_identity": {"paper_id": 4, "title": "Paper"},
            "source_sha256": "b" * 64,
            "article_fingerprint": "c" * 64,
            "summary": "Summary.",
            "claims": [
                {
                    "text": "Claim.",
                    "evidence": [
                        {
                            "node_id": "n-" + "d" * 24,
                            "exact_quote": "Evidence.",
                        }
                    ],
                }
            ],
            "key_methods": [],
            "key_findings": [],
            "limitations": [],
        }
        item = {
            "id": outbox_id,
            "paper_id": 4,
            "revision_id": UUID(revision_id),
            "revision_payload_sha256": payload_sha256(reviewed),
            "lease_token": lease_token,
            "lease_expires_at": datetime(2026, 8, 29, 13, 0, tzinfo=timezone.utc),
            "canonical_payload": reviewed,
        }
        service_id = "a" * 32 + ".access"
        access_environment = {
            "PAPER_ACCESS_ENFORCE": "true",
            "PAPER_BROWSER_MUTATION_PROTECTION": "true",
            "PAPER_ACCESS_TEAM_DOMAIN": "https://rapid-king-ed86.cloudflareaccess.com",
            "PAPER_ACCESS_AUDIENCE": "a" * 64,
            "PAPER_PUBLIC_ORIGIN": "https://papers.minjae-jo.com",
            "PAPER_VAULT_SYNC_SERVICE_ID": service_id,
        }
        with (
            patch.dict(os.environ, access_environment, clear=True),
            patch(
                "paper_service.api.validate_access_token",
                return_value={"kind": "service", "service_id": service_id},
            ),
            patch("paper_service.api.claim_outbox_item", return_value=item),
        ):
            response = self.client.post(
                "/api/knowledge/outbox/lease",
                headers={"Cf-Access-Jwt-Assertion": "signed-token"},
                json={"consumer_id": "mac-mini", "lease_seconds": 300},
            )

        self.assertEqual(response.status_code, 200)
        lease = validate_outbox_lease(response.json())
        self.assertEqual(lease.outbox_id, str(outbox_id))

    def test_user_issue_endpoint_never_accepts_system_issue_fields(self):
        issue_id = uuid4()
        fingerprint = "a" * 64
        anchor = {
            "selector_version": 1,
            "scope": "article",
            "article_fingerprint": fingerprint,
        }
        created = {"id": str(issue_id), "source": "user", "revision": 1}
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("paper_service.api.create_reader_issue", return_value=created) as create,
        ):
            response = self.client.post(
                "/api/papers/4/issues",
                json={
                    "id": str(issue_id),
                    "kind": "figure_crop",
                    "description": "The crop omits panel C.",
                    "anchor": anchor,
                    "article_fingerprint": fingerprint,
                },
            )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["source"], "user")
        self.assertEqual(create.call_args.kwargs["source"], "user")
        self.assertNotIn("external_key", create.call_args.kwargs)

    def test_annotation_endpoint_uses_client_uuid_and_revision_zero(self):
        annotation_id = uuid4()
        fingerprint = "b" * 64
        anchor = {
            "selector_version": 1,
            "scope": "body",
            "section_id": "s-" + "1" * 24,
            "node_id": "n-" + "2" * 24,
            "start_utf16": 0,
            "end_utf16": 4,
            "exact_quote": "test",
            "prefix": "",
            "suffix": "",
            "article_fingerprint": fingerprint,
        }
        created = {"id": str(annotation_id), "kind": "note", "revision": 1}
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("paper_service.api.create_annotation", return_value=created) as create,
        ):
            response = self.client.post(
                "/api/papers/4/annotations",
                json={
                    "id": str(annotation_id),
                    "expected_revision": 0,
                    "kind": "note",
                    "anchor": anchor,
                    "article_fingerprint": fingerprint,
                    "body": "Remember this result.",
                },
            )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(create.call_args.args[:2], (annotation_id, 4))
        self.assertEqual(create.call_args.kwargs["expected_revision"], 0)
        self.assertIs(create.call_args.kwargs["ai_share_allowed"], False)

    def test_annotation_ai_sharing_does_not_coerce_strings(self):
        annotation_id = uuid4()
        fingerprint = "b" * 64
        anchor = {
            "selector_version": 1,
            "scope": "body",
            "section_id": "s-" + "1" * 24,
            "node_id": "n-" + "2" * 24,
            "start_utf16": 0,
            "end_utf16": 4,
            "exact_quote": "test",
            "prefix": "",
            "suffix": "",
            "article_fingerprint": fingerprint,
        }
        response = self.client.post(
            "/api/papers/4/annotations",
            json={
                "id": str(annotation_id),
                "expected_revision": 0,
                "kind": "note",
                "anchor": anchor,
                "article_fingerprint": fingerprint,
                "ai_share_allowed": "true",
            },
        )

        self.assertEqual(response.status_code, 422)

    def test_issue_and_annotation_lists_require_an_existing_paper(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("paper_service.api.get_reader_paper", return_value=None),
        ):
            issues = self.client.get("/api/papers/404/issues")
            annotations = self.client.get("/api/papers/404/annotations")

        self.assertEqual(issues.status_code, 404)
        self.assertEqual(annotations.status_code, 404)


if __name__ == "__main__":
    unittest.main()

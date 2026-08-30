from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import uuid4

from paper_service.knowledge import payload_sha256
from paper_service.vault_bridge_client import (
    ServiceCredentials,
    VaultBridgeClient,
    VaultBridgeError,
    load_service_credentials,
    sync_reviewed_knowledge_once,
    validate_outbox_lease,
)
from paper_service.vault_sync import ImportResult, VaultSyncError


FINGERPRINT = "a" * 64
SOURCE_SHA = "b" * 64
NODE_ID = "n-" + "c" * 24


def reviewed_payload() -> dict:
    return {
        "schema_version": 1,
        "selector_version": 1,
        "revision_id": str(uuid4()),
        "status": "reviewed",
        "paper_identity": {"paper_id": 7, "title": "Paper"},
        "source_sha256": SOURCE_SHA,
        "article_fingerprint": FINGERPRINT,
        "summary": "Summary.",
        "claims": [
            {
                "text": "A claim.",
                "evidence": [{"node_id": NODE_ID, "exact_quote": "evidence"}],
            }
        ],
        "key_methods": [],
        "key_findings": [],
        "limitations": [],
    }


def lease_envelope() -> dict:
    payload = reviewed_payload()
    return {
        "item": {
            "outbox_id": str(uuid4()),
            "paper_id": 7,
            "revision_id": payload["revision_id"],
            "payload_sha256": payload_sha256(payload),
            "lease_token": str(uuid4()),
            "lease_expires_at": "2026-08-29T13:00:00+00:00",
            "payload": payload,
        }
    }


class FakeResponse:
    def __init__(self, value):
        self.raw = io.BytesIO(json.dumps(value).encode("utf-8"))
        self.headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size):
        return self.raw.read(size)


class CredentialTests(unittest.TestCase):
    def test_credentials_require_absolute_private_regular_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            client_id = root / "client-id"
            secret = root / "secret"
            client_id.write_text("a" * 32 + ".access\n")
            secret.write_text("s" * 64 + "\n")
            client_id.chmod(0o600)
            secret.chmod(0o600)
            credentials = load_service_credentials(client_id, secret)
            self.assertTrue(credentials.client_id.endswith(".access"))
            self.assertEqual(len(credentials.client_secret), 64)

            secret.chmod(0o644)
            with self.assertRaisesRegex(VaultBridgeError, "secret_file_invalid"):
                load_service_credentials(client_id, secret)

    def test_credentials_tolerate_short_kernel_reads_without_truncation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            client_id = root / "client-id"
            secret = root / "secret"
            client_id.write_text("a" * 32 + ".access\n")
            secret.write_text("s" * 64 + "\n")
            client_id.chmod(0o600)
            secret.chmod(0o600)
            real_read = os.read

            def short_read(descriptor, size):
                return real_read(descriptor, min(size, 3))

            with patch("paper_service.vault_bridge_client.os.read", short_read):
                credentials = load_service_credentials(client_id, secret)

            self.assertEqual(credentials.client_id, "a" * 32 + ".access")
            self.assertEqual(credentials.client_secret, "s" * 64)

    def test_direct_credential_objects_are_validated_and_secret_repr_is_redacted(self):
        credentials = ServiceCredentials("a" * 32 + ".access", "s" * 64)
        self.assertNotIn("s" * 64, repr(credentials))
        with self.assertRaisesRegex(VaultBridgeError, "client_id_invalid"):
            ServiceCredentials("invalid", "s" * 64)


class LeaseContractTests(unittest.TestCase):
    def test_reviewed_payload_and_all_transport_bindings_are_verified(self):
        value = lease_envelope()
        lease = validate_outbox_lease(value)
        self.assertIsNotNone(lease)
        self.assertEqual(lease.paper_id, 7)

        tampered = lease_envelope()
        tampered["item"]["payload"]["summary"] = "Changed."
        with self.assertRaisesRegex(VaultBridgeError, "digest_mismatch"):
            validate_outbox_lease(tampered)

        draft = lease_envelope()
        draft["item"]["payload"]["status"] = "ai-draft"
        with self.assertRaisesRegex(VaultBridgeError, "not_reviewed"):
            validate_outbox_lease(draft)

    def test_empty_outbox_is_distinct_from_an_invalid_envelope(self):
        self.assertIsNone(validate_outbox_lease({"item": None}))
        with self.assertRaises(VaultBridgeError):
            validate_outbox_lease({"items": []})


class BridgeClientTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.responses = [lease_envelope()]

        def open_request(request, timeout):
            self.calls.append((request, timeout))
            return FakeResponse(self.responses.pop(0))

        self.credentials = ServiceCredentials("a" * 32 + ".access", "s" * 64)
        self.client = VaultBridgeClient(
            credentials=self.credentials,
            consumer_id="mac-mini",
            open_request=open_request,
        )

    def test_lease_sends_service_credentials_only_as_headers(self):
        lease = self.client.lease()
        self.assertIsNotNone(lease)
        request, timeout = self.calls[0]
        self.assertEqual(timeout, 30)
        self.assertEqual(request.method, "POST")
        self.assertEqual(
            request.full_url,
            "https://papers.minjae-jo.com/api/knowledge/outbox/lease",
        )
        self.assertEqual(
            request.get_header("Cf-access-client-id"), self.credentials.client_id
        )
        self.assertEqual(
            request.get_header("Cf-access-client-secret"),
            self.credentials.client_secret,
        )
        self.assertNotIn(self.credentials.client_secret.encode(), request.data)

    def test_successful_import_is_acknowledged_after_writer_returns(self):
        envelope = self.responses[0]
        self.responses.append({"status": "delivered"})
        writer = Mock()
        writer.import_payload.return_value = ImportResult(
            result="created",
            revision_id=envelope["item"]["revision_id"],
            relative_path="Research/Papers/paper.md",
            payload_sha256=envelope["item"]["payload_sha256"],
            markdown_sha256="d" * 64,
        )

        result = sync_reviewed_knowledge_once(self.client, writer)

        self.assertEqual(result.status, "delivered")
        self.assertEqual(result.import_result, "created")
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(self.calls[1][0].full_url.endswith("/ack"))

    def test_failed_import_is_nacked_and_never_acknowledged(self):
        self.responses.append({"status": "pending"})
        writer = Mock()
        writer.import_payload.side_effect = VaultSyncError("target_collision")

        with self.assertRaisesRegex(VaultBridgeError, "vault_import_failed"):
            sync_reviewed_knowledge_once(self.client, writer)

        self.assertEqual(len(self.calls), 2)
        self.assertTrue(self.calls[1][0].full_url.endswith("/nack"))
        body = json.loads(self.calls[1][0].data)
        self.assertEqual(body["error_code"], "target_collision")

    def test_terminal_nack_response_is_accepted_after_server_attempt_limit(self):
        lease = self.client.lease()
        self.responses.append({"status": "dead"})
        self.client.reject(lease, error_code="target_collision")
        self.assertTrue(self.calls[-1][0].full_url.endswith("/nack"))

    def test_origin_consumer_and_lease_bounds_are_strict(self):
        with self.assertRaisesRegex(VaultBridgeError, "origin_invalid"):
            VaultBridgeClient(
                credentials=self.credentials,
                consumer_id="mac-mini",
                origin="https://attacker.invalid",
            )
        with self.assertRaisesRegex(VaultBridgeError, "consumer_id_invalid"):
            VaultBridgeClient(
                credentials=self.credentials,
                consumer_id="../../escape",
            )
        with self.assertRaisesRegex(VaultBridgeError, "lease_seconds_invalid"):
            self.client.lease(lease_seconds=1)


if __name__ == "__main__":
    unittest.main()

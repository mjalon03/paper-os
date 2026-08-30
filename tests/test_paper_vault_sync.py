import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import paper_service.vault_sync as vault_sync
from paper_service.vault_sync import (
    VAULT_SUBTREE,
    VaultSyncError,
    VaultSyncWriter,
    plan_vault_import,
)
from paper_service.knowledge import KnowledgeValidationError
REVISION_ID = "95ccac26-bc11-4d43-9e7f-53fcc6ce8501"
SECOND_REVISION_ID = "7f22451f-a2b6-4542-b330-7ef508b7ff9f"
ANNOTATION_ID = "e3759332-6ac1-46b8-808c-c8be2e5b206e"
NODE_ID = "n-0123456789abcdef01234567"


def knowledge_payload():
    return {
        "schema_version": 1,
        "selector_version": 1,
        "revision_id": REVISION_ID,
        "status": "reviewed",
        "paper_identity": {
            "paper_id": 7,
            "title": "Neural signals & robust models",
            "doi": "10.1234/example",
            "authors": ["Min Kim", "Ada Lee"],
            "year": 2026,
        },
        "source_sha256": "a" * 64,
        "article_fingerprint": "b" * 64,
        "summary": "A bounded synthesis of the reviewed paper.",
        "claims": [
            {
                "text": "The reported signal increased.",
                "evidence": [
                    {
                        "node_id": NODE_ID,
                        "exact_quote": "The signal increased by 12 percent.",
                    }
                ],
            }
        ],
        "key_methods": ["Cross-validated encoding model"],
        "key_findings": ["Signal increase was reproducible"],
        "limitations": ["Single-site cohort"],
        "user_annotation_ids": [ANNOTATION_ID],
    }


class PaperVaultSyncTests(unittest.TestCase):
    def _paths(self, directory: Path):
        vault = directory / "vault"
        vault.mkdir()
        ledger_directory = directory / "state"
        ledger_directory.mkdir()
        return vault, ledger_directory / "paper-knowledge-ledger.json"

    def test_create_only_import_is_atomic_and_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            vault, ledger = self._paths(directory)
            payload = knowledge_payload()
            writer = VaultSyncWriter(vault, ledger)
            created = writer.import_payload(payload)
            self.assertEqual(created.result, "created")
            target = vault / created.relative_path
            self.assertTrue(target.is_file())
            self.assertEqual(
                hashlib.sha256(target.read_bytes()).hexdigest(), created.markdown_sha256
            )
            self.assertFalse(list(target.parent.glob(".paper-knowledge-*.tmp")))
            ledger_value = json.loads(ledger.read_text(encoding="utf-8"))
            self.assertEqual(
                ledger_value["imports"][REVISION_ID]["relative_path"],
                created.relative_path,
            )
            again = writer.import_payload(payload)
            self.assertEqual(again.result, "already_imported")
            self.assertEqual(again.revision_id, created.revision_id)
            self.assertEqual(again.relative_path, created.relative_path)
            self.assertEqual(again.payload_sha256, created.payload_sha256)
            self.assertEqual(again.markdown_sha256, created.markdown_sha256)

    def test_revision_id_is_immutable_and_a_second_revision_is_create_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            vault, ledger = self._paths(directory)
            writer = VaultSyncWriter(vault, ledger)
            writer.import_payload(knowledge_payload())
            changed = knowledge_payload()
            changed["summary"] = "Changed under the same revision ID."
            with self.assertRaisesRegex(VaultSyncError, "revision_id_reused"):
                writer.import_payload(changed)
            next_revision = knowledge_payload()
            next_revision["revision_id"] = SECOND_REVISION_ID
            with self.assertRaisesRegex(VaultSyncError, "paper_already_imported_create_only"):
                writer.import_payload(next_revision)

    def test_changed_note_and_corrupt_ledger_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            vault, ledger = self._paths(directory)
            payload = knowledge_payload()
            writer = VaultSyncWriter(vault, ledger)
            created = writer.import_payload(payload)
            target = vault / created.relative_path
            target.write_text("changed outside the importer", encoding="utf-8")
            with self.assertRaisesRegex(VaultSyncError, "imported_target_changed"):
                writer.import_payload(payload)
            self.assertEqual(
                target.read_text(encoding="utf-8"), "changed outside the importer"
            )

            ledger.write_text("[" * 2_000 + "]" * 2_000, encoding="utf-8")
            with self.assertRaisesRegex(VaultSyncError, "ledger_json_too_deep"):
                writer.import_payload(payload)
            self.assertEqual(
                target.read_text(encoding="utf-8"), "changed outside the importer"
            )

            ledger.write_text(
                '{"schema_version":1,"imports":{},"imports":{},"papers":{}}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(VaultSyncError, "ledger_duplicate_key"):
                writer.import_payload(payload)
            self.assertEqual(
                target.read_text(encoding="utf-8"), "changed outside the importer"
            )

    def test_nonempty_ledger_never_recreates_a_missing_vault_subtree(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            vault, ledger = self._paths(directory)
            writer = VaultSyncWriter(vault, ledger)
            writer.import_payload(knowledge_payload())
            papers = vault / VAULT_SUBTREE
            moved = directory / "temporarily-unavailable-papers"
            papers.rename(moved)
            second = knowledge_payload()
            second["paper_identity"]["paper_id"] = 8
            second["paper_identity"]["title"] = "A second paper"
            second["revision_id"] = SECOND_REVISION_ID
            with self.assertRaisesRegex(
                VaultSyncError, "ledger_history_targets_unavailable"
            ):
                writer.import_payload(second)
            self.assertFalse(papers.exists())
            self.assertTrue(moved.is_dir())

    def test_existing_different_target_is_a_collision(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            vault, ledger = self._paths(directory)
            payload = knowledge_payload()
            plan = plan_vault_import(payload)
            target = vault / plan.relative_path
            target.parent.mkdir(parents=True)
            target.write_text("unrelated existing note", encoding="utf-8")
            with self.assertRaisesRegex(VaultSyncError, "target_collision"):
                VaultSyncWriter(vault, ledger).import_payload(payload)
            self.assertEqual(target.read_text(encoding="utf-8"), "unrelated existing note")
            self.assertFalse(ledger.exists())

    def test_identical_target_without_ledger_recovers_after_interruption(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            vault, ledger = self._paths(directory)
            payload = knowledge_payload()
            writer = VaultSyncWriter(vault, ledger)
            with patch("paper_service.vault_sync._write_ledger", side_effect=OSError("stop")):
                with self.assertRaises(OSError):
                    writer.import_payload(payload)
            self.assertFalse(ledger.exists())
            result = writer.import_payload(payload)
            self.assertEqual(result.result, "recovered")
            self.assertTrue(ledger.is_file())

    def test_crash_orphan_cannot_be_rebound_to_changed_payload(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            vault, ledger = self._paths(directory)
            payload = knowledge_payload()
            writer = VaultSyncWriter(vault, ledger)
            with patch("paper_service.vault_sync._write_ledger", side_effect=OSError("stop")):
                with self.assertRaises(OSError):
                    writer.import_payload(payload)
            changed = knowledge_payload()
            changed["paper_identity"]["title"] = "Changed after the crash"
            with self.assertRaisesRegex(VaultSyncError, "target_collision"):
                writer.import_payload(changed)
            notes = list((vault / VAULT_SUBTREE).glob("*.md"))
            self.assertEqual(len(notes), 1)
            self.assertFalse(ledger.exists())

    def test_interruption_before_publish_removes_temporary_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            vault, ledger = self._paths(directory)
            payload = knowledge_payload()
            plan = plan_vault_import(payload)
            with patch(
                "paper_service.vault_sync._rename_no_replace", side_effect=OSError("stop")
            ):
                with self.assertRaises(OSError):
                    VaultSyncWriter(vault, ledger).import_payload(payload)
            target = vault / plan.relative_path
            self.assertFalse(target.exists())
            self.assertFalse(list((vault / VAULT_SUBTREE).glob(".paper-knowledge-*.tmp")))
            self.assertFalse(ledger.exists())

    def test_publish_race_never_replaces_an_existing_note(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            vault, ledger = self._paths(directory)
            payload = knowledge_payload()
            plan = plan_vault_import(payload)
            original_publish = vault_sync._rename_no_replace

            def create_racing_target(directory_descriptor, source_name, target_name):
                descriptor = os.open(
                    target_name,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                    dir_fd=directory_descriptor,
                )
                try:
                    os.write(descriptor, b"created by another writer")
                finally:
                    os.close(descriptor)
                return original_publish(
                    directory_descriptor, source_name, target_name
                )

            with patch(
                "paper_service.vault_sync._rename_no_replace",
                side_effect=create_racing_target,
            ):
                with self.assertRaisesRegex(VaultSyncError, "target_collision"):
                    VaultSyncWriter(vault, ledger).import_payload(payload)
            target = vault / plan.relative_path
            self.assertEqual(
                target.read_text(encoding="utf-8"), "created by another writer"
            )
            self.assertFalse(ledger.exists())

    def test_subtree_symlink_swap_cannot_publish_outside_vault(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            vault, ledger = self._paths(directory)
            payload = knowledge_payload()
            plan = plan_vault_import(payload)
            original_publish = vault_sync._rename_no_replace
            moved = directory / "moved-papers"
            outside = directory / "outside"
            outside.mkdir()

            def swap_subtree(directory_descriptor, source_name, target_name):
                papers = vault / VAULT_SUBTREE
                papers.rename(moved)
                papers.symlink_to(outside, target_is_directory=True)
                return original_publish(
                    directory_descriptor, source_name, target_name
                )

            with patch(
                "paper_service.vault_sync._rename_no_replace",
                side_effect=swap_subtree,
            ):
                with self.assertRaisesRegex(VaultSyncError, "vault_subtree_changed"):
                    VaultSyncWriter(vault, ledger).import_payload(payload)
            filename = Path(plan.relative_path).name
            self.assertFalse((moved / filename).exists())
            self.assertFalse((outside / filename).exists())
            self.assertFalse(ledger.exists())

    def test_mismatch_rollback_never_deletes_a_replacement_note(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            vault, ledger = self._paths(directory)
            payload = knowledge_payload()
            plan = plan_vault_import(payload)
            original_publish = vault_sync._rename_no_replace
            moved = directory / "moved-papers"
            outside = directory / "outside"
            outside.mkdir()

            def swap_publish_and_replace(
                directory_descriptor, source_name, target_name
            ):
                papers = vault / VAULT_SUBTREE
                papers.rename(moved)
                papers.symlink_to(outside, target_is_directory=True)
                original_publish(directory_descriptor, source_name, target_name)
                os.unlink(target_name, dir_fd=directory_descriptor)
                descriptor = os.open(
                    target_name,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                    dir_fd=directory_descriptor,
                )
                try:
                    os.write(descriptor, b"replacement note")
                finally:
                    os.close(descriptor)

            with patch(
                "paper_service.vault_sync._rename_no_replace",
                side_effect=swap_publish_and_replace,
            ):
                with self.assertRaisesRegex(VaultSyncError, "vault_subtree_changed"):
                    VaultSyncWriter(vault, ledger).import_payload(payload)
            filename = Path(plan.relative_path).name
            self.assertEqual((moved / filename).read_bytes(), b"replacement note")
            self.assertFalse((outside / filename).exists())
            self.assertFalse(ledger.exists())

    def test_traversal_and_symlink_targets_are_rejected(self):
        payload = knowledge_payload()
        unsafe = copy.deepcopy(payload)
        unsafe["paper_identity"]["paper_id"] = "../outside"
        with self.assertRaises(KnowledgeValidationError):
            plan_vault_import(unsafe)

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            real_vault = directory / "real-vault"
            real_vault.mkdir()
            alias = directory / "vault-alias"
            alias.symlink_to(real_vault, target_is_directory=True)
            ledger_directory = directory / "state"
            ledger_directory.mkdir()
            with self.assertRaisesRegex(VaultSyncError, "vault_root_invalid"):
                VaultSyncWriter(alias, ledger_directory / "ledger.json").import_payload(payload)

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            vault, ledger = self._paths(directory)
            outside = directory / "outside"
            outside.mkdir()
            (vault / "Research").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(VaultSyncError, "vault_subtree_symlink"):
                VaultSyncWriter(vault, ledger).import_payload(payload)

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            vault, ledger = self._paths(directory)
            plan = plan_vault_import(payload)
            target = vault / plan.relative_path
            target.parent.mkdir(parents=True)
            outside = directory / "outside.md"
            outside.write_text("outside", encoding="utf-8")
            target.symlink_to(outside)
            with self.assertRaisesRegex(VaultSyncError, "target_symlink"):
                VaultSyncWriter(vault, ledger).import_payload(payload)
            self.assertEqual(outside.read_text(encoding="utf-8"), "outside")

    def test_ledger_inside_vault_or_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            vault, ledger = self._paths(directory)
            payload = knowledge_payload()
            with self.assertRaisesRegex(VaultSyncError, "ledger_must_be_outside_vault"):
                VaultSyncWriter(vault, vault / "ledger.json").import_payload(payload)

            actual = directory / "actual-ledger.json"
            actual.write_text("{}", encoding="utf-8")
            ledger.symlink_to(actual)
            with self.assertRaisesRegex(VaultSyncError, "ledger_file_invalid"):
                VaultSyncWriter(vault, ledger).import_payload(payload)

    def test_filesystem_root_is_never_accepted_as_a_vault(self):
        with tempfile.TemporaryDirectory() as temporary:
            ledger = Path(temporary) / "ledger.json"
            with self.assertRaisesRegex(VaultSyncError, "vault_root_too_broad"):
                VaultSyncWriter(Path("/"), ledger).import_payload(knowledge_payload())


if __name__ == "__main__":
    unittest.main()

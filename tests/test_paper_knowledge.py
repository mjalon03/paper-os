import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from paper_service.knowledge import (
    KnowledgeValidationError,
    canonical_payload_bytes,
    load_knowledge_payload,
    payload_sha256,
    render_knowledge_markdown,
    safe_knowledge_filename,
    validate_knowledge_payload,
)
from paper_service.knowledge_cli import main as knowledge_cli_main
from paper_service.vault_sync import plan_vault_import


REVISION_ID = "95ccac26-bc11-4d43-9e7f-53fcc6ce8501"
ANNOTATION_ID = "e3759332-6ac1-46b8-808c-c8be2e5b206e"
NODE_ID = "n-0123456789abcdef01234567"


def knowledge_payload(*, status="reviewed"):
    return {
        "schema_version": 1,
        "selector_version": 1,
        "revision_id": REVISION_ID,
        "status": status,
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


class PaperKnowledgeSchemaTests(unittest.TestCase):
    def test_canonical_payload_is_strict_deterministic_and_does_not_mutate_input(self):
        payload = knowledge_payload()
        original = copy.deepcopy(payload)
        canonical = validate_knowledge_payload(payload)
        self.assertEqual(payload, original)
        self.assertEqual(canonical["revision_id"], REVISION_ID)
        first = canonical_payload_bytes(payload)
        reordered = {key: payload[key] for key in reversed(payload)}
        self.assertEqual(first, canonical_payload_bytes(reordered))
        self.assertEqual(payload_sha256(payload), payload_sha256(reordered))

    def test_claims_without_exact_evidence_are_rejected(self):
        for evidence in ([], [{"node_id": NODE_ID, "exact_quote": ""}], [{}]):
            with self.subTest(evidence=evidence):
                payload = knowledge_payload()
                payload["claims"][0]["evidence"] = evidence
                with self.assertRaises(KnowledgeValidationError):
                    validate_knowledge_payload(payload)

    def test_source_node_validation_proves_node_and_verbatim_quote(self):
        payload = knowledge_payload()
        canonical = validate_knowledge_payload(
            payload,
            source_nodes={NODE_ID: "Before. The signal increased by 12 percent. After."},
        )
        self.assertEqual(canonical["claims"][0]["evidence"][0]["node_id"], NODE_ID)
        with self.assertRaisesRegex(KnowledgeValidationError, "quote_mismatch"):
            validate_knowledge_payload(payload, source_nodes={NODE_ID: "Different text."})
        with self.assertRaisesRegex(KnowledgeValidationError, "node_missing"):
            validate_knowledge_payload(payload, source_nodes={})

    def test_unknown_fields_invalid_hashes_ids_and_duplicates_fail_closed(self):
        cases = []
        unknown = knowledge_payload()
        unknown["unreviewed_field"] = True
        cases.append(unknown)
        bad_hash = knowledge_payload()
        bad_hash["source_sha256"] = "A" * 64
        cases.append(bad_hash)
        bad_revision = knowledge_payload()
        bad_revision["revision_id"] = "not-a-uuid"
        cases.append(bad_revision)
        bad_node = knowledge_payload()
        bad_node["claims"][0]["evidence"][0]["node_id"] = "../node"
        cases.append(bad_node)
        duplicate_annotation = knowledge_payload()
        duplicate_annotation["user_annotation_ids"].append(ANNOTATION_ID)
        cases.append(duplicate_annotation)
        boolean_schema = knowledge_payload()
        boolean_schema["schema_version"] = True
        cases.append(boolean_schema)
        future_selectors = knowledge_payload()
        future_selectors["selector_version"] = 2
        cases.append(future_selectors)
        unhashable_status = knowledge_payload()
        unhashable_status["status"] = {"reviewed": True}
        cases.append(unhashable_status)
        invalid_key = knowledge_payload()
        invalid_key[7] = "invalid JSON-style key"
        cases.append(invalid_key)
        for payload in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(KnowledgeValidationError):
                    validate_knowledge_payload(payload)
        with self.assertRaisesRegex(KnowledgeValidationError, "source_nodes_invalid"):
            validate_knowledge_payload(knowledge_payload(), source_nodes=[])

    def test_json_loader_rejects_duplicate_keys_and_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            duplicate = directory / "duplicate.json"
            duplicate.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
            with self.assertRaisesRegex(KnowledgeValidationError, "duplicate_key"):
                load_knowledge_payload(duplicate)
            actual = directory / "actual.json"
            actual.write_text(json.dumps(knowledge_payload()), encoding="utf-8")
            alias = directory / "alias.json"
            alias.symlink_to(actual)
            with self.assertRaisesRegex(KnowledgeValidationError, "payload_file_invalid"):
                load_knowledge_payload(alias)

    def test_json_loader_bounds_depth_and_number_parsing(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            deep = directory / "deep.json"
            deep.write_text("[" * 2_000 + "]" * 2_000, encoding="utf-8")
            with self.assertRaisesRegex(KnowledgeValidationError, "too_deep"):
                load_knowledge_payload(deep)
            huge_integer = directory / "integer.json"
            huge_integer.write_text("9" * 100_000, encoding="utf-8")
            with self.assertRaisesRegex(KnowledgeValidationError, "integer_too_long"):
                load_knowledge_payload(huge_integer)
            floating = directory / "float.json"
            floating.write_text("1.5", encoding="utf-8")
            with self.assertRaisesRegex(KnowledgeValidationError, "float_unsupported"):
                load_knowledge_payload(floating)

    def test_renderer_is_deterministic_marks_status_and_cites_source_nodes(self):
        payload = knowledge_payload()
        first = render_knowledge_markdown(payload)
        self.assertEqual(first, render_knowledge_markdown(payload))
        self.assertIn("[!success] Reviewed knowledge", first)
        self.assertIn(f"Source node `{NODE_ID}`", first)
        self.assertIn("Exact quote: “The signal increased by 12 percent.”", first)
        self.assertIn("Source PDF SHA-256: `" + "a" * 64 + "`", first)
        self.assertIn("Article fingerprint: `" + "b" * 64 + "`", first)
        self.assertIn("Selector version: `1`", first)
        draft = knowledge_payload(status="ai-draft")
        self.assertIn("AI draft — not a source of truth", render_knowledge_markdown(draft))
        no_authors = knowledge_payload()
        no_authors["paper_identity"]["authors"] = []
        self.assertIn("\nauthors: []\n", render_knowledge_markdown(no_authors))

    def test_renderer_escapes_active_html_and_markdown_structure(self):
        payload = knowledge_payload()
        payload["paper_identity"]["title"] = "# Heading <script>alert(1)</script>"
        payload["summary"] = (
            "> [!danger]\n<script>alert(2)</script>\n"
            "%% hidden %% #tag ^block $math$"
        )
        rendered = render_knowledge_markdown(payload)
        self.assertNotIn("<script>", rendered)
        self.assertIn("&lt;script&gt;", rendered)
        self.assertIn("# \\# Heading", rendered)
        self.assertNotIn("> [!danger]", rendered)
        self.assertIn("&gt; \\[!danger\\]", rendered)
        self.assertNotIn("%% hidden %%", rendered)
        self.assertNotIn(" #tag", rendered)
        self.assertNotIn(" ^block", rendered)
        self.assertNotIn("$math$", rendered)

    def test_safe_filename_removes_path_characters_and_honors_utf8_bound(self):
        payload = knowledge_payload()
        payload["paper_identity"]["title"] = "../Neuro: paper\\title? " + "뇌" * 200
        filename = safe_knowledge_filename(payload)
        self.assertNotIn("/", filename)
        self.assertNotIn("\\", filename)
        self.assertNotIn(":", filename)
        self.assertLessEqual(len(filename.encode("utf-8")), 180)
        self.assertTrue(filename.startswith("Paper 7 -- "))
        self.assertTrue(filename.endswith(".md"))

    def test_filename_namespace_distinguishes_typed_and_casefolded_ids(self):
        integer = knowledge_payload()
        string = knowledge_payload()
        string["paper_identity"]["paper_id"] = "7"
        upper = knowledge_payload()
        upper["paper_identity"]["paper_id"] = "ABC"
        lower = knowledge_payload()
        lower["paper_identity"]["paper_id"] = "abc"
        self.assertNotEqual(
            safe_knowledge_filename(integer), safe_knowledge_filename(string)
        )
        self.assertIn("\npaper_id: 7\n", render_knowledge_markdown(integer))
        self.assertIn("\npaper_id: \"7\"\n", render_knowledge_markdown(string))
        self.assertNotEqual(
            safe_knowledge_filename(upper).casefold(),
            safe_knowledge_filename(lower).casefold(),
        )

    def test_direct_call_payload_budget_rejects_oversized_and_aliased_input(self):
        oversized = knowledge_payload()
        oversized["summary"] = "x" * 2_000_001
        with self.assertRaisesRegex(KnowledgeValidationError, "payload_too_large"):
            validate_knowledge_payload(oversized)
        aliased = knowledge_payload()
        same = ["shared"]
        aliased["key_methods"] = same
        aliased["limitations"] = same
        with self.assertRaisesRegex(KnowledgeValidationError, "payload_alias_or_cycle"):
            validate_knowledge_payload(aliased)

    def test_identity_metadata_is_single_line(self):
        for field, value in (
            ("title", "Title\nInjected"),
            ("doi", "10.1/example\nstatus: reviewed"),
        ):
            payload = knowledge_payload()
            payload["paper_identity"][field] = value
            with self.subTest(field=field):
                with self.assertRaisesRegex(KnowledgeValidationError, "single_line"):
                    validate_knowledge_payload(payload)
        payload = knowledge_payload()
        payload["paper_identity"]["authors"] = ["Author\nInjected"]
        with self.assertRaisesRegex(KnowledgeValidationError, "single_line"):
            validate_knowledge_payload(payload)

    def test_optional_null_identity_fields_canonicalize_to_omission(self):
        omitted = knowledge_payload()
        omitted["paper_identity"].pop("doi")
        omitted["paper_identity"].pop("year")
        explicit_null = knowledge_payload()
        explicit_null["paper_identity"]["doi"] = None
        explicit_null["paper_identity"]["year"] = None
        self.assertEqual(payload_sha256(omitted), payload_sha256(explicit_null))
        rendered = render_knowledge_markdown(omitted)
        self.assertEqual(rendered, render_knowledge_markdown(explicit_null))
        self.assertIn(
            f"canonical_payload_sha256: \"{payload_sha256(omitted)}\"", rendered
        )


class PaperKnowledgeCliTests(unittest.TestCase):
    def _payload_file(self, directory: Path, *, status="reviewed") -> Path:
        path = directory / "payload.json"
        path.write_text(
            json.dumps(knowledge_payload(status=status), ensure_ascii=False),
            encoding="utf-8",
        )
        return path

    def test_validate_and_import_dry_run_make_no_vault_or_ledger_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            payload = self._payload_file(directory)
            vault = directory / "vault"
            vault.mkdir()
            ledger = directory / "ledger.json"
            output = io.StringIO()
            with redirect_stdout(output):
                code = knowledge_cli_main(["validate", str(payload)])
            self.assertEqual(code, 0)
            validation = json.loads(output.getvalue())
            self.assertEqual(validation["status"], "valid")
            self.assertIn("server-owned", validation["validation_scope"])

            output = io.StringIO()
            with redirect_stdout(output):
                code = knowledge_cli_main(
                    [
                        "import",
                        str(payload),
                        "--vault-root",
                        str(vault),
                        "--ledger",
                        str(ledger),
                    ]
                )
            self.assertEqual(code, 0)
            plan = json.loads(output.getvalue())
            self.assertEqual(plan["mode"], "dry-run")
            self.assertEqual(plan["filesystem_preflight"], "ready_new_subtree")
            self.assertIn("server-owned", plan["validation_scope"])
            self.assertFalse((vault / "Research").exists())
            self.assertFalse(ledger.exists())

    def test_cli_dry_run_detects_collision_without_writing(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            payload_path = self._payload_file(directory)
            payload = knowledge_payload()
            vault = directory / "vault"
            vault.mkdir()
            ledger = directory / "ledger.json"
            target = vault / plan_vault_import(payload).relative_path
            target.parent.mkdir(parents=True)
            target.write_text("existing note", encoding="utf-8")
            error = io.StringIO()
            with redirect_stderr(error):
                code = knowledge_cli_main(
                    [
                        "import",
                        str(payload_path),
                        "--vault-root",
                        str(vault),
                        "--ledger",
                        str(ledger),
                    ]
                )
            self.assertEqual(code, 2)
            self.assertIn("target_collision", error.getvalue())
            self.assertEqual(target.read_text(encoding="utf-8"), "existing note")
            self.assertFalse(ledger.exists())

    def test_cli_rejects_ai_draft_import(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            payload = self._payload_file(directory, status="ai-draft")
            vault = directory / "vault"
            vault.mkdir()
            error = io.StringIO()
            with redirect_stderr(error):
                code = knowledge_cli_main(
                    [
                        "import",
                        str(payload),
                        "--vault-root",
                        str(vault),
                        "--ledger",
                        str(directory / "ledger.json"),
                    ]
                )
            self.assertEqual(code, 2)
            self.assertIn("reviewed_status_required", error.getvalue())

    def test_cli_apply_imports_reviewed_payload_create_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            payload = self._payload_file(directory)
            vault = directory / "vault"
            vault.mkdir()
            ledger = directory / "ledger.json"
            output = io.StringIO()
            with redirect_stdout(output):
                code = knowledge_cli_main(
                    [
                        "import",
                        str(payload),
                        "--vault-root",
                        str(vault),
                        "--ledger",
                        str(ledger),
                        "--apply",
                    ]
                )
            self.assertEqual(code, 0)
            result = json.loads(output.getvalue())
            self.assertEqual(result["mode"], "apply")
            self.assertEqual(result["result"], "created")
            self.assertTrue((vault / result["relative_path"]).is_file())
            self.assertTrue(ledger.is_file())


if __name__ == "__main__":
    unittest.main()

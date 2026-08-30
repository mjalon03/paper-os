from __future__ import annotations

from dataclasses import replace
import copy
import unittest

from paper_service.grounded_search import (
    AuthorityScore,
    GroundedSearchError,
    SearchChunk,
    SourceKind,
    authority_score,
    build_search_chunks,
    chunk_search_document,
    escape_plain_text_for_html,
    lexical_terms,
    normalize_lexical_text,
    normalize_search_document,
    normalize_search_documents,
    search_chunks,
    source_kind_for_knowledge_status,
)
from paper_service.selectors import SELECTOR_VERSION


NODE_A = "n-0123456789abcdef01234567"
NODE_B = "n-1123456789abcdef01234567"
NODE_C = "n-2123456789abcdef01234567"
NODE_D = "n-3123456789abcdef01234567"


def document(
    source_kind: str = "source_text",
    *,
    source_id: str = "paper:7",
    node_id: str = NODE_A,
    text: str = "The signal increased by 12 percent.",
) -> dict:
    return {
        "schema_version": 1,
        "selector_version": SELECTOR_VERSION,
        "source_kind": source_kind,
        "source_id": source_id,
        "node_id": node_id,
        "text": text,
    }


class GroundedSearchAuthorityTests(unittest.TestCase):
    def test_authority_bands_are_explicit_strict_and_ai_drafts_are_excluded(self):
        self.assertGreater(
            authority_score(SourceKind.USER_ANNOTATION),
            authority_score(SourceKind.REVIEWED_KNOWLEDGE),
        )
        self.assertGreater(
            authority_score(SourceKind.REVIEWED_KNOWLEDGE),
            authority_score(SourceKind.SOURCE_TEXT),
        )
        self.assertEqual(
            authority_score(SourceKind.AI_DRAFT_KNOWLEDGE), AuthorityScore.EXCLUDED
        )

        values = [
            document("ai_draft_knowledge", source_id="draft:1"),
            document("source_text"),
        ]
        normalized = normalize_search_documents(values)
        chunks = build_search_chunks(values)
        self.assertEqual([item.source_kind for item in normalized], [SourceKind.SOURCE_TEXT])
        self.assertEqual([item.source_kind for item in chunks], [SourceKind.SOURCE_TEXT])
        self.assertNotIn("draft:1", repr(chunks))

    def test_existing_knowledge_status_maps_without_promoting_ai_drafts(self):
        self.assertEqual(
            source_kind_for_knowledge_status("reviewed"),
            SourceKind.REVIEWED_KNOWLEDGE,
        )
        self.assertEqual(
            source_kind_for_knowledge_status("ai-draft"),
            SourceKind.AI_DRAFT_KNOWLEDGE,
        )
        with self.assertRaisesRegex(GroundedSearchError, "knowledge_status_invalid"):
            source_kind_for_knowledge_status("pending")

    def test_authority_is_primary_and_lexical_relevance_only_breaks_local_ties(self):
        values = [
            document(
                "source_text",
                source_id="paper:7",
                node_id=NODE_A,
                text="signal " * 200,
            ),
            document(
                "reviewed_knowledge",
                source_id="review:7",
                node_id=NODE_B,
                text="signal once",
            ),
            document(
                "user_annotation",
                source_id="annotation:7",
                node_id=NODE_C,
                text="signal once",
            ),
        ]
        hits = search_chunks("signal", build_search_chunks(values), limit=10)
        kinds = [hit.chunk.source_kind for hit in hits]
        self.assertEqual(
            kinds[:2],
            [SourceKind.USER_ANNOTATION, SourceKind.REVIEWED_KNOWLEDGE],
        )
        self.assertTrue(kinds[2:])
        self.assertTrue(all(kind == SourceKind.SOURCE_TEXT for kind in kinds[2:]))
        self.assertTrue(all(hit.total_score > 0 for hit in hits))


class GroundedSearchNormalizationTests(unittest.TestCase):
    def test_unicode_and_whitespace_have_deterministic_lexical_form(self):
        self.assertEqual(normalize_lexical_text("  Ｓignal\r\nSTRASSE  "), "signal strasse")
        self.assertEqual(lexical_terms("Café CAFÉ signal"), ("café", "signal"))
        normalized = normalize_search_document(
            document(text="Cafe\u0301\r\nline two")
        )
        self.assertEqual(normalized.text, "Café\nline two")
        self.assertEqual(normalized.lexical_text, "café line two")

    def test_dangerous_controls_and_multiline_identifiers_are_rejected(self):
        for text in ("safe\x00unsafe", "left\u202eright"):
            with self.subTest(text=text):
                with self.assertRaisesRegex(GroundedSearchError, "unsafe_control"):
                    normalize_search_document(document(text=text))
        with self.assertRaisesRegex(GroundedSearchError, "must_be_single_line"):
            normalize_search_document(document(source_id="paper\n7"))

    def test_contract_is_strict_and_rejects_selector_version_mismatch(self):
        wrong_selector = document()
        wrong_selector["selector_version"] = SELECTOR_VERSION + 1
        wrong_schema = document()
        wrong_schema["schema_version"] = 2
        unknown = document()
        unknown["metadata"] = {"trusted": True}
        bad_node = document(node_id="../source")
        for payload, code in (
            (wrong_selector, "unsupported_selector_version"),
            (wrong_schema, "unsupported_search_schema_version"),
            (unknown, "unknown_field"),
            (bad_node, "node_id_invalid"),
        ):
            with self.subTest(code=code):
                with self.assertRaisesRegex(GroundedSearchError, code):
                    normalize_search_document(payload)

    def test_normalization_does_not_mutate_and_ids_ignore_mapping_order(self):
        payload = document(text="Stable text")
        original = copy.deepcopy(payload)
        reordered = {key: payload[key] for key in reversed(payload)}
        first = normalize_search_document(payload)
        second = normalize_search_document(reordered)
        self.assertEqual(payload, original)
        self.assertEqual(first, second)
        self.assertRegex(first.document_id, r"^d-[0-9a-f]{32}$")
        self.assertEqual(
            set(first.to_source_node()),
            {"selector_version", "source_kind", "source_id", "node_id", "text"},
        )

    def test_duplicate_logical_nodes_fail_closed_but_input_order_is_canonical(self):
        one = document(source_id="paper:b", node_id=NODE_B)
        two = document(source_id="paper:a", node_id=NODE_A)
        before = normalize_search_documents([one, two])
        after = normalize_search_documents([two, one])
        self.assertEqual(before, after)
        with self.assertRaisesRegex(GroundedSearchError, "duplicate_search_document"):
            normalize_search_documents([one, dict(one, text="changed")])
        with self.assertRaisesRegex(GroundedSearchError, "documents_must_be_iterable"):
            normalize_search_documents(None)

    def test_html_escaping_is_explicit_and_does_not_change_stored_plain_text(self):
        value = '<script data-x="1">& unsafe</script>'
        normalized = normalize_search_document(document(text=value))
        self.assertEqual(normalized.text, value)
        escaped = escape_plain_text_for_html(value)
        self.assertNotIn("<script", escaped)
        self.assertIn("&lt;script", escaped)
        self.assertIn("&amp;", escaped)


class GroundedSearchChunkTests(unittest.TestCase):
    def test_chunk_ids_and_spans_are_deterministic_and_bounded(self):
        text = " ".join(f"token-{index}" for index in range(350))
        normalized = normalize_search_document(document(text=text))
        first = chunk_search_document(normalized, chunk_chars=256, overlap_chars=32)
        second = chunk_search_document(normalized, chunk_chars=256, overlap_chars=32)
        self.assertEqual(first, second)
        self.assertGreater(len(first), 2)
        self.assertEqual(len({item.chunk_id for item in first}), len(first))
        for chunk in first:
            self.assertLessEqual(len(chunk.text), 256)
            self.assertEqual(chunk.text, normalized.text[chunk.start : chunk.end])
            self.assertRegex(chunk.chunk_id, r"^c-[0-9a-f]{32}$")
            self.assertEqual(chunk.to_fts_row()["content"], chunk.lexical_text)

    def test_chunk_ids_bind_source_node_text_and_offsets(self):
        base = normalize_search_document(document(text="word " * 100))
        changed_text = normalize_search_document(document(text="word " * 99 + "other"))
        changed_source = normalize_search_document(
            document(source_id="paper:8", text="word " * 100)
        )
        ids = {
            chunk_search_document(item, chunk_chars=128, overlap_chars=0)[0].chunk_id
            for item in (base, changed_text, changed_source)
        }
        self.assertEqual(len(ids), 3)

    def test_invalid_chunk_configuration_fails_closed(self):
        normalized = normalize_search_document(document())
        for kwargs in (
            {"chunk_chars": 127},
            {"chunk_chars": 8_001},
            {"overlap_chars": -1},
            {"chunk_chars": 128, "overlap_chars": 64},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(GroundedSearchError):
                    chunk_search_document(normalized, **kwargs)

    def test_search_results_are_deterministic_for_ties_and_skip_nonmatches(self):
        values = [
            document(source_id="paper:b", node_id=NODE_B, text="signal response"),
            document(source_id="paper:a", node_id=NODE_A, text="signal response"),
            document(source_id="paper:c", node_id=NODE_C, text="unrelated"),
        ]
        chunks = build_search_chunks(values)
        forward = search_chunks("SIGNAL", chunks)
        reverse = search_chunks("SIGNAL", reversed(chunks))
        self.assertEqual(forward, reverse)
        self.assertEqual([hit.chunk.source_id for hit in forward], ["paper:a", "paper:b"])

    def test_tampered_authority_selector_duplicates_and_query_budgets_are_rejected(self):
        chunk = build_search_chunks([document()])[0]
        with self.assertRaisesRegex(GroundedSearchError, "authority_score_mismatch"):
            search_chunks("signal", [replace(chunk, authority_score=999)])
        with self.assertRaisesRegex(GroundedSearchError, "unsupported_selector_version"):
            search_chunks(
                "signal", [replace(chunk, selector_version=SELECTOR_VERSION + 1)]
            )
        with self.assertRaisesRegex(GroundedSearchError, "duplicate_chunk"):
            search_chunks("signal", [chunk, chunk])
        with self.assertRaisesRegex(GroundedSearchError, "normalization_mismatch"):
            search_chunks("signal", [replace(chunk, lexical_text="tampered")])
        with self.assertRaisesRegex(GroundedSearchError, "chunks_must_be_iterable"):
            search_chunks("signal", None)
        with self.assertRaisesRegex(GroundedSearchError, "query_too_long"):
            search_chunks("q" * 1_001, [chunk])
        with self.assertRaisesRegex(GroundedSearchError, "query_has_no_lexical_terms"):
            search_chunks("---", [chunk])
        with self.assertRaisesRegex(GroundedSearchError, "search_limit_invalid"):
            search_chunks("signal", [chunk], limit=101)

    def test_an_ai_draft_chunk_cannot_surface_even_if_manually_supplied(self):
        source = build_search_chunks([document()])[0]
        draft = SearchChunk(
            schema_version=source.schema_version,
            selector_version=source.selector_version,
            chunk_id="c-" + "f" * 32,
            document_id="d-" + "f" * 32,
            source_kind=SourceKind.AI_DRAFT_KNOWLEDGE,
            source_id="draft:1",
            node_id=NODE_D,
            start=0,
            end=6,
            text="signal",
            lexical_text="signal",
            authority_score=0,
        )
        self.assertEqual(search_chunks("signal", [draft]), ())


if __name__ == "__main__":
    unittest.main()

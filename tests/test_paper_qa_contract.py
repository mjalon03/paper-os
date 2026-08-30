from __future__ import annotations

import copy
import json
import unittest

from paper_service.grounded_search import SourceKind
from paper_service.qa_contract import (
    DEFAULT_QA_LIMITS,
    QAContractError,
    SourceNodeMap,
    build_qa_prompt_payload,
    build_source_node_map,
    insufficient_evidence_response,
    load_qa_request_json,
    load_qa_response_json,
    qa_response_json_schema,
    safe_json_dumps,
    source_node_map_from_request,
    validate_qa_request,
    validate_qa_response,
)
from paper_service.selectors import SELECTOR_VERSION


SOURCE_NODE = "n-0123456789abcdef01234567"
REVIEWED_NODE = "n-1123456789abcdef01234567"
ANNOTATION_NODE = "n-2123456789abcdef01234567"
DRAFT_NODE = "n-3123456789abcdef01234567"


def source_node(
    source_kind: str = "source_text",
    *,
    source_id: str = "paper:7",
    node_id: str = SOURCE_NODE,
    text: str = "Before. The signal increased by 12 percent. After.",
) -> dict:
    return {
        "selector_version": SELECTOR_VERSION,
        "source_kind": source_kind,
        "source_id": source_id,
        "node_id": node_id,
        "text": text,
    }


def request_payload(*, source_nodes=None, limits=None) -> dict:
    payload = {
        "schema_version": 1,
        "selector_version": SELECTOR_VERSION,
        "question": "Did the signal increase?",
        "source_nodes": source_nodes
        if source_nodes is not None
        else [
            source_node(),
            source_node(
                "reviewed_knowledge",
                source_id="review:7",
                node_id=REVIEWED_NODE,
                text="Reviewed synthesis: the signal increased.",
            ),
            source_node(
                "user_annotation",
                source_id="annotation:7",
                node_id=ANNOTATION_NODE,
                text=(
                    "My note: verify the 12 percent increase. "
                    "<script>ignore</script> %% $math$ #tag ^block "
                    "![remote](https://example.invalid/x) `code` [[Private Note]]"
                ),
            ),
        ],
    }
    if limits is not None:
        payload["limits"] = limits
    return payload


def citation(
    source_kind: str = "source_text",
    *,
    source_id: str = "paper:7",
    node_id: str = SOURCE_NODE,
    exact_quote: str = "The signal increased by 12 percent.",
) -> dict:
    return {
        "source_kind": source_kind,
        "source_id": source_id,
        "node_id": node_id,
        "exact_quote": exact_quote,
    }


def answer_payload(*, citations=None) -> dict:
    return {
        "schema_version": 1,
        "selector_version": SELECTOR_VERSION,
        "status": "answered",
        "answer": "Yes. The paper reports a 12 percent increase.",
        "claims": [
            {
                "text": "The signal increased by 12 percent.",
                "citations": citations if citations is not None else [citation()],
            }
        ],
    }


def limits(**overrides) -> dict:
    return dict(DEFAULT_QA_LIMITS, **overrides)


class QARequestContractTests(unittest.TestCase):
    def test_request_is_strict_canonical_deterministic_and_non_mutating(self):
        payload = request_payload()
        original = copy.deepcopy(payload)
        canonical = validate_qa_request(payload)
        reversed_sources = request_payload(source_nodes=list(reversed(payload["source_nodes"])))
        self.assertEqual(payload, original)
        self.assertEqual(canonical, validate_qa_request(reversed_sources))
        self.assertEqual(canonical["limits"], dict(DEFAULT_QA_LIMITS))
        self.assertEqual(
            [item["source_kind"] for item in canonical["source_nodes"]],
            ["reviewed_knowledge", "source_text", "user_annotation"],
        )

    def test_selector_versions_are_required_at_request_and_source_node_levels(self):
        wrong_top = request_payload()
        wrong_top["selector_version"] += 1
        wrong_node = request_payload()
        wrong_node["source_nodes"][0]["selector_version"] += 1
        boolean = request_payload()
        boolean["selector_version"] = True
        for payload in (wrong_top, wrong_node, boolean):
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(QAContractError, "selector_version"):
                    validate_qa_request(payload)

    def test_ai_drafts_are_never_valid_question_evidence(self):
        payload = request_payload(
            source_nodes=[
                source_node(
                    "ai_draft_knowledge",
                    source_id="draft:7",
                    node_id=DRAFT_NODE,
                    text="An unreviewed generated assertion.",
                )
            ]
        )
        with self.assertRaisesRegex(QAContractError, "ai_draft_forbidden"):
            validate_qa_request(payload)

    def test_duplicate_missing_invalid_and_unknown_source_fields_fail_closed(self):
        duplicate = request_payload(source_nodes=[source_node(), source_node()])
        missing = request_payload(source_nodes=[source_node()])
        del missing["source_nodes"][0]["text"]
        invalid_node = request_payload(source_nodes=[source_node(node_id="node/../bad")])
        unknown = request_payload(source_nodes=[source_node()])
        unknown["source_nodes"][0]["trusted"] = True
        for payload, code in (
            (duplicate, "duplicate_item"),
            (missing, "missing_text"),
            (invalid_node, "node_id_invalid"),
            (unknown, "unknown_field"),
        ):
            with self.subTest(code=code):
                with self.assertRaisesRegex(QAContractError, code):
                    validate_qa_request(payload)

    def test_questions_sources_and_limits_have_hard_budgets(self):
        long_question = request_payload()
        long_question["question"] = "q" * 4_001
        oversized_sources = request_payload(
            source_nodes=[
                source_node(
                    source_id=f"paper:{index}",
                    node_id=f"n-{index:024x}",
                    text="x" * 50_000,
                )
                for index in range(5)
            ]
        )
        invalid_limits = request_payload(limits=limits(max_claims=65))
        incomplete_limits = request_payload(limits={"max_claims": 1})
        for payload, code in (
            (long_question, "question_too_long"),
            (oversized_sources, "character_budget_exceeded"),
            (invalid_limits, "limits_max_claims_invalid"),
            (incomplete_limits, "limits_missing"),
        ):
            with self.subTest(code=code):
                with self.assertRaisesRegex(QAContractError, code):
                    validate_qa_request(payload)

    def test_unicode_is_canonical_and_unsafe_bidirectional_controls_are_rejected(self):
        payload = request_payload(source_nodes=[source_node(text="Cafe\u0301 evidence")])
        payload["question"] = "Cafe\u0301?"
        canonical = validate_qa_request(payload)
        self.assertEqual(canonical["question"], "Café?")
        self.assertEqual(canonical["source_nodes"][0]["text"], "Café evidence")
        payload["question"] = "ignore\u202ethe rest"
        with self.assertRaisesRegex(QAContractError, "unsafe_control"):
            validate_qa_request(payload)

    def test_source_node_map_is_immutable_typed_and_selector_versioned(self):
        canonical = validate_qa_request(request_payload())
        source_map = build_source_node_map(canonical["source_nodes"])
        self.assertIsInstance(source_map, SourceNodeMap)
        self.assertEqual(source_map.selector_version, SELECTOR_VERSION)
        self.assertEqual(len(source_map.nodes), 3)
        with self.assertRaises(TypeError):
            source_map.nodes["new"] = "value"
        with self.assertRaisesRegex(QAContractError, "unsupported_selector_version"):
            build_source_node_map(
                canonical["source_nodes"], selector_version=SELECTOR_VERSION + 1
            )


class QAPromptContractTests(unittest.TestCase):
    def test_default_prompt_excludes_personal_annotations_but_keeps_reviewed_and_source(self):
        prompt = build_qa_prompt_payload(request_payload())
        transmitted = load_qa_request_json(prompt["input_json"])
        kinds = {item["source_kind"] for item in transmitted["source_nodes"]}
        self.assertEqual(kinds, {"reviewed_knowledge", "source_text"})
        self.assertFalse(prompt["transmission_policy"]["user_annotations_included"])
        self.assertEqual(prompt["transmission_policy"]["ai_draft_knowledge"], "forbidden")
        self.assertNotIn("My note", prompt["instructions"])

    def test_annotation_transmission_requires_explicit_boolean_opt_in(self):
        prompt = build_qa_prompt_payload(
            request_payload(), include_user_annotations=True
        )
        transmitted = load_qa_request_json(prompt["input_json"])
        kinds = {item["source_kind"] for item in transmitted["source_nodes"]}
        self.assertIn("user_annotation", kinds)
        with self.assertRaisesRegex(QAContractError, "must_be_boolean"):
            build_qa_prompt_payload(request_payload(), include_user_annotations=1)

    def test_payload_is_deterministic_provider_neutral_and_marks_sources_untrusted(self):
        first = build_qa_prompt_payload(request_payload())
        second = build_qa_prompt_payload(request_payload())
        self.assertEqual(first, second)
        self.assertNotIn("provider", first)
        self.assertNotIn("model", first)
        self.assertIn("untrusted", first["instructions"])
        self.assertRegex(first["validation_context_sha256"], r"^[0-9a-f]{64}$")
        allowed = first["transmission_policy"]["allowed_source_kinds"]
        self.assertNotIn("ai_draft_knowledge", allowed)

    def test_serialized_input_is_html_and_obsidian_inert_but_lossless_json(self):
        prompt = build_qa_prompt_payload(
            request_payload(), include_user_annotations=True
        )
        serialized = prompt["input_json"]
        self.assertIn('"source_nodes"', serialized)
        self.assertNotIn("<script>", serialized)
        self.assertNotIn("%%", serialized)
        self.assertNotIn("$math$", serialized)
        self.assertNotIn("#tag", serialized)
        self.assertNotIn("^block", serialized)
        self.assertNotIn("![remote](", serialized)
        self.assertNotIn("`code`", serialized)
        self.assertNotIn("[[Private Note]]", serialized)
        self.assertNotIn("\n", serialized)
        decoded = json.loads(serialized)
        annotation = next(
            item
            for item in decoded["source_nodes"]
            if item["source_kind"] == "user_annotation"
        )
        self.assertIn("<script>ignore</script>", annotation["text"])
        self.assertIn("%%", annotation["text"])
        self.assertIn("$math$ #tag ^block", annotation["text"])
        self.assertIn("![remote](https://example.invalid/x)", annotation["text"])
        self.assertIn("[[Private Note]]", annotation["text"])

    def test_response_schema_requires_every_citation_field_and_excludes_drafts(self):
        schema = qa_response_json_schema()
        citation_schema = schema["properties"]["claims"]["items"]["properties"][
            "citations"
        ]["items"]
        self.assertEqual(
            set(citation_schema["required"]),
            {"source_kind", "source_id", "node_id", "exact_quote"},
        )
        self.assertNotIn(
            "ai_draft_knowledge",
            citation_schema["properties"]["source_kind"]["enum"],
        )
        self.assertFalse(citation_schema["additionalProperties"])


class QAResponseContractTests(unittest.TestCase):
    def test_answer_citations_are_proven_against_typed_source_node_map(self):
        request = request_payload()
        canonical = validate_qa_response(answer_payload(), request=request)
        self.assertEqual(canonical["status"], "answered")
        self.assertEqual(
            canonical["claims"][0]["citations"][0]["exact_quote"],
            "The signal increased by 12 percent.",
        )

        reviewed_answer = answer_payload(
            citations=[
                citation(
                    "reviewed_knowledge",
                    source_id="review:7",
                    node_id=REVIEWED_NODE,
                    exact_quote="the signal increased",
                )
            ]
        )
        self.assertEqual(
            validate_qa_response(reviewed_answer, request=request)["claims"][0][
                "citations"
            ][0]["source_kind"],
            "reviewed_knowledge",
        )

    def test_missing_citations_sources_quotes_and_fields_fail_closed(self):
        no_citations = answer_payload(citations=[])
        missing_source = answer_payload(
            citations=[citation(source_id="paper:missing")]
        )
        mismatched_quote = answer_payload(
            citations=[citation(exact_quote="The signal decreased.")]
        )
        missing_field = answer_payload()
        del missing_field["claims"][0]["citations"][0]["node_id"]
        unknown_field = answer_payload()
        unknown_field["claims"][0]["citations"][0]["confidence"] = 0.9
        for payload, code in (
            (no_citations, "citation_required"),
            (missing_source, "source_missing"),
            (mismatched_quote, "exact_quote_mismatch"),
            (missing_field, "missing_node_id"),
            (unknown_field, "unknown_field"),
        ):
            with self.subTest(code=code):
                with self.assertRaisesRegex(QAContractError, code):
                    validate_qa_response(payload, request=request_payload())

    def test_duplicate_claims_and_citations_fail_closed(self):
        duplicate_claim = answer_payload()
        duplicate_claim["claims"].append(copy.deepcopy(duplicate_claim["claims"][0]))
        duplicate_citation = answer_payload()
        duplicate_citation["claims"][0]["citations"].append(citation())
        cross_claim_duplicate = answer_payload()
        cross_claim_duplicate["claims"].append(
            {"text": "A second phrasing.", "citations": [citation()]}
        )
        for payload, code in (
            (duplicate_claim, "duplicate_claim"),
            (duplicate_citation, "duplicate_citation"),
            (cross_claim_duplicate, "duplicate_citation"),
        ):
            with self.subTest(code=code):
                with self.assertRaisesRegex(QAContractError, code):
                    validate_qa_response(payload, request=request_payload())

    def test_selector_versions_and_validation_context_fail_closed(self):
        answer = answer_payload()
        answer["selector_version"] += 1
        with self.assertRaisesRegex(QAContractError, "selector_version"):
            validate_qa_response(answer, request=request_payload())
        with self.assertRaisesRegex(QAContractError, "source_node_map_required"):
            validate_qa_response(answer_payload())
        with self.assertRaisesRegex(QAContractError, "context_ambiguous"):
            validate_qa_response(
                answer_payload(),
                request=request_payload(),
                source_node_map=source_node_map_from_request(request_payload()),
            )
        with self.assertRaisesRegex(QAContractError, "source_node_map_key_invalid"):
            validate_qa_response(
                answer_payload(),
                source_node_map=SourceNodeMap(SELECTOR_VERSION, {"bad": "text"}),
            )
        invalid_status = answer_payload()
        invalid_status["status"] = {"answered": True}
        with self.assertRaisesRegex(QAContractError, "answer_status_invalid"):
            validate_qa_response(invalid_status, request=request_payload())

    def test_answer_and_evidence_budgets_are_enforced(self):
        cases = []
        long_answer = answer_payload()
        long_answer["answer"] = "12345"
        cases.append((long_answer, limits(max_answer_chars=4), "answer_too_long"))

        too_many_claims = answer_payload()
        too_many_claims["claims"].append(
            {
                "text": "Second claim.",
                "citations": [
                    citation(
                        "reviewed_knowledge",
                        source_id="review:7",
                        node_id=REVIEWED_NODE,
                        exact_quote="signal increased",
                    )
                ],
            }
        )
        cases.append((too_many_claims, limits(max_claims=1), "claim_budget_exceeded"))

        too_many_citations = answer_payload(
            citations=[
                citation(),
                citation(
                    "reviewed_knowledge",
                    source_id="review:7",
                    node_id=REVIEWED_NODE,
                    exact_quote="signal increased",
                ),
            ]
        )
        cases.append(
            (
                too_many_citations,
                limits(max_citations_per_claim=1),
                "citation_budget_exceeded",
            )
        )

        quote_too_long = answer_payload()
        cases.append((quote_too_long, limits(max_quote_chars=10), "exact_quote_too_long"))

        for payload, output_limits, code in cases:
            with self.subTest(code=code):
                source_map = source_node_map_from_request(request_payload())
                with self.assertRaisesRegex(QAContractError, code):
                    validate_qa_response(
                        payload,
                        source_node_map=source_map,
                        limits=output_limits,
                    )

    def test_insufficient_evidence_is_a_first_class_claim_free_state(self):
        response = insufficient_evidence_response(
            "The supplied nodes do not report the requested outcome."
        )
        canonical = validate_qa_response(response, request=request_payload())
        self.assertEqual(canonical["status"], "insufficient_evidence")
        self.assertIsNone(canonical["answer"])
        self.assertEqual(canonical["claims"], [])

        invalid_answer = copy.deepcopy(response)
        invalid_answer["answer"] = "Maybe."
        invalid_claim = copy.deepcopy(response)
        invalid_claim["claims"] = answer_payload()["claims"]
        missing_reason = copy.deepcopy(response)
        del missing_reason["insufficient_evidence_reason"]
        for payload, code in (
            (invalid_answer, "answer_must_be_null"),
            (invalid_claim, "claims_must_be_empty"),
            (missing_reason, "reason_required"),
        ):
            with self.subTest(code=code):
                with self.assertRaisesRegex(QAContractError, code):
                    validate_qa_response(payload, request=request_payload())

    def test_answered_state_rejects_reason_null_answer_empty_claims_and_ai_draft_citation(self):
        has_reason = answer_payload()
        has_reason["insufficient_evidence_reason"] = "not really"
        null_answer = answer_payload()
        null_answer["answer"] = None
        empty_claims = answer_payload()
        empty_claims["claims"] = []
        draft_citation = answer_payload(
            citations=[
                citation(
                    "ai_draft_knowledge",
                    source_id="draft:7",
                    node_id=DRAFT_NODE,
                    exact_quote="unreviewed",
                )
            ]
        )
        for payload, code in (
            (has_reason, "has_insufficient"),
            (null_answer, "answer_must_be_string"),
            (empty_claims, "requires_claims"),
            (draft_citation, "source_kind_forbidden"),
        ):
            with self.subTest(code=code):
                with self.assertRaisesRegex(QAContractError, code):
                    validate_qa_response(payload, request=request_payload())

    def test_default_prompt_annotation_cannot_be_cited_against_transmitted_context(self):
        prompt = build_qa_prompt_payload(request_payload())
        transmitted_request = load_qa_request_json(prompt["input_json"])
        annotation_answer = answer_payload(
            citations=[
                citation(
                    "user_annotation",
                    source_id="annotation:7",
                    node_id=ANNOTATION_NODE,
                    exact_quote="verify the 12 percent increase",
                )
            ]
        )
        with self.assertRaisesRegex(QAContractError, "source_missing"):
            validate_qa_response(annotation_answer, request=transmitted_request)


class QAJsonBoundaryTests(unittest.TestCase):
    def test_json_loaders_reject_duplicate_keys_floats_constants_and_large_integers(self):
        duplicate = '{"schema_version":1,"schema_version":1}'
        for value, code in (
            (duplicate, "duplicate_key"),
            ('{"value":1.5}', "float_unsupported"),
            ('{"value":NaN}', "constant_unsupported"),
            ('{"value":123456789012345678901}', "integer_too_long"),
        ):
            with self.subTest(code=code):
                with self.assertRaisesRegex(QAContractError, code):
                    load_qa_request_json(value)

    def test_request_and_response_json_round_trip_canonically(self):
        request = validate_qa_request(request_payload())
        encoded_request = safe_json_dumps(request)
        self.assertEqual(load_qa_request_json(encoded_request), request)
        encoded_answer = safe_json_dumps(answer_payload())
        self.assertEqual(
            load_qa_response_json(encoded_answer, request=request),
            validate_qa_response(answer_payload(), request=request),
        )

    def test_non_json_input_and_oversized_json_fail_closed(self):
        with self.assertRaisesRegex(QAContractError, "must_be_text"):
            load_qa_request_json({})
        with self.assertRaisesRegex(QAContractError, "json_too_large"):
            load_qa_request_json(" " * 1_000_001)


if __name__ == "__main__":
    unittest.main()

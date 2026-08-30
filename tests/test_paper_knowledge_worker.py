import copy
import json
import unittest

from paper_service.knowledge_worker import (
    GenerationPolicy,
    GenerationUsage,
    KnowledgeGenerationError,
    MAX_SOURCE_CHARACTERS,
    MAX_SOURCE_PAGES,
    article_source_node_records,
    build_deepseek_compatible_request,
    build_generation_request,
    parse_generation_response,
)


REVISION_ID = "b260d841-a9d1-4c8c-97aa-e0737eb47a7d"
ANNOTATION_ID = "ef475659-5da4-4935-9b05-5dfad749d398"


def article_fixture():
    return {
        "schema_version": 2,
        "reader_version": 2,
        "identity": {"title": "Bounded evidence"},
        "provenance": {"source": "docling_pdf"},
        "abstract": [
            {
                "kind": "paragraph",
                "text": "The abstract reports a bounded signal.",
                "page": 1,
            }
        ],
        "sections": [
            {
                "id": "results",
                "level": 1,
                "title": "Results",
                "content": [
                    {
                        "kind": "paragraph",
                        "text": "The signal increased by 12 percent.",
                        "page": 2,
                    },
                    {
                        "kind": "paragraph",
                        "text": "The effect replicated in the held-out cohort.",
                        "page": 3,
                    },
                ],
                "sections": [],
            }
        ],
        "figures": [],
        "tables": [],
        "references": [],
        "excluded": [],
        "warnings": [],
    }


def paper_fixture():
    return {
        "id": 7,
        "sha256": "a" * 64,
        "title": "Bounded evidence",
        "authors": ["Min Kim", "Ada Lee"],
        "publication_year": 2026,
        "doi": "10.1234/bounded",
    }


def draft_payload(request, **overrides):
    first = request.input_payload["source_nodes"][0]
    value = {
        "schema_version": 1,
        "selector_version": request.input_payload["selector_version"],
        "revision_id": request.revision_id,
        "status": "ai-draft",
        "paper_identity": request.input_payload["paper_identity"],
        "source_sha256": request.input_payload["source_sha256"],
        "article_fingerprint": request.input_payload["article_fingerprint"],
        "summary": "A bounded draft grounded in the selected article nodes.",
        "claims": [
            {
                "text": "The paper reports a bounded signal.",
                "evidence": [
                    {
                        "node_id": first["node_id"],
                        "exact_quote": first["text"],
                    }
                ],
            }
        ],
        "key_methods": ["Held-out validation"],
        "key_findings": ["A bounded signal was reported"],
        "limitations": ["Only provided nodes were synthesized"],
    }
    value.update(overrides)
    return value


class ExplodingAnnotations:
    def __iter__(self):
        raise AssertionError("annotations must not be inspected without opt-in")


class GenerationPolicyTests(unittest.TestCase):
    def test_reviewed_bounds_can_only_be_tightened(self):
        for arguments in (
            {"max_source_pages": MAX_SOURCE_PAGES + 1},
            {"max_source_characters": MAX_SOURCE_CHARACTERS + 1},
            {"max_output_tokens": 8193},
            {"max_source_pages": True},
            {"include_personal_annotations": 1},
        ):
            with self.subTest(arguments=arguments):
                with self.assertRaises(KnowledgeGenerationError):
                    GenerationPolicy(**arguments)

    def test_usage_requires_consistent_tokens_and_explicit_cost_basis(self):
        usage = GenerationUsage(
            provider="deepseek",
            model="deepseek-chat",
            prompt_tokens=10,
            completion_tokens=4,
            total_tokens=14,
        )
        self.assertEqual(usage.cost_basis, "unavailable")
        invalid = (
            {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 13},
            {
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
                "cost_basis": "provider-reported",
            },
            {
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
                "cost_basis": "reviewed-price-table",
                "cost_microusd": 8,
            },
        )
        for values in invalid:
            with self.subTest(values=values):
                with self.assertRaises(KnowledgeGenerationError):
                    GenerationUsage(
                        provider="deepseek", model="deepseek-chat", **values
                    )


class GenerationRequestTests(unittest.TestCase):
    def test_request_is_one_paper_deterministic_and_selector_grounded(self):
        article = article_fixture()
        first = build_generation_request(
            paper_fixture(),
            article,
            provider="deepseek",
            model="deepseek-chat",
            revision_id=REVISION_ID,
        )
        second = build_generation_request(
            copy.deepcopy(paper_fixture()),
            copy.deepcopy(article),
            provider="deepseek",
            model="deepseek-chat",
            revision_id=REVISION_ID,
        )
        self.assertEqual(first, second)
        self.assertEqual(first.input_payload["paper_identity"]["paper_id"], 7)
        self.assertNotIn("papers", first.input_payload)
        self.assertEqual(
            list(first.source_nodes),
            [item["node_id"] for item in article_source_node_records(article)],
        )
        self.assertEqual(len(first.request_digest), 64)

    def test_source_page_and_character_limits_are_hard_bounds(self):
        article = article_fixture()
        article["abstract"] = []
        article["sections"][0]["content"] = [
            {
                "kind": "paragraph",
                "text": f"page {page}:" + "x" * 2_000,
                "page": page,
            }
            for page in range(1, 12)
        ]
        request = build_generation_request(
            paper_fixture(),
            article,
            provider="local-test",
            model="strict-json",
            revision_id=REVISION_ID,
            policy=GenerationPolicy(max_source_pages=2, max_source_characters=2_000),
        )
        policy = request.input_payload["source_policy"]
        self.assertLessEqual(len(policy["included_pages"]), 2)
        self.assertLessEqual(policy["included_characters"], 2_000)
        self.assertLessEqual(
            sum(len(value) for value in request.source_nodes.values()), 2_000
        )
        body = build_deepseek_compatible_request(request)
        prompt_characters = sum(len(item["content"]) for item in body["messages"])
        self.assertLessEqual(prompt_characters, 2_000)

    def test_annotations_are_not_even_read_without_explicit_opt_in(self):
        request = build_generation_request(
            paper_fixture(),
            article_fixture(),
            provider="deepseek",
            model="deepseek-chat",
            revision_id=REVISION_ID,
            annotations=ExplodingAnnotations(),
        )
        self.assertNotIn("personal_annotations", request.input_payload)

    def test_explicit_annotation_opt_in_is_scoped_to_current_paper_and_article(self):
        article = article_fixture()
        base = build_generation_request(
            paper_fixture(),
            article,
            provider="deepseek",
            model="deepseek-chat",
            revision_id=REVISION_ID,
        )
        annotation = {
            "id": ANNOTATION_ID,
            "paper_id": 7,
            "article_fingerprint": base.input_payload["article_fingerprint"],
            "kind": "note",
            "body": "Compare with the held-out cohort.",
            "anchor": {"exact_quote": "The signal increased by 12 percent."},
            "trashed_at": None,
        }
        request = build_generation_request(
            paper_fixture(),
            article,
            provider="deepseek",
            model="deepseek-chat",
            revision_id=REVISION_ID,
            policy=GenerationPolicy(include_personal_annotations=True),
            annotations=[annotation],
        )
        self.assertEqual(
            request.input_payload["personal_annotations"][0]["annotation_id"],
            ANNOTATION_ID,
        )
        stale = dict(annotation, article_fingerprint="f" * 64)
        with self.assertRaisesRegex(KnowledgeGenerationError, "source_mismatch"):
            build_generation_request(
                paper_fixture(),
                article,
                provider="deepseek",
                model="deepseek-chat",
                revision_id=REVISION_ID,
                policy=GenerationPolicy(include_personal_annotations=True),
                annotations=[stale],
            )

    def test_deepseek_builder_is_strict_json_and_contains_no_transport_secret(self):
        request = build_generation_request(
            paper_fixture(),
            article_fixture(),
            provider="deepseek",
            model="deepseek-chat",
            revision_id=REVISION_ID,
        )
        body = build_deepseek_compatible_request(request)
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertFalse(body["stream"])
        self.assertEqual(body["temperature"], 0)
        self.assertEqual(json.loads(body["messages"][1]["content"]), request.input_payload)
        serialized = json.dumps(body).casefold()
        for forbidden in ("authorization", "api_key", "bearer", "endpoint", "url"):
            self.assertNotIn(forbidden, serialized)

    def test_shallow_frozen_request_detects_nested_payload_mutation(self):
        request = build_generation_request(
            paper_fixture(),
            article_fixture(),
            provider="deepseek",
            model="deepseek-chat",
            revision_id=REVISION_ID,
        )
        request.input_payload["paper_identity"]["title"] = "Changed after digest"
        with self.assertRaisesRegex(KnowledgeGenerationError, "request_mutated"):
            build_deepseek_compatible_request(request)


class GenerationResponseTests(unittest.TestCase):
    def setUp(self):
        self.request = build_generation_request(
            paper_fixture(),
            article_fixture(),
            provider="deepseek",
            model="deepseek-chat",
            revision_id=REVISION_ID,
        )

    def test_valid_response_is_canonical_ai_draft(self):
        parsed = parse_generation_response(
            json.dumps(draft_payload(self.request), ensure_ascii=False), self.request
        )
        self.assertEqual(parsed["status"], "ai-draft")
        self.assertEqual(parsed["revision_id"], REVISION_ID)

    def test_model_cannot_self_approve_or_change_source_identity(self):
        for field, value in (
            ("status", "reviewed"),
            ("source_sha256", "f" * 64),
            ("article_fingerprint", "e" * 64),
            ("revision_id", "311c355b-3892-46c3-aa33-4e14f82133c0"),
        ):
            payload = draft_payload(self.request)
            payload[field] = value
            with self.subTest(field=field):
                with self.assertRaises(KnowledgeGenerationError):
                    parse_generation_response(json.dumps(payload), self.request)

        changed_identity = draft_payload(self.request)
        changed_identity["paper_identity"] = dict(
            changed_identity["paper_identity"], title="Model-rewritten title"
        )
        with self.assertRaisesRegex(KnowledgeGenerationError, "paper_identity_mismatch"):
            parse_generation_response(json.dumps(changed_identity), self.request)

    def test_unshared_annotations_and_unsupported_evidence_fail_closed(self):
        annotation = draft_payload(
            self.request, user_annotation_ids=[ANNOTATION_ID]
        )
        mismatch = draft_payload(self.request)
        mismatch["claims"][0]["evidence"][0]["exact_quote"] = "Invented quote"
        for payload in (annotation, mismatch):
            with self.subTest(payload=payload):
                with self.assertRaises(KnowledgeGenerationError):
                    parse_generation_response(json.dumps(payload), self.request)

    def test_wrappers_duplicate_keys_and_oversize_are_rejected(self):
        payload = json.dumps(draft_payload(self.request))
        cases = (
            "```json\n" + payload + "\n```",
            '{"schema_version":1,"schema_version":1}',
            b"{" + b" " * 2_000_001 + b"}",
        )
        for raw in cases:
            with self.subTest(size=len(raw)):
                with self.assertRaises(KnowledgeGenerationError):
                    parse_generation_response(raw, self.request)


if __name__ == "__main__":
    unittest.main()

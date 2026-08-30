import copy
import unittest
from pathlib import Path

from paper_service.reader_v2 import render_reader_v2
from paper_service.selectors import (
    SELECTOR_VERSION,
    article_fingerprint,
    build_selector_index,
    utf16_offset,
)


def article_fixture():
    return {
        "schema_version": 2,
        "reader_version": 2,
        "identity": {
            "title": "Stable anchors",
            "authors": ["Min Kim"],
            "year": "2026",
            "doi": "10.1234/stable",
        },
        "provenance": {"source": "docling_pdf"},
        "abstract": [
            {"kind": "paragraph", "text": "An abstract with a 🧠 symbol."}
        ],
        "sections": [
            {
                "id": "extractor-intro",
                "title": "Introduction",
                "level": 1,
                "content": [
                    {
                        "kind": "paragraph",
                        "text": "A sentence whose anchor must remain stable.",
                        "page": 1,
                    },
                    {"kind": "figure", "figure_id": "extractor-figure"},
                ],
                "sections": [
                    {
                        "id": "extractor-method",
                        "title": "Method",
                        "level": 2,
                        "content": [
                            {"kind": "paragraph", "text": "Nested content."}
                        ],
                        "sections": [],
                    }
                ],
            },
            {
                "id": "extractor-results",
                "title": "Results",
                "level": 1,
                "content": [{"kind": "table", "table_id": "extractor-table"}],
                "sections": [],
            },
        ],
        "figures": [
            {
                "id": "extractor-figure",
                "label": "Figure 1",
                "caption": "Neural response.",
                "asset_path": "assets/figure.jpg",
                "page": 3,
                "bbox": {"l": 10.0, "t": 100.0, "r": 200.0, "b": 20.0},
            }
        ],
        "tables": [
            {
                "id": "extractor-table",
                "label": "Table 1",
                "caption": "Scores.",
                "rows": [["Model", "Score"], ["A", "1"]],
            }
        ],
        "references": [],
        "excluded": [],
        "warnings": [],
    }


class StableSelectorTests(unittest.TestCase):
    def test_index_is_deterministic_and_fingerprint_ignores_dict_key_order(self):
        article = article_fixture()
        first = build_selector_index(article)
        second = build_selector_index(copy.deepcopy(article))
        reordered = copy.deepcopy(article)
        reordered["identity"] = dict(reversed(list(reordered["identity"].items())))

        self.assertEqual(first, second)
        self.assertEqual(first.selector_version, SELECTOR_VERSION)
        self.assertEqual(article_fingerprint(article), article_fingerprint(reordered))
        self.assertRegex(first.article_fingerprint, r"^[0-9a-f]{64}$")

    def test_extractor_ids_do_not_define_user_facing_selectors(self):
        article = article_fixture()
        before = build_selector_index(article)
        changed = copy.deepcopy(article)
        changed["sections"][0]["id"] = "new-section-id"
        changed["figures"][0]["id"] = "new-figure-id"
        changed["sections"][0]["content"][1]["figure_id"] = "new-figure-id"
        changed["tables"][0]["id"] = "new-table-id"
        changed["sections"][1]["content"][0]["table_id"] = "new-table-id"
        after = build_selector_index(changed)

        self.assertEqual(
            [item.section_id for item in before.sections],
            [item.section_id for item in after.sections],
        )
        self.assertEqual(
            [item.node_id for item in before.nodes],
            [item.node_id for item in after.nodes],
        )
        self.assertNotEqual(before.article_fingerprint, after.article_fingerprint)

    def test_unrelated_insertions_do_not_renumber_existing_content(self):
        article = article_fixture()
        before = build_selector_index(article)
        intro_section = before.section((0,)).section_id
        intro_nodes = {item.node_id for item in before.nodes if item.section_id == intro_section}

        changed = copy.deepcopy(article)
        changed["sections"].insert(
            0,
            {
                "id": "new-background",
                "title": "Background",
                "level": 1,
                "content": [{"kind": "paragraph", "text": "New material."}],
                "sections": [],
            },
        )
        changed["sections"][1]["content"].insert(
            0, {"kind": "paragraph", "text": "A newly inserted sentence."}
        )
        after = build_selector_index(changed)
        moved_intro = next(
            item for item in after.sections if item.title_path == ("Introduction",)
        )
        moved_nodes = {
            item.node_id for item in after.nodes if item.section_id == moved_intro.section_id
        }

        self.assertEqual(intro_section, moved_intro.section_id)
        self.assertLessEqual(intro_nodes, moved_nodes)

    def test_locator_text_uses_the_same_whitespace_normalization_as_content(self):
        article = article_fixture()
        article["sections"][0]["content"][0]["locator"] = "block   one"
        before = build_selector_index(article).node((0,), 0).node_id
        article["sections"][0]["content"][0]["locator"] = "  block one  "
        after = build_selector_index(article).node((0,), 0).node_id

        self.assertEqual(before, after)

    def test_ambiguous_duplicate_nodes_get_deterministic_suffixes(self):
        article = article_fixture()
        duplicate = copy.deepcopy(article["sections"][0]["content"][0])
        article["sections"][0]["content"].append(duplicate)
        index = build_selector_index(article)
        paragraph_ids = [
            item.node_id
            for item in index.nodes
            if item.section_path == (0,) and item.kind == "paragraph"
        ]

        self.assertEqual(len(paragraph_ids), 2)
        self.assertEqual(paragraph_ids[1], f"{paragraph_ids[0]}-2")
        self.assertEqual(index, build_selector_index(copy.deepcopy(article)))

    def test_same_subsection_title_under_different_parents_is_distinct(self):
        article = article_fixture()
        article["sections"][1]["sections"].append(
            {
                "id": "second-method",
                "title": "Method",
                "level": 2,
                "content": [{"kind": "paragraph", "text": "Another method."}],
                "sections": [],
            }
        )
        index = build_selector_index(article)
        methods = [item for item in index.sections if item.title_path[-1] == "Method"]

        self.assertEqual(len(methods), 2)
        self.assertNotEqual(methods[0].section_id, methods[1].section_id)
        self.assertEqual(methods[0].title_path, ("Introduction", "Method"))
        self.assertEqual(methods[1].title_path, ("Results", "Method"))

    def test_duplicate_title_encoding_cannot_collide_with_a_literal_suffix(self):
        article = article_fixture()
        article["sections"].extend(
            [
                {
                    "id": "duplicate-results",
                    "title": "Results",
                    "level": 1,
                    "content": [],
                    "sections": [],
                },
                {
                    "id": "literal-suffix",
                    "title": "Results#2",
                    "level": 1,
                    "content": [],
                    "sections": [],
                },
            ]
        )
        index = build_selector_index(article)

        self.assertEqual(len({item.section_id for item in index.sections}), 5)
        self.assertEqual(index.sections[-2].title_path, ("Results",))
        self.assertEqual(index.sections[-1].title_path, ("Results#2",))

    def test_browser_offsets_are_utf16_code_units(self):
        value = "A🧠B"
        self.assertEqual(utf16_offset(value, 0), 0)
        self.assertEqual(utf16_offset(value, 1), 1)
        self.assertEqual(utf16_offset(value, 2), 3)
        self.assertEqual(utf16_offset(value, 3), 4)
        with self.assertRaisesRegex(ValueError, "codepoint_offset_out_of_range"):
            utf16_offset(value, 4)

    def test_reader_emits_stable_deep_links_and_anchor_metadata(self):
        article = article_fixture()
        index = build_selector_index(article)
        intro = index.section((0,))
        paragraph = index.node((0,), 0)
        figure = index.node((0,), 1)
        abstract = index.abstract_node(0)
        rendered = render_reader_v2(
            {"id": 7, "title": "ignored", "category": "NeuroAI"}, article
        )

        self.assertIn(
            f'id="section-{intro.section_id}" data-section-id="{intro.section_id}"',
            rendered,
        )
        self.assertIn(f'href="#section-{intro.section_id}"', rendered)
        self.assertIn(
            f'id="node-{paragraph.node_id}" data-node-id="{paragraph.node_id}" '
            f'data-section-id="{intro.section_id}" data-kind="paragraph" data-page="1"',
            rendered,
        )
        self.assertIn(
            f'id="node-{figure.node_id}" data-node-id="{figure.node_id}" '
            f'data-section-id="{intro.section_id}" data-kind="figure" data-page="3"',
            rendered,
        )
        self.assertIn(f'id="node-{abstract.node_id}"', rendered)
        self.assertIn('data-selector-version="1"', rendered)
        self.assertIn(
            f'data-article-fingerprint="{index.article_fingerprint}"', rendered
        )
        self.assertIn('<script src="/static/reader-v2.js" defer>', rendered)
        controller = (
            Path(__file__).parents[1]
            / "paper_service"
            / "static"
            / "reader-v2.js"
        ).read_text(encoding="utf-8")
        self.assertIn("`/api/papers/${paperId}/reading-state`", controller)
        self.assertIn('"X-Paper-Action": "1"', controller)
        self.assertIn("expected_revision: Number(state.revision || 0)", controller)
        self.assertIn("exact_quote: exact", controller)
        self.assertIn("start_utf16: 0", controller)
        self.assertIn('id="statePanel"', rendered)
        self.assertIn('id="annotationPanel"', rendered)
        self.assertIn('id="issuePanel"', rendered)
        self.assertEqual(rendered.count("</head>"), 1)
        self.assertEqual(rendered.count("<body>"), 1)
        self.assertEqual(rendered.count('<header class="top">'), 1)
        self.assertIn('href="/knowledge/7"', rendered)
        self.assertNotIn('id="extractor-intro"', rendered)


if __name__ == "__main__":
    unittest.main()

import copy
import unittest

from paper_service.anchors import remap_anchor
from paper_service.selectors import build_selector_index
from tests.test_paper_selectors import article_fixture


def selection_anchor(article, section_path=(0,), content_index=0):
    index = build_selector_index(article)
    node = index.node(section_path, content_index)
    quote = article["sections"][section_path[0]]["content"][content_index]["text"]
    return {
        "selector_version": 1,
        "scope": "body",
        "section_id": node.section_id,
        "node_id": node.node_id,
        "start_utf16": 0,
        "end_utf16": len(quote),
        "exact_quote": quote,
        "prefix": "",
        "suffix": "",
        "page": 1,
        "article_fingerprint": index.article_fingerprint,
    }


class AnchorRemapTests(unittest.TestCase):
    def test_unchanged_node_is_exact(self):
        article = article_fixture()
        result = remap_anchor(selection_anchor(article), article)
        self.assertEqual(result.status, "exact")
        self.assertEqual(result.confidence, 1.0)

    def test_unrelated_insertion_keeps_stable_node(self):
        article = article_fixture()
        anchor = selection_anchor(article)
        rebuilt = copy.deepcopy(article)
        rebuilt["sections"].insert(
            0,
            {
                "id": "background",
                "title": "Background",
                "level": 1,
                "content": [{"kind": "paragraph", "text": "New content."}],
                "sections": [],
            },
        )
        result = remap_anchor(anchor, rebuilt)
        self.assertEqual(result.status, "remapped")
        self.assertEqual(result.reason, "stable_node_id")

    def test_unique_quote_recovers_after_section_rename(self):
        article = article_fixture()
        anchor = selection_anchor(article)
        rebuilt = copy.deepcopy(article)
        rebuilt["sections"][0]["title"] = "Overview"
        result = remap_anchor(anchor, rebuilt)
        self.assertEqual(result.status, "remapped")
        self.assertEqual(result.reason, "unique_quote_in_article")
        self.assertNotEqual(result.anchor["section_id"], anchor["section_id"])

    def test_ambiguous_quote_never_attaches_automatically(self):
        article = article_fixture()
        anchor = selection_anchor(article)
        duplicate = copy.deepcopy(article["sections"][0]["content"][0])
        rebuilt = copy.deepcopy(article)
        rebuilt["sections"][0]["title"] = "Overview"
        rebuilt["sections"][1]["content"].insert(0, duplicate)
        result = remap_anchor(anchor, rebuilt)
        self.assertEqual(result.status, "needs_review")
        self.assertIsNone(result.anchor)

    def test_missing_quote_needs_review(self):
        article = article_fixture()
        anchor = selection_anchor(article)
        rebuilt = copy.deepcopy(article)
        rebuilt["sections"][0]["content"][0]["text"] = "Completely replaced."
        result = remap_anchor(anchor, rebuilt)
        self.assertEqual(result.status, "needs_review")

    def test_utf16_offsets_are_recomputed_for_emoji_prefix(self):
        article = article_fixture()
        article["sections"][0]["content"][0]["text"] = "🧠 target phrase"
        anchor = selection_anchor(article)
        anchor.update(
            start_utf16=3,
            end_utf16=16,
            exact_quote="target phrase",
            prefix="🧠 ",
        )
        rebuilt = copy.deepcopy(article)
        rebuilt["sections"][0]["title"] = "Overview"
        result = remap_anchor(anchor, rebuilt)
        self.assertEqual(result.anchor["start_utf16"], 3)
        self.assertEqual(result.anchor["end_utf16"], 16)


if __name__ == "__main__":
    unittest.main()

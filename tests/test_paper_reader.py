import json
import tempfile
import unittest
from pathlib import Path

from paper_service.reader import (
    ReaderFailure,
    _text_blocks,
    classify_block,
    order_page_blocks,
    read_manifest,
    render_library,
    render_reader,
    safe_relative_path,
)


def block(text, x0, y0, x1, y1, size=9, bold=False):
    return {
        "text": text,
        "bbox": (x0, y0, x1, y1),
        "size": size,
        "bold": bold,
    }


class PaperReaderTests(unittest.TestCase):
    def test_extracts_leading_bold_heading_and_repairs_soft_hyphen(self):
        extracted = _text_blocks(
            {
                "type": 0,
                "bbox": (40, 100, 550, 180),
                "lines": [
                    {
                        "spans": [
                            {
                                "text": "Methods",
                                "size": 11,
                                "flags": 16,
                                "bbox": (40, 100, 100, 113),
                            }
                        ]
                    },
                    {
                        "spans": [
                            {
                                "text": "We trained a representa\u00ad",
                                "size": 9,
                                "flags": 0,
                                "bbox": (40, 120, 220, 131),
                            },
                            {
                                "text": "tion model with enough explanatory text to form a full paragraph for reading.",
                                "size": 9,
                                "flags": 0,
                                "bbox": (40, 132, 500, 143),
                            },
                        ]
                    },
                ],
            }
        )
        self.assertEqual(extracted[0]["forced_kind"], "heading")
        self.assertEqual(extracted[0]["text"], "Methods")
        self.assertIn("representation model", extracted[1]["text"])

    def test_two_column_order_preserves_full_width_anchors(self):
        blocks = [
            block("right one", 310, 100, 550, 130),
            block("left two", 40, 180, 280, 210),
            block("abstract", 40, 20, 550, 70),
            block("right two", 310, 180, 550, 210),
            block("left one", 40, 100, 280, 130),
            block("full heading", 40, 270, 550, 295),
        ]
        ordered = order_page_blocks(blocks, 600)
        self.assertEqual(
            [item["text"] for item in ordered],
            [
                "abstract",
                "left one",
                "left two",
                "right one",
                "right two",
                "full heading",
            ],
        )

    def test_heading_and_caption_classification(self):
        self.assertEqual(
            classify_block(block("Methods", 0, 0, 100, 20, 10, True), 9),
            "heading",
        )
        self.assertEqual(
            classify_block(block("Figure 2 | Network", 0, 0, 100, 20), 9),
            "caption",
        )
        self.assertEqual(
            classify_block(block("A normal sentence.", 0, 0, 100, 20), 9),
            "paragraph",
        )

    def test_rendering_escapes_untrusted_paper_text(self):
        paper = {
            "id": 7,
            "title": "<script>alert(1)</script>",
            "authors": ["A & B"],
            "publication_year": 2026,
            "category": "NeuroAI",
            "paper_type": "Research",
            "doi": None,
            "reader_status": "ready",
        }
        manifest = {
            "title": paper["title"],
            "headings": [{"text": "Intro <x>", "anchor": "section-1"}],
            "blocks": [
                {
                    "kind": "heading",
                    "text": "Intro <x>",
                    "page": 1,
                    "anchor": "section-1",
                },
                {"kind": "paragraph", "text": "A & B", "page": 1},
            ],
        }
        rendered = render_reader(paper, manifest)
        self.assertNotIn("<script>alert(1)</script>", rendered)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", rendered)
        self.assertIn("A &amp; B", rendered)
        library = render_library([paper])
        self.assertIn('href="/read/7"', library)

        paper["reader_v2_status"] = "ready"
        library = render_library([paper])
        self.assertIn('href="/read/7/v2"', library)
        self.assertNotIn('href="/read/7"', library)

    def test_manifest_path_is_confined_and_schema_checked(self):
        with self.assertRaises(ReaderFailure):
            safe_relative_path("../manifest.json")
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            manifest_path = data / "library" / "mobile" / "1" / "v1" / "manifest.json"
            manifest_path.parent.mkdir(parents=True)
            manifest_path.write_text(json.dumps({"schema_version": 1, "blocks": []}))
            loaded = read_manifest(data, "1/v1/manifest.json")
            self.assertEqual(loaded["schema_version"], 1)
            manifest_path.write_text(json.dumps({"schema_version": 99}))
            with self.assertRaisesRegex(ReaderFailure, "reader_manifest_invalid"):
                read_manifest(data, "1/v1/manifest.json")


if __name__ == "__main__":
    unittest.main()

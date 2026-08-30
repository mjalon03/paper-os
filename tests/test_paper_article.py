import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from paper_service.article import ArticleValidationError, validate_article
from paper_service.docling_adapter import _Builder, adapt_docling
from paper_service.jats import JATSFailure, parse_jats
from paper_service.pmc import (
    PMCFailure,
    discover_article,
    download_media,
    media_urls,
    normalize_doi,
)
from paper_service.reader_v2 import (
    build_pmc_article,
    read_article,
    render_reader_v2,
    v2_asset_path,
    write_article,
    convert_paper_v2,
    raw_sha256,
)
from paper_service.reader_v2_worker import convert_isolated, parse_allowed_ids


JATS = b"""<?xml version="1.0" encoding="UTF-8"?>
<article xmlns:xlink="http://www.w3.org/1999/xlink">
  <front><article-meta>
    <article-id pub-id-type="doi">10.1234/Test.1</article-id>
    <title-group><article-title>Structured &amp; Safe</article-title></title-group>
    <contrib-group><contrib contrib-type="author"><name><surname>Kim</surname><given-names>Min</given-names></name><xref ref-type="aff" rid="a1"/></contrib></contrib-group>
    <pub-date><year>2026</year></pub-date>
    <abstract><p>A complete <italic>abstract</italic> sentence.</p></abstract>
    <aff id="a1">Department that must not enter the body.</aff>
  </article-meta></front>
  <body>
    <sec id="s1"><title>Introduction</title>
      <p>This sentence has an inline <xref ref-type="bibr" rid="r1">citation</xref> and stays together.</p>
      <fig id="f1"><label>Figure 1</label><caption><p>Neural response.</p></caption><graphic xlink:href="fig1.jpg"/></fig>
      <sec id="s1a"><title>Nested method</title><p>Nested paragraph.</p></sec>
    </sec>
    <sec id="s2"><title>Results</title>
      <table-wrap id="t1"><label>Table 1</label><caption><p>Scores.</p></caption><table><tr><th>Model</th><th>Score</th></tr><tr><td>A</td><td>1</td></tr></table></table-wrap>
    </sec>
  </body>
  <back><ref-list><ref id="r1"><mixed-citation>Example reference.</mixed-citation></ref></ref-list></back>
</article>"""


class ArticleModelTests(unittest.TestCase):
    def test_jats_preserves_structure_and_separates_furniture(self):
        article = parse_jats(
            JATS,
            {"source": "pmc_jats", "pmcid": "PMC123", "version": 1},
        )
        self.assertEqual(article["schema_version"], 2)
        self.assertEqual(article["identity"]["title"], "Structured & Safe")
        self.assertEqual(article["identity"]["authors"], ["Min Kim"])
        self.assertEqual(article["abstract"][0]["text"], "A complete abstract sentence.")
        self.assertEqual(article["sections"][0]["title"], "Introduction")
        self.assertEqual(article["sections"][0]["sections"][0]["level"], 2)
        self.assertIn("citation and stays together", article["sections"][0]["content"][0]["text"])
        self.assertNotIn("Department", json.dumps(article["sections"]))
        self.assertEqual(article["figures"][0]["asset_name"], "fig1.jpg")
        self.assertEqual(article["tables"][0]["rows"][1], ["A", "1"])
        self.assertEqual(article["references"][0]["text"], "Example reference.")

    def test_jats_rejects_entities_and_invalid_root(self):
        with self.assertRaisesRegex(JATSFailure, "jats_unsafe_xml"):
            parse_jats(
                b'<!DOCTYPE article [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><article>&xxe;</article>',
                {"source": "pmc_jats"},
            )
        with self.assertRaisesRegex(JATSFailure, "jats_root_invalid"):
            parse_jats(b"<book/>", {"source": "pmc_jats"})

    def test_model_rejects_duplicate_section_ids(self):
        article = parse_jats(JATS, {"source": "pmc_jats"})
        article["sections"][1]["id"] = article["sections"][0]["id"]
        with self.assertRaisesRegex(ArticleValidationError, "invalid_section_id"):
            validate_article(article)

    def test_docling_adapter_removes_furniture_repairs_drop_cap_and_groups_panels(self):
        def text(index, label, value, page=1, level=None):
            item = {
                "self_ref": f"#/texts/{index}",
                "label": label,
                "text": value,
                "prov": [{"page_no": page, "bbox": {"l": 20, "t": 700, "r": 500, "b": 650}}],
            }
            if level:
                item["level"] = level
            return item

        texts = [
            text(0, "page_header", "JOURNAL HEADER"),
            text(1, "section_header", "Paper title", level=1),
            text(2, "text", "Ann Kim & Bob Lee"),
            text(3, "text", "This abstract is deliberately long enough to be recognized as the standalone abstract paragraph for this structured article."),
            text(4, "text", "M achine learning begins here and the sentence remains complete."),
            text(5, "caption", "Figure 1 | Two bounded panels.", page=2),
            text(6, "section_header", "Results", page=2, level=1),
            text(7, "text", "A result paragraph.", page=2),
            text(8, "section_header", "Received 1 May 2026.", page=2, level=1),
            text(9, "list_item", "Reference one.", page=2),
        ]
        pictures = [
            {"prov": [{"page_no": 2, "bbox": {"l": 30, "t": 500, "r": 250, "b": 250}}], "captions": []},
            {"prov": [{"page_no": 2, "bbox": {"l": 260, "t": 500, "r": 560, "b": 250}}], "captions": []},
        ]
        refs = [{"$ref": f"#/texts/{index}"} for index in range(5)] + [
            {"$ref": "#/pictures/0"},
            {"$ref": "#/pictures/1"},
        ] + [{"$ref": f"#/texts/{index}"} for index in range(5, 10)]
        document = {
            "schema_name": "DoclingDocument",
            "version": "1.0",
            "name": "Paper title",
            "body": {"children": refs},
            "texts": texts,
            "pictures": pictures,
            "groups": [],
            "pages": {
                "1": {"size": {"width": 600, "height": 800}},
                "2": {"size": {"width": 600, "height": 800}},
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            original = _Builder.crop
            _Builder.crop = lambda self, page, bbox, destination: destination.parent.mkdir(parents=True, exist_ok=True) or destination.write_bytes(b"crop")
            try:
                article = adapt_docling(
                    document,
                    Path(temporary) / "source.pdf",
                    Path(temporary) / "output",
                    {"title": "Paper title", "authors": ["Ann Kim", "Bob Lee"]},
                )
            finally:
                _Builder.crop = original
        self.assertEqual(article["abstract"][0]["text"][:13], "This abstract")
        self.assertTrue(article["sections"][0]["content"][0]["text"].startswith("Machine"))
        self.assertEqual(len(article["figures"]), 1)
        self.assertEqual(article["figures"][0]["bbox"]["r"], 560)
        self.assertEqual(article["references"][0]["text"], "Reference one.")
        self.assertTrue(any(item["kind"] == "page_header" for item in article["excluded"]))

    def test_v2_rendering_escapes_text_and_never_embeds_a_page_image(self):
        article = parse_jats(JATS, {"source": "pmc_jats"})
        article["identity"]["title"] = "<script>alert(1)</script>"
        article["figures"][0]["asset_path"] = "assets/fig1.jpg"
        rendered = render_reader_v2(
            {"id": 7, "title": "ignored", "category": "NeuroAI"}, article
        )
        self.assertNotIn("<script>alert(1)</script>", rendered)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", rendered)
        self.assertIn('/read/7/v2/assets/fig1.jpg', rendered)
        self.assertIn('href="/read/7">기존 리더</a>', rendered)
        self.assertNotIn("page-001.jpg", rendered)
        self.assertIn("참고문헌", rendered)

    def test_v2_storage_is_versioned_and_assets_are_confined(self):
        article = parse_jats(JATS, {"source": "pmc_jats"})
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            output = data / "library" / "mobile" / "7" / "v2"
            write_article(article, output)
            assets = output / "assets"
            assets.mkdir()
            (assets / "fig1.jpg").write_bytes(b"image")
            self.assertEqual(read_article(data, 7)["schema_version"], 2)
            self.assertEqual(v2_asset_path(data, 7, "fig1.jpg").name, "fig1.jpg")
            with self.assertRaises(FileNotFoundError):
                v2_asset_path(data, 7, "../article.json")

    def test_v2_conversion_preserves_raw_pdf_and_v1(self):
        article = parse_jats(JATS, {"source": "pmc_jats"})
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            raw = data / "library" / "raw" / "aa" / "paper.pdf"
            raw.parent.mkdir(parents=True)
            raw.write_bytes(b"immutable-pdf")
            v1 = data / "library" / "mobile" / "7" / "v1" / "manifest.json"
            v1.parent.mkdir(parents=True)
            v1.write_text("v1")
            paper = {
                "id": 7,
                "stored_path": "aa/paper.pdf",
                "sha256": raw_sha256(raw),
                "doi": "10.1234/test.1",
            }
            with patch("paper_service.reader_v2.build_pmc_article", return_value=article):
                manifest, source = convert_paper_v2(paper, data)
            self.assertEqual((manifest, source), ("7/v2/article.json", "pmc_jats"))
            self.assertEqual(raw.read_bytes(), b"immutable-pdf")
            self.assertEqual(v1.read_text(), "v1")
            self.assertEqual(read_article(data, 7)["schema_version"], 2)

    def test_staged_reader_worker_ids_are_strict(self):
        self.assertEqual(parse_allowed_ids("21, 1,15,1"), [1, 15, 21])
        self.assertIsNone(parse_allowed_ids(None))
        self.assertIsNone(parse_allowed_ids("all"))
        with self.assertRaises(RuntimeError):
            parse_allowed_ids("1,bad")

    def test_isolated_conversion_uses_remote_source_boundaries(self):
        with patch(
            "paper_service.reader_v2_worker.convert_paper_v2",
            return_value=("7/v2/article.json", "docling_pdf"),
        ) as convert:
            converted = convert_isolated({"id": 7}, Path("/data"))
        self.assertEqual(converted, ("7/v2/article.json", "docling_pdf"))
        self.assertEqual(convert.call_args.args, ({"id": 7}, Path("/data")))
        self.assertTrue(callable(convert.call_args.kwargs["pmc_article_builder"]))
        self.assertTrue(callable(convert.call_args.kwargs["pdf_article_converter"]))


class PMCSourceTests(unittest.TestCase):
    def test_discovers_latest_eligible_version_and_media(self):
        calls = []

        def fetch(url, _limit):
            calls.append(url)
            if "idconv" in url:
                query = parse_qs(urlsplit(url).query)
                self.assertEqual(query["ids"], ["10.1234/test.1"])
                return b'{"records":[{"pmcid":"PMC123"}]}'
            if "list-type=2" in url:
                return b"<ListBucketResult><CommonPrefixes><Prefix>PMC123.1/</Prefix></CommonPrefixes><CommonPrefixes><Prefix>PMC123.2/</Prefix></CommonPrefixes></ListBucketResult>"
            if url.endswith("PMC123.2.json"):
                return json.dumps(
                    {
                        "is_pmc_openaccess": False,
                        "license_code": "restricted",
                        "xml_url": "s3://pmc-oa-opendata/PMC123.2/article.xml",
                    }
                ).encode()
            if url.endswith("PMC123.1.json"):
                return json.dumps(
                    {
                        "is_pmc_openaccess": True,
                        "license_code": "CC BY",
                        "xml_url": "s3://pmc-oa-opendata/PMC123.1/article.xml",
                        "media_urls": [
                            "s3://pmc-oa-opendata/PMC123.1/fig1.jpg",
                            "https://evil.example/fig2.jpg",
                        ],
                    }
                ).encode()
            raise AssertionError(url)

        metadata = discover_article("https://doi.org/10.1234/Test.1", fetch)
        self.assertEqual(metadata["resolved_version"], 1)
        self.assertEqual(
            metadata["xml_url"],
            "https://pmc-oa-opendata.s3.amazonaws.com/PMC123.1/article.xml",
        )
        self.assertEqual(list(media_urls(metadata)), ["fig1.jpg"])
        self.assertTrue(any(url.endswith("PMC123.2.json") for url in calls))

    def test_rejects_invalid_doi_and_media_hash(self):
        with self.assertRaisesRegex(PMCFailure, "pmc_doi_invalid"):
            normalize_doi("not a DOI")
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(PMCFailure, "pmc_asset_hash_mismatch"):
                download_media(
                    "https://pmc-oa-opendata.s3.amazonaws.com/PMC1.1/figure.jpg",
                    Path(temporary),
                    "figure.jpg",
                    lambda _url, _limit: b"figure",
                    expected_md5="0" * 32,
                )
            self.assertFalse((Path(temporary) / "figure.jpg").exists())
            with self.assertRaisesRegex(PMCFailure, "pmc_asset_name_invalid"):
                download_media(
                    "https://pmc-oa-opendata.s3.amazonaws.com/PMC1.1/figure.jpg",
                    Path(temporary),
                    "../figure.jpg",
                    lambda _url, _limit: b"figure",
                )

    def test_builds_article_with_individual_figure_asset(self):
        listing = b"<ListBucketResult><CommonPrefixes><Prefix>PMC123.1/</Prefix></CommonPrefixes></ListBucketResult>"
        metadata = json.dumps(
            {
                "is_pmc_openaccess": True,
                "license_code": "CC BY",
                "is_retracted": False,
                "xml_url": "s3://pmc-oa-opendata/PMC123.1/article.xml",
                "media_urls": ["s3://pmc-oa-opendata/PMC123.1/fig1.jpg"],
            }
        ).encode()

        def fetch(url, _limit):
            if "idconv" in url:
                return b'{"records":[{"pmcid":"PMC123"}]}'
            if "list-type=2" in url:
                return listing
            if url.endswith("PMC123.1.json"):
                return metadata
            if url.endswith("article.xml"):
                return JATS
            if url.endswith("fig1.jpg"):
                return b"individual-figure-bytes"
            raise AssertionError(url)

        with tempfile.TemporaryDirectory() as temporary:
            article = build_pmc_article(
                {"doi": "10.1234/test.1"}, Path(temporary), fetch
            )
            self.assertEqual(article["figures"][0]["asset_path"], "assets/fig1.jpg")
            self.assertEqual(
                (Path(temporary) / "assets" / "fig1.jpg").read_bytes(),
                b"individual-figure-bytes",
            )


if __name__ == "__main__":
    unittest.main()

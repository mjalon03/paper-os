"""Convert trusted-size JATS XML into the canonical article model."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import PurePosixPath
from typing import Any

from .article import ARTICLE_SCHEMA_VERSION, READER_VERSION, stable_id, validate_article


MAX_JATS_BYTES = 16 * 1024 * 1024
ENTITY_DECLARATION = re.compile(br"<!\s*ENTITY\b", re.IGNORECASE)
DOCTYPE_DECLARATION = re.compile(br"<!\s*DOCTYPE\s+[^>\[]+>", re.IGNORECASE)
DOCTYPE_START = re.compile(br"<!\s*DOCTYPE\b", re.IGNORECASE)
SKIP_TEXT_TAGS = {"fig", "table-wrap", "supplementary-material", "media"}


class JATSFailure(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _tag(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _children(element: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in element if _tag(child) == name]


def _first(element: ET.Element | None, name: str) -> ET.Element | None:
    if element is None:
        return None
    return next((item for item in element.iter() if _tag(item) == name), None)


def _clean_text(value: str) -> str:
    return " ".join(value.split())


def _text(element: ET.Element | None) -> str:
    return _clean_text("" if element is None else "".join(element.itertext()))


def _body_text(element: ET.Element) -> str:
    parts: list[str] = []

    def visit(node: ET.Element) -> None:
        if _tag(node) in SKIP_TEXT_TAGS:
            return
        if node.text:
            parts.append(node.text)
        for child in node:
            visit(child)
            if child.tail:
                parts.append(child.tail)

    visit(element)
    return _clean_text("".join(parts))


def _attribute_local(element: ET.Element, name: str) -> str | None:
    for key, value in element.attrib.items():
        if key.rsplit("}", 1)[-1] == name:
            return value
    return None


def _asset_name(href: str | None) -> str | None:
    if not href:
        return None
    name = PurePosixPath(href).name
    if name in {"", ".", ".."} or not re.fullmatch(r"[A-Za-z0-9._-]{1,180}", name):
        return None
    return name


class _JATSBuilder:
    def __init__(self, root: ET.Element, provenance: dict[str, Any]):
        self.root = root
        self.provenance = provenance
        self.figures: list[dict[str, Any]] = []
        self.tables: list[dict[str, Any]] = []
        self.references: list[dict[str, Any]] = []
        self.warnings: list[str] = []
        self.used_ids: set[str] = set()
        self.figure_source_ids: dict[str, str] = {}
        self.table_source_ids: dict[str, str] = {}
        self.section_count = 0

    def unique_id(self, raw: str, prefix: str) -> str:
        base = stable_id(raw, prefix)
        candidate = base
        suffix = 2
        while candidate in self.used_ids:
            candidate = f"{base}-{suffix}"
            suffix += 1
        self.used_ids.add(candidate)
        return candidate

    def figure(self, element: ET.Element) -> dict[str, Any]:
        source_id = element.get("id", "")
        if source_id and source_id in self.figure_source_ids:
            return {"kind": "figure", "figure_id": self.figure_source_ids[source_id]}
        figure_id = self.unique_id(source_id, f"figure-{len(self.figures) + 1}")
        if source_id:
            self.figure_source_ids[source_id] = figure_id
        graphic = _first(element, "graphic")
        figure = {
            "id": figure_id,
            "label": _text(_first(element, "label")),
            "caption": _text(_first(element, "caption")),
            "asset_name": _asset_name(_attribute_local(graphic, "href")) if graphic is not None else None,
        }
        if not figure["asset_name"]:
            self.warnings.append(f"figure_asset_missing:{figure_id}")
        self.figures.append(figure)
        return {"kind": "figure", "figure_id": figure_id}

    def table(self, element: ET.Element) -> dict[str, Any]:
        source_id = element.get("id", "")
        if source_id and source_id in self.table_source_ids:
            return {"kind": "table", "table_id": self.table_source_ids[source_id]}
        table_id = self.unique_id(source_id, f"table-{len(self.tables) + 1}")
        if source_id:
            self.table_source_ids[source_id] = table_id
        rows: list[list[str]] = []
        html_table = _first(element, "table")
        if html_table is not None:
            for row in (item for item in html_table.iter() if _tag(item) == "tr"):
                cells = [_text(cell) for cell in row if _tag(cell) in {"th", "td"}]
                if cells:
                    rows.append(cells)
        table = {
            "id": table_id,
            "label": _text(_first(element, "label")),
            "caption": _text(_first(element, "caption")),
            "rows": rows,
        }
        self.tables.append(table)
        return {"kind": "table", "table_id": table_id}

    def content(self, container: ET.Element) -> list[dict[str, Any]]:
        content: list[dict[str, Any]] = []
        for child in container:
            name = _tag(child)
            if name == "p":
                value = _body_text(child)
                if value:
                    paragraph: dict[str, Any] = {"kind": "paragraph", "text": value}
                    targets = [
                        target
                        for xref in child.iter()
                        if _tag(xref) == "xref"
                        for target in xref.get("rid", "").split()
                        if target
                    ]
                    if targets:
                        paragraph["targets"] = targets
                    content.append(paragraph)
                for nested in child.iter():
                    if nested is child:
                        continue
                    nested_name = _tag(nested)
                    if nested_name == "fig":
                        content.append(self.figure(nested))
                    elif nested_name == "table-wrap":
                        content.append(self.table(nested))
            elif name == "fig":
                content.append(self.figure(child))
            elif name == "table-wrap":
                content.append(self.table(child))
            elif name in {"list", "disp-formula"}:
                value = _text(child)
                if value:
                    kind = "list" if name == "list" else "equation"
                    content.append({"kind": kind, "text": value})
        return content

    def section(self, element: ET.Element, level: int) -> dict[str, Any]:
        self.section_count += 1
        title = _text(next(iter(_children(element, "title")), None))
        if not title:
            title = f"Untitled section {self.section_count}"
            self.warnings.append(f"section_title_missing:{self.section_count}")
        section_id = self.unique_id(element.get("id", ""), f"section-{self.section_count}")
        return {
            "id": section_id,
            "title": title,
            "level": level,
            "content": self.content(element),
            "sections": [self.section(child, level + 1) for child in _children(element, "sec")],
        }

    def build_references(self, back: ET.Element | None) -> None:
        if back is None:
            return
        for ref in (item for item in back.iter() if _tag(item) == "ref"):
            value = _text(ref)
            if value:
                self.references.append({"id": ref.get("id"), "text": value})

    def place_floats(self, sections: list[dict[str, Any]]) -> None:
        placed_figures = {
            node["figure_id"]
            for section in self.walk_sections(sections)
            for node in section["content"]
            if node["kind"] == "figure"
        }
        placed_tables = {
            node["table_id"]
            for section in self.walk_sections(sections)
            for node in section["content"]
            if node["kind"] == "table"
        }
        for section in self.walk_sections(sections):
            expanded: list[dict[str, Any]] = []
            for node in section["content"]:
                expanded.append(node)
                if node["kind"] != "paragraph":
                    continue
                for target in node.get("targets", []):
                    figure_id = self.figure_source_ids.get(target)
                    table_id = self.table_source_ids.get(target)
                    if figure_id and figure_id not in placed_figures:
                        expanded.append({"kind": "figure", "figure_id": figure_id})
                        placed_figures.add(figure_id)
                    if table_id and table_id not in placed_tables:
                        expanded.append({"kind": "table", "table_id": table_id})
                        placed_tables.add(table_id)
            section["content"] = expanded

        remainder = [
            {"kind": "figure", "figure_id": figure["id"]}
            for figure in self.figures
            if figure["id"] not in placed_figures
        ] + [
            {"kind": "table", "table_id": table["id"]}
            for table in self.tables
            if table["id"] not in placed_tables
        ]
        if remainder:
            sections.append(
                {
                    "id": self.unique_id("unplaced-figures-and-tables", "media"),
                    "title": "Figures and tables",
                    "level": 1,
                    "content": remainder,
                    "sections": [],
                }
            )

    @staticmethod
    def walk_sections(sections: list[dict[str, Any]]):
        for section in sections:
            yield section
            yield from _JATSBuilder.walk_sections(section["sections"])

    def build(self) -> dict[str, Any]:
        front = _first(self.root, "front")
        article_meta = _first(front, "article-meta")
        body = _first(self.root, "body")
        back = _first(self.root, "back")
        title = _text(_first(article_meta, "article-title"))
        if not title:
            raise JATSFailure("jats_title_missing")

        authors: list[str] = []
        if article_meta is not None:
            for contributor in (item for item in article_meta.iter() if _tag(item) == "contrib"):
                if contributor.get("contrib-type") not in {None, "author"}:
                    continue
                name = _first(contributor, "name")
                given = _text(_first(name, "given-names"))
                surname = _text(_first(name, "surname"))
                value = " ".join(part for part in (given, surname) if part)
                if value:
                    authors.append(value)

        doi = None
        if article_meta is not None:
            for article_id in (item for item in article_meta.iter() if _tag(item) == "article-id"):
                if article_id.get("pub-id-type", "").casefold() == "doi":
                    doi = _text(article_id).casefold() or None
                    break
        year = None
        year_text = _text(_first(_first(article_meta, "pub-date"), "year"))
        if re.fullmatch(r"\d{4}", year_text):
            year = int(year_text)

        abstract: list[dict[str, str]] = []
        abstract_element = _first(article_meta, "abstract")
        if abstract_element is not None:
            paragraphs = [item for item in abstract_element.iter() if _tag(item) == "p"]
            if paragraphs:
                abstract = [
                    {"kind": "paragraph", "text": value}
                    for item in paragraphs
                    if (value := _body_text(item))
                ]
            elif (value := _body_text(abstract_element)):
                abstract = [{"kind": "paragraph", "text": value}]

        sections: list[dict[str, Any]] = []
        if body is not None:
            preamble = self.content(body)
            if preamble:
                sections.append(
                    {
                        "id": self.unique_id("body-preamble", "body-preamble"),
                        "title": "Main text",
                        "level": 1,
                        "content": preamble,
                        "sections": [],
                    }
                )
            sections.extend(self.section(section, 1) for section in _children(body, "sec"))
        for floats in _children(self.root, "floats-group"):
            for element in floats.iter():
                if _tag(element) == "fig":
                    self.figure(element)
                elif _tag(element) == "table-wrap":
                    self.table(element)
        self.place_floats(sections)
        self.build_references(back)

        article = {
            "schema_version": ARTICLE_SCHEMA_VERSION,
            "reader_version": READER_VERSION,
            "identity": {"title": title, "authors": authors, "year": year, "doi": doi},
            "provenance": self.provenance,
            "abstract": abstract,
            "sections": sections,
            "figures": self.figures,
            "tables": self.tables,
            "references": self.references,
            "excluded": [{"kind": "front_matter", "reason": "kept_in_article_header"}],
            "warnings": self.warnings,
        }
        return validate_article(article)


def parse_jats(xml_bytes: bytes, provenance: dict[str, Any]) -> dict[str, Any]:
    if not xml_bytes or len(xml_bytes) > MAX_JATS_BYTES:
        raise JATSFailure("jats_size_invalid")
    if ENTITY_DECLARATION.search(xml_bytes):
        raise JATSFailure("jats_unsafe_xml")
    sanitized = DOCTYPE_DECLARATION.sub(b"", xml_bytes)
    if DOCTYPE_START.search(sanitized):
        raise JATSFailure("jats_unsafe_xml")
    try:
        root = ET.fromstring(sanitized)
    except ET.ParseError as exc:
        raise JATSFailure("jats_parse_failed") from exc
    article_root = root if _tag(root) == "article" else _first(root, "article")
    if article_root is None:
        raise JATSFailure("jats_root_invalid")
    return _JATSBuilder(article_root, provenance).build()

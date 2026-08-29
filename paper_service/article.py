"""Versioned, extractor-independent article representation."""

from __future__ import annotations

import re
from typing import Any


ARTICLE_SCHEMA_VERSION = 2
READER_VERSION = 2
NODE_KINDS = {"paragraph", "figure", "table", "list", "equation"}
SOURCE_KINDS = {"pmc_jats", "docling_pdf"}


class ArticleValidationError(ValueError):
    pass


def stable_id(value: str, fallback: str) -> str:
    candidate = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return candidate[:80] or fallback


def validate_article(article: dict[str, Any]) -> dict[str, Any]:
    """Validate the boundary consumed by storage and rendering."""
    if article.get("schema_version") != ARTICLE_SCHEMA_VERSION:
        raise ArticleValidationError("invalid_schema_version")
    if article.get("reader_version") != READER_VERSION:
        raise ArticleValidationError("invalid_reader_version")
    identity = article.get("identity")
    if not isinstance(identity, dict) or not str(identity.get("title", "")).strip():
        raise ArticleValidationError("missing_title")
    provenance = article.get("provenance")
    if not isinstance(provenance, dict) or provenance.get("source") not in SOURCE_KINDS:
        raise ArticleValidationError("invalid_provenance")

    seen: set[str] = set()
    figure_refs: set[str] = set()
    table_refs: set[str] = set()

    def check_section(section: Any, parent_level: int) -> None:
        if not isinstance(section, dict):
            raise ArticleValidationError("invalid_section")
        section_id = str(section.get("id", ""))
        level = section.get("level")
        if not section_id or section_id in seen:
            raise ArticleValidationError("invalid_section_id")
        if not isinstance(level, int) or level <= parent_level:
            raise ArticleValidationError("invalid_section_level")
        if not str(section.get("title", "")).strip():
            raise ArticleValidationError("missing_section_title")
        seen.add(section_id)
        for node in section.get("content", []):
            if not isinstance(node, dict) or node.get("kind") not in NODE_KINDS:
                raise ArticleValidationError("invalid_content_node")
            if node.get("kind") == "paragraph" and not str(node.get("text", "")).strip():
                raise ArticleValidationError("empty_paragraph")
            if node.get("kind") == "figure":
                figure_refs.add(str(node.get("figure_id", "")))
            if node.get("kind") == "table":
                table_refs.add(str(node.get("table_id", "")))
        for child in section.get("sections", []):
            check_section(child, level)

    sections = article.get("sections")
    if not isinstance(sections, list):
        raise ArticleValidationError("invalid_sections")
    for section in sections:
        check_section(section, 0)

    for collection in ("figures", "tables", "references", "excluded", "warnings"):
        if not isinstance(article.get(collection), list):
            raise ArticleValidationError(f"invalid_{collection}")
    figure_ids: set[str] = set()
    for figure in article["figures"]:
        if not isinstance(figure, dict) or not str(figure.get("id", "")):
            raise ArticleValidationError("invalid_figure")
        figure_id = str(figure["id"])
        if figure_id in figure_ids:
            raise ArticleValidationError("duplicate_figure")
        figure_ids.add(figure_id)
        asset_path = figure.get("asset_path")
        if asset_path is not None and not re.fullmatch(
            r"assets/[A-Za-z0-9._-]{1,180}", str(asset_path)
        ):
            raise ArticleValidationError("invalid_figure_asset")
    table_ids = {
        str(table.get("id", ""))
        for table in article["tables"]
        if isinstance(table, dict) and str(table.get("id", ""))
    }
    if len(table_ids) != len(article["tables"]):
        raise ArticleValidationError("invalid_table")
    if not figure_refs <= figure_ids or not table_refs <= table_ids:
        raise ArticleValidationError("missing_content_target")
    return article

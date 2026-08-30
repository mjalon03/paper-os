"""Deterministic selectors for content in a version-two article.

Selectors are derived at read time and never written back into ``article.json``.
They deliberately avoid extractor-generated section, figure, and table IDs so a
paper can be re-indexed without making its user-facing anchors depend on those
implementation details.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import unicodedata
from typing import Any

from .article import validate_article


SELECTOR_VERSION = 1


def _clean(value: Any) -> str:
    """Return the Unicode- and whitespace-normal form used by selectors."""
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    return " ".join(normalized.split())


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _digest(kind: str, value: Any, length: int = 24) -> str:
    payload = _canonical_bytes(
        {"kind": kind, "selector_version": SELECTOR_VERSION, "value": value}
    )
    return hashlib.sha256(payload).hexdigest()[:length]


def article_fingerprint(article: dict[str, Any]) -> str:
    """Fingerprint the exact canonical article artifact.

    The fingerprint is intentionally more sensitive than a selector. Any
    article-level change produces a new fingerprint, which lets callers decide
    whether saved anchors need to be checked or remapped.
    """
    return hashlib.sha256(_canonical_bytes(article)).hexdigest()


def utf16_offset(value: str, codepoint_offset: int) -> int:
    """Translate a Python string offset to the UTF-16 offset used by browsers."""
    if not 0 <= codepoint_offset <= len(value):
        raise ValueError("codepoint_offset_out_of_range")
    return len(value[:codepoint_offset].encode("utf-16-le")) // 2


@dataclass(frozen=True)
class SectionSelector:
    selector_version: int
    section_id: str
    source_path: tuple[int, ...]
    title_path: tuple[str, ...]


@dataclass(frozen=True)
class NodeSelector:
    selector_version: int
    node_id: str
    section_id: str
    scope: str
    kind: str
    page: int | None
    section_path: tuple[int, ...] | None
    content_index: int


@dataclass(frozen=True)
class SelectorIndex:
    selector_version: int
    article_fingerprint: str
    sections: tuple[SectionSelector, ...]
    nodes: tuple[NodeSelector, ...]
    abstract_section_id: str

    def section(self, path: tuple[int, ...]) -> SectionSelector:
        return next(item for item in self.sections if item.source_path == path)

    def node(self, section_path: tuple[int, ...], content_index: int) -> NodeSelector:
        return next(
            item
            for item in self.nodes
            if item.scope == "body"
            and item.section_path == section_path
            and item.content_index == content_index
        )

    def abstract_node(self, content_index: int) -> NodeSelector:
        return next(
            item
            for item in self.nodes
            if item.scope == "abstract" and item.content_index == content_index
        )

    def by_node_id(self, node_id: str) -> NodeSelector:
        return next(item for item in self.nodes if item.node_id == node_id)


def _positive_page(value: Any) -> int | None:
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value > 0
        else None
    )


def _normalized_value(value: Any) -> Any:
    if isinstance(value, str):
        return _clean(value)
    if isinstance(value, dict):
        return {str(key): _normalized_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalized_value(item) for item in value]
    return value


def _normalized_location(node: dict[str, Any], target: dict[str, Any] | None) -> Any:
    location: dict[str, Any] = {}
    for key in ("locator", "bbox"):
        if node.get(key) is not None:
            location[f"node_{key}"] = _normalized_value(node[key])
    if target:
        for key in ("locator", "bbox", "caption_bbox"):
            if target.get(key) is not None:
                location[f"target_{key}"] = _normalized_value(target[key])
    return location


def _node_identity(
    node: dict[str, Any],
    figures: dict[str, dict[str, Any]],
    tables: dict[str, dict[str, Any]],
) -> tuple[str, int | None, Any, Any]:
    kind = str(node.get("kind") or "paragraph")
    target: dict[str, Any] | None = None
    if kind == "figure":
        target = figures.get(str(node.get("figure_id")))
        content: Any = {
            "caption": _clean((target or {}).get("caption")),
            "label": _clean((target or {}).get("label")),
        }
    elif kind == "table":
        target = tables.get(str(node.get("table_id")))
        content = {
            "caption": _clean((target or {}).get("caption")),
            "label": _clean((target or {}).get("label")),
            "rows": [
                [_clean(cell) for cell in row]
                for row in (target or {}).get("rows", [])
                if isinstance(row, list)
            ],
        }
    else:
        content = _clean(node.get("text"))
    page = _positive_page(node.get("page")) or _positive_page((target or {}).get("page"))
    return kind, page, content, _normalized_location(node, target)


def build_selector_index(article: dict[str, Any]) -> SelectorIndex:
    """Create deterministic section and content-node selectors for an article."""
    validate_article(article)
    figures = {str(item["id"]): item for item in article["figures"]}
    tables = {str(item["id"]): item for item in article["tables"]}
    section_selectors: list[SectionSelector] = []
    node_selectors: list[NodeSelector] = []

    abstract_section_id = f"s-{_digest('section', {'scope': 'abstract'})}"
    abstract_counts: dict[str, int] = {}
    for index, node in enumerate(article.get("abstract", [])):
        kind, page, content, location = _node_identity(node, figures, tables)
        base = f"n-{_digest('node', [abstract_section_id, kind, content, page, location])}"
        abstract_counts[base] = abstract_counts.get(base, 0) + 1
        occurrence = abstract_counts[base]
        node_selectors.append(
            NodeSelector(
                selector_version=SELECTOR_VERSION,
                node_id=base if occurrence == 1 else f"{base}-{occurrence}",
                section_id=abstract_section_id,
                scope="abstract",
                kind=kind,
                page=page,
                section_path=None,
                content_index=index,
            )
        )

    def visit_sections(
        sections: list[dict[str, Any]],
        parent_path: tuple[int, ...],
        parent_titles: tuple[str, ...],
        parent_identity: tuple[tuple[str, int], ...],
    ) -> None:
        sibling_counts: dict[str, int] = {}
        for section_index, section in enumerate(sections):
            source_path = parent_path + (section_index,)
            title = _clean(section.get("title"))
            sibling_counts[title] = sibling_counts.get(title, 0) + 1
            occurrence = sibling_counts[title]
            title_path = parent_titles + (title,)
            identity_path = parent_identity + ((title, occurrence),)
            section_id = f"s-{_digest('section', {'scope': 'body', 'path': identity_path})}"
            section_selectors.append(
                SectionSelector(
                    selector_version=SELECTOR_VERSION,
                    section_id=section_id,
                    source_path=source_path,
                    title_path=title_path,
                )
            )

            node_counts: dict[str, int] = {}
            for content_index, node in enumerate(section.get("content", [])):
                kind, page, content, location = _node_identity(node, figures, tables)
                base = f"n-{_digest('node', [section_id, kind, content, page, location])}"
                node_counts[base] = node_counts.get(base, 0) + 1
                node_occurrence = node_counts[base]
                node_selectors.append(
                    NodeSelector(
                        selector_version=SELECTOR_VERSION,
                        node_id=(
                            base
                            if node_occurrence == 1
                            else f"{base}-{node_occurrence}"
                        ),
                        section_id=section_id,
                        scope="body",
                        kind=kind,
                        page=page,
                        section_path=source_path,
                        content_index=content_index,
                    )
                )
            visit_sections(
                section.get("sections", []), source_path, title_path, identity_path
            )

    visit_sections(article["sections"], (), (), ())
    return SelectorIndex(
        selector_version=SELECTOR_VERSION,
        article_fingerprint=article_fingerprint(article),
        sections=tuple(section_selectors),
        nodes=tuple(node_selectors),
        abstract_section_id=abstract_section_id,
    )

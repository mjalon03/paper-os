"""Conservative recovery of stable reader anchors after article rebuilds."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .selectors import SelectorIndex, build_selector_index, utf16_offset


@dataclass(frozen=True)
class AnchorRemapResult:
    status: str
    anchor: dict[str, Any] | None
    confidence: float
    reason: str


@dataclass(frozen=True)
class _ContentNode:
    node_id: str
    section_id: str
    scope: str
    page: int | None
    text: str


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def _node_text(
    node: dict[str, Any],
    figures: dict[str, dict[str, Any]],
    tables: dict[str, dict[str, Any]],
) -> str:
    kind = node.get("kind")
    if kind == "figure":
        figure = figures.get(str(node.get("figure_id")), {})
        return _clean(figure.get("caption") or figure.get("label"))
    if kind == "table":
        table = tables.get(str(node.get("table_id")), {})
        cells = [
            _clean(cell)
            for row in table.get("rows", [])
            if isinstance(row, list)
            for cell in row
        ]
        return _clean(" ".join([_clean(table.get("caption")), *cells]))
    return _clean(node.get("text"))


def _content_nodes(
    article: dict[str, Any], index: SelectorIndex
) -> list[_ContentNode]:
    figures = {str(item["id"]): item for item in article["figures"]}
    tables = {str(item["id"]): item for item in article["tables"]}
    output: list[_ContentNode] = []
    for position, node in enumerate(article.get("abstract", [])):
        selector = index.abstract_node(position)
        output.append(
            _ContentNode(
                selector.node_id,
                selector.section_id,
                selector.scope,
                selector.page,
                _node_text(node, figures, tables),
            )
        )

    def visit(sections: list[dict[str, Any]], parent: tuple[int, ...] = ()) -> None:
        for section_position, section in enumerate(sections):
            path = parent + (section_position,)
            for content_position, node in enumerate(section.get("content", [])):
                selector = index.node(path, content_position)
                output.append(
                    _ContentNode(
                        selector.node_id,
                        selector.section_id,
                        selector.scope,
                        selector.page,
                        _node_text(node, figures, tables),
                    )
                )
            visit(section.get("sections", []), path)

    visit(article["sections"])
    return output


def _remapped_anchor(
    old: dict[str, Any],
    node: _ContentNode,
    fingerprint: str,
) -> dict[str, Any]:
    anchor: dict[str, Any] = {
        "selector_version": 1,
        "scope": node.scope,
        "section_id": node.section_id,
        "node_id": node.node_id,
        "article_fingerprint": fingerprint,
    }
    if node.page is not None:
        anchor["page"] = node.page
    exact = old.get("exact_quote")
    if isinstance(exact, str) and exact:
        start = node.text.find(exact)
        if start >= 0:
            end = start + len(exact)
            anchor.update(
                start_utf16=utf16_offset(node.text, start),
                end_utf16=utf16_offset(node.text, end),
                exact_quote=exact,
                prefix=node.text[max(0, start - 120) : start],
                suffix=node.text[end : end + 120],
            )
    return anchor


def remap_anchor(
    old_anchor: dict[str, Any], new_article: dict[str, Any]
) -> AnchorRemapResult:
    """Map an anchor only when evidence is unique and high confidence.

    No fuzzy string similarity is used. An ambiguous or absent quote is kept
    out of the new artifact and explicitly returned as ``needs_review``.
    """

    if not isinstance(old_anchor, dict):
        return AnchorRemapResult("needs_review", None, 0.0, "invalid_anchor")
    index = build_selector_index(new_article)
    nodes = _content_nodes(new_article, index)
    by_id = {node.node_id: node for node in nodes}
    node_id = old_anchor.get("node_id")
    if isinstance(node_id, str) and node_id in by_id:
        status = (
            "exact"
            if old_anchor.get("article_fingerprint") == index.article_fingerprint
            else "remapped"
        )
        return AnchorRemapResult(
            status,
            _remapped_anchor(old_anchor, by_id[node_id], index.article_fingerprint),
            1.0,
            "stable_node_id",
        )

    exact = old_anchor.get("exact_quote")
    if not isinstance(exact, str) or not exact:
        return AnchorRemapResult("needs_review", None, 0.0, "no_exact_quote")
    quote_matches = [node for node in nodes if exact in node.text]
    old_section = old_anchor.get("section_id")
    same_section = [node for node in quote_matches if node.section_id == old_section]
    if len(same_section) == 1:
        return AnchorRemapResult(
            "remapped",
            _remapped_anchor(old_anchor, same_section[0], index.article_fingerprint),
            0.95,
            "unique_quote_in_section",
        )
    if len(quote_matches) == 1:
        return AnchorRemapResult(
            "remapped",
            _remapped_anchor(old_anchor, quote_matches[0], index.article_fingerprint),
            0.9,
            "unique_quote_in_article",
        )

    prefix = old_anchor.get("prefix")
    suffix = old_anchor.get("suffix")
    context_matches = [
        node
        for node in quote_matches
        if (not prefix or f"{prefix}{exact}" in node.text)
        and (not suffix or f"{exact}{suffix}" in node.text)
    ]
    if len(context_matches) == 1 and (prefix or suffix):
        return AnchorRemapResult(
            "remapped",
            _remapped_anchor(old_anchor, context_matches[0], index.article_fingerprint),
            0.9,
            "unique_quote_with_context",
        )
    return AnchorRemapResult("needs_review", None, 0.0, "ambiguous_or_missing_quote")

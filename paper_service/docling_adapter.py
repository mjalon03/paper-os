"""Normalize Docling JSON and verified PDF crops into the article model."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterator

from .article import ARTICLE_SCHEMA_VERSION, READER_VERSION, stable_id, validate_article


END_MATTER = re.compile(
    r"^(?:received\b|submitted\b|accepted\b|references\b)", re.IGNORECASE
)
DROP_CAP = re.compile(r"^([B-HJ-Z])\s+([a-z]{2,})")
PERMISSION_TAIL = re.compile(r"\bwith permission from ref\..*$", re.IGNORECASE)
IGNORED_LABELS = {"page_header", "page_footer", "footnote"}


class DoclingAdapterFailure(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def _resolve(document: dict[str, Any], reference: dict[str, str]) -> dict[str, Any]:
    path = reference.get("$ref", "")
    match = re.fullmatch(r"#/([a-z_]+)/([0-9]+)", path)
    if not match:
        raise DoclingAdapterFailure("docling_reference_invalid")
    collection = document.get(match.group(1))
    index = int(match.group(2))
    if not isinstance(collection, list) or index >= len(collection):
        raise DoclingAdapterFailure("docling_reference_invalid")
    return collection[index]


def _flatten_group(document: dict[str, Any], group: dict[str, Any]) -> Iterator[dict[str, Any]]:
    for reference in group.get("children", []):
        item = _resolve(document, reference)
        path = reference["$ref"]
        if path.startswith("#/groups/"):
            yield from _flatten_group(document, item)
        else:
            yield item


def _page_and_bbox(item: dict[str, Any]) -> tuple[int, dict[str, float]] | None:
    provenance = item.get("prov")
    if not isinstance(provenance, list) or not provenance:
        return None
    first = provenance[0]
    bbox = first.get("bbox")
    page = first.get("page_no")
    if not isinstance(bbox, dict) or not isinstance(page, int):
        return None
    try:
        return page, {key: float(bbox[key]) for key in ("l", "t", "r", "b")}
    except (KeyError, TypeError, ValueError):
        return None


def _page_for_offset(item: dict[str, Any], offset: int) -> int | None:
    for provenance in item.get("prov", []):
        span = provenance.get("charspan")
        page = provenance.get("page_no")
        if (
            isinstance(span, list)
            and len(span) == 2
            and isinstance(page, int)
            and int(span[0]) <= offset <= int(span[1])
        ):
            return page
    located = _page_and_bbox(item)
    return located[0] if located else None


def _caption(document: dict[str, Any], picture: dict[str, Any]) -> tuple[str, dict[str, float] | None]:
    values = []
    bbox = None
    for reference in picture.get("captions", []):
        item = _resolve(document, reference)
        values.append(_clean(item.get("text")))
        located = _page_and_bbox(item)
        if located:
            bbox = located[1]
    return " ".join(value for value in values if value), bbox


def _is_author_line(text: str, authors: Any) -> bool:
    if not text or len(text) > 350:
        return False
    if isinstance(authors, list):
        surnames = [str(author).split()[-1].casefold() for author in authors if str(author).split()]
        if surnames and sum(name in text.casefold() for name in surnames) >= min(2, len(surnames)):
            return True
    return " & " in text and "," in text and not text.endswith(".")


class _Builder:
    def __init__(
        self,
        document: dict[str, Any],
        pdf_path: Path,
        output_dir: Path,
        paper: dict[str, Any],
    ):
        self.document = document
        self.pdf_path = pdf_path
        self.output_dir = output_dir
        self.paper = paper
        self.figures: list[dict[str, Any]] = []
        self.references: list[dict[str, Any]] = []
        self.excluded: list[dict[str, Any]] = []
        self.warnings: list[str] = []
        self.pending_pictures: dict[int, list[dict[str, Any]]] = {}
        self.pending_captions: dict[int, tuple[str, dict[str, float] | None]] = {}
        self.orphan_caption_tails: dict[int, str] = {}
        self.last_figure: dict[str, Any] | None = None
        self.sections: list[dict[str, Any]] = []
        self.current: dict[str, Any] | None = None

    def page_size(self, page: int) -> tuple[float, float]:
        pages = self.document.get("pages", {})
        value = pages.get(str(page), {}) if isinstance(pages, dict) else {}
        size = value.get("size", {})
        try:
            return float(size["width"]), float(size["height"])
        except (KeyError, TypeError, ValueError) as exc:
            raise DoclingAdapterFailure("docling_page_size_missing") from exc

    def useful_picture(self, picture: dict[str, Any]) -> bool:
        located = _page_and_bbox(picture)
        if located is None:
            return False
        page, bbox = located
        width, height = self.page_size(page)
        area = max(0, bbox["r"] - bbox["l"]) * max(0, bbox["t"] - bbox["b"])
        return area / (width * height) >= 0.02

    def start_section(self, title: str, level: int = 1) -> None:
        section = {
            "id": stable_id(title, f"section-{len(self.sections) + 1}"),
            "title": title,
            "level": max(1, level),
            "content": [],
            "sections": [],
        }
        existing = {item["id"] for item in self.sections}
        if section["id"] in existing:
            section["id"] = f'{section["id"]}-{len(self.sections) + 1}'
        self.sections.append(section)
        self.current = section

    def add_paragraph(self, text: str, page: int | None) -> None:
        if self.current is None:
            self.start_section("Introduction")
        text = DROP_CAP.sub(r"\1\2", text)
        previous = next(
            (
                item
                for item in reversed(self.current["content"])
                if item.get("kind") == "paragraph"
            ),
            None,
        )
        if (
            previous
            and (
                previous.get("page") == page
                or previous.get("page") == (page - 1 if page else None)
            )
            and previous["text"].rstrip().casefold().endswith((" and", " or", "-"))
            and text[:1].islower()
        ):
            previous["text"] += " " + text
            return
        node: dict[str, Any] = {"kind": "paragraph", "text": text}
        if page:
            node["page"] = page
        self.current["content"].append(node)

    def add_picture(self, picture: dict[str, Any]) -> None:
        if not self.useful_picture(picture):
            self.excluded.append({"kind": "picture", "reason": "small_page_furniture"})
            return
        page, _bbox = _page_and_bbox(picture)  # useful_picture verified this
        caption, caption_bbox = _caption(self.document, picture)
        pending = self.pending_pictures.setdefault(page, [])
        pending.append(picture)
        if re.match(r"^(?:figure|fig\.)\s*\d+", caption, re.IGNORECASE):
            pending_caption, pending_bbox = self.pending_captions.pop(
                page, (caption, caption_bbox)
            )
            self.emit_figure(page, pending_caption, pending_bbox)

    def emit_figure(
        self,
        page: int,
        caption: str,
        caption_bbox: dict[str, float] | None = None,
    ) -> None:
        pictures = self.pending_pictures.pop(page, [])
        if not pictures:
            self.warnings.append(f"figure_crop_missing:page-{page}")
            return
        boxes = [_page_and_bbox(item)[1] for item in pictures if _page_and_bbox(item)]
        if not boxes:
            return
        width, height = self.page_size(page)
        bbox = {
            "l": min(box["l"] for box in boxes),
            "t": max(box["t"] for box in boxes),
            "r": max(box["r"] for box in boxes),
            "b": min(box["b"] for box in boxes),
        }
        ratio = (bbox["r"] - bbox["l"]) * (bbox["t"] - bbox["b"]) / (width * height)
        if ratio >= 0.9:
            self.warnings.append(f"figure_crop_rejected:page-{page}")
            return
        figure_id = f"figure-{len(self.figures) + 1}"
        asset_name = f"{figure_id}.jpg"
        self.crop(page, bbox, self.output_dir / "assets" / asset_name)
        figure = {
            "id": figure_id,
            "label": caption.split("|", 1)[0].strip(),
            "caption": caption,
            "asset_name": asset_name,
            "asset_path": f"assets/{asset_name}",
            "page": page,
            "bbox": bbox,
            "caption_bbox": caption_bbox,
        }
        self.figures.append(figure)
        self.last_figure = figure
        if self.current is None:
            self.start_section("Introduction")
        self.current["content"].append({"kind": "figure", "figure_id": figure_id})

    def crop(self, page_number: int, bbox: dict[str, float], destination: Path) -> None:
        try:
            import pymupdf
        except ImportError as exc:
            raise DoclingAdapterFailure("pymupdf_unavailable") from exc
        destination.parent.mkdir(parents=True, exist_ok=True)
        with pymupdf.open(self.pdf_path) as pdf:
            if not 1 <= page_number <= pdf.page_count:
                raise DoclingAdapterFailure("docling_page_invalid")
            page = pdf[page_number - 1]
            padding = 4.0
            clip = pymupdf.Rect(
                max(0, bbox["l"] - padding),
                max(0, page.rect.height - bbox["t"] - padding),
                min(page.rect.width, bbox["r"] + padding),
                min(page.rect.height, page.rect.height - bbox["b"] + padding),
            )
            pixmap = page.get_pixmap(matrix=pymupdf.Matrix(2, 2), clip=clip, alpha=False)
            destination.write_bytes(pixmap.tobytes("jpeg", jpg_quality=88))

    def finish_pending(self) -> None:
        for page, (caption, bbox) in list(self.pending_captions.items()):
            self.emit_figure(page, caption, bbox)
        self.pending_captions.clear()
        for page in sorted(self.pending_pictures):
            self.warnings.append(f"unassociated_picture:page-{page}")
        self.pending_pictures.clear()

    def append_caption_continuation(
        self, item: dict[str, Any], text: str, page: int | None
    ) -> bool:
        if not self.last_figure or self.last_figure.get("page") != page:
            return False
        current = _page_and_bbox(item)
        original = self.last_figure.get("caption_bbox")
        if not current or not isinstance(original, dict):
            return False
        bbox = current[1]
        if abs(bbox["t"] - original["t"]) > 5 or abs(bbox["b"] - original["b"]) > 5:
            return False
        self.last_figure["caption"] = f'{self.last_figure["caption"]} {text}'
        return True

    def build(self) -> dict[str, Any]:
        body = self.document.get("body")
        if not isinstance(body, dict) or not isinstance(body.get("children"), list):
            raise DoclingAdapterFailure("docling_body_invalid")
        title = _clean(self.paper.get("title") or self.document.get("name") or "Untitled Paper")
        authors = self.paper.get("authors") if isinstance(self.paper.get("authors"), list) else []
        abstract: list[dict[str, Any]] = []
        main_started = False
        end_matter = False
        for reference in body["children"]:
            item = _resolve(self.document, reference)
            if reference["$ref"].startswith("#/groups/"):
                if end_matter:
                    for grouped in _flatten_group(self.document, item):
                        value = _clean(grouped.get("text"))
                        if value:
                            self.references.append({"id": None, "text": value})
                continue
            if reference["$ref"].startswith("#/pictures/"):
                if not end_matter:
                    self.add_picture(item)
                continue
            label = str(item.get("label") or "")
            text = _clean(item.get("text"))
            location = _page_and_bbox(item)
            page = location[0] if location else None
            if not text:
                continue
            if label in IGNORED_LABELS:
                self.excluded.append({"kind": label, "reason": "publication_furniture", "text": text[:160]})
                continue
            if END_MATTER.match(text):
                end_matter = True
                self.excluded.append({"kind": "end_matter_marker", "reason": "outside_reading_flow", "text": text[:160]})
                continue
            if end_matter:
                self.references.append({"id": None, "text": text})
                continue
            if label == "caption" and re.match(r"^(?:figure|fig\.)\s*\d+", text, re.IGNORECASE):
                if page:
                    tail = self.orphan_caption_tails.pop(page, "")
                    self.pending_captions[page] = (
                        f"{text} {tail}".strip(),
                        location[1] if location else None,
                    )
                continue
            if label == "caption":
                if self.append_caption_continuation(item, text, page):
                    continue
                self.excluded.append({"kind": "caption_fragment", "reason": "inside_figure_asset"})
                continue
            if label == "section_header":
                if text.casefold() == title.casefold():
                    continue
                if page in self.pending_captions:
                    pending_text, pending_bbox = self.pending_captions.pop(page)
                    self.emit_figure(page, pending_text, pending_bbox)
                self.start_section(text, int(item.get("level") or 1))
                main_started = True
                continue
            if _is_author_line(text, authors):
                self.excluded.append({"kind": "author_line", "reason": "kept_in_article_header"})
                continue
            permission = PERMISSION_TAIL.search(text)
            if permission:
                tail_page = _page_for_offset(item, permission.start())
                if tail_page:
                    self.orphan_caption_tails[tail_page] = text[permission.start() :]
                text = text[: permission.start()].rstrip()
            if self.append_caption_continuation(item, text, page):
                continue
            if page in self.pending_captions:
                pending_text, pending_bbox = self.pending_captions.pop(page)
                self.emit_figure(page, pending_text, pending_bbox)
            if not main_started and not abstract and len(text) >= 80:
                abstract.append({"kind": "paragraph", "text": text, **({"page": page} if page else {})})
                main_started = True
                continue
            if label in {"text", "list_item", "caption"}:
                self.add_paragraph(text, page)
                main_started = True
        self.finish_pending()
        if not self.sections:
            raise DoclingAdapterFailure("docling_text_unavailable")
        article = {
            "schema_version": ARTICLE_SCHEMA_VERSION,
            "reader_version": READER_VERSION,
            "identity": {
                "title": title,
                "authors": authors,
                "year": self.paper.get("publication_year"),
                "doi": self.paper.get("doi"),
            },
            "provenance": {"source": "docling_pdf", "docling_version": self.document.get("version")},
            "abstract": abstract,
            "sections": self.sections,
            "figures": self.figures,
            "tables": [],
            "references": self.references,
            "excluded": self.excluded,
            "warnings": self.warnings,
        }
        return validate_article(article)


def adapt_docling(
    document: dict[str, Any],
    pdf_path: Path,
    output_dir: Path,
    paper: dict[str, Any],
) -> dict[str, Any]:
    if document.get("schema_name") != "DoclingDocument":
        raise DoclingAdapterFailure("docling_schema_invalid")
    return _Builder(document, pdf_path, output_dir, paper).build()

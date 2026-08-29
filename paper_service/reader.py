"""Convert registered PDFs into a private, mobile-first reading representation."""

from __future__ import annotations

import html
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from statistics import median
from typing import Any


READER_VERSION = 1
HEADING_WORDS = {
    "abstract",
    "introduction",
    "background",
    "methods",
    "materials and methods",
    "results",
    "discussion",
    "conclusion",
    "conclusions",
    "references",
    "acknowledgements",
    "acknowledgments",
    "supplementary information",
}


class ReaderFailure(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ReaderStats:
    ready: int = 0
    failed: int = 0


def safe_relative_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ReaderFailure("unsafe_reader_path")
    return path


def _join_lines(lines: list[str]) -> str:
    output = ""
    for line in lines:
        line = re.sub("\u00ad\\s*$", "-", line).replace("\u00ad", "")
        cleaned = " ".join(line.split())
        if not cleaned:
            continue
        if output.endswith("-") and cleaned[:1].islower():
            output = output[:-1] + cleaned
        else:
            output = f"{output} {cleaned}".strip()
    return output


def _text_blocks(block: dict[str, Any]) -> list[dict[str, Any]]:
    if block.get("type") != 0 or "lines" not in block:
        return []
    lines: list[str] = []
    sizes: list[float] = []
    bold = False
    spans: list[dict[str, Any]] = []
    for line in block["lines"]:
        pieces: list[str] = []
        for span in line.get("spans", []):
            text = span.get("text", "")
            pieces.append(text)
            if text.strip():
                sizes.append(float(span.get("size", 0)))
                bold = bold or bool(int(span.get("flags", 0)) & 16)
                spans.append(span)
        lines.append("".join(pieces))
    text = _join_lines(lines)
    if not text:
        return []
    x0, y0, x1, y1 = (float(value) for value in block["bbox"])
    combined = {
        "text": text,
        "bbox": (x0, y0, x1, y1),
        "size": max(sizes, default=0),
        "bold": bold,
    }
    if len(spans) < 2 or re.match(r"^(figure|table|box)\s+\d+", text, re.I):
        return [combined]
    first = spans[0]
    first_text = " ".join(str(first.get("text", "")).split()).strip()
    rest_lines = [str(span.get("text", "")) for span in spans[1:]]
    rest_text = _join_lines(rest_lines)
    rest_sizes = [float(span.get("size", 0)) for span in spans[1:] if span.get("text", "").strip()]
    rest_size = median(rest_sizes) if rest_sizes else 0
    first_bold = bool(int(first.get("flags", 0)) & 16)
    if (
        first_text
        and rest_text
        and len(first_text) <= 100
        and len(rest_text) >= 80
        and first_bold
        and float(first.get("size", 0)) >= rest_size * 1.04
    ):
        fx0, fy0, fx1, fy1 = (float(value) for value in first["bbox"])
        return [
            {
                "text": first_text,
                "bbox": (fx0, fy0, fx1, fy1),
                "size": float(first.get("size", 0)),
                "bold": True,
                "forced_kind": "heading",
            },
            {
                "text": rest_text,
                "bbox": (x0, fy0, x1, y1),
                "size": max(rest_sizes, default=rest_size),
                "bold": False,
            },
        ]
    return [combined]


def _split_known_heading(block: dict[str, Any]) -> list[dict[str, Any]]:
    lowered = block["text"].casefold()
    for heading in sorted(HEADING_WORDS, key=len, reverse=True):
        prefix = f"{heading} "
        if lowered.startswith(prefix) and len(block["text"]) > len(prefix) + 30:
            label = block["text"][: len(heading)]
            remainder = block["text"][len(prefix) :].strip()
            return [
                {**block, "text": label, "forced_kind": "heading"},
                {**block, "text": remainder, "bold": False, "forced_kind": "paragraph"},
            ]
    return [block]


def order_page_blocks(blocks: list[dict[str, Any]], page_width: float) -> list[dict[str, Any]]:
    """Order two-column blocks while preserving full-width anchors."""
    if len(blocks) < 2:
        return blocks
    midpoint = page_width / 2
    anchors = [
        block
        for block in blocks
        if block["bbox"][0] < midpoint < block["bbox"][2]
        and block["bbox"][2] - block["bbox"][0] > page_width * 0.55
    ]
    anchor_ids = {id(block) for block in anchors}
    remaining = [block for block in blocks if id(block) not in anchor_ids]
    anchors.sort(key=lambda block: (block["bbox"][1], block["bbox"][0]))

    def order_region(region: list[dict[str, Any]]) -> list[dict[str, Any]]:
        left = [
            block
            for block in region
            if (block["bbox"][0] + block["bbox"][2]) / 2 < midpoint
        ]
        left_ids = {id(block) for block in left}
        right = [block for block in region if id(block) not in left_ids]
        if left and right:
            return sorted(left, key=lambda block: block["bbox"][1]) + sorted(
                right, key=lambda block: block["bbox"][1]
            )
        return sorted(region, key=lambda block: (block["bbox"][1], block["bbox"][0]))

    ordered: list[dict[str, Any]] = []
    used: set[int] = set()
    cursor = -1.0
    for anchor in anchors:
        y0 = anchor["bbox"][1]
        region = [
            block
            for block in remaining
            if id(block) not in used and cursor <= block["bbox"][1] < y0
        ]
        ordered_region = order_region(region)
        ordered.extend(ordered_region)
        used.update(id(block) for block in ordered_region)
        ordered.append(anchor)
        cursor = max(cursor, anchor["bbox"][3])
    tail = [block for block in remaining if id(block) not in used]
    ordered.extend(order_region(tail))
    return ordered


def classify_block(block: dict[str, Any], body_size: float) -> str:
    if block.get("forced_kind"):
        return str(block["forced_kind"])
    text = block["text"].strip()
    lowered = text.casefold().rstrip(":.")
    if re.match(r"^(figure|table|box)\s+\d+", text, re.IGNORECASE):
        return "caption"
    known_heading = lowered in HEADING_WORDS
    visual_heading = (
        len(text) <= 120
        and not text.endswith(".")
        and block["bold"]
        and block["size"] >= body_size * 1.04
    )
    return "heading" if known_heading or visual_heading else "paragraph"


def extract_document(
    pdf_path: Path, paper: dict[str, Any], output_dir: Path
) -> dict[str, Any]:
    try:
        import pymupdf
    except ImportError as exc:
        raise ReaderFailure("pymupdf_unavailable") from exc

    try:
        document = pymupdf.open(pdf_path)
    except Exception as exc:
        raise ReaderFailure("reader_pdf_open_failed") from exc
    if document.needs_pass:
        document.close()
        raise ReaderFailure("reader_pdf_encrypted")

    page_count = document.page_count
    assets_dir = output_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    blocks_out: list[dict[str, Any]] = []
    headings: list[dict[str, Any]] = []
    title = str(paper.get("title") or "Untitled Paper")
    try:
        for page_index, page in enumerate(document):
            raw = page.get_text("dict", sort=False)
            page_blocks: list[dict[str, Any]] = []
            for raw_block in raw.get("blocks", []):
                extracted_blocks = [
                    split
                    for block in _text_blocks(raw_block)
                    for split in _split_known_heading(block)
                ]
                for block in extracted_blocks:
                    y0, y1 = block["bbox"][1], block["bbox"][3]
                    lowered = block["text"].casefold().strip()
                    if page_index > 0 and y0 < 48:
                        continue
                    if y1 > page.rect.height - 34:
                        continue
                    if page_index == 0 and y0 > page.rect.height * 0.84:
                        continue
                    if page_index == 0 and lowered == title.casefold():
                        continue
                    if lowered in {"review", "insight review", "review insight"}:
                        continue
                    if lowered.startswith("doi:"):
                        continue
                    if re.fullmatch(r"[a-z](?:\s+[a-z]){1,5}", lowered):
                        continue
                    page_blocks.append(block)
            likely_body_sizes = [
                block["size"] for block in page_blocks if 6 <= block["size"] <= 14
            ]
            body_size = median(likely_body_sizes) if likely_body_sizes else 9.0
            ordered = order_page_blocks(page_blocks, page.rect.width)
            for index in range(len(ordered) - 1):
                current = ordered[index]
                following = ordered[index + 1]
                if (
                    len(current["text"]) == 1
                    and current["text"].isalpha()
                    and current["text"].isupper()
                ):
                    following["text"] = current["text"] + following["text"]
                    current["text"] = ""
            ordered = [block for block in ordered if block["text"]]
            page_number = page_index + 1
            page_asset = f"assets/page-{page_number:03d}.jpg"
            pixmap = page.get_pixmap(
                matrix=pymupdf.Matrix(1.35, 1.35), alpha=False
            )
            asset_path = output_dir / page_asset
            asset_path.write_bytes(pixmap.tobytes("jpeg", jpg_quality=78))
            os.chmod(asset_path, 0o640)
            for block in ordered:
                kind = classify_block(block, body_size)
                item: dict[str, Any] = {
                    "kind": kind,
                    "text": block["text"],
                    "page": page_number,
                }
                if kind == "caption":
                    item["page_asset"] = page_asset
                if kind == "heading":
                    item["anchor"] = f"section-{len(headings) + 1}"
                    headings.append(
                        {
                            "text": block["text"],
                            "anchor": item["anchor"],
                            "page": page_number,
                        }
                    )
                blocks_out.append(item)
    except ReaderFailure:
        raise
    except Exception as exc:
        raise ReaderFailure("reader_extract_failed") from exc
    finally:
        document.close()

    if not blocks_out:
        raise ReaderFailure("reader_text_unavailable")
    return {
        "schema_version": 1,
        "reader_version": READER_VERSION,
        "paper_id": int(paper["id"]),
        "title": title,
        "authors": paper.get("authors") or [],
        "year": paper.get("publication_year"),
        "doi": paper.get("doi"),
        "category": paper.get("category"),
        "paper_type": paper.get("paper_type"),
        "page_count": page_count,
        "headings": headings,
        "blocks": blocks_out,
    }


def convert_paper(paper: dict[str, Any], data_dir: Path) -> str:
    raw_relative = safe_relative_path(str(paper["stored_path"]))
    raw_path = data_dir / "library" / "raw" / raw_relative
    if not raw_path.is_file():
        raise ReaderFailure("reader_raw_missing")
    relative_manifest = (
        PurePosixPath(str(paper["id"])) / f"v{READER_VERSION}" / "manifest.json"
    )
    final_dir = (
        data_dir / "library" / "mobile" / str(paper["id"]) / f"v{READER_VERSION}"
    )
    if final_dir.is_dir() and (final_dir / "manifest.json").is_file():
        return relative_manifest.as_posix()
    if final_dir.exists():
        raise ReaderFailure("reader_output_incomplete")
    final_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="reader-", dir=final_dir.parent
    ) as temporary:
        temporary_dir = Path(temporary)
        manifest = extract_document(raw_path, paper, temporary_dir)
        manifest_path = temporary_dir / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        os.chmod(manifest_path, 0o640)
        os.replace(temporary_dir, final_dir)
    return relative_manifest.as_posix()


def _authors_text(authors: Any) -> str:
    if not isinstance(authors, list):
        return "Unknown authors"
    cleaned = [str(author) for author in authors if str(author).strip()]
    return ", ".join(cleaned) if cleaned else "Unknown authors"


def _shell(title: str, body: str, script: str = "") -> str:
    safe_title = html.escape(title)
    return f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="light dark"><title>{safe_title}</title>
<style>
:root{{--paper:#f7f3ea;--ink:#20211f;--muted:#696b66;--accent:#176b65;--line:#d8d2c5;--card:#fffdf7;--measure:43rem;--scale:1}}
@media(prefers-color-scheme:dark){{:root{{--paper:#161817;--ink:#ece9df;--muted:#a8aaa4;--accent:#74c9bd;--line:#343936;--card:#202320}}}}
*{{box-sizing:border-box}}html{{scroll-behavior:smooth}}body{{margin:0;background:var(--paper);color:var(--ink);font-family:ui-serif,Georgia,"Times New Roman",serif}}
a{{color:var(--accent)}}.wrap{{width:min(100% - 2rem,var(--measure));margin:auto}}.top{{position:sticky;top:0;z-index:5;background:color-mix(in srgb,var(--paper) 92%,transparent);backdrop-filter:blur(12px);border-bottom:1px solid var(--line)}}
.bar{{height:3px;background:var(--line)}}#progress{{height:100%;width:0;background:var(--accent)}}.nav{{display:flex;align-items:center;gap:.65rem;min-height:3.25rem;font-family:ui-sans-serif,system-ui,sans-serif}}
.nav a,.nav button{{border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:999px;padding:.45rem .7rem;text-decoration:none;font:inherit;cursor:pointer}}.spacer{{flex:1}}
.hero{{padding:2.8rem 0 2rem;border-bottom:1px solid var(--line)}}.eyebrow{{font:600 .76rem/1.2 ui-sans-serif,system-ui;color:var(--accent);letter-spacing:.08em;text-transform:uppercase}}
h1{{font-size:clamp(2rem,8vw,3.4rem);line-height:1.02;letter-spacing:-.035em;margin:.55rem 0 1rem}}.meta{{color:var(--muted);font:400 .95rem/1.55 ui-sans-serif,system-ui,sans-serif}}
main.reader{{padding:1.8rem 0 20vh;font-size:calc(1.12rem * var(--scale));line-height:1.78}}.reader p{{margin:0 0 1.25em;text-wrap:pretty}}.reader h2{{font-size:1.55em;line-height:1.22;margin:2.5em 0 .8em;color:var(--accent);scroll-margin-top:4.5rem}}
.page-tag{{display:flex;align-items:center;gap:.7rem;margin:2.5rem 0 1.2rem;color:var(--muted);font:600 .75rem/1 ui-sans-serif,system-ui,sans-serif}}.page-tag:before{{content:"";height:1px;background:var(--line);flex:1}}.page-tag a{{text-decoration:none}}
figure{{margin:1.8rem 0 2.2rem;padding:.7rem;background:var(--card);border:1px solid var(--line);border-radius:1rem}}figure img{{display:block;width:100%;height:auto;border-radius:.55rem}}figcaption{{padding:.8rem .35rem .2rem;color:var(--muted);font:.88rem/1.5 ui-sans-serif,system-ui,sans-serif}}
details.toc{{margin:1rem 0;border:1px solid var(--line);border-radius:.8rem;background:var(--card);padding:.8rem 1rem;font-family:ui-sans-serif,system-ui,sans-serif}}.toc li{{margin:.55rem 0}}.toc a{{text-decoration:none}}
.library{{display:grid;gap:1rem;padding:2rem 0}}.paper-card{{display:block;padding:1.2rem;border:1px solid var(--line);border-radius:1rem;background:var(--card);text-decoration:none;color:var(--ink)}}.paper-card:hover{{border-color:var(--accent)}}.paper-card h2{{margin:.35rem 0 .55rem;font-size:1.3rem}}.status{{font:600 .75rem/1 ui-sans-serif,system-ui;color:var(--accent)}}
body.force-dark{{--paper:#161817;--ink:#ece9df;--muted:#a8aaa4;--accent:#74c9bd;--line:#343936;--card:#202320}}@media(min-width:48rem){{.wrap{{width:min(100% - 4rem,var(--measure))}}}}
</style></head><body>{body}<script>{script}</script></body></html>"""


def render_library(papers: list[dict[str, Any]]) -> str:
    cards: list[str] = []
    for paper in papers:
        title = html.escape(str(paper.get("title") or "Untitled Paper"))
        meta = " · ".join(
            html.escape(str(value))
            for value in (
                paper.get("publication_year"),
                paper.get("category"),
                paper.get("paper_type"),
            )
            if value
        )
        reader_status = str(paper.get("reader_status") or "pending")
        if reader_status == "ready":
            cards.append(
                f'<a class="paper-card" href="/read/{int(paper["id"])}"><span class="status">READY</span><h2>{title}</h2><div class="meta">{meta}</div></a>'
            )
        else:
            cards.append(
                f'<div class="paper-card"><span class="status">{html.escape(reader_status.upper())}</span><h2>{title}</h2><div class="meta">{meta}</div></div>'
            )
    content = "".join(cards) or '<p class="meta">아직 정리된 논문이 없습니다.</p>'
    body = f'<header class="hero"><div class="wrap"><div class="eyebrow">Personal paper library</div><h1>읽을 논문</h1><div class="meta">모바일 읽기용으로 변환된 개인 라이브러리</div></div></header><main class="wrap library">{content}</main>'
    return _shell("Paper Library", body)


def render_reader(paper: dict[str, Any], manifest: dict[str, Any]) -> str:
    paper_id = int(paper["id"])
    title = str(paper.get("title") or manifest.get("title") or "Untitled Paper")
    authors = html.escape(_authors_text(paper.get("authors")))
    meta = " · ".join(
        html.escape(str(value))
        for value in (
            paper.get("publication_year"),
            paper.get("category"),
            paper.get("paper_type"),
        )
        if value
    )
    toc = "".join(
        f'<li><a href="#{html.escape(str(item["anchor"]))}">{html.escape(str(item["text"]))}</a></li>'
        for item in manifest.get("headings", [])
    )
    rendered: list[str] = []
    last_page = None
    for block in manifest.get("blocks", []):
        page = int(block.get("page", 0))
        if page != last_page:
            rendered.append(
                f'<div class="page-tag" id="page-{page}"><a href="/read/{paper_id}/assets/page-{page:03d}.jpg" target="_blank" rel="noopener">원본 {page}쪽</a></div>'
            )
            last_page = page
        text = html.escape(str(block.get("text", "")))
        kind = block.get("kind")
        if kind == "heading":
            anchor = html.escape(str(block.get("anchor", "")))
            rendered.append(f'<h2 id="{anchor}">{text}</h2>')
        elif kind == "caption":
            asset = html.escape(str(block.get("page_asset", "")))
            rendered.append(
                f'<figure><a href="/read/{paper_id}/{asset}" target="_blank" rel="noopener"><img src="/read/{paper_id}/{asset}" loading="lazy" alt="원본 {page}쪽"></a><figcaption>{text}</figcaption></figure>'
            )
        else:
            rendered.append(f"<p>{text}</p>")
    doi = paper.get("doi")
    doi_html = (
        f' · <a href="https://doi.org/{html.escape(str(doi))}" target="_blank" rel="noopener">DOI</a>'
        if doi
        else ""
    )
    body = f'''<header class="top"><div class="bar"><div id="progress"></div></div><div class="wrap nav"><a href="/library">← 목록</a><button id="smaller" aria-label="글자 작게">A−</button><button id="larger" aria-label="글자 크게">A+</button><button id="theme" aria-label="테마 변경">◐</button><span class="spacer"></span><a href="/read/{paper_id}/source.pdf" target="_blank">PDF</a></div></header>
<section class="hero"><div class="wrap"><div class="eyebrow">{html.escape(str(paper.get("category") or "Paper"))}</div><h1>{html.escape(title)}</h1><div class="meta">{authors}<br>{meta}{doi_html}</div></div></section>
<main class="wrap reader"><details class="toc"><summary>목차</summary><ol>{toc}</ol></details>{''.join(rendered)}</main>'''
    script = f'''const root=document.documentElement,key="paper-reader-{paper_id}";let scale=Number(localStorage.getItem(key+"-scale")||1);root.style.setProperty("--scale",scale);document.querySelector("#larger").onclick=()=>{{scale=Math.min(1.35,scale+.08);root.style.setProperty("--scale",scale);localStorage.setItem(key+"-scale",scale)}};document.querySelector("#smaller").onclick=()=>{{scale=Math.max(.85,scale-.08);root.style.setProperty("--scale",scale);localStorage.setItem(key+"-scale",scale)}};document.querySelector("#theme").onclick=()=>{{document.body.classList.toggle("force-dark");localStorage.setItem(key+"-dark",document.body.classList.contains("force-dark"))}};if(localStorage.getItem(key+"-dark")==="true")document.body.classList.add("force-dark");let ticking=false;function update(){{const max=document.documentElement.scrollHeight-innerHeight,p=max>0?scrollY/max:0;document.querySelector("#progress").style.width=(p*100)+"%";localStorage.setItem(key+"-scroll",scrollY);ticking=false}}addEventListener("scroll",()=>{{if(!ticking){{requestAnimationFrame(update);ticking=true}}}},{{passive:true}});const saved=Number(localStorage.getItem(key+"-scroll")||0);if(saved>0)requestAnimationFrame(()=>scrollTo(0,saved));update();'''
    return _shell(title, body, script)


def read_manifest(data_dir: Path, relative_path: str) -> dict[str, Any]:
    relative = safe_relative_path(relative_path)
    root = (data_dir / "library" / "mobile").resolve()
    path = (root / relative).resolve()
    if root not in path.parents or not path.is_file():
        raise ReaderFailure("reader_manifest_missing")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReaderFailure("reader_manifest_invalid") from exc
    if data.get("schema_version") != 1:
        raise ReaderFailure("reader_manifest_invalid")
    return data

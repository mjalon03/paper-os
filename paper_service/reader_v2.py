"""Build version-two reader artifacts while v1 remains live."""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from .article import ArticleValidationError, validate_article
from .docling_adapter import DoclingAdapterFailure, adapt_docling
from .jats import JATSFailure, parse_jats
from .pmc import Fetch, PMCFailure, _fetch, discover_article, download_media, fetch_jats, media_urls


DISPLAY_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp"}


class ReaderV2Failure(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def build_pmc_article(
    paper: dict[str, Any],
    output_dir: Path,
    fetch: Fetch = _fetch,
) -> dict[str, Any] | None:
    doi = str(paper.get("doi") or "").strip()
    if not doi:
        return None
    metadata = discover_article(doi, fetch)
    if metadata is None:
        return None
    provenance = {
        "source": "pmc_jats",
        "pmcid": metadata["resolved_pmcid"],
        "version": metadata["resolved_version"],
        "license": metadata.get("license_code"),
        "is_retracted": bool(metadata.get("is_retracted")),
        "xml_url": metadata["xml_url"],
    }
    article = parse_jats(fetch_jats(metadata, fetch), provenance)
    available = media_urls(metadata)
    assets_dir = output_dir / "assets"
    for figure in article["figures"]:
        name = figure.get("asset_name")
        url = available.get(name) if isinstance(name, str) else None
        if not url or Path(name).suffix.casefold() not in DISPLAY_IMAGE_SUFFIXES:
            figure["asset_path"] = None
            if f"figure_asset_missing:{figure['id']}" not in article["warnings"]:
                article["warnings"].append(f"figure_asset_missing:{figure['id']}")
            continue
        destination, md5 = download_media(url, assets_dir, name, fetch)
        figure["asset_path"] = f"assets/{destination.name}"
        figure["asset_md5"] = md5
    if provenance["is_retracted"]:
        article["warnings"].append("article_retracted")
    return validate_article(article)


def write_article(article: dict[str, Any], output_dir: Path) -> Path:
    validate_article(article)
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / "article.json"
    temporary = output_dir / "article.json.tmp"
    payload = json.dumps(article, ensure_ascii=False, separators=(",", ":")).encode()
    temporary.write_bytes(payload)
    os.chmod(temporary, 0o640)
    temporary.replace(destination)
    return destination


def raw_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def run_docling(pdf_path: Path, scratch_dir: Path) -> dict[str, Any]:
    try:
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.document_converter import DocumentConverter, PdfFormatOption
    except ImportError as exc:
        raise ReaderV2Failure("docling_unavailable") from exc
    options = PdfPipelineOptions()
    options.do_ocr = False
    options.do_table_structure = True
    options.generate_page_images = False
    options.generate_picture_images = True
    options.images_scale = 1.5
    converter = DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)}
    )
    try:
        result = converter.convert(pdf_path)
        if not str(result.status).endswith("SUCCESS"):
            raise ReaderV2Failure("docling_conversion_incomplete")
        path = scratch_dir / "docling.json"
        result.document.save_as_json(path)
        return json.loads(path.read_text(encoding="utf-8"))
    except ReaderV2Failure:
        raise
    except Exception as exc:
        raise ReaderV2Failure("docling_conversion_failed") from exc


def convert_paper_v2(paper: dict[str, Any], data_dir: Path) -> tuple[str, str]:
    paper_id = int(paper["id"])
    raw_relative = Path(str(paper["stored_path"]))
    if raw_relative.is_absolute() or ".." in raw_relative.parts:
        raise ReaderV2Failure("reader_v2_raw_path_invalid")
    raw_path = data_dir / "library" / "raw" / raw_relative
    if not raw_path.is_file():
        raise ReaderV2Failure("reader_v2_raw_missing")
    if raw_sha256(raw_path) != str(paper["sha256"]):
        raise ReaderV2Failure("reader_v2_raw_hash_mismatch")
    final_dir = data_dir / "library" / "mobile" / str(paper_id) / "v2"
    relative_manifest = f"{paper_id}/v2/article.json"
    if (final_dir / "article.json").is_file():
        article = read_article(data_dir, paper_id)
        return relative_manifest, str(article["provenance"]["source"])
    if final_dir.exists():
        raise ReaderV2Failure("reader_v2_output_incomplete")
    final_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="reader-v2-", dir=final_dir.parent) as temporary:
        output_dir = Path(temporary)
        article = None
        try:
            article = build_pmc_article(paper, output_dir)
        except (PMCFailure, JATSFailure, ArticleValidationError):
            shutil.rmtree(output_dir / "assets", ignore_errors=True)
        if article is None:
            docling = run_docling(raw_path, output_dir)
            try:
                article = adapt_docling(docling, raw_path, output_dir, paper)
            except DoclingAdapterFailure as exc:
                raise ReaderV2Failure(exc.code) from exc
        write_article(article, output_dir)
        if raw_sha256(raw_path) != str(paper["sha256"]):
            raise ReaderV2Failure("reader_v2_raw_changed")
        os.replace(output_dir, final_dir)
    return relative_manifest, str(article["provenance"]["source"])


def read_article(data_dir: Path, paper_id: int) -> dict[str, Any]:
    root = (data_dir / "library" / "mobile").resolve()
    path = (root / str(paper_id) / "v2" / "article.json").resolve()
    if root not in path.parents or not path.is_file():
        raise FileNotFoundError("reader_v2_missing")
    try:
        article = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("reader_v2_invalid") from exc
    return validate_article(article)


def v2_asset_path(data_dir: Path, paper_id: int, asset_name: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,180}", asset_name):
        raise FileNotFoundError("reader_v2_asset_invalid")
    root = (data_dir / "library" / "mobile" / str(paper_id) / "v2" / "assets").resolve()
    path = (root / asset_name).resolve()
    if path.parent != root or not path.is_file():
        raise FileNotFoundError("reader_v2_asset_missing")
    return path


def _authors(value: Any) -> str:
    if not isinstance(value, list):
        return "Unknown authors"
    names = [_clean(author) for author in value if _clean(author)]
    return ", ".join(names) or "Unknown authors"


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def _section_list(sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for section in sections:
        output.append(section)
        output.extend(_section_list(section["sections"]))
    return output


def _render_node(
    node: dict[str, Any],
    figures: dict[str, dict[str, Any]],
    tables: dict[str, dict[str, Any]],
    paper_id: int,
) -> str:
    kind = node["kind"]
    if kind == "paragraph":
        return f'<p>{html.escape(_clean(node.get("text")))}</p>'
    if kind == "list":
        return f'<div class="article-list">{html.escape(_clean(node.get("text")))}</div>'
    if kind == "equation":
        return f'<pre class="equation">{html.escape(_clean(node.get("text")))}</pre>'
    if kind == "figure":
        figure = figures.get(str(node.get("figure_id")))
        if not figure:
            return ""
        caption = html.escape(_clean(figure.get("caption") or figure.get("label")))
        page = figure.get("page")
        source = (
            f'<a class="source-page" href="/read/{paper_id}/source.pdf#page={int(page)}" target="_blank" rel="noopener">원본 {int(page)}쪽</a>'
            if isinstance(page, int) and page > 0
            else ""
        )
        asset = figure.get("asset_path")
        image = ""
        if isinstance(asset, str) and re.fullmatch(r"assets/[A-Za-z0-9._-]{1,180}", asset):
            name = html.escape(asset.split("/", 1)[1])
            image = f'<button class="figure-open" type="button" aria-label="그림 확대"><img src="/read/{paper_id}/v2/assets/{name}" loading="lazy" alt="{html.escape(_clean(figure.get("label") or "논문 그림"))}"></button>'
        return f'<figure id="{html.escape(str(figure["id"]))}">{image}<figcaption>{caption} {source}</figcaption></figure>'
    if kind == "table":
        table = tables.get(str(node.get("table_id")))
        if not table:
            return ""
        rows = []
        for index, row in enumerate(table.get("rows", [])):
            tag = "th" if index == 0 else "td"
            rows.append("<tr>" + "".join(f"<{tag}>{html.escape(_clean(cell))}</{tag}>" for cell in row) + "</tr>")
        caption = html.escape(_clean(table.get("caption") or table.get("label")))
        return f'<figure class="table-card"><div class="table-scroll"><table>{"".join(rows)}</table></div><figcaption>{caption}</figcaption></figure>'
    return ""


def _render_section(
    section: dict[str, Any],
    figures: dict[str, dict[str, Any]],
    tables: dict[str, dict[str, Any]],
    paper_id: int,
) -> str:
    level = min(4, int(section["level"]) + 1)
    content = "".join(_render_node(node, figures, tables, paper_id) for node in section["content"])
    children = "".join(_render_section(child, figures, tables, paper_id) for child in section["sections"])
    return f'<section class="article-section" data-section="{html.escape(_clean(section["title"]))}"><h{level} id="{html.escape(str(section["id"]))}">{html.escape(_clean(section["title"]))}</h{level}>{content}{children}</section>'


def render_reader_v2(paper: dict[str, Any], article: dict[str, Any]) -> str:
    validate_article(article)
    paper_id = int(paper["id"])
    identity = article["identity"]
    title = _clean(identity.get("title") or paper.get("title") or "Untitled Paper")
    figures = {str(item["id"]): item for item in article["figures"]}
    tables = {str(item["id"]): item for item in article["tables"]}
    abstract = "".join(
        f'<p>{html.escape(_clean(node.get("text")))}</p>' for node in article.get("abstract", [])
    )
    flat_sections = _section_list(article["sections"])
    toc = "".join(
        f'<li class="level-{min(3, int(section["level"]))}"><a href="#{html.escape(str(section["id"]))}">{html.escape(_clean(section["title"]))}</a></li>'
        for section in flat_sections
    )
    body = "".join(_render_section(section, figures, tables, paper_id) for section in article["sections"])
    references = "".join(
        f'<li>{html.escape(_clean(reference.get("text")))}</li>'
        for reference in article["references"]
    )
    reference_panel = (
        f'<details class="secondary"><summary>참고문헌 <span>{len(article["references"])}</span></summary><ol class="references">{references}</ol></details>'
        if references
        else ""
    )
    meta = " · ".join(
        html.escape(_clean(value))
        for value in (identity.get("year"), paper.get("category"), paper.get("paper_type"))
        if value
    )
    doi = identity.get("doi")
    doi_link = (
        f'<a href="https://doi.org/{html.escape(_clean(doi))}" target="_blank" rel="noopener">DOI</a>'
        if doi
        else ""
    )
    source_label = "PMC JATS" if article["provenance"]["source"] == "pmc_jats" else "PDF structure"
    return f'''<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="color-scheme" content="light dark"><title>{html.escape(title)}</title>
<style>
:root{{--bg:#f6f4ee;--surface:#fffdf8;--text:#20211f;--muted:#696d68;--accent:#146d65;--line:#d9d5ca;--measure:42rem;--scale:1}}@media(prefers-color-scheme:dark){{:root{{--bg:#151817;--surface:#202422;--text:#eeeae0;--muted:#a8aea9;--accent:#78d1c3;--line:#343a37}}}}*{{box-sizing:border-box}}html{{scroll-behavior:smooth}}body{{margin:0;background:var(--bg);color:var(--text);font-family:ui-serif,Georgia,"Times New Roman",serif}}body.theme-light{{--bg:#f6f4ee;--surface:#fffdf8;--text:#20211f;--muted:#696d68;--accent:#146d65;--line:#d9d5ca}}body.theme-dark{{--bg:#151817;--surface:#202422;--text:#eeeae0;--muted:#a8aea9;--accent:#78d1c3;--line:#343a37}}a{{color:var(--accent)}}.measure{{width:min(calc(100% - 2rem),var(--measure));margin:auto}}.top{{position:sticky;top:0;z-index:20;background:color-mix(in srgb,var(--bg) 94%,transparent);backdrop-filter:blur(14px);border-bottom:1px solid var(--line)}}.progress-track{{height:3px;background:var(--line)}}#progress{{height:100%;width:0;background:var(--accent)}}.toolbar{{min-height:3rem;display:flex;align-items:center;gap:.45rem;font:600 .78rem/1 ui-sans-serif,system-ui,sans-serif}}.toolbar a,.toolbar button{{padding:.45rem .62rem;border:1px solid var(--line);border-radius:999px;background:var(--surface);color:var(--text);text-decoration:none;font:inherit}}#current{{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--muted);flex:1;text-align:center}}.hero{{padding:2.5rem 0 1.6rem;border-bottom:1px solid var(--line)}}.eyebrow{{color:var(--accent);font:700 .72rem/1.3 ui-sans-serif,system-ui;letter-spacing:.08em;text-transform:uppercase}}h1{{font-size:clamp(2rem,9vw,3.5rem);line-height:1.04;letter-spacing:-.035em;margin:.55rem 0 1rem}}.meta{{color:var(--muted);font:.92rem/1.55 ui-sans-serif,system-ui,sans-serif}}.start{{display:inline-block;margin-top:1.2rem;padding:.75rem 1rem;border-radius:999px;background:var(--accent);color:var(--bg);text-decoration:none;font:700 .9rem/1 ui-sans-serif,system-ui,sans-serif}}.abstract{{padding:1.5rem 0;border-bottom:1px solid var(--line)}}.abstract h2{{font:700 .8rem/1 ui-sans-serif,system-ui;color:var(--accent);letter-spacing:.06em;text-transform:uppercase}}.reader{{padding:1.5rem 0 18vh;font-size:calc(1.12rem * var(--scale));line-height:1.78}}.reader p{{margin:0 0 1.25em;text-wrap:pretty}}.article-section h2,.article-section h3,.article-section h4{{line-height:1.22;scroll-margin-top:4rem;color:var(--accent);margin:2.5em 0 .8em}}.article-section h2{{font-size:1.55em}}.article-section h3{{font-size:1.25em}}.toc,.secondary{{border:1px solid var(--line);background:var(--surface);border-radius:.9rem;padding:.8rem 1rem;margin:0 0 1.5rem;font:400 .9rem/1.5 ui-sans-serif,system-ui,sans-serif}}summary{{cursor:pointer;font-weight:700}}.toc ol{{padding-left:1.35rem}}.toc li{{margin:.5rem 0}}.toc .level-2{{margin-left:.8rem}}figure{{margin:2rem 0;padding:.65rem;background:var(--surface);border:1px solid var(--line);border-radius:1rem}}.figure-open{{display:block;width:100%;padding:0;border:0;background:none;cursor:zoom-in}}figure img{{display:block;width:100%;height:auto;border-radius:.55rem}}figcaption{{padding:.75rem .3rem .15rem;color:var(--muted);font:.86rem/1.55 ui-sans-serif,system-ui,sans-serif}}.source-page{{white-space:nowrap}}.table-scroll{{overflow:auto}}table{{border-collapse:collapse;width:100%;font:.82rem/1.4 ui-sans-serif,system-ui,sans-serif}}th,td{{border:1px solid var(--line);padding:.5rem;text-align:left;vertical-align:top}}.references{{padding-left:1.4rem}}.references li{{margin:.65rem 0;color:var(--muted)}}dialog{{max-width:min(96vw,70rem);max-height:94vh;padding:.5rem;border:0;border-radius:1rem;background:var(--surface)}}dialog::backdrop{{background:#000b}}dialog img{{display:block;max-width:100%;max-height:88vh}}dialog button{{position:absolute;right:1rem;top:1rem;border:0;border-radius:999px;background:#111c;color:white;width:2.2rem;height:2.2rem}}@media(min-width:48rem){{.measure{{width:min(calc(100% - 4rem),var(--measure))}}}}
</style></head><body><header class="top"><div class="progress-track"><div id="progress"></div></div><div class="measure toolbar"><a href="/library">← 목록</a><button id="tocButton" aria-label="목차 열기">목차</button><span id="current">시작</span><button id="size" aria-label="글자 크기">가</button><button id="theme" aria-label="테마">◐</button><a href="/read/{paper_id}/source.pdf" target="_blank">PDF</a></div></header>
<section class="hero"><div class="measure"><div class="eyebrow">Reader v2 · {source_label}</div><h1>{html.escape(title)}</h1><div class="meta">{html.escape(_authors(identity.get("authors")))}<br>{meta} {doi_link}</div><a class="start" href="#{html.escape(str(flat_sections[0]["id"])) if flat_sections else "article"}" id="start">읽기 시작</a></div></section>
{f'<section class="abstract"><div class="measure"><h2>Abstract</h2>{abstract}</div></section>' if abstract else ''}<main class="measure reader" id="article"><details class="toc" id="toc"><summary>논문 구조</summary><ol>{toc}</ol></details>{body}{reference_panel}</main><dialog id="zoom"><button type="button" aria-label="닫기">×</button><img alt="확대된 논문 그림"></dialog>
<script>const key="paper-reader-v2-{paper_id}",root=document.documentElement,body=document.body;let size=Number(localStorage.getItem(key+"-size")||1);root.style.setProperty("--scale",size);document.querySelector("#size").onclick=()=>{{size=size>=1.3?.9:size+.1;root.style.setProperty("--scale",size);localStorage.setItem(key+"-size",size)}};const themes=["system","light","dark"];let theme=localStorage.getItem(key+"-theme")||"system";function paint(){{body.classList.remove("theme-light","theme-dark");if(theme!=="system")body.classList.add("theme-"+theme)}}paint();document.querySelector("#theme").onclick=()=>{{theme=themes[(themes.indexOf(theme)+1)%themes.length];localStorage.setItem(key+"-theme",theme);paint()}};const toc=document.querySelector("#toc");document.querySelector("#tocButton").onclick=()=>{{toc.open=!toc.open;toc.scrollIntoView({{block:"start"}})}};const saved=Number(localStorage.getItem(key+"-scroll")||0),start=document.querySelector("#start");if(saved>40){{start.textContent="이어 읽기";start.onclick=e=>{{e.preventDefault();scrollTo(0,saved)}}}}let ticking=false;function update(){{const max=document.documentElement.scrollHeight-innerHeight,p=max>0?scrollY/max:0;document.querySelector("#progress").style.width=(p*100)+"%";localStorage.setItem(key+"-scroll",scrollY);ticking=false}}addEventListener("scroll",()=>{{if(!ticking){{requestAnimationFrame(update);ticking=true}}}},{{passive:true}});const current=document.querySelector("#current"),sections=[...document.querySelectorAll(".article-section")];const observer=new IntersectionObserver(entries=>{{entries.filter(e=>e.isIntersecting).forEach(e=>current.textContent=e.target.dataset.section)}},{{rootMargin:"-20% 0px -70% 0px"}});sections.forEach(s=>observer.observe(s));const zoom=document.querySelector("#zoom"),zoomImage=zoom.querySelector("img");document.querySelectorAll(".figure-open img").forEach(img=>img.parentElement.onclick=()=>{{zoomImage.src=img.src;zoomImage.alt=img.alt;zoom.showModal()}});zoom.querySelector("button").onclick=()=>zoom.close();zoom.onclick=e=>{{if(e.target===zoom)zoom.close()}};update();</script></body></html>'''

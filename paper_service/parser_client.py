"""Strict client and artifact installer for the credential-free parser service."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import build_opener, HTTPRedirectHandler, Request
import zipfile

from .metadata import ExtractedContext, MetadataFailure
from .reader import READER_VERSION, ReaderFailure


MAX_METADATA_RESPONSE_BYTES = 64 * 1024
MAX_READER_BUNDLE_BYTES = 96 * 1024 * 1024
MAX_READER_UNCOMPRESSED_BYTES = 96 * 1024 * 1024
MAX_READER_FILES = 1_002
MAX_READER_MANIFEST_BYTES = 8 * 1024 * 1024
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_ERROR_RE = re.compile(r"[a-z][a-z0-9_]{0,79}")
_ASSET_RE = re.compile(r"assets/page-[0-9]{3}\.jpg")
_ANCHOR_RE = re.compile(r"section-[1-9][0-9]{0,3}")


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any):
        return None


_PARSER_OPENER = build_opener(_NoRedirectHandler)


def _open_parser_request(request: Request, *, timeout: int):
    return _PARSER_OPENER.open(request, timeout=timeout)


def _parser_base_url() -> str:
    value = os.environ.get("PAPER_PARSER_URL", "http://paper-parser:8090")
    parsed = urlsplit(value)
    allowed_hosts = {
        item.strip()
        for item in os.environ.get(
            "PAPER_PARSER_ALLOWED_HOSTS", "paper-parser"
        ).split(",")
        if item.strip()
    }
    try:
        port = parsed.port
    except ValueError as exc:
        raise MetadataFailure("parser_configuration_invalid") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname not in allowed_hosts
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or port is None
    ):
        raise MetadataFailure("parser_configuration_invalid")
    return value.rstrip("/")


def _timeout(variable: str, default: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(variable, str(default)))
    except ValueError as exc:
        raise MetadataFailure("parser_configuration_invalid") from exc
    if not 5 <= value <= maximum:
        raise MetadataFailure("parser_configuration_invalid")
    return value


def _request_json(
    endpoint: str,
    payload: dict[str, Any],
    *,
    maximum: int,
    timeout: int,
) -> tuple[str, bytes]:
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise MetadataFailure("parser_request_invalid") from exc
    if not 1 <= len(encoded) <= 64 * 1024:
        raise MetadataFailure("parser_request_too_large")
    request = Request(
        f"{_parser_base_url()}{endpoint}",
        data=encoded,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with _open_parser_request(request, timeout=timeout) as response:
            content_type = response.headers.get_content_type()
            body = response.read(maximum + 1)
    except HTTPError as exc:
        try:
            body = exc.read(MAX_METADATA_RESPONSE_BYTES + 1)
            error = json.loads(body.decode("utf-8")).get("error")
        except Exception:
            error = None
        if not isinstance(error, str) or not _ERROR_RE.fullmatch(error):
            error = "parser_unavailable" if exc.code >= 500 else "parser_rejected"
        raise MetadataFailure(error) from exc
    except (OSError, TimeoutError, URLError) as exc:
        raise MetadataFailure("parser_unavailable") from exc
    if len(body) > maximum:
        raise MetadataFailure("parser_output_too_large")
    return content_type, body


def extract_pdf_context(
    stored_path: str,
    expected_sha256: str,
) -> ExtractedContext:
    if not isinstance(expected_sha256, str) or not _SHA256_RE.fullmatch(
        expected_sha256.strip()
    ):
        raise MetadataFailure("parser_source_sha_invalid")
    content_type, body = _request_json(
        "/v1/metadata",
        {
            "stored_path": stored_path,
            "source_sha256": expected_sha256.strip(),
        },
        maximum=MAX_METADATA_RESPONSE_BYTES,
        timeout=_timeout("PAPER_PARSER_METADATA_TIMEOUT_SECONDS", 2_000, 3_000),
    )
    if content_type != "application/json":
        raise MetadataFailure("parser_output_invalid")
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise MetadataFailure("parser_output_invalid") from exc
    if not isinstance(value, dict) or set(value) != {
        "document_title",
        "document_author",
        "detected_doi",
        "text",
    }:
        raise MetadataFailure("parser_output_invalid")
    for key, maximum in (
        ("document_title", 1_000),
        ("document_author", 2_000),
        ("detected_doi", 500),
    ):
        if value[key] is not None and (
            not isinstance(value[key], str) or len(value[key]) > maximum
        ):
            raise MetadataFailure("parser_output_invalid")
    text = value["text"]
    if not isinstance(text, str) or not text.strip() or len(text) > 30_000:
        raise MetadataFailure("parser_output_invalid")
    return ExtractedContext(
        document_title=value["document_title"],
        document_author=value["document_author"],
        detected_doi=value["detected_doi"],
        text=text,
    )


def _clean_reader_paper(paper: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(paper, dict):
        raise ReaderFailure("reader_paper_invalid")
    required = {
        "id",
        "stored_path",
        "sha256",
        "size_bytes",
        "title",
        "authors",
        "publication_year",
        "doi",
        "category",
        "paper_type",
    }
    if not required.issubset(paper):
        raise ReaderFailure("reader_paper_invalid")
    clean = {key: paper[key] for key in required}
    if isinstance(clean.get("sha256"), str):
        clean["sha256"] = clean["sha256"].strip()
    try:
        encoded = json.dumps(clean, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise ReaderFailure("reader_paper_invalid") from exc
    if len(encoded) > 48 * 1024:
        raise ReaderFailure("reader_paper_invalid")
    return clean


def _reader_request(paper: dict[str, Any]) -> bytes:
    try:
        content_type, body = _request_json(
            "/v1/reader-v1",
            {"paper": _clean_reader_paper(paper)},
            maximum=MAX_READER_BUNDLE_BYTES,
            timeout=_timeout("PAPER_PARSER_READER_TIMEOUT_SECONDS", 2_520, 3_600),
        )
    except MetadataFailure as exc:
        raise ReaderFailure(exc.code) from exc
    if content_type != "application/zip":
        raise ReaderFailure("reader_output_invalid")
    return body


def _validate_manifest(
    manifest: Any,
    *,
    paper_id: int,
    archive_names: set[str],
) -> dict[str, Any]:
    required = {
        "schema_version",
        "reader_version",
        "paper_id",
        "title",
        "authors",
        "year",
        "doi",
        "category",
        "paper_type",
        "page_count",
        "headings",
        "blocks",
    }
    if not isinstance(manifest, dict) or set(manifest) != required:
        raise ReaderFailure("reader_manifest_invalid")
    page_count = manifest["page_count"]
    authors = manifest["authors"]
    if (
        manifest["schema_version"] != 1
        or manifest["reader_version"] != READER_VERSION
        or manifest["paper_id"] != paper_id
        or not isinstance(page_count, int)
        or isinstance(page_count, bool)
        or not 1 <= page_count <= 500
        or not isinstance(manifest["title"], str)
        or not 1 <= len(manifest["title"]) <= 500
        or not isinstance(authors, list)
        or len(authors) > 100
        or any(not isinstance(author, str) or len(author) > 300 for author in authors)
        or not isinstance(manifest["headings"], list)
        or len(manifest["headings"]) > 500
        or not isinstance(manifest["blocks"], list)
        or not 1 <= len(manifest["blocks"]) <= 200_000
    ):
        raise ReaderFailure("reader_manifest_invalid")
    for key, maximum in (("doi", 500), ("category", 120), ("paper_type", 120)):
        value = manifest[key]
        if value is not None and (not isinstance(value, str) or len(value) > maximum):
            raise ReaderFailure("reader_manifest_invalid")
    year = manifest["year"]
    if year is not None and (
        not isinstance(year, int)
        or isinstance(year, bool)
        or not 1800 <= year <= 2200
    ):
        raise ReaderFailure("reader_manifest_invalid")

    heading_anchors: set[str] = set()
    for heading in manifest["headings"]:
        if not isinstance(heading, dict) or set(heading) != {"text", "anchor", "page"}:
            raise ReaderFailure("reader_manifest_invalid")
        anchor = heading["anchor"]
        if (
            not isinstance(heading["text"], str)
            or not 1 <= len(heading["text"]) <= 10_000
            or not isinstance(anchor, str)
            or not _ANCHOR_RE.fullmatch(anchor)
            or anchor in heading_anchors
            or not isinstance(heading["page"], int)
            or isinstance(heading["page"], bool)
            or not 1 <= heading["page"] <= page_count
        ):
            raise ReaderFailure("reader_manifest_invalid")
        heading_anchors.add(anchor)

    block_heading_anchors: set[str] = set()
    referenced_assets: set[str] = set()
    for block in manifest["blocks"]:
        if not isinstance(block, dict):
            raise ReaderFailure("reader_manifest_invalid")
        kind = block.get("kind")
        allowed = {"kind", "text", "page"}
        if kind == "heading":
            allowed.add("anchor")
        elif kind == "caption":
            allowed.add("page_asset")
        elif kind != "paragraph":
            raise ReaderFailure("reader_manifest_invalid")
        page = block.get("page")
        text = block.get("text")
        if (
            set(block) != allowed
            or not isinstance(text, str)
            or not 1 <= len(text) <= 50_000
            or not isinstance(page, int)
            or isinstance(page, bool)
            or not 1 <= page <= page_count
        ):
            raise ReaderFailure("reader_manifest_invalid")
        if kind == "heading":
            anchor = block["anchor"]
            if (
                not isinstance(anchor, str)
                or not _ANCHOR_RE.fullmatch(anchor)
                or anchor in block_heading_anchors
            ):
                raise ReaderFailure("reader_manifest_invalid")
            block_heading_anchors.add(anchor)
        elif kind == "caption":
            asset = block["page_asset"]
            if not isinstance(asset, str) or not _ASSET_RE.fullmatch(asset):
                raise ReaderFailure("reader_manifest_invalid")
            referenced_assets.add(asset)
    expected_assets = {
        f"assets/page-{page:03d}.jpg" for page in range(1, page_count + 1)
    }
    if (
        heading_anchors != block_heading_anchors
        or not referenced_assets.issubset(expected_assets)
        or expected_assets - archive_names
        or archive_names - expected_assets - {"manifest.json"}
    ):
        raise ReaderFailure("reader_manifest_invalid")
    return manifest


def _validated_bundle(bundle_bytes: bytes, paper_id: int) -> tuple[zipfile.ZipFile, dict[str, Any]]:
    try:
        bundle = zipfile.ZipFile(io.BytesIO(bundle_bytes))
        entries = bundle.infolist()
    except (OSError, zipfile.BadZipFile) as exc:
        raise ReaderFailure("reader_output_invalid") from exc
    if not 1 <= len(entries) <= MAX_READER_FILES:
        bundle.close()
        raise ReaderFailure("reader_output_file_limit")
    names: set[str] = set()
    total = 0
    for entry in entries:
        name = entry.filename
        path = PurePosixPath(name)
        mode = entry.external_attr >> 16
        if (
            entry.is_dir()
            or path.is_absolute()
            or path.as_posix() != name
            or any(part in {"", ".", ".."} for part in path.parts)
            or name in names
            or (name != "manifest.json" and not _ASSET_RE.fullmatch(name))
            or stat.S_ISLNK(mode)
            or entry.flag_bits & 0x1
            or entry.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
            or entry.file_size < 1
        ):
            bundle.close()
            raise ReaderFailure("reader_output_invalid")
        names.add(name)
        total += entry.file_size
        if total > MAX_READER_UNCOMPRESSED_BYTES:
            bundle.close()
            raise ReaderFailure("reader_output_too_large")
    if "manifest.json" not in names:
        bundle.close()
        raise ReaderFailure("reader_output_invalid")
    manifest_info = bundle.getinfo("manifest.json")
    if manifest_info.file_size > MAX_READER_MANIFEST_BYTES:
        bundle.close()
        raise ReaderFailure("reader_output_too_large")
    try:
        manifest = json.loads(bundle.read(manifest_info).decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError, RuntimeError) as exc:
        bundle.close()
        raise ReaderFailure("reader_manifest_invalid") from exc
    try:
        manifest = _validate_manifest(
            manifest,
            paper_id=paper_id,
            archive_names=names,
        )
    except ReaderFailure:
        bundle.close()
        raise
    return bundle, manifest


def convert_paper(paper: dict[str, Any], data_dir: Path) -> str:
    """Install one bounded reader artifact returned by the isolated service."""

    clean = _clean_reader_paper(paper)
    paper_id = clean["id"]
    if not isinstance(paper_id, int) or isinstance(paper_id, bool) or paper_id < 1:
        raise ReaderFailure("reader_paper_invalid")
    relative_manifest = PurePosixPath(str(paper_id)) / f"v{READER_VERSION}" / "manifest.json"
    final_dir = data_dir / "library" / "mobile" / str(paper_id) / f"v{READER_VERSION}"
    if final_dir.is_dir() and (final_dir / "manifest.json").is_file():
        return relative_manifest.as_posix()
    if final_dir.exists():
        raise ReaderFailure("reader_output_incomplete")
    bundle_bytes = _reader_request(clean)
    bundle, _manifest = _validated_bundle(bundle_bytes, paper_id)
    final_dir.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(
            prefix="reader-v1-install-", dir=final_dir.parent
        ) as temporary_name:
            temporary = Path(temporary_name)
            temporary.chmod(0o700)
            for entry in bundle.infolist():
                target = temporary / PurePosixPath(entry.filename)
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(entry) as source, target.open("xb") as destination:
                    shutil.copyfileobj(source, destination, length=1024 * 1024)
                target.chmod(0o640)
            os.replace(temporary, final_dir)
    except ReaderFailure:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise ReaderFailure("reader_output_install_failed") from exc
    finally:
        bundle.close()
    return relative_manifest.as_posix()


__all__ = ["convert_paper", "extract_pdf_context"]

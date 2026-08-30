"""Credential-free child process for parsing one untrusted PDF.

The parent parser service supplies a private, SHA-verified PDF copy and private
output paths.  This module applies process resource limits before importing a
PDF library.  It never reads configuration, talks to the database, or performs
network requests.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import resource
import stat
import sys
import zipfile
from typing import Any


MAX_REQUEST_BYTES = 64 * 1024
MAX_STATUS_BYTES = 64 * 1024
MAX_READER_FILES = 1_002
MAX_READER_OUTPUT_BYTES = 96 * 1024 * 1024
MAX_READER_PAGES = 500
_ERROR_RE = re.compile(r"[a-z][a-z0-9_]{0,79}")


def _apply_resource_limits() -> None:
    limits = (
        (resource.RLIMIT_CORE, 0),
        (resource.RLIMIT_CPU, 120),
        (resource.RLIMIT_AS, 512 * 1024 * 1024),
        (resource.RLIMIT_FSIZE, MAX_READER_OUTPUT_BYTES),
        (resource.RLIMIT_NOFILE, 64),
    )
    for kind, maximum in limits:
        resource.setrlimit(kind, (maximum, maximum))


def _private_regular_file(path: Path, *, maximum: int) -> None:
    details = path.lstat()
    if (
        not stat.S_ISREG(details.st_mode)
        or stat.S_ISLNK(details.st_mode)
        or not 1 <= details.st_size <= maximum
        or stat.S_IMODE(details.st_mode) & 0o077
    ):
        raise ValueError("parser_private_file_invalid")


def _read_request(path: Path) -> dict[str, Any]:
    _private_regular_file(path, maximum=MAX_REQUEST_BYTES)
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("parser_request_invalid")
    return value


def _write_status(path: Path, value: dict[str, Any]) -> None:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if len(encoded) > MAX_STATUS_BYTES:
        encoded = b'{"ok":false,"error":"parser_status_too_large"}'
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def _safe_error(exc: BaseException) -> str:
    code = getattr(exc, "code", None)
    if isinstance(code, str) and _ERROR_RE.fullmatch(code):
        return code
    if isinstance(exc, ValueError) and exc.args:
        candidate = exc.args[0]
        if isinstance(candidate, str) and _ERROR_RE.fullmatch(candidate):
            return candidate
    return "parser_task_failed"


def _metadata(request: dict[str, Any]) -> dict[str, Any]:
    if set(request) != {"operation", "pdf_path", "max_pages", "max_chars"}:
        raise ValueError("parser_request_invalid")
    max_pages = request["max_pages"]
    max_chars = request["max_chars"]
    if (
        not isinstance(max_pages, int)
        or isinstance(max_pages, bool)
        or not 1 <= max_pages <= 16
        or not isinstance(max_chars, int)
        or isinstance(max_chars, bool)
        or not 1 <= max_chars <= 50_000
    ):
        raise ValueError("parser_request_invalid")
    pdf_path = Path(request["pdf_path"])
    _private_regular_file(pdf_path, maximum=128 * 1024 * 1024)
    # Import only after process limits and the request boundary are active.
    from .metadata import extract_pdf_context

    context = extract_pdf_context(
        pdf_path,
        max_pages=max_pages,
        max_chars=max_chars,
    )
    return {
        "ok": True,
        "context": {
            "document_title": context.document_title,
            "document_author": context.document_author,
            "detected_doi": context.detected_doi,
            "text": context.text,
        },
    }


def _reader(request: dict[str, Any]) -> dict[str, Any]:
    if set(request) != {"operation", "pdf_path", "paper", "bundle_path"}:
        raise ValueError("parser_request_invalid")
    paper = request["paper"]
    if not isinstance(paper, dict):
        raise ValueError("parser_request_invalid")
    pdf_path = Path(request["pdf_path"])
    bundle_path = Path(request["bundle_path"])
    _private_regular_file(pdf_path, maximum=128 * 1024 * 1024)
    artifact_dir = bundle_path.parent / "artifact"
    artifact_dir.mkdir(mode=0o700)

    # Import only after process limits and the request boundary are active.
    from .reader import READER_VERSION, extract_document

    manifest = extract_document(
        pdf_path,
        paper,
        artifact_dir,
        max_pages=MAX_READER_PAGES,
    )
    manifest_path = artifact_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            manifest,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ),
        encoding="utf-8",
    )
    manifest_path.chmod(0o600)

    files = sorted(path for path in artifact_dir.rglob("*") if path.is_file())
    if not files or len(files) > MAX_READER_FILES:
        raise ValueError("reader_output_file_limit")
    total = 0
    for path in files:
        details = path.lstat()
        if not stat.S_ISREG(details.st_mode) or stat.S_ISLNK(details.st_mode):
            raise ValueError("reader_output_invalid")
        total += details.st_size
        if total > MAX_READER_OUTPUT_BYTES:
            raise ValueError("reader_output_too_large")
    with zipfile.ZipFile(
        bundle_path,
        mode="x",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=6,
    ) as bundle:
        for path in files:
            bundle.write(path, path.relative_to(artifact_dir).as_posix())
    bundle_path.chmod(0o600)
    if bundle_path.stat().st_size > MAX_READER_OUTPUT_BYTES:
        raise ValueError("reader_output_too_large")
    return {
        "ok": True,
        "reader_version": READER_VERSION,
        "bundle_size": bundle_path.stat().st_size,
    }


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 2:
        return 2
    request_path = Path(arguments[0])
    status_path = Path(arguments[1])
    try:
        _apply_resource_limits()
        request = _read_request(request_path)
        operation = request.get("operation")
        if operation == "metadata":
            status = _metadata(request)
        elif operation == "reader-v1":
            status = _reader(request)
        else:
            raise ValueError("parser_operation_invalid")
    except BaseException as exc:
        status = {"ok": False, "error": _safe_error(exc)}
    try:
        _write_status(status_path, status)
    except BaseException:
        return 3
    return 0 if status.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

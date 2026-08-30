"""One-shot, credential-free Docling task for a single projected PDF."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import resource
import stat
import sys
from typing import Any


MAX_TASK_STATUS_BYTES = 256 * 1024
MAX_PDF_BYTES = 512 * 1024 * 1024
_ERROR_RE = re.compile(r"[a-z][a-z0-9_]{0,119}")


def _apply_resource_limits() -> None:
    limits = (
        (resource.RLIMIT_CORE, 0),
        (resource.RLIMIT_CPU, 900),
        (resource.RLIMIT_AS, 2400 * 1024 * 1024),
        (resource.RLIMIT_FSIZE, 256 * 1024 * 1024),
        (resource.RLIMIT_NOFILE, 128),
    )
    for kind, maximum in limits:
        resource.setrlimit(kind, (maximum, maximum))


def _private_regular(path: Path, *, maximum: int) -> os.stat_result:
    details = path.lstat()
    if (
        not stat.S_ISREG(details.st_mode)
        or stat.S_ISLNK(details.st_mode)
        or not 1 <= details.st_size <= maximum
        or stat.S_IMODE(details.st_mode) & 0o077
    ):
        raise ValueError("reader_v2_converter_source_invalid")
    return details


def _source_digest(path: Path, *, expected_size: int) -> str:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(descriptor)
        digest = hashlib.sha256()
        total = 0
        while chunk := os.read(descriptor, 1024 * 1024):
            total += len(chunk)
            if total > MAX_PDF_BYTES:
                raise ValueError("reader_v2_converter_source_invalid")
            digest.update(chunk)
        after = os.fstat(descriptor)
        if (
            total != expected_size
            or (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
            )
            != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            )
        ):
            raise ValueError("reader_v2_converter_source_changed")
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _harden_output_permissions(root: Path) -> None:
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        current_details = current_path.lstat()
        if not stat.S_ISDIR(current_details.st_mode) or stat.S_ISLNK(
            current_details.st_mode
        ):
            raise ValueError("reader_v2_converter_output_invalid")
        current_path.chmod(0o700)
        for name in directories:
            details = (current_path / name).lstat()
            if not stat.S_ISDIR(details.st_mode) or stat.S_ISLNK(details.st_mode):
                raise ValueError("reader_v2_converter_output_invalid")
        for name in files:
            path = current_path / name
            details = path.lstat()
            if not stat.S_ISREG(details.st_mode) or stat.S_ISLNK(details.st_mode):
                raise ValueError("reader_v2_converter_output_invalid")
            path.chmod(0o600)


def _write_status(path: Path, value: dict[str, Any]) -> None:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if not 1 <= len(encoded) <= MAX_TASK_STATUS_BYTES:
        encoded = b'{"ok":false,"error":"reader_v2_converter_status_too_large"}'
    descriptor = os.open(
        path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def _safe_error(exc: BaseException) -> str:
    candidate = getattr(exc, "code", None)
    if not isinstance(candidate, str) and exc.args:
        candidate = exc.args[0]
    if isinstance(candidate, str) and _ERROR_RE.fullmatch(candidate):
        return candidate
    return "reader_v2_converter_task_failed"


def _run(request_path: Path, source_path: Path, output_path: Path) -> dict[str, Any]:
    from .reader_v2_job import (
        build_output_manifest,
        read_private_json,
        validate_request,
    )

    request = validate_request(read_private_json(request_path))
    details = _private_regular(source_path, maximum=MAX_PDF_BYTES)
    if details.st_size != request["source_size"]:
        raise ValueError("reader_v2_converter_source_invalid")
    before = _source_digest(source_path, expected_size=request["source_size"])
    if before != request["source_sha256"]:
        raise ValueError("reader_v2_converter_source_hash_mismatch")
    output_path.mkdir(mode=0o700)

    # Import the PDF engine only after process resource limits are active.
    from .article import validate_article
    from .docling_adapter import DoclingAdapterFailure, adapt_docling
    from .reader_v2 import run_docling, write_article

    try:
        document = run_docling(source_path, output_path)
        article = adapt_docling(
            document,
            source_path,
            output_path,
            request["paper"],
        )
    except DoclingAdapterFailure as exc:
        raise ValueError(exc.code) from exc
    write_article(validate_article(article), output_path)
    _harden_output_permissions(output_path)
    manifest = build_output_manifest(output_path)
    after = _source_digest(source_path, expected_size=request["source_size"])
    if after != request["source_sha256"]:
        raise ValueError("reader_v2_converter_source_changed")
    return {"ok": True, "source": "docling_pdf", "files": manifest}


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 4:
        return 2
    request_path, source_path, output_path, status_path = map(Path, arguments)
    try:
        _apply_resource_limits()
        status = _run(request_path, source_path, output_path)
    except BaseException as exc:
        status = {"ok": False, "error": _safe_error(exc)}
    try:
        _write_status(status_path, status)
    except BaseException:
        return 3
    return 0 if status.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Internal, credential-free broker for isolated PDF parsing tasks."""

from __future__ import annotations

import hashlib
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path, PurePosixPath
import re
import signal
import stat
import subprocess
import sys
import tempfile
import threading
from typing import Any

from .conversion_lock import ConversionLockFailure, conversion_lock_from_env
from .parser_task import (
    MAX_READER_OUTPUT_BYTES,
    MAX_STATUS_BYTES,
)


MAX_REQUEST_BYTES = 64 * 1024
MAX_SOURCE_PDF_BYTES = 128 * 1024 * 1024
MAX_METADATA_RESPONSE_BYTES = 64 * 1024
MIN_READER_SCRATCH_HEADROOM = 16 * 1024 * 1024
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_ERROR_RE = re.compile(r"[a-z][a-z0-9_]{0,79}")
_CHILD_TIMEOUTS = {"metadata": 90, "reader-v1": 600}
_CONCURRENCY = threading.BoundedSemaphore(1)


class ParserServiceFailure(RuntimeError):
    def __init__(self, code: str, *, http_status: int = 422):
        super().__init__(code)
        self.code = code
        self.http_status = http_status


def _child_environment(base: dict[str, str] | None = None) -> dict[str, str]:
    """Build an allowlisted environment with no inherited credentials."""

    source = os.environ if base is None else base
    return {
        "PATH": source.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PAPER_PARSER_CHILD": "1",
    }


def _positive_timeout(operation: str) -> int:
    default = _CHILD_TIMEOUTS[operation]
    variable = (
        "PAPER_PARSER_METADATA_TIMEOUT_SECONDS"
        if operation == "metadata"
        else "PAPER_PARSER_READER_TIMEOUT_SECONDS"
    )
    try:
        value = int(os.environ.get(variable, str(default)))
    except ValueError as exc:
        raise ParserServiceFailure("parser_configuration_invalid", http_status=503) from exc
    upper = 300 if operation == "metadata" else 1_800
    if not 5 <= value <= upper:
        raise ParserServiceFailure("parser_configuration_invalid", http_status=503)
    return value


def _stored_path(value: Any) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 1_000:
        raise ParserServiceFailure("parser_stored_path_invalid")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
        or not value.casefold().endswith(".pdf")
    ):
        raise ParserServiceFailure("parser_stored_path_invalid")
    return value


def _sha256(value: Any) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ParserServiceFailure("parser_source_sha_invalid")
    return value


def _bounded_text(value: Any, *, maximum: int, nullable: bool = True) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or len(value) > maximum:
        raise ParserServiceFailure("parser_paper_metadata_invalid")
    return value


def _paper(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ParserServiceFailure("parser_paper_metadata_invalid")
    allowed = {
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
    if set(value) != allowed:
        raise ParserServiceFailure("parser_paper_metadata_invalid")
    paper_id = value["id"]
    size_bytes = value["size_bytes"]
    if (
        not isinstance(paper_id, int)
        or isinstance(paper_id, bool)
        or paper_id < 1
        or not isinstance(size_bytes, int)
        or isinstance(size_bytes, bool)
        or not 1 <= size_bytes <= MAX_SOURCE_PDF_BYTES
    ):
        raise ParserServiceFailure("parser_paper_metadata_invalid")
    authors = value["authors"]
    if not isinstance(authors, list) or len(authors) > 100:
        raise ParserServiceFailure("parser_paper_metadata_invalid")
    clean_authors = [
        _bounded_text(author, maximum=300, nullable=False) for author in authors
    ]
    year = value["publication_year"]
    if year is not None and (
        not isinstance(year, int)
        or isinstance(year, bool)
        or not 1800 <= year <= 2200
    ):
        raise ParserServiceFailure("parser_paper_metadata_invalid")
    return {
        "id": paper_id,
        "stored_path": _stored_path(value["stored_path"]),
        "sha256": _sha256(value["sha256"]),
        "size_bytes": size_bytes,
        "title": _bounded_text(value["title"], maximum=500),
        "authors": clean_authors,
        "publication_year": year,
        "doi": _bounded_text(value["doi"], maximum=500),
        "category": _bounded_text(value["category"], maximum=120),
        "paper_type": _bounded_text(value["paper_type"], maximum=120),
    }


def _validate_request(value: Any, operation: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ParserServiceFailure("parser_request_invalid")
    if operation == "metadata":
        if set(value) != {"stored_path", "source_sha256"}:
            raise ParserServiceFailure("parser_request_invalid")
        return {
            "stored_path": _stored_path(value["stored_path"]),
            "source_sha256": _sha256(value["source_sha256"]),
        }
    if operation == "reader-v1":
        if set(value) != {"paper"}:
            raise ParserServiceFailure("parser_request_invalid")
        return {"paper": _paper(value["paper"])}
    raise ParserServiceFailure("parser_operation_invalid", http_status=404)


def _copy_verified_pdf(
    stored_path: str,
    expected_sha256: str,
    destination: Path,
    *,
    expected_size: int | None = None,
) -> int:
    data_dir = Path(os.environ.get("PAPER_DATA_DIR", "/data"))
    raw_root = data_dir / "library" / "raw"
    relative = PurePosixPath(stored_path)
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    directory = -1
    try:
        directory = os.open(raw_root, directory_flags)
        root_details = os.fstat(directory)
        if not stat.S_ISDIR(root_details.st_mode):
            raise ParserServiceFailure("parser_source_invalid")
        for part in relative.parts[:-1]:
            child = os.open(part, directory_flags, dir_fd=directory)
            details = os.fstat(child)
            if not stat.S_ISDIR(details.st_mode):
                os.close(child)
                raise ParserServiceFailure("parser_source_invalid")
            os.close(directory)
            directory = child
    except ParserServiceFailure:
        if directory >= 0:
            os.close(directory)
        raise
    except OSError as exc:
        if directory >= 0:
            os.close(directory)
        raise ParserServiceFailure("parser_source_missing") from exc
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(relative.name, flags, dir_fd=directory)
    except OSError as exc:
        raise ParserServiceFailure("parser_source_missing") from exc
    finally:
        os.close(directory)
    digest = hashlib.sha256()
    copied = 0
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or not 1 <= details.st_size <= MAX_SOURCE_PDF_BYTES:
            raise ParserServiceFailure("parser_source_invalid")
        if expected_size is not None and details.st_size != expected_size:
            raise ParserServiceFailure("parser_source_size_mismatch")
        output = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                copied += len(chunk)
                if copied > MAX_SOURCE_PDF_BYTES:
                    raise ParserServiceFailure("parser_source_too_large")
                digest.update(chunk)
                remaining = memoryview(chunk)
                while remaining:
                    written = os.write(output, remaining)
                    if written <= 0:
                        raise ParserServiceFailure("parser_source_copy_failed")
                    remaining = remaining[written:]
            os.fsync(output)
        finally:
            os.close(output)
    finally:
        os.close(descriptor)
    if copied < 1 or digest.hexdigest() != expected_sha256:
        raise ParserServiceFailure("parser_source_sha_mismatch")
    return copied


def _require_reader_scratch_budget(directory: Path) -> None:
    """Reserve room for bounded uncompressed artifacts plus their zip."""

    try:
        details = os.statvfs(directory)
        available = details.f_bavail * details.f_frsize
    except OSError as exc:
        raise ParserServiceFailure("parser_scratch_unavailable", http_status=503) from exc
    required = (2 * MAX_READER_OUTPUT_BYTES) + MIN_READER_SCRATCH_HEADROOM
    if available < required:
        raise ParserServiceFailure("parser_scratch_insufficient", http_status=503)


def _write_private_json(path: Path, value: dict[str, Any]) -> None:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if len(encoded) > MAX_REQUEST_BYTES:
        raise ParserServiceFailure("parser_request_too_large")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def _terminate_process_group(process: subprocess.Popen[Any]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _kill_remaining_process_group(process_group_id: int) -> None:
    """Ensure a parser cannot leave detached work after its leader exits."""

    try:
        os.killpg(process_group_id, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass


def _run_child(request: dict[str, Any], status_path: Path, *, timeout: int) -> None:
    request_path = status_path.parent / "request.json"
    _write_private_json(request_path, request)
    argv = [
        sys.executable,
        "-m",
        "paper_service.parser_task",
        str(request_path),
        str(status_path),
    ]
    try:
        process = subprocess.Popen(
            argv,
            cwd="/app" if Path("/app/paper_service").is_dir() else str(Path.cwd()),
            env=_child_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
    except OSError as exc:
        raise ParserServiceFailure("parser_child_unavailable", http_status=503) from exc
    try:
        return_code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _terminate_process_group(process)
        raise ParserServiceFailure("parser_timeout", http_status=503) from exc
    _kill_remaining_process_group(process.pid)
    if return_code not in {0, 1}:
        raise ParserServiceFailure("parser_child_failed", http_status=503)


def _read_status(path: Path) -> dict[str, Any]:
    try:
        details = path.lstat()
        if (
            not stat.S_ISREG(details.st_mode)
            or stat.S_ISLNK(details.st_mode)
            or not 1 <= details.st_size <= MAX_STATUS_BYTES
        ):
            raise ParserServiceFailure("parser_status_invalid", http_status=503)
        value = json.loads(path.read_text(encoding="utf-8"))
    except ParserServiceFailure:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ParserServiceFailure("parser_status_invalid", http_status=503) from exc
    if not isinstance(value, dict) or type(value.get("ok")) is not bool:
        raise ParserServiceFailure("parser_status_invalid", http_status=503)
    if value["ok"] is False:
        code = value.get("error")
        if not isinstance(code, str) or not _ERROR_RE.fullmatch(code):
            code = "parser_task_failed"
        raise ParserServiceFailure(code)
    return value


def _validate_metadata_context(status: dict[str, Any]) -> dict[str, Any]:
    if set(status) != {"ok", "context"} or status["ok"] is not True:
        raise ParserServiceFailure("parser_status_invalid", http_status=503)
    context = status["context"]
    if not isinstance(context, dict) or set(context) != {
        "document_title",
        "document_author",
        "detected_doi",
        "text",
    }:
        raise ParserServiceFailure("parser_output_invalid")
    for key, maximum in (
        ("document_title", 1_000),
        ("document_author", 2_000),
        ("detected_doi", 500),
    ):
        if context[key] is not None and (
            not isinstance(context[key], str) or len(context[key]) > maximum
        ):
            raise ParserServiceFailure("parser_output_invalid")
    if (
        not isinstance(context["text"], str)
        or not context["text"].strip()
        or len(context["text"]) > 30_000
    ):
        raise ParserServiceFailure("parser_output_invalid")
    return context


def _execute_locked_parser_request(
    operation: str,
    clean: dict[str, Any],
) -> tuple[str, bytes]:
    with tempfile.TemporaryDirectory(prefix="paper-parser-") as temporary_name:
        temporary = Path(temporary_name)
        temporary.chmod(0o700)
        pdf_path = temporary / "source.pdf"
        if operation == "metadata":
            stored_path = clean["stored_path"]
            source_sha = clean["source_sha256"]
            expected_size = None
        else:
            paper = clean["paper"]
            stored_path = paper["stored_path"]
            source_sha = paper["sha256"]
            expected_size = paper["size_bytes"]
        _copy_verified_pdf(
            stored_path,
            source_sha,
            pdf_path,
            expected_size=expected_size,
        )
        if operation == "reader-v1":
            _require_reader_scratch_budget(temporary)
        status_path = temporary / "status.json"
        if operation == "metadata":
            child_request = {
                "operation": "metadata",
                "pdf_path": str(pdf_path),
                "max_pages": 8,
                "max_chars": 30_000,
            }
        else:
            child_request = {
                "operation": "reader-v1",
                "pdf_path": str(pdf_path),
                "paper": paper,
                "bundle_path": str(temporary / "reader.zip"),
            }
        _run_child(
            child_request,
            status_path,
            timeout=_positive_timeout(operation),
        )
        status = _read_status(status_path)
        if operation == "metadata":
            context = _validate_metadata_context(status)
            encoded = json.dumps(
                context,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            if len(encoded) > MAX_METADATA_RESPONSE_BYTES:
                raise ParserServiceFailure("parser_output_too_large")
            return "application/json", encoded
        if set(status) != {"ok", "reader_version", "bundle_size"}:
            raise ParserServiceFailure("parser_status_invalid", http_status=503)
        bundle_path = temporary / "reader.zip"
        try:
            details = bundle_path.lstat()
        except OSError as exc:
            raise ParserServiceFailure("reader_output_missing") from exc
        if (
            not stat.S_ISREG(details.st_mode)
            or stat.S_ISLNK(details.st_mode)
            or not 1 <= details.st_size <= MAX_READER_OUTPUT_BYTES
            or status["bundle_size"] != details.st_size
        ):
            raise ParserServiceFailure("reader_output_invalid")
        return "application/zip", bundle_path.read_bytes()


def execute_parser_request(operation: str, payload: Any) -> tuple[str, bytes]:
    clean = _validate_request(payload, operation)
    try:
        with conversion_lock_from_env():
            return _execute_locked_parser_request(operation, clean)
    except ConversionLockFailure as exc:
        raise ParserServiceFailure(exc.code, http_status=503) from exc


class ParserRequestHandler(BaseHTTPRequestHandler):
    server_version = "paper-parser"
    sys_version = ""

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, _format: str, *_args: Any) -> None:
        # Paths and document metadata must never enter access logs.
        return

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
        if self.path != "/healthz":
            self._send(HTTPStatus.NOT_FOUND, "application/json", b'{"status":"missing"}')
            return
        self._send(HTTPStatus.OK, "application/json", b'{"status":"ok"}')

    def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
        operation = {
            "/v1/metadata": "metadata",
            "/v1/reader-v1": "reader-v1",
        }.get(self.path)
        if operation is None:
            self._send(HTTPStatus.NOT_FOUND, "application/json", b'{"error":"parser_operation_invalid"}')
            return
        try:
            length_text = self.headers.get("Content-Length")
            length = int(length_text) if length_text is not None else -1
            if not 1 <= length <= MAX_REQUEST_BYTES:
                raise ParserServiceFailure("parser_request_too_large", http_status=413)
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ParserServiceFailure("parser_request_invalid")
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise ParserServiceFailure("parser_request_invalid") from exc
            if not _CONCURRENCY.acquire(blocking=False):
                raise ParserServiceFailure("parser_busy", http_status=503)
            try:
                content_type, body = execute_parser_request(operation, payload)
            finally:
                _CONCURRENCY.release()
            self._send(HTTPStatus.OK, content_type, body)
        except ParserServiceFailure as exc:
            body = json.dumps({"error": exc.code}, separators=(",", ":")).encode()
            self._send(exc.http_status, "application/json", body)
        except Exception:
            self._send(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "application/json",
                b'{"error":"parser_service_failed"}',
            )


def run_server() -> None:
    try:
        port = int(os.environ.get("PAPER_PARSER_PORT", "8090"))
    except ValueError as exc:
        raise RuntimeError("PAPER_PARSER_PORT invalid") from exc
    if not 1 <= port <= 65_535:
        raise RuntimeError("PAPER_PARSER_PORT invalid")
    server = ThreadingHTTPServer(("0.0.0.0", port), ParserRequestHandler)
    server.daemon_threads = True
    server.serve_forever(poll_interval=0.5)


if __name__ == "__main__":
    run_server()

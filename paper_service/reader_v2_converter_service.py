"""Internal service that runs one Docling job from a SHA-bound shared volume."""

from __future__ import annotations

import hashlib
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import threading
from typing import Any, Mapping

from .conversion_lock import ConversionLockFailure, conversion_lock_from_env
from .reader_v2_converter_task import MAX_TASK_STATUS_BYTES
from .reader_v2_job import (
    MAX_HTTP_BYTES,
    OUTPUT_NAME,
    OWNER_NAME,
    READY_NAME,
    REQUEST_NAME,
    RESULT_NAME,
    SOURCE_NAME,
    ReaderV2JobFailure,
    build_output_manifest,
    canonical_json_bytes,
    job_directory,
    jobs_root_from_env,
    payload_sha256,
    read_private_json,
    result_payload,
    validate_http_request,
    validate_owner,
    validate_ready,
    validate_request,
    validate_result,
    write_private_json,
)


TASK_STATUS_NAME = "task-status.json"
TASK_TEMP_NAME = "tmp"
_ERROR_RE = re.compile(r"[a-z][a-z0-9_]{0,119}")
_CONCURRENCY = threading.BoundedSemaphore(1)


class ReaderV2ConverterServiceFailure(RuntimeError):
    def __init__(self, code: str, *, http_status: int = 422):
        super().__init__(code)
        self.code = code
        self.http_status = http_status


def _fail_job(exc: ReaderV2JobFailure) -> ReaderV2ConverterServiceFailure:
    return ReaderV2ConverterServiceFailure(exc.code)


def _converter_timeout(env: Mapping[str, str] | None = None) -> int:
    values = os.environ if env is None else env
    raw = values.get("PAPER_READER_V2_CONVERTER_TIMEOUT_SECONDS", "1200")
    if not isinstance(raw, str) or not re.fullmatch(r"[0-9]+", raw):
        raise ReaderV2ConverterServiceFailure(
            "reader_v2_converter_configuration_invalid", http_status=503
        )
    value = int(raw)
    if not 60 <= value <= 1800:
        raise ReaderV2ConverterServiceFailure(
            "reader_v2_converter_configuration_invalid", http_status=503
        )
    return value


def _child_environment(job_root: Path, base: Mapping[str, str] | None = None) -> dict[str, str]:
    source = os.environ if base is None else base
    temporary = job_root / TASK_TEMP_NAME
    return {
        "PATH": source.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "TMPDIR": str(temporary),
        "TMP": str(temporary),
        "TEMP": str(temporary),
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "HF_HOME": source.get("HF_HOME", "/models"),
        "DOCLING_ARTIFACTS_PATH": source.get(
            "DOCLING_ARTIFACTS_PATH", "/models/docling"
        ),
        "PAPER_READER_V2_CONVERTER_CHILD": "1",
    }


def _source_sha256(path: Path, *, expected_size: int) -> str:
    descriptor = -1
    try:
        details = path.lstat()
        if (
            not stat.S_ISREG(details.st_mode)
            or stat.S_ISLNK(details.st_mode)
            or details.st_size != expected_size
            or stat.S_IMODE(details.st_mode) & 0o077
        ):
            raise ReaderV2ConverterServiceFailure(
                "reader_v2_converter_source_invalid"
            )
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        before = os.fstat(descriptor)
        digest = hashlib.sha256()
        total = 0
        while chunk := os.read(descriptor, 1024 * 1024):
            total += len(chunk)
            if total > expected_size:
                raise ReaderV2ConverterServiceFailure(
                    "reader_v2_converter_source_invalid"
                )
            digest.update(chunk)
        after = os.fstat(descriptor)
        if total != expected_size or (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ReaderV2ConverterServiceFailure(
                "reader_v2_converter_source_changed"
            )
        return digest.hexdigest()
    except ReaderV2ConverterServiceFailure:
        raise
    except OSError as exc:
        raise ReaderV2ConverterServiceFailure(
            "reader_v2_converter_source_invalid"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _read_task_status(path: Path) -> dict[str, Any]:
    try:
        value = read_private_json(path, maximum=MAX_TASK_STATUS_BYTES)
    except ReaderV2JobFailure as exc:
        raise ReaderV2ConverterServiceFailure(
            "reader_v2_converter_status_invalid", http_status=503
        ) from exc
    if not isinstance(value, dict):
        raise ReaderV2ConverterServiceFailure(
            "reader_v2_converter_status_invalid", http_status=503
        )
    if value.get("ok") is False and set(value) == {"ok", "error"}:
        error = value["error"]
        if isinstance(error, str) and _ERROR_RE.fullmatch(error):
            raise ReaderV2ConverterServiceFailure(error)
    if value.get("ok") is not True or set(value) != {"ok", "source", "files"}:
        raise ReaderV2ConverterServiceFailure(
            "reader_v2_converter_status_invalid", http_status=503
        )
    if value["source"] != "docling_pdf":
        raise ReaderV2ConverterServiceFailure(
            "reader_v2_converter_status_invalid", http_status=503
        )
    return value


def _run_child(job_root: Path, *, timeout: int) -> None:
    argv = [
        sys.executable,
        "-m",
        "paper_service.reader_v2_converter_task",
        str(job_root / REQUEST_NAME),
        str(job_root / SOURCE_NAME),
        str(job_root / OUTPUT_NAME),
        str(job_root / TASK_STATUS_NAME),
    ]
    process = subprocess.Popen(  # noqa: S603 - argv is fixed and shell-free
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=_child_environment(job_root),
        close_fds=True,
        start_new_session=True,
    )
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        finally:
            process.wait()
        raise ReaderV2ConverterServiceFailure(
            "reader_v2_converter_timeout", http_status=503
        ) from exc


def _existing_result(
    root: Path,
    *,
    request: Mapping[str, Any],
    request_digest: str,
) -> dict[str, Any] | None:
    try:
        (root / RESULT_NAME).lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ReaderV2ConverterServiceFailure(
            "reader_v2_converter_result_invalid", http_status=503
        ) from exc
    try:
        result = validate_result(read_private_json(root / RESULT_NAME))
        actual = build_output_manifest(root / OUTPUT_NAME)
    except ReaderV2JobFailure as exc:
        raise ReaderV2ConverterServiceFailure(
            "reader_v2_converter_result_invalid", http_status=503
        ) from exc
    if (
        result["job_id"] != request["job_id"]
        or result["request_sha256"] != request_digest
        or result["source_sha256"] != request["source_sha256"]
        or result["source"] != "docling_pdf"
        or result["files"] != actual
    ):
        raise ReaderV2ConverterServiceFailure(
            "reader_v2_converter_result_invalid", http_status=503
        )
    return result


def execute_converter_request(value: Any) -> dict[str, Any]:
    try:
        http_request = validate_http_request(value)
        jobs_root = jobs_root_from_env()
        root = job_directory(jobs_root, http_request["job_id"])
        details = root.lstat()
        if not stat.S_ISDIR(details.st_mode) or stat.S_ISLNK(details.st_mode):
            raise ReaderV2JobFailure("reader_v2_job_directory_invalid")
        validate_owner(
            read_private_json(root / OWNER_NAME),
            expected_job_id=http_request["job_id"],
        )
        request = validate_request(read_private_json(root / REQUEST_NAME))
        ready = validate_ready(
            read_private_json(root / READY_NAME),
            expected_job_id=http_request["job_id"],
        )
    except ReaderV2JobFailure as exc:
        raise _fail_job(exc) from exc
    except OSError as exc:
        raise ReaderV2ConverterServiceFailure("reader_v2_job_missing") from exc

    request_digest = payload_sha256(request)
    if (
        request["job_id"] != http_request["job_id"]
        or request_digest != http_request["request_sha256"]
        or ready["request_sha256"] != request_digest
        or ready["source_sha256"] != request["source_sha256"]
        or ready["nonce_sha256"]
        != hashlib.sha256(http_request["nonce"].encode("ascii")).hexdigest()
    ):
        raise ReaderV2ConverterServiceFailure("reader_v2_job_authorization_invalid")

    source = root / SOURCE_NAME
    if _source_sha256(source, expected_size=request["source_size"]) != request[
        "source_sha256"
    ]:
        raise ReaderV2ConverterServiceFailure("reader_v2_converter_source_hash_mismatch")
    existing = _existing_result(
        root,
        request=request,
        request_digest=request_digest,
    )
    if existing is not None:
        return existing
    try:
        (root / OUTPUT_NAME).lstat()
        raise ReaderV2ConverterServiceFailure("reader_v2_converter_output_exists")
    except FileNotFoundError:
        pass
    try:
        (root / TASK_STATUS_NAME).lstat()
        raise ReaderV2ConverterServiceFailure("reader_v2_converter_status_exists")
    except FileNotFoundError:
        pass
    temporary = root / TASK_TEMP_NAME
    temporary.mkdir(mode=0o700)
    try:
        with conversion_lock_from_env():
            _run_child(root, timeout=_converter_timeout())
    except ConversionLockFailure as exc:
        raise ReaderV2ConverterServiceFailure(exc.code, http_status=503) from exc
    status = _read_task_status(root / TASK_STATUS_NAME)
    try:
        actual = build_output_manifest(root / OUTPUT_NAME)
    except ReaderV2JobFailure as exc:
        raise _fail_job(exc) from exc
    if status["files"] != actual:
        raise ReaderV2ConverterServiceFailure("reader_v2_converter_manifest_mismatch")
    if _source_sha256(source, expected_size=request["source_size"]) != request[
        "source_sha256"
    ]:
        raise ReaderV2ConverterServiceFailure("reader_v2_converter_source_changed")
    result = result_payload(
        job=request["job_id"],
        request_sha256=request_digest,
        source_sha256=request["source_sha256"],
        source="docling_pdf",
        files=actual,
    )
    try:
        write_private_json(root / RESULT_NAME, result, mode=0o400)
    except ReaderV2JobFailure as exc:
        raise _fail_job(exc) from exc
    return result


class ReaderV2ConverterRequestHandler(BaseHTTPRequestHandler):
    server_version = "paper-reader-v2-converter"
    sys_version = ""

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _send(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/healthz":
            self._send(HTTPStatus.NOT_FOUND, b'{"status":"missing"}')
            return
        self._send(HTTPStatus.OK, b'{"status":"ok"}')

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/v1/convert":
            self._send(HTTPStatus.NOT_FOUND, b'{"error":"reader_v2_converter_operation_invalid"}')
            return
        try:
            length_text = self.headers.get("Content-Length")
            length = int(length_text) if length_text is not None else -1
            if not 1 <= length <= MAX_HTTP_BYTES:
                raise ReaderV2ConverterServiceFailure(
                    "reader_v2_converter_request_too_large", http_status=413
                )
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ReaderV2ConverterServiceFailure(
                    "reader_v2_converter_request_invalid"
                )
            try:
                value = json.loads(raw.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise ReaderV2ConverterServiceFailure(
                    "reader_v2_converter_request_invalid"
                ) from exc
            if not _CONCURRENCY.acquire(blocking=False):
                raise ReaderV2ConverterServiceFailure(
                    "reader_v2_converter_busy", http_status=503
                )
            try:
                result = execute_converter_request(value)
            finally:
                _CONCURRENCY.release()
            self._send(HTTPStatus.OK, canonical_json_bytes(result))
        except ReaderV2ConverterServiceFailure as exc:
            body = json.dumps({"error": exc.code}, separators=(",", ":")).encode()
            self._send(exc.http_status, body)
        except Exception:
            self._send(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                b'{"error":"reader_v2_converter_service_failed"}',
            )


def run_server() -> None:
    raw = os.environ.get("PAPER_READER_V2_CONVERTER_PORT", "8091")
    if not re.fullmatch(r"[0-9]+", raw):
        raise RuntimeError("PAPER_READER_V2_CONVERTER_PORT invalid")
    port = int(raw)
    if not 1 <= port <= 65_535:
        raise RuntimeError("PAPER_READER_V2_CONVERTER_PORT invalid")
    server = ThreadingHTTPServer(("0.0.0.0", port), ReaderV2ConverterRequestHandler)
    server.daemon_threads = True
    server.serve_forever(poll_interval=0.5)


if __name__ == "__main__":
    run_server()

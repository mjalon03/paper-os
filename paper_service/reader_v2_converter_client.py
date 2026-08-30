"""Coordinator-side client for the isolated Reader v2 Docling converter."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
from typing import Any, Iterator, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import build_opener, HTTPRedirectHandler, Request

from .artifact_versions import (
    ArtifactVersionError,
    copy_artifact_files_from_directory,
    copy_raw_source_to_file,
    verify_raw_source_sha256,
)
from .reader_v2_job import (
    JOBS_LOCK_NAME,
    MAX_HTTP_BYTES,
    OUTPUT_NAME,
    OWNER_NAME,
    READY_NAME,
    REQUEST_NAME,
    RESULT_NAME,
    SOURCE_NAME,
    ReaderV2JobFailure,
    build_output_manifest,
    build_request,
    canonical_json_bytes,
    http_request_payload,
    job_directory,
    jobs_root_from_env,
    owner_payload,
    payload_sha256,
    read_private_json,
    ready_payload,
    validate_owner,
    validate_result,
    write_private_json,
)


_ERROR_RE = re.compile(r"[a-z][a-z0-9_]{0,119}")


class ReaderV2ConverterClientFailure(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any):
        return None


_OPENER = build_opener(_NoRedirectHandler)


def _base_url(env: Mapping[str, str] | None = None) -> str:
    values = os.environ if env is None else env
    raw = values.get(
        "PAPER_READER_V2_CONVERTER_URL",
        "http://paper-reader-v2-converter:8091",
    )
    parsed = urlsplit(raw)
    allowed = {
        item.strip()
        for item in values.get(
            "PAPER_READER_V2_CONVERTER_ALLOWED_HOSTS",
            "paper-reader-v2-converter",
        ).split(",")
        if item.strip()
    }
    try:
        port = parsed.port
    except ValueError as exc:
        raise ReaderV2ConverterClientFailure(
            "reader_v2_converter_configuration_invalid"
        ) from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname not in allowed
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or port is None
    ):
        raise ReaderV2ConverterClientFailure(
            "reader_v2_converter_configuration_invalid"
        )
    return raw.rstrip("/")


def _timeout(env: Mapping[str, str] | None = None) -> int:
    values = os.environ if env is None else env
    raw = values.get("PAPER_READER_V2_CONVERTER_CLIENT_TIMEOUT_SECONDS", "1260")
    if not isinstance(raw, str) or not re.fullmatch(r"[0-9]+", raw):
        raise ReaderV2ConverterClientFailure(
            "reader_v2_converter_configuration_invalid"
        )
    value = int(raw)
    if not 65 <= value <= 1860:
        raise ReaderV2ConverterClientFailure(
            "reader_v2_converter_configuration_invalid"
        )
    return value


def _post_converter(value: Mapping[str, Any]) -> dict[str, Any]:
    encoded = canonical_json_bytes(value)
    request = Request(
        f"{_base_url()}/v1/convert",
        data=encoded,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with _OPENER.open(request, timeout=_timeout()) as response:
            content_type = response.headers.get_content_type()
            body = response.read(MAX_HTTP_BYTES + 1)
    except HTTPError as exc:
        try:
            body = exc.read(MAX_HTTP_BYTES + 1)
            error = json.loads(body.decode("utf-8")).get("error")
        except Exception:
            error = None
        if not isinstance(error, str) or not _ERROR_RE.fullmatch(error):
            error = "reader_v2_converter_unavailable"
        raise ReaderV2ConverterClientFailure(error) from exc
    except (OSError, TimeoutError, URLError) as exc:
        raise ReaderV2ConverterClientFailure(
            "reader_v2_converter_unavailable"
        ) from exc
    if content_type != "application/json" or len(body) > MAX_HTTP_BYTES:
        raise ReaderV2ConverterClientFailure("reader_v2_converter_response_invalid")
    try:
        return validate_result(json.loads(body.decode("utf-8")))
    except (ReaderV2JobFailure, UnicodeError, json.JSONDecodeError) as exc:
        raise ReaderV2ConverterClientFailure(
            "reader_v2_converter_response_invalid"
        ) from exc


def _lock_descriptor(root: Path) -> int:
    descriptor = -1
    try:
        descriptor = os.open(
            root / JOBS_LOCK_NAME,
            os.O_RDWR
            | os.O_CREAT
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or stat.S_IMODE(details.st_mode) & 0o077:
            raise ReaderV2ConverterClientFailure("reader_v2_job_lock_invalid")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        return descriptor
    except ReaderV2ConverterClientFailure:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    except OSError as exc:
        if descriptor >= 0:
            os.close(descriptor)
        raise ReaderV2ConverterClientFailure("reader_v2_job_lock_unavailable") from exc


def _owned_job(root: Path, expected_job: str) -> bool:
    try:
        details = root.lstat()
        if not stat.S_ISDIR(details.st_mode) or stat.S_ISLNK(details.st_mode):
            return False
        validate_owner(
            read_private_json(root / OWNER_NAME),
            expected_job_id=expected_job,
        )
        return True
    except (OSError, ReaderV2JobFailure):
        return False


@contextmanager
def _job_workspace(jobs_root: Path) -> Iterator[tuple[str, Path]]:
    lock = _lock_descriptor(jobs_root)
    identifier = f"job-{secrets.token_hex(16)}"
    root = job_directory(jobs_root, identifier)
    try:
        root.mkdir(mode=0o700)
        write_private_json(root / OWNER_NAME, owner_payload(identifier), mode=0o400)
        yield identifier, root
    finally:
        if _owned_job(root, identifier):
            shutil.rmtree(root, ignore_errors=True)
        fcntl.flock(lock, fcntl.LOCK_UN)
        os.close(lock)


def convert_pdf_to_directory(
    paper: Mapping[str, Any],
    data_dir: Path,
    output_dir: Path,
) -> str:
    """Project one exact PDF, invoke the isolated service, and stream its result."""

    try:
        paper_id = paper["id"]
        stored_path = paper["stored_path"]
        source_sha256 = paper["sha256"]
        if (
            not isinstance(paper_id, int)
            or isinstance(paper_id, bool)
            or paper_id < 1
            or not isinstance(stored_path, str)
            or not isinstance(source_sha256, str)
        ):
            raise ReaderV2ConverterClientFailure("reader_v2_paper_invalid")
        if not isinstance(data_dir, Path) or not data_dir.is_absolute():
            raise ReaderV2ConverterClientFailure("reader_v2_data_dir_invalid")
        if not isinstance(output_dir, Path) or not output_dir.is_absolute():
            raise ReaderV2ConverterClientFailure("reader_v2_output_dir_invalid")
        jobs_root = jobs_root_from_env()
        with _job_workspace(jobs_root) as (identifier, root):
            size = copy_raw_source_to_file(
                data_dir,
                paper_id,
                stored_path,
                source_sha256,
                root / SOURCE_NAME,
            )
            request = build_request(
                job=identifier,
                paper=paper,
                source_sha256=source_sha256,
                source_size=size,
            )
            request_digest = payload_sha256(request)
            write_private_json(root / REQUEST_NAME, request, mode=0o400)
            request_nonce = secrets.token_hex(32)
            ready = ready_payload(
                job=identifier,
                request_sha256=request_digest,
                nonce_sha256=hashlib.sha256(request_nonce.encode("ascii")).hexdigest(),
                source_sha256=source_sha256,
            )
            write_private_json(root / READY_NAME, ready, mode=0o400)
            response = _post_converter(
                http_request_payload(
                    job=identifier,
                    request_sha256=request_digest,
                    request_nonce=request_nonce,
                )
            )
            disk_result = validate_result(read_private_json(root / RESULT_NAME))
            actual = build_output_manifest(root / OUTPUT_NAME)
            if (
                response != disk_result
                or disk_result["job_id"] != identifier
                or disk_result["request_sha256"] != request_digest
                or disk_result["source_sha256"] != source_sha256
                or disk_result["source"] != "docling_pdf"
                or disk_result["files"] != actual
            ):
                raise ReaderV2ConverterClientFailure(
                    "reader_v2_converter_result_mismatch"
                )
            verify_raw_source_sha256(
                data_dir,
                paper_id,
                stored_path,
                source_sha256,
            )
            copy_artifact_files_from_directory(
                root / OUTPUT_NAME,
                output_dir,
                disk_result["files"],
            )
            verify_raw_source_sha256(
                data_dir,
                paper_id,
                stored_path,
                source_sha256,
            )
            return "docling_pdf"
    except ReaderV2ConverterClientFailure:
        raise
    except (ArtifactVersionError, ReaderV2JobFailure, OSError) as exc:
        raise ReaderV2ConverterClientFailure("reader_v2_converter_boundary_failed") from exc


__all__ = [
    "ReaderV2ConverterClientFailure",
    "convert_pdf_to_directory",
]

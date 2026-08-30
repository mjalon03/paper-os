"""Strict shared-volume contract for one isolated Reader v2 conversion job."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, Mapping

from .artifact_versions import MAX_RAW_SOURCE_BYTES
from .artifact_versions import MAX_FILE_BYTES, MAX_FILES, MAX_TOTAL_BYTES


JOB_SCHEMA = "paper-reader-v2-conversion-job"
JOB_SCHEMA_VERSION = 1
OWNER_NAME = ".reader-v2-job-owner.json"
REQUEST_NAME = "request.json"
READY_NAME = "ready.json"
SOURCE_NAME = "source.pdf"
OUTPUT_NAME = "output"
RESULT_NAME = "result.json"
JOBS_LOCK_NAME = ".jobs.lock"
MAX_JOB_JSON_BYTES = 256 * 1024
MAX_HTTP_BYTES = MAX_JOB_JSON_BYTES
_JOB_ID_RE = re.compile(r"job-[0-9a-f]{32}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_SOURCE_RE = re.compile(r"(?:pmc_jats|docling_pdf)")


class ReaderV2JobFailure(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _fail(code: str) -> ReaderV2JobFailure:
    return ReaderV2JobFailure(code)


def job_id(value: Any) -> str:
    if not isinstance(value, str) or not _JOB_ID_RE.fullmatch(value):
        raise _fail("reader_v2_job_id_invalid")
    return value


def sha256_text(value: Any, *, code: str = "reader_v2_job_sha_invalid") -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise _fail(code)
    return value


def nonce(value: Any) -> str:
    return sha256_text(value, code="reader_v2_job_nonce_invalid")


def canonical_json_bytes(value: Any) -> bytes:
    try:
        encoded = (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise _fail("reader_v2_job_json_invalid") from exc
    if not 1 <= len(encoded) <= MAX_JOB_JSON_BYTES:
        raise _fail("reader_v2_job_json_too_large")
    return encoded


def payload_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _bounded_text(value: Any, *, maximum: int, nullable: bool = True) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or len(value) > maximum:
        raise _fail("reader_v2_job_metadata_invalid")
    return value


def clean_conversion_paper(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise _fail("reader_v2_job_metadata_invalid")
    paper_id = value.get("id")
    if (
        not isinstance(paper_id, int)
        or isinstance(paper_id, bool)
        or paper_id < 1
    ):
        raise _fail("reader_v2_job_metadata_invalid")
    raw_authors = value.get("authors", [])
    if not isinstance(raw_authors, list) or len(raw_authors) > 100:
        raise _fail("reader_v2_job_metadata_invalid")
    authors = [
        _bounded_text(author, maximum=300, nullable=False)
        for author in raw_authors
    ]
    year = value.get("publication_year")
    if year is not None and (
        not isinstance(year, int)
        or isinstance(year, bool)
        or not 1800 <= year <= 2200
    ):
        raise _fail("reader_v2_job_metadata_invalid")
    return {
        "id": paper_id,
        "title": _bounded_text(value.get("title"), maximum=500),
        "authors": authors,
        "publication_year": year,
        "doi": _bounded_text(value.get("doi"), maximum=500),
    }


def build_request(
    *,
    job: str,
    paper: Mapping[str, Any],
    source_sha256: str,
    source_size: int,
) -> dict[str, Any]:
    value = {
        "schema": JOB_SCHEMA,
        "schema_version": JOB_SCHEMA_VERSION,
        "job_id": job_id(job),
        "source_sha256": sha256_text(source_sha256),
        "source_size": source_size,
        "paper": clean_conversion_paper(paper),
    }
    return validate_request(value)


def validate_request(value: Any) -> dict[str, Any]:
    required = {
        "schema",
        "schema_version",
        "job_id",
        "source_sha256",
        "source_size",
        "paper",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise _fail("reader_v2_job_request_invalid")
    size = value["source_size"]
    if (
        value["schema"] != JOB_SCHEMA
        or value["schema_version"] != JOB_SCHEMA_VERSION
        or not isinstance(size, int)
        or isinstance(size, bool)
        or not 1 <= size <= MAX_RAW_SOURCE_BYTES
    ):
        raise _fail("reader_v2_job_request_invalid")
    return {
        "schema": JOB_SCHEMA,
        "schema_version": JOB_SCHEMA_VERSION,
        "job_id": job_id(value["job_id"]),
        "source_sha256": sha256_text(value["source_sha256"]),
        "source_size": size,
        "paper": clean_conversion_paper(value["paper"]),
    }


def owner_payload(job: str) -> dict[str, Any]:
    return {
        "schema": JOB_SCHEMA,
        "schema_version": JOB_SCHEMA_VERSION,
        "job_id": job_id(job),
        "kind": "owner",
    }


def validate_owner(value: Any, *, expected_job_id: str) -> dict[str, Any]:
    expected = owner_payload(expected_job_id)
    if not isinstance(value, Mapping) or dict(value) != expected:
        raise _fail("reader_v2_job_owner_invalid")
    return expected


def ready_payload(
    *,
    job: str,
    request_sha256: str,
    nonce_sha256: str,
    source_sha256: str,
) -> dict[str, Any]:
    return {
        "schema": JOB_SCHEMA,
        "schema_version": JOB_SCHEMA_VERSION,
        "job_id": job_id(job),
        "kind": "ready",
        "request_sha256": sha256_text(request_sha256),
        "nonce_sha256": sha256_text(nonce_sha256),
        "source_sha256": sha256_text(source_sha256),
    }


def validate_ready(value: Any, *, expected_job_id: str) -> dict[str, Any]:
    required = {
        "schema",
        "schema_version",
        "job_id",
        "kind",
        "request_sha256",
        "nonce_sha256",
        "source_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise _fail("reader_v2_job_ready_invalid")
    canonical = ready_payload(
        job=expected_job_id,
        request_sha256=value["request_sha256"],
        nonce_sha256=value["nonce_sha256"],
        source_sha256=value["source_sha256"],
    )
    if dict(value) != canonical:
        raise _fail("reader_v2_job_ready_invalid")
    return canonical


def http_request_payload(
    *, job: str, request_sha256: str, request_nonce: str
) -> dict[str, Any]:
    return {
        "job_id": job_id(job),
        "request_sha256": sha256_text(request_sha256),
        "nonce": nonce(request_nonce),
    }


def validate_http_request(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
        "job_id",
        "request_sha256",
        "nonce",
    }:
        raise _fail("reader_v2_converter_request_invalid")
    return http_request_payload(
        job=value["job_id"],
        request_sha256=value["request_sha256"],
        request_nonce=value["nonce"],
    )


def validate_file_manifest(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, Mapping) or not 1 <= len(value) <= MAX_FILES:
        raise _fail("reader_v2_converter_manifest_invalid")
    clean: dict[str, dict[str, Any]] = {}
    total = 0
    for raw_path, raw_metadata in value.items():
        if not isinstance(raw_path, str) or (
            raw_path != "article.json"
            and not re.fullmatch(r"assets/[A-Za-z0-9._-]{1,180}", raw_path)
        ):
            raise _fail("reader_v2_converter_manifest_invalid")
        if not isinstance(raw_metadata, Mapping) or set(raw_metadata) != {
            "sha256",
            "size",
        }:
            raise _fail("reader_v2_converter_manifest_invalid")
        size = raw_metadata["size"]
        if (
            not isinstance(size, int)
            or isinstance(size, bool)
            or not 0 <= size <= MAX_FILE_BYTES
        ):
            raise _fail("reader_v2_converter_manifest_invalid")
        total += size
        if total > MAX_TOTAL_BYTES:
            raise _fail("reader_v2_converter_manifest_invalid")
        clean[raw_path] = {
            "sha256": sha256_text(
                raw_metadata["sha256"],
                code="reader_v2_converter_manifest_invalid",
            ),
            "size": size,
        }
    if "article.json" not in clean:
        raise _fail("reader_v2_converter_manifest_invalid")
    return {path: clean[path] for path in sorted(clean)}


def _hash_regular_file(path: Path) -> tuple[str, int]:
    descriptor = -1
    try:
        details = path.lstat()
        if (
            not stat.S_ISREG(details.st_mode)
            or stat.S_ISLNK(details.st_mode)
            or details.st_size > MAX_FILE_BYTES
        ):
            raise _fail("reader_v2_converter_output_invalid")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        before = os.fstat(descriptor)
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(
                descriptor,
                min(1024 * 1024, MAX_FILE_BYTES + 1 - total),
            )
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_FILE_BYTES:
                raise _fail("reader_v2_converter_output_invalid")
            digest.update(chunk)
        after = os.fstat(descriptor)
        if (
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
            raise _fail("reader_v2_converter_output_changed")
        return digest.hexdigest(), total
    except ReaderV2JobFailure:
        raise
    except OSError as exc:
        raise _fail("reader_v2_converter_output_invalid") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def build_output_manifest(output_root: Path) -> dict[str, dict[str, Any]]:
    """Hash the exact, bounded converter output tree without following links."""

    try:
        details = output_root.lstat()
        if not stat.S_ISDIR(details.st_mode) or stat.S_ISLNK(details.st_mode):
            raise _fail("reader_v2_converter_output_invalid")
        top = {entry.name: entry for entry in output_root.iterdir()}
    except ReaderV2JobFailure:
        raise
    except OSError as exc:
        raise _fail("reader_v2_converter_output_invalid") from exc
    if not set(top) <= {"article.json", "assets"} or "article.json" not in top:
        raise _fail("reader_v2_converter_output_invalid")
    paths: dict[str, Path] = {"article.json": top["article.json"]}
    assets = top.get("assets")
    if assets is not None:
        try:
            metadata = assets.lstat()
            if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                raise _fail("reader_v2_converter_output_invalid")
            for entry in assets.iterdir():
                if not re.fullmatch(r"[A-Za-z0-9._-]{1,180}", entry.name):
                    raise _fail("reader_v2_converter_output_invalid")
                paths[f"assets/{entry.name}"] = entry
        except ReaderV2JobFailure:
            raise
        except OSError as exc:
            raise _fail("reader_v2_converter_output_invalid") from exc
    if len(paths) > MAX_FILES:
        raise _fail("reader_v2_converter_output_invalid")
    manifest: dict[str, dict[str, Any]] = {}
    total = 0
    for relative in sorted(paths):
        digest, size = _hash_regular_file(paths[relative])
        total += size
        if total > MAX_TOTAL_BYTES:
            raise _fail("reader_v2_converter_output_invalid")
        manifest[relative] = {"sha256": digest, "size": size}
    return validate_file_manifest(manifest)


def result_payload(
    *,
    job: str,
    request_sha256: str,
    source_sha256: str,
    source: str,
    files: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    if not isinstance(source, str) or not _SOURCE_RE.fullmatch(source):
        raise _fail("reader_v2_converter_result_invalid")
    return {
        "schema": JOB_SCHEMA,
        "schema_version": JOB_SCHEMA_VERSION,
        "job_id": job_id(job),
        "request_sha256": sha256_text(request_sha256),
        "source_sha256": sha256_text(source_sha256),
        "source": source,
        "files": validate_file_manifest(files),
        "status": "ready",
    }


def validate_result(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
        "schema",
        "schema_version",
        "job_id",
        "request_sha256",
        "source_sha256",
        "source",
        "files",
        "status",
    }:
        raise _fail("reader_v2_converter_result_invalid")
    canonical = result_payload(
        job=value["job_id"],
        request_sha256=value["request_sha256"],
        source_sha256=value["source_sha256"],
        source=value["source"],
        files=value["files"],
    )
    if dict(value) != canonical:
        raise _fail("reader_v2_converter_result_invalid")
    return canonical


def jobs_root_from_env(env: Mapping[str, str] | None = None) -> Path:
    values = os.environ if env is None else env
    raw = values.get("PAPER_READER_V2_JOBS_DIR", "/jobs")
    root = Path(raw)
    if not root.is_absolute():
        raise _fail("reader_v2_job_configuration_invalid")
    try:
        details = root.lstat()
    except OSError as exc:
        raise _fail("reader_v2_job_root_unavailable") from exc
    if (
        not stat.S_ISDIR(details.st_mode)
        or stat.S_ISLNK(details.st_mode)
        or details.st_uid != os.geteuid()
        or stat.S_IMODE(details.st_mode) & 0o077
    ):
        raise _fail("reader_v2_job_root_unavailable")
    return root


def job_directory(root: Path, value: Any) -> Path:
    return root / job_id(value)


def read_private_json(path: Path, *, maximum: int = MAX_JOB_JSON_BYTES) -> Any:
    descriptor = -1
    try:
        details = path.lstat()
        if (
            not stat.S_ISREG(details.st_mode)
            or stat.S_ISLNK(details.st_mode)
            or not 1 <= details.st_size <= maximum
            or stat.S_IMODE(details.st_mode) & 0o077
        ):
            raise _fail("reader_v2_job_file_invalid")
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        before = os.fstat(descriptor)
        payload = bytearray()
        while True:
            chunk = os.read(descriptor, min(64 * 1024, maximum + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
            if len(payload) > maximum:
                raise _fail("reader_v2_job_file_invalid")
        after = os.fstat(descriptor)
        if (
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
            raise _fail("reader_v2_job_file_changed")
        return json.loads(bytes(payload).decode("utf-8"))
    except ReaderV2JobFailure:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise _fail("reader_v2_job_file_invalid") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def write_private_json(path: Path, value: Any, *, mode: int = 0o400) -> None:
    encoded = canonical_json_bytes(value)
    parent = os.open(
        path.parent,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    descriptor = -1
    try:
        descriptor = os.open(
            path.name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            mode,
            dir_fd=parent,
        )
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise _fail("reader_v2_job_file_write_failed")
            view = view[written:]
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
        os.fsync(parent)
    except FileExistsError as exc:
        raise _fail("reader_v2_job_file_exists") from exc
    except OSError as exc:
        raise _fail("reader_v2_job_file_write_failed") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent)


__all__ = [
    "JOBS_LOCK_NAME",
    "MAX_HTTP_BYTES",
    "OUTPUT_NAME",
    "OWNER_NAME",
    "READY_NAME",
    "RESULT_NAME",
    "REQUEST_NAME",
    "SOURCE_NAME",
    "ReaderV2JobFailure",
    "build_request",
    "build_output_manifest",
    "canonical_json_bytes",
    "clean_conversion_paper",
    "http_request_payload",
    "job_directory",
    "job_id",
    "jobs_root_from_env",
    "owner_payload",
    "payload_sha256",
    "read_private_json",
    "ready_payload",
    "result_payload",
    "validate_http_request",
    "validate_file_manifest",
    "validate_owner",
    "validate_ready",
    "validate_request",
    "validate_result",
    "write_private_json",
]

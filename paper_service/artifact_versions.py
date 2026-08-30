"""Immutable, crash-safe version storage for Reader v2 artifacts.

The existing ``<paper>/v2`` directory remains a read-only legacy fallback.
New artifacts live in a separate owned store and become visible only through an
atomically replaced JSON pointer after every expected file and checksum passes.
This module is independent from the database and HTTP reader integration.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
from types import MappingProxyType
from typing import Any, Iterator

from .selectors import SELECTOR_VERSION


MANIFEST_SCHEMA = "paper-reader-artifact-version"
MANIFEST_SCHEMA_VERSION = 1
STORE_SCHEMA = "paper-reader-artifact-store"
POINTER_SCHEMA = "paper-reader-current-version"
OWNER_SCHEMA = "paper-reader-owned-version"
STORE_DIRECTORY_NAME = "reader-v2-versions"
MANIFEST_NAME = "artifact-manifest.json"
OWNER_MARKER_NAME = ".owned-version.json"
STORE_MARKER_NAME = ".artifact-store.json"
CURRENT_POINTER_NAME = "current.json"
LOCK_NAME = ".operations.lock"

MAX_FILES = 512
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
MAX_RAW_SOURCE_BYTES = 512 * 1024 * 1024
MAX_MANIFEST_BYTES = 2 * 1024 * 1024

__all__ = [
    "CURRENT_POINTER_NAME",
    "MANIFEST_NAME",
    "MANIFEST_SCHEMA",
    "MANIFEST_SCHEMA_VERSION",
    "OWNER_MARKER_NAME",
    "STORE_DIRECTORY_NAME",
    "ArtifactResolution",
    "ArtifactStorePaths",
    "ArtifactVersionError",
    "StagedArtifactVersion",
    "activate_staged_version",
    "apply_retention",
    "artifact_store_paths",
    "build_version_descriptor",
    "build_version_descriptor_from_directory",
    "canonical_descriptor_bytes",
    "copy_artifact_files_from_directory",
    "copy_raw_source_to_file",
    "create_staged_version",
    "create_staged_version_from_directory",
    "descriptor_sha256",
    "plan_retention",
    "publish_version",
    "resolve_current_artifact",
    "rollback_current_version",
    "safe_relative_artifact_path",
    "validate_version_descriptor",
    "verify_raw_source_sha256",
    "verify_version_directory",
    "version_directory",
]

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_VERSION_ID_RE = re.compile(r"rv-[0-9a-f]{32}")
_VERSION_TEXT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+:-]{0,127}")
_PATH_SEGMENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,179}")
_CREATED_AT_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")

_MANIFEST_REQUIRED = frozenset(
    {
        "schema",
        "schema_version",
        "version_id",
        "paper_id",
        "source_sha256",
        "article_fingerprint",
        "selector_version",
        "extractor_version",
        "render_version",
        "created_at",
        "files",
    }
)
_FILE_META_REQUIRED = frozenset({"sha256", "size"})
_POINTER_REQUIRED = frozenset(
    {
        "schema",
        "schema_version",
        "paper_id",
        "version_id",
        "manifest_sha256",
    }
)
_OWNER_REQUIRED = _POINTER_REQUIRED
_STORE_REQUIRED = frozenset({"schema", "schema_version", "paper_id"})
_RESERVED_VERSION_FILES = frozenset({MANIFEST_NAME, OWNER_MARKER_NAME})
_EXPECTED_CURRENT_UNSET = object()


class ArtifactVersionError(RuntimeError):
    """The artifact store cannot prove a requested operation is safe."""


def _fail(code: str) -> ArtifactVersionError:
    return ArtifactVersionError(code)


def _object(
    value: Any,
    *,
    field: str,
    required: frozenset[str],
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _fail(f"{field}_must_be_object")
    if any(not isinstance(key, str) for key in value):
        raise _fail(f"{field}_key_invalid")
    missing = required - set(value)
    unknown = set(value) - required
    if missing:
        raise _fail(f"{field}_missing_{sorted(missing)[0]}")
    if unknown:
        raise _fail(f"{field}_unknown_field")
    return value


def _positive_paper_id(value: Any) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= 9_223_372_036_854_775_807
    ):
        raise _fail("paper_id_invalid")
    return value


def _sha256(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise _fail(f"{field}_invalid")
    return value


def _version_text(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not _VERSION_TEXT_RE.fullmatch(value):
        raise _fail(f"{field}_invalid")
    return value


def _created_at(value: Any) -> str:
    if not isinstance(value, str) or not _CREATED_AT_RE.fullmatch(value):
        raise _fail("created_at_invalid")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError as exc:
        raise _fail("created_at_invalid") from exc
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise _fail("created_at_invalid")
    return value


def safe_relative_artifact_path(value: Any) -> str:
    """Return one canonical POSIX path confined to an artifact directory."""

    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 512:
        raise _fail("artifact_path_invalid")
    if "\\" in value or "\x00" in value:
        raise _fail("artifact_path_invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value or not path.parts:
        raise _fail("artifact_path_invalid")
    if any(
        part in {"", ".", ".."} or not _PATH_SEGMENT_RE.fullmatch(part)
        for part in path.parts
    ):
        raise _fail("artifact_path_invalid")
    if value in _RESERVED_VERSION_FILES:
        raise _fail("artifact_path_reserved")
    return value


def _safe_raw_relative_path(value: Any) -> PurePosixPath:
    path = safe_relative_artifact_path(value)
    return PurePosixPath(path)


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        return (
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
        raise _fail("json_serialization_invalid") from exc


def _descriptor_identity(descriptor: Mapping[str, Any]) -> dict[str, Any]:
    return {key: descriptor[key] for key in sorted(descriptor) if key != "version_id"}


def _version_id_for(descriptor_without_id: Mapping[str, Any]) -> str:
    digest = hashlib.sha256(_canonical_json_bytes(descriptor_without_id)).hexdigest()
    return f"rv-{digest[:32]}"


def _canonical_file_metadata(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, Mapping):
        raise _fail("files_must_be_object")
    if len(value) == 0:
        raise _fail("files_empty")
    if len(value) > MAX_FILES:
        raise _fail("files_too_many_items")
    output: dict[str, dict[str, Any]] = {}
    total_size = 0
    for raw_path, raw_metadata in value.items():
        path = safe_relative_artifact_path(raw_path)
        if path in output:
            raise _fail("files_duplicate_path")
        metadata = _object(
            raw_metadata,
            field="file_metadata",
            required=_FILE_META_REQUIRED,
        )
        size = metadata["size"]
        if (
            isinstance(size, bool)
            or not isinstance(size, int)
            or not 0 <= size <= MAX_FILE_BYTES
        ):
            raise _fail("file_size_invalid")
        total_size += size
        if total_size > MAX_TOTAL_BYTES:
            raise _fail("files_total_size_exceeded")
        output[path] = {
            "sha256": _sha256(metadata["sha256"], field="file_sha256"),
            "size": size,
        }
    for path in output:
        if any(
            parent.as_posix() in output
            for parent in PurePosixPath(path).parents
            if parent.as_posix() != "."
        ):
            raise _fail("files_path_collision")
    return {key: output[key] for key in sorted(output)}


def validate_version_descriptor(payload: Any) -> dict[str, Any]:
    """Validate and return a canonical immutable artifact descriptor."""

    descriptor = _object(
        payload,
        field="manifest",
        required=_MANIFEST_REQUIRED,
    )
    if descriptor["schema"] != MANIFEST_SCHEMA:
        raise _fail("manifest_schema_invalid")
    if (
        isinstance(descriptor["schema_version"], bool)
        or descriptor["schema_version"] != MANIFEST_SCHEMA_VERSION
    ):
        raise _fail("manifest_schema_version_invalid")
    paper_id = _positive_paper_id(descriptor["paper_id"])
    if (
        isinstance(descriptor["selector_version"], bool)
        or not isinstance(descriptor["selector_version"], int)
        or descriptor["selector_version"] != SELECTOR_VERSION
    ):
        raise _fail("selector_version_invalid")
    version_id = descriptor["version_id"]
    if not isinstance(version_id, str) or not _VERSION_ID_RE.fullmatch(version_id):
        raise _fail("version_id_invalid")
    canonical = {
        "schema": MANIFEST_SCHEMA,
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "version_id": version_id,
        "paper_id": paper_id,
        "source_sha256": _sha256(
            descriptor["source_sha256"], field="source_sha256"
        ),
        "article_fingerprint": _sha256(
            descriptor["article_fingerprint"], field="article_fingerprint"
        ),
        "selector_version": SELECTOR_VERSION,
        "extractor_version": _version_text(
            descriptor["extractor_version"], field="extractor_version"
        ),
        "render_version": _version_text(
            descriptor["render_version"], field="render_version"
        ),
        "created_at": _created_at(descriptor["created_at"]),
        "files": _canonical_file_metadata(descriptor["files"]),
    }
    expected_id = _version_id_for(_descriptor_identity(canonical))
    if version_id != expected_id:
        raise _fail("version_id_mismatch")
    return canonical


def build_version_descriptor(
    *,
    paper_id: int,
    source_sha256: str,
    article_fingerprint: str,
    extractor_version: str,
    render_version: str,
    created_at: str,
    files: Mapping[str, bytes],
    selector_version: int = SELECTOR_VERSION,
) -> dict[str, Any]:
    """Build a descriptor directly from the immutable bytes to be staged."""

    if not isinstance(files, Mapping):
        raise _fail("file_payloads_must_be_object")
    if len(files) == 0:
        raise _fail("files_empty")
    if len(files) > MAX_FILES:
        raise _fail("files_too_many_items")
    metadata: dict[str, dict[str, Any]] = {}
    total_size = 0
    for raw_path, payload in files.items():
        path = safe_relative_artifact_path(raw_path)
        if not isinstance(payload, bytes):
            raise _fail("file_payload_must_be_bytes")
        if len(payload) > MAX_FILE_BYTES:
            raise _fail("file_size_invalid")
        total_size += len(payload)
        if total_size > MAX_TOTAL_BYTES:
            raise _fail("files_total_size_exceeded")
        metadata[path] = {
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size": len(payload),
        }
    return _build_version_descriptor_from_metadata(
        paper_id=paper_id,
        source_sha256=source_sha256,
        article_fingerprint=article_fingerprint,
        extractor_version=extractor_version,
        render_version=render_version,
        created_at=created_at,
        metadata=metadata,
        selector_version=selector_version,
    )


def _build_version_descriptor_from_metadata(
    *,
    paper_id: int,
    source_sha256: str,
    article_fingerprint: str,
    extractor_version: str,
    render_version: str,
    created_at: str,
    metadata: Mapping[str, Mapping[str, Any]],
    selector_version: int,
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema": MANIFEST_SCHEMA,
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "paper_id": paper_id,
        "source_sha256": source_sha256,
        "article_fingerprint": article_fingerprint,
        "selector_version": selector_version,
        "extractor_version": extractor_version,
        "render_version": render_version,
        "created_at": created_at,
        "files": metadata,
    }
    provisional = dict(base, version_id="rv-" + "0" * 32)
    canonical_base = validate_version_descriptor(
        dict(
            provisional,
            version_id=_version_id_for(_descriptor_identity(provisional)),
        )
    )
    return canonical_base


def canonical_descriptor_bytes(payload: Any) -> bytes:
    return _canonical_json_bytes(validate_version_descriptor(payload))


def descriptor_sha256(payload: Any) -> str:
    return hashlib.sha256(canonical_descriptor_bytes(payload)).hexdigest()


def _freeze_descriptor(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    canonical = validate_version_descriptor(payload)
    frozen_files = MappingProxyType(
        {
            path: MappingProxyType(dict(metadata))
            for path, metadata in canonical["files"].items()
        }
    )
    return MappingProxyType(dict(canonical, files=frozen_files))


@dataclass(frozen=True)
class ArtifactStorePaths:
    data_dir: Path
    paper_id: int
    paper_root: Path
    store_root: Path
    versions_root: Path
    staging_root: Path
    current_pointer: Path
    legacy_root: Path


def artifact_store_paths(data_dir: Path, paper_id: int) -> ArtifactStorePaths:
    data = Path(data_dir)
    if not data.is_absolute():
        raise _fail("data_dir_must_be_absolute")
    paper = _positive_paper_id(paper_id)
    paper_root = data / "library" / "mobile" / str(paper)
    store = paper_root / STORE_DIRECTORY_NAME
    return ArtifactStorePaths(
        data,
        paper,
        paper_root,
        store,
        store / "versions",
        store / "staging",
        store / CURRENT_POINTER_NAME,
        paper_root / "v2",
    )


def version_directory(paths: ArtifactStorePaths, version_id: Any) -> Path:
    if not isinstance(version_id, str) or not _VERSION_ID_RE.fullmatch(version_id):
        raise _fail("version_id_invalid")
    return paths.versions_root / version_id


def _open_directory(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise _fail("unsafe_directory") from exc
    if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise _fail("unsafe_directory")
    return descriptor


def _absolute_source_root(value: Path) -> Path:
    root = Path(value)
    if not root.is_absolute():
        raise _fail("source_root_must_be_absolute")
    return root


def _canonical_source_files(value: Sequence[str]) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise _fail("source_files_must_be_sequence")
    if not value:
        raise _fail("files_empty")
    if len(value) > MAX_FILES:
        raise _fail("files_too_many_items")
    paths = tuple(safe_relative_artifact_path(item) for item in value)
    if len(set(paths)) != len(paths):
        raise _fail("files_duplicate_path")
    return tuple(sorted(paths))


def _open_relative_regular(root: Path, relative: str) -> int:
    safe = PurePosixPath(safe_relative_artifact_path(relative))
    try:
        directory = _open_directory(_absolute_source_root(root))
    except ArtifactVersionError as exc:
        raise _fail("artifact_source_unsafe") from exc
    try:
        for part in safe.parts[:-1]:
            try:
                child = os.open(
                    part,
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=directory,
                )
            except OSError as exc:
                raise _fail("artifact_source_unsafe") from exc
            os.close(directory)
            directory = child
        try:
            source = os.open(
                safe.parts[-1],
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory,
            )
        except FileNotFoundError as exc:
            raise _fail("artifact_source_missing") from exc
        except OSError as exc:
            raise _fail("artifact_source_unsafe") from exc
        if not stat.S_ISREG(os.fstat(source).st_mode):
            os.close(source)
            raise _fail("artifact_source_unsafe")
        return source
    finally:
        os.close(directory)


def _ensure_directory_chain(base: Path, parts: Sequence[str]) -> Path:
    descriptor = _open_directory(base)
    current = base
    try:
        for part in parts:
            if not _PATH_SEGMENT_RE.fullmatch(part):
                raise _fail("directory_segment_invalid")
            try:
                os.mkdir(part, 0o750, dir_fd=descriptor)
            except FileExistsError:
                pass
            try:
                child = os.open(
                    part,
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=descriptor,
                )
            except OSError as exc:
                raise _fail("unsafe_directory") from exc
            os.close(descriptor)
            descriptor = child
            current = current / part
        return current
    finally:
        os.close(descriptor)


def _require_regular_no_follow(path: Path) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise _fail("file_missing") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise _fail("unsafe_file_type")
    return metadata


def _write_exclusive(path: Path, payload: bytes, mode: int = 0o640) -> None:
    parent_descriptor = _open_directory(path.parent)
    descriptor = -1
    try:
        descriptor = os.open(
            path.name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            mode,
            dir_fd=parent_descriptor,
        )
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
        os.fchmod(descriptor, mode)
        os.fsync(parent_descriptor)
    except FileExistsError as exc:
        raise _fail("file_already_exists") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_descriptor)


def _copy_relative_exclusive(
    source_root: Path,
    relative: str,
    destination: Path,
    expected: Mapping[str, Any],
    *,
    mode: int = 0o640,
) -> None:
    """Copy one no-follow source while proving identity and expected digest."""

    source = _open_relative_regular(source_root, relative)
    parent = -1
    target = -1
    try:
        parent = _open_directory(destination.parent)
        before = os.fstat(source)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size > MAX_FILE_BYTES
            or before.st_size != expected["size"]
        ):
            raise _fail("staging_source_checksum_mismatch")
        try:
            target = os.open(
                destination.name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0),
                mode,
                dir_fd=parent,
            )
        except FileExistsError as exc:
            raise _fail("file_already_exists") from exc
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(source, min(1024 * 1024, MAX_FILE_BYTES + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_FILE_BYTES:
                raise _fail("file_too_large")
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(target, view)
                if written <= 0:
                    raise _fail("staging_copy_failed")
                view = view[written:]
        after = os.fstat(source)
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
            raise _fail("artifact_source_changed_while_copying")
        if total != expected["size"] or digest.hexdigest() != expected["sha256"]:
            raise _fail("staging_source_checksum_mismatch")
        os.fchmod(target, mode)
        os.fsync(target)
        os.fsync(parent)
    finally:
        if target >= 0:
            os.close(target)
        if parent >= 0:
            os.close(parent)
        os.close(source)


def _atomic_replace_file(
    path: Path,
    payload: bytes,
    mode: int = 0o640,
    *,
    replaced_callback: Callable[[], None] | None = None,
) -> None:
    parent_descriptor = _open_directory(path.parent)
    temporary_name = f".{path.name}.{hashlib.sha256(payload).hexdigest()[:16]}.tmp"
    descriptor = -1
    try:
        try:
            existing = path.lstat()
        except FileNotFoundError:
            existing = None
        if existing is not None and not stat.S_ISREG(existing.st_mode):
            raise _fail("unsafe_pointer_type")
        try:
            temporary = (path.parent / temporary_name).lstat()
        except FileNotFoundError:
            temporary = None
        if temporary is not None:
            if not stat.S_ISREG(temporary.st_mode):
                raise _fail("unsafe_pointer_temp")
            if _read_bytes_no_follow(path.parent / temporary_name, limit=len(payload)) != payload:
                raise _fail("pointer_temp_conflict")
            (path.parent / temporary_name).unlink()
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            mode,
            dir_fd=parent_descriptor,
        )
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
        os.fchmod(descriptor, mode)
        os.close(descriptor)
        descriptor = -1
        os.replace(
            temporary_name,
            path.name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )
        if replaced_callback is not None:
            replaced_callback()
        os.fsync(parent_descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_descriptor)


def _read_bytes_no_follow(path: Path, *, limit: int) -> bytes:
    metadata = _require_regular_no_follow(path)
    if metadata.st_size > limit:
        raise _fail("file_too_large")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(descriptor)
        output = bytearray()
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, limit + 1 - len(output)))
            if not chunk:
                break
            output.extend(chunk)
            if len(output) > limit:
                raise _fail("file_too_large")
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
            raise _fail("file_changed_while_reading")
        return bytes(output)
    finally:
        os.close(descriptor)


def _sha256_file_no_follow(path: Path, *, maximum: int = MAX_FILE_BYTES) -> tuple[str, int]:
    _require_regular_no_follow(path)
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise _fail("unsafe_file_type") from exc
    try:
        return _sha256_open_descriptor(descriptor, maximum=maximum)
    finally:
        os.close(descriptor)


def _sha256_open_descriptor(descriptor: int, *, maximum: int) -> tuple[str, int]:
    before = os.fstat(descriptor)
    if not stat.S_ISREG(before.st_mode):
        raise _fail("unsafe_file_type")
    if before.st_size > maximum:
        raise _fail("file_too_large")
    digest = hashlib.sha256()
    total = 0
    while True:
        chunk = os.read(descriptor, min(1024 * 1024, maximum + 1 - total))
        if not chunk:
            break
        total += len(chunk)
        if total > maximum:
            raise _fail("file_too_large")
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
        raise _fail("file_changed_while_reading")
    return digest.hexdigest(), total


def build_version_descriptor_from_directory(
    *,
    paper_id: int,
    source_sha256: str,
    article_fingerprint: str,
    extractor_version: str,
    render_version: str,
    created_at: str,
    source_root: Path,
    files: Sequence[str],
    selector_version: int = SELECTOR_VERSION,
) -> dict[str, Any]:
    """Hash bounded regular files without loading their payloads into memory."""

    root = _absolute_source_root(source_root)
    relative_files = _canonical_source_files(files)
    metadata: dict[str, dict[str, Any]] = {}
    total_size = 0
    for relative in relative_files:
        descriptor = _open_relative_regular(root, relative)
        try:
            digest, size = _sha256_open_descriptor(
                descriptor,
                maximum=MAX_FILE_BYTES,
            )
        finally:
            os.close(descriptor)
        total_size += size
        if total_size > MAX_TOTAL_BYTES:
            raise _fail("files_total_size_exceeded")
        metadata[relative] = {"sha256": digest, "size": size}
    return _build_version_descriptor_from_metadata(
        paper_id=paper_id,
        source_sha256=source_sha256,
        article_fingerprint=article_fingerprint,
        extractor_version=extractor_version,
        render_version=render_version,
        created_at=created_at,
        metadata=metadata,
        selector_version=selector_version,
    )


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _fail("json_duplicate_key")
        result[key] = value
    return result


def _load_json_file(path: Path, *, limit: int = MAX_MANIFEST_BYTES) -> Any:
    payload = _read_bytes_no_follow(path, limit=limit)
    try:
        return json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_float=lambda _value: (_ for _ in ()).throw(_fail("json_float_invalid")),
            parse_constant=lambda _value: (_ for _ in ()).throw(_fail("json_constant_invalid")),
        )
    except ArtifactVersionError:
        raise
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise _fail("json_invalid") from exc


def _store_marker(paper_id: int) -> dict[str, Any]:
    return {
        "schema": STORE_SCHEMA,
        "schema_version": 1,
        "paper_id": paper_id,
    }


def _owner_marker(descriptor: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": OWNER_SCHEMA,
        "schema_version": 1,
        "paper_id": descriptor["paper_id"],
        "version_id": descriptor["version_id"],
        "manifest_sha256": descriptor_sha256(descriptor),
    }


def _pointer(descriptor: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": POINTER_SCHEMA,
        "schema_version": 1,
        "paper_id": descriptor["paper_id"],
        "version_id": descriptor["version_id"],
        "manifest_sha256": descriptor_sha256(descriptor),
    }


def _validate_store_marker(payload: Any, paper_id: int) -> dict[str, Any]:
    marker = _object(payload, field="store_marker", required=_STORE_REQUIRED)
    schema_version = marker["schema_version"]
    if (
        marker["schema"] != STORE_SCHEMA
        or isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != 1
        or _positive_paper_id(marker["paper_id"]) != paper_id
    ):
        raise _fail("artifact_store_marker_invalid")
    return _store_marker(paper_id)


def _validate_pointer_like(
    payload: Any,
    *,
    paper_id: int,
    schema: str,
    field: str,
) -> dict[str, Any]:
    pointer = _object(payload, field=field, required=_POINTER_REQUIRED)
    schema_version = pointer["schema_version"]
    if (
        pointer["schema"] != schema
        or isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != 1
    ):
        raise _fail(f"{field}_schema_invalid")
    if _positive_paper_id(pointer["paper_id"]) != paper_id:
        raise _fail(f"{field}_paper_id_mismatch")
    version_id = pointer["version_id"]
    if not isinstance(version_id, str) or not _VERSION_ID_RE.fullmatch(version_id):
        raise _fail(f"{field}_version_id_invalid")
    return {
        "schema": schema,
        "schema_version": 1,
        "paper_id": paper_id,
        "version_id": version_id,
        "manifest_sha256": _sha256(
            pointer["manifest_sha256"], field=f"{field}_manifest_sha256"
        ),
    }


def _initialize_store(paths: ArtifactStorePaths) -> None:
    try:
        data_metadata = paths.data_dir.lstat()
    except OSError as exc:
        raise _fail("data_dir_invalid") from exc
    if not stat.S_ISDIR(data_metadata.st_mode):
        raise _fail("data_dir_invalid")
    _ensure_directory_chain(
        paths.data_dir,
        ["library", "mobile", str(paths.paper_id), STORE_DIRECTORY_NAME],
    )
    marker_path = paths.store_root / STORE_MARKER_NAME
    if not marker_path.exists():
        entries = list(paths.store_root.iterdir())
        if entries:
            raise _fail("artifact_store_unowned")
        _write_exclusive(marker_path, _canonical_json_bytes(_store_marker(paths.paper_id)))
    _validate_store_marker(_load_json_file(marker_path), paths.paper_id)
    _ensure_directory_chain(paths.store_root, ["versions"])
    _ensure_directory_chain(paths.store_root, ["staging"])
    lock_path = paths.store_root / LOCK_NAME
    if not lock_path.exists():
        _write_exclusive(lock_path, b"paper-reader-artifact-operations-v1\n", 0o600)
    _require_regular_no_follow(lock_path)


@contextmanager
def _store_lock(paths: ArtifactStorePaths) -> Iterator[None]:
    _initialize_store(paths)
    lock_path = paths.store_root / LOCK_NAME
    descriptor = os.open(
        lock_path,
        os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _open_raw_source(paths: ArtifactStorePaths, relative: Any) -> int:
    safe = _safe_raw_relative_path(relative)
    parts = ("library", "raw", *safe.parts)
    try:
        descriptor = _open_directory(paths.data_dir)
    except ArtifactVersionError as exc:
        raise _fail("raw_source_unsafe") from exc
    try:
        for part in parts[:-1]:
            try:
                child = os.open(
                    part,
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=descriptor,
                )
            except FileNotFoundError as exc:
                raise _fail("raw_source_missing") from exc
            except OSError as exc:
                raise _fail("raw_source_unsafe") from exc
            os.close(descriptor)
            descriptor = child
        try:
            source = os.open(
                parts[-1],
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=descriptor,
            )
        except FileNotFoundError as exc:
            raise _fail("raw_source_missing") from exc
        except OSError as exc:
            raise _fail("raw_source_unsafe") from exc
        if not stat.S_ISREG(os.fstat(source).st_mode):
            os.close(source)
            raise _fail("raw_source_unsafe")
        return source
    finally:
        os.close(descriptor)


def verify_raw_source_sha256(
    data_dir: Path,
    paper_id: int,
    raw_source_relative: str,
    expected_sha256: str,
) -> str:
    paths = artifact_store_paths(data_dir, paper_id)
    expected = _sha256(expected_sha256, field="source_sha256")
    descriptor = _open_raw_source(paths, raw_source_relative)
    try:
        actual, _size = _sha256_open_descriptor(
            descriptor, maximum=MAX_RAW_SOURCE_BYTES
        )
    finally:
        os.close(descriptor)
    if actual != expected:
        raise _fail("raw_source_sha256_mismatch")
    return actual


def copy_raw_source_to_file(
    data_dir: Path,
    paper_id: int,
    raw_source_relative: str,
    expected_sha256: str,
    destination: Path,
) -> int:
    """Stream one exact raw PDF into a new private projection file."""

    paths = artifact_store_paths(data_dir, paper_id)
    expected = _sha256(expected_sha256, field="source_sha256")
    target = Path(destination)
    if not target.is_absolute():
        raise _fail("destination_must_be_absolute")
    source = _open_raw_source(paths, raw_source_relative)
    parent = -1
    output = -1
    created = False
    try:
        before = os.fstat(source)
        if (
            not stat.S_ISREG(before.st_mode)
            or not 1 <= before.st_size <= MAX_RAW_SOURCE_BYTES
        ):
            raise _fail("raw_source_unsafe")
        parent = _open_directory(target.parent)
        try:
            output = os.open(
                target.name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0),
                0o400,
                dir_fd=parent,
            )
            created = True
        except FileExistsError as exc:
            raise _fail("file_already_exists") from exc
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(source, min(1024 * 1024, MAX_RAW_SOURCE_BYTES + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_RAW_SOURCE_BYTES:
                raise _fail("raw_source_too_large")
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(output, view)
                if written <= 0:
                    raise _fail("raw_source_copy_failed")
                view = view[written:]
        after = os.fstat(source)
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
            raise _fail("raw_source_changed_while_copying")
        if total != before.st_size or digest.hexdigest() != expected:
            raise _fail("raw_source_sha256_mismatch")
        os.fchmod(output, 0o400)
        os.fsync(output)
        os.fsync(parent)
        return total
    except Exception:
        if created and parent >= 0:
            try:
                os.unlink(target.name, dir_fd=parent)
                os.fsync(parent)
            except OSError:
                pass
        raise
    finally:
        if output >= 0:
            os.close(output)
        if parent >= 0:
            os.close(parent)
        os.close(source)


def _ensure_parent_directories(root: Path, relative_path: str) -> None:
    parts = PurePosixPath(relative_path).parts[:-1]
    if parts:
        _ensure_directory_chain(root, parts)


def copy_artifact_files_from_directory(
    source_root: Path,
    destination_root: Path,
    files: Mapping[str, Mapping[str, Any]],
) -> tuple[str, ...]:
    """Stream a bounded, checksum-bound file set into one empty directory."""

    source = _absolute_source_root(source_root)
    destination = _absolute_source_root(destination_root)
    canonical = _canonical_file_metadata(files)
    descriptor = _open_directory(destination)
    try:
        if os.listdir(descriptor):
            raise _fail("destination_not_empty")
    finally:
        os.close(descriptor)
    for relative, metadata in canonical.items():
        _ensure_parent_directories(destination, relative)
        _copy_relative_exclusive(
            source,
            relative,
            destination / relative,
            metadata,
        )
    _fsync_tree(destination)
    return tuple(canonical)


def _walk_version_tree(root: Path) -> tuple[set[str], set[str]]:
    metadata = root.lstat()
    if not stat.S_ISDIR(metadata.st_mode):
        raise _fail("version_directory_unsafe")
    files: set[str] = set()
    directories: set[str] = set()
    for current, directory_names, file_names in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in list(directory_names):
            path = current_path / name
            item = path.lstat()
            if not stat.S_ISDIR(item.st_mode):
                raise _fail("version_tree_unsafe")
            directories.add(path.relative_to(root).as_posix())
        for name in file_names:
            path = current_path / name
            item = path.lstat()
            if not stat.S_ISREG(item.st_mode):
                raise _fail("version_tree_unsafe")
            files.add(path.relative_to(root).as_posix())
    return files, directories


def verify_version_directory(
    root: Path,
    *,
    expected_descriptor: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Prove ownership, exact file set, and every immutable checksum."""

    files, directories = _walk_version_tree(root)
    if MANIFEST_NAME not in files or OWNER_MARKER_NAME not in files:
        raise _fail("version_ownership_missing")
    descriptor = validate_version_descriptor(_load_json_file(root / MANIFEST_NAME))
    if expected_descriptor is not None:
        expected = validate_version_descriptor(expected_descriptor)
        if canonical_descriptor_bytes(descriptor) != canonical_descriptor_bytes(expected):
            raise _fail("version_descriptor_mismatch")
    owner = _validate_pointer_like(
        _load_json_file(root / OWNER_MARKER_NAME),
        paper_id=descriptor["paper_id"],
        schema=OWNER_SCHEMA,
        field="owner_marker",
    )
    if (
        owner["version_id"] != descriptor["version_id"]
        or owner["manifest_sha256"] != descriptor_sha256(descriptor)
    ):
        raise _fail("version_ownership_mismatch")
    expected_files = set(descriptor["files"]) | set(_RESERVED_VERSION_FILES)
    if files != expected_files:
        raise _fail("version_file_set_mismatch")
    expected_directories = {
        PurePosixPath(path).parent.as_posix()
        for path in descriptor["files"]
        if PurePosixPath(path).parent.as_posix() != "."
    }
    expanded_directories: set[str] = set()
    for directory in expected_directories:
        parent = PurePosixPath(directory)
        while parent.as_posix() != ".":
            expanded_directories.add(parent.as_posix())
            parent = parent.parent
    if directories != expanded_directories:
        raise _fail("version_directory_set_mismatch")
    for relative, expected_metadata in descriptor["files"].items():
        actual_sha, actual_size = _sha256_file_no_follow(root / relative)
        if (
            actual_sha != expected_metadata["sha256"]
            or actual_size != expected_metadata["size"]
        ):
            raise _fail("version_file_checksum_mismatch")
    return descriptor


def _fsync_tree(root: Path) -> None:
    files, directories = _walk_version_tree(root)
    for relative in sorted(files):
        descriptor = os.open(
            root / relative,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    for relative in sorted(directories, key=lambda value: value.count("/"), reverse=True):
        descriptor = _open_directory(root / relative)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    descriptor = _open_directory(root)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _make_version_read_only(root: Path, *, root_mode: int = 0o550) -> None:
    files, directories = _walk_version_tree(root)
    for relative in files:
        os.chmod(root / relative, 0o440, follow_symlinks=False)
    for relative in sorted(directories, key=lambda value: value.count("/"), reverse=True):
        os.chmod(root / relative, 0o550, follow_symlinks=False)
    os.chmod(root, root_mode, follow_symlinks=False)


def _inject(injector: Callable[[str], None] | None, point: str) -> None:
    if injector is not None:
        if not callable(injector):
            raise _fail("failure_injector_invalid")
        injector(point)


@dataclass(frozen=True)
class StagedArtifactVersion:
    data_dir: Path
    paper_id: int
    version_id: str
    path: Path
    descriptor: Mapping[str, Any]
    raw_source_relative: str


def _create_staged_version(
    data_dir: Path,
    canonical: Mapping[str, Any],
    *,
    raw_source_relative: str,
    write_file: Callable[[Path, str, Mapping[str, Any]], None],
    failure_injector: Callable[[str], None] | None,
) -> StagedArtifactVersion:
    paths = artifact_store_paths(data_dir, canonical["paper_id"])
    verify_raw_source_sha256(
        data_dir,
        canonical["paper_id"],
        raw_source_relative,
        canonical["source_sha256"],
    )
    with _store_lock(paths):
        stage = paths.staging_root / canonical["version_id"]
        try:
            os.mkdir(stage, 0o750)
        except FileExistsError as exc:
            raise _fail("staging_version_exists") from exc
        try:
            _inject(failure_injector, "after_staging_directory_created")
            for relative in sorted(canonical["files"]):
                _ensure_parent_directories(stage, relative)
                write_file(stage, relative, canonical["files"][relative])
                _inject(failure_injector, "after_staging_file_written")
            _write_exclusive(stage / MANIFEST_NAME, canonical_descriptor_bytes(canonical))
            _write_exclusive(
                stage / OWNER_MARKER_NAME,
                _canonical_json_bytes(_owner_marker(canonical)),
            )
            verified = verify_version_directory(stage, expected_descriptor=canonical)
            _inject(failure_injector, "after_staging_verified")
            verify_raw_source_sha256(
                data_dir,
                canonical["paper_id"],
                raw_source_relative,
                canonical["source_sha256"],
            )
            _fsync_tree(stage)
            # Keep the staging root renameable on platforms that require write
            # access to the moved directory itself. Its contents are already
            # read-only and activation verifies the complete tree again.
            _make_version_read_only(stage, root_mode=0o750)
        except Exception:
            if stage.exists():
                _remove_tree(stage, allow_read_only=True)
            raise
    return StagedArtifactVersion(
        paths.data_dir,
        canonical["paper_id"],
        canonical["version_id"],
        stage,
        _freeze_descriptor(canonical),
        safe_relative_artifact_path(raw_source_relative),
    )


def create_staged_version(
    data_dir: Path,
    descriptor: Mapping[str, Any],
    files: Mapping[str, bytes],
    *,
    raw_source_relative: str,
    failure_injector: Callable[[str], None] | None = None,
) -> StagedArtifactVersion:
    """Create a stage from small in-memory payloads (compatibility API)."""

    canonical = validate_version_descriptor(descriptor)
    if not isinstance(files, Mapping) or set(files) != set(canonical["files"]):
        raise _fail("staging_file_set_mismatch")
    for path, payload in files.items():
        safe_relative_artifact_path(path)
        if not isinstance(payload, bytes):
            raise _fail("file_payload_must_be_bytes")
        expected = canonical["files"][path]
        if (
            len(payload) != expected["size"]
            or hashlib.sha256(payload).hexdigest() != expected["sha256"]
        ):
            raise _fail("staging_payload_checksum_mismatch")

    def write_file(
        stage: Path,
        relative: str,
        _expected: Mapping[str, Any],
    ) -> None:
        _write_exclusive(stage / relative, files[relative])

    return _create_staged_version(
        data_dir,
        canonical,
        raw_source_relative=raw_source_relative,
        write_file=write_file,
        failure_injector=failure_injector,
    )


def create_staged_version_from_directory(
    data_dir: Path,
    descriptor: Mapping[str, Any],
    *,
    source_root: Path,
    files: Sequence[str],
    raw_source_relative: str,
    failure_injector: Callable[[str], None] | None = None,
) -> StagedArtifactVersion:
    """Stream exact descriptor files into a verified non-visible stage."""

    canonical = validate_version_descriptor(descriptor)
    root = _absolute_source_root(source_root)
    relative_files = _canonical_source_files(files)
    if set(relative_files) != set(canonical["files"]):
        raise _fail("staging_file_set_mismatch")

    def write_file(
        stage: Path,
        relative: str,
        expected: Mapping[str, Any],
    ) -> None:
        _copy_relative_exclusive(
            root,
            relative,
            stage / relative,
            expected,
        )

    return _create_staged_version(
        data_dir,
        canonical,
        raw_source_relative=raw_source_relative,
        write_file=write_file,
        failure_injector=failure_injector,
    )


def _load_current_pointer(paths: ArtifactStorePaths) -> dict[str, Any] | None:
    try:
        paths.current_pointer.lstat()
    except FileNotFoundError:
        return None
    pointer = _validate_pointer_like(
        _load_json_file(paths.current_pointer),
        paper_id=paths.paper_id,
        schema=POINTER_SCHEMA,
        field="current_pointer",
    )
    target = version_directory(paths, pointer["version_id"])
    descriptor = verify_version_directory(target)
    if pointer["manifest_sha256"] != descriptor_sha256(descriptor):
        raise _fail("current_pointer_manifest_mismatch")
    return pointer


def _restore_pointer(paths: ArtifactStorePaths, previous: bytes | None) -> None:
    if previous is None:
        try:
            metadata = paths.current_pointer.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISREG(metadata.st_mode):
            raise _fail("unsafe_pointer_type")
        paths.current_pointer.unlink()
        descriptor = _open_directory(paths.store_root)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    else:
        _atomic_replace_file(paths.current_pointer, previous)


def activate_staged_version(
    staged: StagedArtifactVersion,
    *,
    failure_injector: Callable[[str], None] | None = None,
    expected_current_version_id: Any = _EXPECTED_CURRENT_UNSET,
) -> dict[str, Any]:
    """Install a verified version and atomically switch the current pointer."""

    if not isinstance(staged, StagedArtifactVersion):
        raise _fail("staged_version_invalid")
    if expected_current_version_id is not _EXPECTED_CURRENT_UNSET and (
        expected_current_version_id is not None
        and (
            not isinstance(expected_current_version_id, str)
            or not _VERSION_ID_RE.fullmatch(expected_current_version_id)
        )
    ):
        raise _fail("expected_current_version_id_invalid")
    paths = artifact_store_paths(staged.data_dir, staged.paper_id)
    expected_stage = paths.staging_root / staged.version_id
    if staged.path != expected_stage:
        raise _fail("staged_path_invalid")
    with _store_lock(paths):
        canonical = verify_version_directory(
            expected_stage, expected_descriptor=staged.descriptor
        )
        verify_raw_source_sha256(
            paths.data_dir,
            staged.paper_id,
            staged.raw_source_relative,
            canonical["source_sha256"],
        )
        previous_pointer = _load_current_pointer(paths)
        if (
            expected_current_version_id is not _EXPECTED_CURRENT_UNSET
            and (
                previous_pointer["version_id"]
                if previous_pointer is not None
                else None
            )
            != expected_current_version_id
        ):
            raise _fail("activation_current_version_mismatch")
        previous_bytes = (
            _canonical_json_bytes(previous_pointer) if previous_pointer is not None else None
        )
        final = version_directory(paths, staged.version_id)
        pointer_replaced = [False]

        def mark_pointer_replaced() -> None:
            pointer_replaced[0] = True
            _inject(
                failure_injector,
                "after_pointer_file_replace_before_fsync",
            )

        try:
            _inject(failure_injector, "before_version_install")
            try:
                final.lstat()
                final_exists = True
            except FileNotFoundError:
                final_exists = False
            if final_exists:
                verify_version_directory(final, expected_descriptor=canonical)
                _remove_tree(expected_stage, allow_read_only=True)
            else:
                os.rename(expected_stage, final)
                _make_version_read_only(final)
                _fsync_tree(final)
                versions_descriptor = _open_directory(paths.versions_root)
                try:
                    os.fsync(versions_descriptor)
                finally:
                    os.close(versions_descriptor)
            _inject(failure_injector, "after_version_install")
            verify_version_directory(final, expected_descriptor=canonical)
            verify_raw_source_sha256(
                paths.data_dir,
                staged.paper_id,
                staged.raw_source_relative,
                canonical["source_sha256"],
            )
            _inject(failure_injector, "before_pointer_swap")
            _atomic_replace_file(
                paths.current_pointer,
                _canonical_json_bytes(_pointer(canonical)),
                replaced_callback=mark_pointer_replaced,
            )
            _inject(failure_injector, "after_pointer_swap")
            verify_raw_source_sha256(
                paths.data_dir,
                staged.paper_id,
                staged.raw_source_relative,
                canonical["source_sha256"],
            )
            return canonical
        except BaseException as exc:
            if pointer_replaced[0]:
                try:
                    _restore_pointer(paths, previous_bytes)
                except Exception as restore_exc:
                    raise _fail("activation_pointer_restore_failed") from restore_exc
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            if isinstance(exc, ArtifactVersionError):
                raise
            raise _fail("artifact_activation_failed") from exc


def publish_version(
    data_dir: Path,
    descriptor: Mapping[str, Any],
    files: Mapping[str, bytes],
    *,
    raw_source_relative: str,
    failure_injector: Callable[[str], None] | None = None,
    expected_current_version_id: Any = _EXPECTED_CURRENT_UNSET,
) -> dict[str, Any]:
    staged = create_staged_version(
        data_dir,
        descriptor,
        files,
        raw_source_relative=raw_source_relative,
        failure_injector=failure_injector,
    )
    return activate_staged_version(
        staged,
        failure_injector=failure_injector,
        expected_current_version_id=expected_current_version_id,
    )


@dataclass(frozen=True)
class ArtifactResolution:
    mode: str
    paper_id: int
    root: Path
    version_id: str | None
    descriptor: Mapping[str, Any] | None


def _verify_legacy_root(paths: ArtifactStorePaths) -> Path:
    current = paths.data_dir
    for part in ("library", "mobile", str(paths.paper_id), "v2"):
        current = current / part
        metadata = current.lstat()
        if not stat.S_ISDIR(metadata.st_mode):
            raise _fail("legacy_v2_unsafe")
    article = current / "article.json"
    _require_regular_no_follow(article)
    return current


def resolve_current_artifact(
    data_dir: Path,
    paper_id: int,
    *,
    allow_legacy_v2: bool = True,
) -> ArtifactResolution:
    """Resolve current version, falling back only when no new pointer exists."""

    if not isinstance(allow_legacy_v2, bool):
        raise _fail("allow_legacy_v2_invalid")
    paths = artifact_store_paths(data_dir, paper_id)
    data_descriptor = _open_directory(paths.data_dir)
    os.close(data_descriptor)
    try:
        store_metadata = paths.store_root.lstat()
    except FileNotFoundError:
        store_metadata = None
    if store_metadata is not None:
        if not stat.S_ISDIR(store_metadata.st_mode):
            raise _fail("artifact_store_unsafe")
        _validate_store_marker(
            _load_json_file(paths.store_root / STORE_MARKER_NAME), paths.paper_id
        )
        pointer = _load_current_pointer(paths)
        if pointer is not None:
            root = version_directory(paths, pointer["version_id"])
            descriptor = verify_version_directory(root)
            return ArtifactResolution(
                "versioned",
                paths.paper_id,
                root,
                pointer["version_id"],
                _freeze_descriptor(descriptor),
            )
    if not allow_legacy_v2:
        raise _fail("current_artifact_missing")
    try:
        legacy = _verify_legacy_root(paths)
    except FileNotFoundError as exc:
        raise _fail("current_artifact_missing") from exc
    except ArtifactVersionError:
        raise
    return ArtifactResolution("legacy_v2", paths.paper_id, legacy, None, None)


def rollback_current_version(
    data_dir: Path,
    paper_id: int,
    target_version_id: str,
    *,
    raw_source_relative: str,
    dry_run: bool = True,
    failure_injector: Callable[[str], None] | None = None,
    expected_current_version_id: Any = _EXPECTED_CURRENT_UNSET,
) -> dict[str, Any]:
    """Atomically point to a verified prior version, restoring on any failure."""

    if not isinstance(dry_run, bool):
        raise _fail("dry_run_invalid")
    if expected_current_version_id is not _EXPECTED_CURRENT_UNSET and (
        not isinstance(expected_current_version_id, str)
        or not _VERSION_ID_RE.fullmatch(expected_current_version_id)
    ):
        raise _fail("expected_current_version_id_invalid")
    paths = artifact_store_paths(data_dir, paper_id)
    target_path = version_directory(paths, target_version_id)
    with _store_lock(paths):
        current = _load_current_pointer(paths)
        if current is None:
            raise _fail("rollback_requires_versioned_current")
        if (
            expected_current_version_id is not _EXPECTED_CURRENT_UNSET
            and current["version_id"] != expected_current_version_id
        ):
            raise _fail("rollback_current_version_mismatch")
        target = verify_version_directory(target_path)
        if target["paper_id"] != paths.paper_id:
            raise _fail("rollback_paper_id_mismatch")
        verify_raw_source_sha256(
            data_dir,
            paper_id,
            raw_source_relative,
            target["source_sha256"],
        )
        result = {
            "schema_version": 1,
            "paper_id": paper_id,
            "from_version_id": current["version_id"],
            "to_version_id": target_version_id,
            "dry_run": dry_run,
            "changed": current["version_id"] != target_version_id,
        }
        if dry_run or not result["changed"]:
            return result
        previous_bytes = _canonical_json_bytes(current)
        pointer_replaced = [False]

        def mark_pointer_replaced() -> None:
            pointer_replaced[0] = True
            _inject(
                failure_injector,
                "after_rollback_pointer_file_replace_before_fsync",
            )

        try:
            _inject(failure_injector, "before_rollback_pointer_swap")
            _atomic_replace_file(
                paths.current_pointer,
                _canonical_json_bytes(_pointer(target)),
                replaced_callback=mark_pointer_replaced,
            )
            _inject(failure_injector, "after_rollback_pointer_swap")
            verify_version_directory(target_path, expected_descriptor=target)
            verify_raw_source_sha256(
                data_dir,
                paper_id,
                raw_source_relative,
                target["source_sha256"],
            )
            return result
        except BaseException as exc:
            if pointer_replaced[0]:
                try:
                    _restore_pointer(paths, previous_bytes)
                except Exception as restore_exc:
                    raise _fail("rollback_pointer_restore_failed") from restore_exc
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            if isinstance(exc, ArtifactVersionError):
                raise
            raise _fail("artifact_rollback_failed") from exc


def _remove_tree(root: Path, *, allow_read_only: bool) -> None:
    files, directories = _walk_version_tree(root)
    if allow_read_only:
        for relative in directories:
            os.chmod(root / relative, 0o700, follow_symlinks=False)
        os.chmod(root, 0o700, follow_symlinks=False)
    for relative in sorted(files):
        (root / relative).unlink()
    for relative in sorted(
        directories, key=lambda value: (value.count("/"), value), reverse=True
    ):
        (root / relative).rmdir()
    root.rmdir()


def plan_retention(
    data_dir: Path,
    paper_id: int,
    *,
    keep: int = 3,
) -> dict[str, Any]:
    if isinstance(keep, bool) or not isinstance(keep, int) or not 1 <= keep <= 100:
        raise _fail("retention_keep_invalid")
    paths = artifact_store_paths(data_dir, paper_id)
    with _store_lock(paths):
        current = _load_current_pointer(paths)
        active = current["version_id"] if current is not None else None
        verified: list[tuple[str, str]] = []
        skipped_unowned: list[str] = []
        for entry in sorted(paths.versions_root.iterdir(), key=lambda item: item.name):
            try:
                metadata = entry.lstat()
            except OSError as exc:
                raise _fail("retention_entry_unreadable") from exc
            if not stat.S_ISDIR(metadata.st_mode) or not _VERSION_ID_RE.fullmatch(entry.name):
                skipped_unowned.append(entry.name)
                continue
            marker = entry / OWNER_MARKER_NAME
            try:
                marker_metadata = marker.lstat()
            except FileNotFoundError:
                skipped_unowned.append(entry.name)
                continue
            if not stat.S_ISREG(marker_metadata.st_mode):
                skipped_unowned.append(entry.name)
                continue
            descriptor = verify_version_directory(entry)
            if descriptor["version_id"] != entry.name:
                raise _fail("retention_version_id_mismatch")
            verified.append((descriptor["created_at"], entry.name))
        ordered = [item[1] for item in sorted(verified, reverse=True)]
        retained = set(ordered[:keep])
        if active is not None:
            retained.add(active)
        delete = [version_id for version_id in ordered if version_id not in retained]
        return {
            "schema_version": 1,
            "paper_id": paper_id,
            "active_version_id": active,
            "keep": keep,
            "retain_version_ids": sorted(retained),
            "delete_version_ids": delete,
            "skipped_unowned": sorted(skipped_unowned),
        }


def apply_retention(
    data_dir: Path,
    paper_id: int,
    *,
    keep: int = 3,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Delete only checksum-verified owned versions, never the active version."""

    if not isinstance(dry_run, bool):
        raise _fail("dry_run_invalid")
    plan = plan_retention(data_dir, paper_id, keep=keep)
    plan = dict(plan, dry_run=dry_run, deleted_version_ids=[])
    if dry_run:
        return plan
    paths = artifact_store_paths(data_dir, paper_id)
    deleted: list[str] = []
    with _store_lock(paths):
        current = _load_current_pointer(paths)
        active = current["version_id"] if current is not None else None
        for version_id in plan["delete_version_ids"]:
            if version_id == active:
                raise _fail("active_version_delete_forbidden")
            root = version_directory(paths, version_id)
            descriptor = verify_version_directory(root)
            if descriptor["version_id"] != version_id:
                raise _fail("retention_version_id_mismatch")
            _remove_tree(root, allow_read_only=True)
            deleted.append(version_id)
        versions_descriptor = _open_directory(paths.versions_root)
        try:
            os.fsync(versions_descriptor)
        finally:
            os.close(versions_descriptor)
    plan["deleted_version_ids"] = deleted
    return plan

"""Create and prove a private archive of durable Paper OS filesystem state."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tarfile
from typing import Any, Iterator, Mapping, Sequence


ARCHIVE_SCHEMA = "paper-os-durable-data"
ARCHIVE_SCHEMA_VERSION = 1
DURABLE_ROOTS = ("library/mobile", "library/raw")
MAX_MANIFEST_BYTES = 64 * 1024 * 1024
MAX_FILES = 1_000_000
MAX_TOTAL_BYTES = 1024 * 1024 * 1024 * 1024
MAX_PATH_BYTES = 1024
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class DataArchiveError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _safe_relative(value: str) -> str:
    try:
        path = PurePosixPath(value)
    except (TypeError, ValueError) as exc:
        raise DataArchiveError("data_archive_path_invalid") from exc
    if (
        not value
        or path.is_absolute()
        or str(path) != value
        or any(part in {"", ".", ".."} for part in path.parts)
        or len(value.encode("utf-8")) > MAX_PATH_BYTES
    ):
        raise DataArchiveError("data_archive_path_invalid")
    return value


def _private_root(path: Path, *, must_be_empty: bool = False) -> Path:
    if not path.is_absolute():
        raise DataArchiveError("data_archive_root_invalid")
    try:
        details = path.lstat()
    except OSError as exc:
        raise DataArchiveError("data_archive_root_invalid") from exc
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
        raise DataArchiveError("data_archive_root_invalid")
    if must_be_empty:
        try:
            if next(path.iterdir(), None) is not None:
                raise DataArchiveError("data_archive_restore_not_empty")
        except OSError as exc:
            raise DataArchiveError("data_archive_root_invalid") from exc
    return path


def _walk_durable(data_root: Path) -> tuple[tuple[str, ...], tuple[tuple[str, Path], ...]]:
    directories: list[str] = []
    files: list[tuple[str, Path]] = []
    for relative_root in DURABLE_ROOTS:
        source_root = data_root / relative_root
        try:
            details = source_root.lstat()
        except OSError as exc:
            raise DataArchiveError("data_archive_durable_root_missing") from exc
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
            raise DataArchiveError("data_archive_entry_unsafe")
        for current, directory_names, file_names in os.walk(
            source_root, topdown=True, followlinks=False
        ):
            current_path = Path(current)
            relative_current = current_path.relative_to(data_root).as_posix()
            directories.append(_safe_relative(relative_current))
            directory_names.sort()
            file_names.sort()
            for name in directory_names:
                child = current_path / name
                child_details = child.lstat()
                if stat.S_ISLNK(child_details.st_mode) or not stat.S_ISDIR(
                    child_details.st_mode
                ):
                    raise DataArchiveError("data_archive_entry_unsafe")
            for name in file_names:
                child = current_path / name
                child_details = child.lstat()
                if stat.S_ISLNK(child_details.st_mode) or not stat.S_ISREG(
                    child_details.st_mode
                ):
                    raise DataArchiveError("data_archive_entry_unsafe")
                relative = _safe_relative(child.relative_to(data_root).as_posix())
                files.append((relative, child))
                if len(files) > MAX_FILES:
                    raise DataArchiveError("data_archive_file_limit")
    return tuple(sorted(set(directories))), tuple(sorted(files))


def _open_regular(path: Path) -> tuple[int, os.stat_result]:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_nlink < 1:
            raise DataArchiveError("data_archive_entry_unsafe")
        return descriptor, details
    except DataArchiveError:
        if "descriptor" in locals():
            os.close(descriptor)
        raise
    except OSError as exc:
        raise DataArchiveError("data_archive_entry_unsafe") from exc


def _unchanged(before: os.stat_result, after: os.stat_result) -> bool:
    return (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) == (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )


def _hash_descriptor(descriptor: int, before: os.stat_result) -> str:
    digest = hashlib.sha256()
    total = 0
    while chunk := os.read(descriptor, 1024 * 1024):
        total += len(chunk)
        if total > MAX_TOTAL_BYTES:
            raise DataArchiveError("data_archive_size_limit")
        digest.update(chunk)
    after = os.fstat(descriptor)
    if total != before.st_size or not _unchanged(before, after):
        raise DataArchiveError("data_archive_source_changed")
    os.lseek(descriptor, 0, os.SEEK_SET)
    return digest.hexdigest()


def _tar_info(name: str, *, size: int, directory: bool) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name + ("/" if directory else ""))
    info.type = tarfile.DIRTYPE if directory else tarfile.REGTYPE
    info.size = 0 if directory else size
    info.mode = 0o700 if directory else 0o600
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = 0
    return info


def _open_exclusive(path: Path) -> Any:
    descriptor = os.open(
        path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    return os.fdopen(descriptor, "wb")


def create_archive(data_root: Path, archive_path: Path, manifest_path: Path) -> dict[str, Any]:
    root = _private_root(data_root)
    directories, files = _walk_durable(root)
    entries: dict[str, dict[str, Any]] = {}
    total = 0
    with _open_exclusive(archive_path) as raw_archive:
        with tarfile.open(fileobj=raw_archive, mode="w|") as archive:
            for relative in directories:
                archive.addfile(_tar_info(relative, size=0, directory=True))
            for relative, path in files:
                descriptor, before = _open_regular(path)
                try:
                    total += before.st_size
                    if total > MAX_TOTAL_BYTES:
                        raise DataArchiveError("data_archive_size_limit")
                    digest = _hash_descriptor(descriptor, before)
                    with os.fdopen(descriptor, "rb", closefd=False) as source:
                        archive.addfile(
                            _tar_info(relative, size=before.st_size, directory=False),
                            source,
                        )
                    if not _unchanged(before, os.fstat(descriptor)):
                        raise DataArchiveError("data_archive_source_changed")
                    entries[relative] = {
                        "sha256": digest,
                        "size": before.st_size,
                    }
                finally:
                    os.close(descriptor)
        raw_archive.flush()
        os.fsync(raw_archive.fileno())
    manifest = {
        "schema": ARCHIVE_SCHEMA,
        "schema_version": ARCHIVE_SCHEMA_VERSION,
        "roots": list(DURABLE_ROOTS),
        "directories": list(directories),
        "files": entries,
        "file_count": len(entries),
        "total_bytes": total,
    }
    validate_manifest(manifest)
    encoded = (
        json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    if len(encoded) > MAX_MANIFEST_BYTES:
        raise DataArchiveError("data_archive_manifest_too_large")
    with _open_exclusive(manifest_path) as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    return manifest


def validate_manifest(value: Any) -> dict[str, Any]:
    required = {
        "schema",
        "schema_version",
        "roots",
        "directories",
        "files",
        "file_count",
        "total_bytes",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise DataArchiveError("data_archive_manifest_invalid")
    roots = value["roots"]
    directories = value["directories"]
    files = value["files"]
    if (
        value["schema"] != ARCHIVE_SCHEMA
        or value["schema_version"] != ARCHIVE_SCHEMA_VERSION
        or roots != list(DURABLE_ROOTS)
        or not isinstance(directories, list)
        or not isinstance(files, Mapping)
        or directories != sorted(set(directories))
        or len(files) > MAX_FILES
        or value["file_count"] != len(files)
    ):
        raise DataArchiveError("data_archive_manifest_invalid")
    clean_directories = [_safe_relative(item) for item in directories]
    if not set(DURABLE_ROOTS).issubset(clean_directories):
        raise DataArchiveError("data_archive_manifest_invalid")
    clean_files: dict[str, dict[str, Any]] = {}
    total = 0
    for raw_path, raw_metadata in files.items():
        path = _safe_relative(raw_path)
        if not any(path.startswith(root + "/") for root in DURABLE_ROOTS):
            raise DataArchiveError("data_archive_manifest_invalid")
        if not isinstance(raw_metadata, Mapping) or set(raw_metadata) != {
            "sha256",
            "size",
        }:
            raise DataArchiveError("data_archive_manifest_invalid")
        size = raw_metadata["size"]
        digest = raw_metadata["sha256"]
        if (
            not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(digest, str)
            or not _SHA256_RE.fullmatch(digest)
        ):
            raise DataArchiveError("data_archive_manifest_invalid")
        total += size
        if total > MAX_TOTAL_BYTES:
            raise DataArchiveError("data_archive_manifest_invalid")
        clean_files[path] = {"sha256": digest, "size": size}
    if value["total_bytes"] != total or list(files) != sorted(files):
        raise DataArchiveError("data_archive_manifest_invalid")
    return {
        "schema": ARCHIVE_SCHEMA,
        "schema_version": ARCHIVE_SCHEMA_VERSION,
        "roots": list(DURABLE_ROOTS),
        "directories": clean_directories,
        "files": clean_files,
        "file_count": len(clean_files),
        "total_bytes": total,
    }


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        details = path.lstat()
        if (
            stat.S_ISLNK(details.st_mode)
            or not stat.S_ISREG(details.st_mode)
            or not 1 <= details.st_size <= MAX_MANIFEST_BYTES
        ):
            raise DataArchiveError("data_archive_manifest_invalid")
        return validate_manifest(json.loads(path.read_text(encoding="utf-8")))
    except DataArchiveError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DataArchiveError("data_archive_manifest_invalid") from exc


def _expected_members(manifest: Mapping[str, Any]) -> dict[str, str]:
    expected = {path: "directory" for path in manifest["directories"]}
    expected.update({path: "file" for path in manifest["files"]})
    return expected


def _validate_members(
    archive: tarfile.TarFile, manifest: Mapping[str, Any]
) -> dict[str, tarfile.TarInfo]:
    expected = _expected_members(manifest)
    actual: dict[str, tarfile.TarInfo] = {}
    for member in archive.getmembers():
        name = member.name
        if name in actual or name not in expected:
            raise DataArchiveError("data_archive_member_invalid")
        kind = expected[name]
        if (kind == "directory" and not member.isdir()) or (
            kind == "file" and not member.isreg()
        ):
            raise DataArchiveError("data_archive_member_invalid")
        if member.issym() or member.islnk() or member.size < 0:
            raise DataArchiveError("data_archive_member_invalid")
        if kind == "file" and member.size != manifest["files"][name]["size"]:
            raise DataArchiveError("data_archive_member_invalid")
        actual[name] = member
    if set(actual) != set(expected):
        raise DataArchiveError("data_archive_member_invalid")
    return actual


def verify_and_restore(
    archive_path: Path,
    manifest_path: Path,
    restore_root: Path,
) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    root = _private_root(restore_root, must_be_empty=True)
    try:
        details = archive_path.lstat()
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
            raise DataArchiveError("data_archive_invalid")
        with tarfile.open(archive_path, mode="r:") as archive:
            members = _validate_members(archive, manifest)
            for relative in manifest["directories"]:
                destination = root / relative
                destination.mkdir(mode=0o700, parents=True)
            for relative, expected in manifest["files"].items():
                source = archive.extractfile(members[relative])
                if source is None:
                    raise DataArchiveError("data_archive_member_invalid")
                destination = root / relative
                descriptor = os.open(
                    destination,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
                digest = hashlib.sha256()
                total = 0
                try:
                    with source:
                        while chunk := source.read(1024 * 1024):
                            total += len(chunk)
                            if total > expected["size"]:
                                raise DataArchiveError("data_archive_hash_mismatch")
                            digest.update(chunk)
                            view = memoryview(chunk)
                            while view:
                                written = os.write(descriptor, view)
                                if written <= 0:
                                    raise DataArchiveError("data_archive_write_failed")
                                view = view[written:]
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                if total != expected["size"] or digest.hexdigest() != expected["sha256"]:
                    raise DataArchiveError("data_archive_hash_mismatch")
        restored_directories, restored_files = _walk_durable(root)
        if list(restored_directories) != manifest["directories"]:
            raise DataArchiveError("data_archive_restore_mismatch")
        if [relative for relative, _path in restored_files] != list(manifest["files"]):
            raise DataArchiveError("data_archive_restore_mismatch")
        for relative, path in restored_files:
            descriptor, before = _open_regular(path)
            try:
                digest = _hash_descriptor(descriptor, before)
            finally:
                os.close(descriptor)
            expected = manifest["files"][relative]
            if before.st_size != expected["size"] or digest != expected["sha256"]:
                raise DataArchiveError("data_archive_restore_mismatch")
        return manifest
    except DataArchiveError:
        raise
    except (OSError, tarfile.TarError) as exc:
        raise DataArchiveError("data_archive_invalid") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Paper OS durable data archive")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("--data-root", required=True, type=Path)
    create.add_argument("--archive", required=True, type=Path)
    create.add_argument("--manifest", required=True, type=Path)
    verify = commands.add_parser("verify")
    verify.add_argument("--archive", required=True, type=Path)
    verify.add_argument("--manifest", required=True, type=Path)
    verify.add_argument("--restore-root", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.command == "create":
            create_archive(arguments.data_root, arguments.archive, arguments.manifest)
        else:
            verify_and_restore(
                arguments.archive,
                arguments.manifest,
                arguments.restore_root,
            )
        return 0
    except DataArchiveError as exc:
        print(exc.code, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

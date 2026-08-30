#!/usr/bin/env python3
"""Safe local backup and isolated restore drill for Paper OS PostgreSQL.

The program deliberately uses argv arrays (never a host shell), never reads or
prints database credentials, and only applies retention inside an initialized
backup root carrying the marker defined below.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Sequence

# Direct systemd execution starts with this file's directory on sys.path rather
# than the repository root. Add the exact repository ancestor so the shared
# archive contract can be validated without installing the application package.
_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from paper_service.data_archive import DataArchiveError, load_manifest


POSTGRES_IMAGE = "postgres:17.11-alpine3.24"
ARCHIVE_TOOL_IMAGE = "python:3.13.7-slim-bookworm"
BACKUP_MARKER_NAME = ".paper-os-backup-root"
BACKUP_MARKER_CONTENT = "paper-os-backup-root-v1\n"
BACKUP_ID_PATTERN = re.compile(r"^(\d{8}T\d{6}Z)-([0-9a-f]{8})$")
CONTAINER_NAME_PATTERN = re.compile(
    r"^paper-os-(?:restore-drill|rollback-db)-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$"
)
RESTORE_VOLUME_PATTERN = re.compile(
    r"^paper-os-data-restore-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$"
)
ROLLBACK_DB_VOLUME_PATTERN = re.compile(
    r"^paper-os-db-rollback-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$"
)
ROLLBACK_DATA_VOLUME_PATTERN = re.compile(
    r"^paper-os-data-rollback-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$"
)
CONTAINER_ID_PATTERN = re.compile(r"^[0-9a-f]{12,64}$")
REMOTE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
TABLE_NAME_PATTERN = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
LEGACY_EXPECTED_FILES = frozenset(
    {"paper_os.dump", "backup.json", "SHA256SUMS"}
)
EXPECTED_FILES = frozenset(
    {
        "paper_os.dump",
        "paper_data.tar",
        "paper_data.json",
        "backup.json",
        "SHA256SUMS",
    }
)
LEGACY_PAYLOAD_FILES = ("paper_os.dump", "backup.json")
PAYLOAD_FILES = (
    "paper_os.dump",
    "paper_data.tar",
    "paper_data.json",
    "backup.json",
)
QUIESCE_SERVICES = (
    "paper-api",
    "paper-knowledge-worker",
    "paper-ops-monitor",
    "paper-parser",
    "paper-pmc-broker",
    "paper-qa-worker",
    "paper-reader-v1-worker",
    "paper-reader-v2-converter",
    "paper-reader-v2-worker",
    "paper-search-index-worker",
    "paper-taxonomy-worker",
    "paper-worker",
)
# `schema_migrations` is present after Paper OS 0.6, but the first verified
# backup may intentionally be taken from the legacy schema immediately before
# that migration.  The exact table inventory is still captured and compared at
# restore time; only the two tables required to identify a Paper OS database
# must exist in every supported generation.
CORE_TABLES = ("papers", "sync_jobs")

TABLE_QUERY = """SELECT table_name
FROM information_schema.tables
WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
ORDER BY table_name"""
COUNT_QUERY = """SELECT
  (SELECT count(*) FROM papers)::text || '|' ||
  (SELECT count(*) FROM sync_jobs)::text"""
INTEGRITY_QUERY = """SELECT
  (SELECT count(*) FROM papers
   WHERE sha256 !~ '^[0-9a-f]{64}$')::text || '|' ||
  (SELECT count(*) FROM (
     SELECT sha256 FROM papers GROUP BY sha256 HAVING count(*) > 1
   ) duplicate_hashes)::text"""
CONSTRAINT_QUERY = """SELECT count(*)
FROM pg_constraint
WHERE connamespace = 'public'::regnamespace AND NOT convalidated"""
PAPER_SHA_QUERY = "SELECT sha256 FROM papers ORDER BY sha256"


class OpsError(RuntimeError):
    """An intentionally sanitized operational failure."""


@dataclass(frozen=True)
class ServicePaths:
    service_root: Path
    compose_file: Path
    env_file: Path
    backup_root: Path


@dataclass(frozen=True)
class DatabaseSnapshot:
    tables: tuple[str, ...]
    paper_count: int
    sync_job_count: int
    invalid_paper_hash_count: int
    duplicate_paper_hash_count: int
    row_counts: tuple[tuple[str, int], ...] = ()
    paper_sha256_set_digest: str | None = None

    def to_json(self, created_at: datetime) -> dict[str, object]:
        return {
            "format_version": 1,
            "created_at": created_at.astimezone(timezone.utc).isoformat(),
            "postgres_image": POSTGRES_IMAGE,
            "expected_tables": list(self.tables),
            "counts": {
                "papers": self.paper_count,
                "sync_jobs": self.sync_job_count,
            },
            "integrity": {
                "invalid_paper_hashes": self.invalid_paper_hash_count,
                "duplicate_paper_hashes": self.duplicate_paper_hash_count,
            },
        }

    def to_v2_json(
        self,
        created_at: datetime,
        *,
        quiesced_services: Sequence[str],
    ) -> dict[str, object]:
        if (
            not self.row_counts
            or self.paper_sha256_set_digest is None
            or not re.fullmatch(r"[0-9a-f]{64}", self.paper_sha256_set_digest)
            or tuple(name for name, _count in self.row_counts) != self.tables
            or any(type(count) is not int or count < 0 for _name, count in self.row_counts)
            or dict(self.row_counts).get("papers") != self.paper_count
            or dict(self.row_counts).get("sync_jobs") != self.sync_job_count
        ):
            raise OpsError("database snapshot is incomplete")
        return {
            "format_version": 2,
            "created_at": created_at.astimezone(timezone.utc).isoformat(),
            "postgres_image": POSTGRES_IMAGE,
            "archive_tool_image": ARCHIVE_TOOL_IMAGE,
            "expected_tables": list(self.tables),
            "counts": dict(self.row_counts),
            "integrity": {
                "invalid_paper_hashes": self.invalid_paper_hash_count,
                "duplicate_paper_hashes": self.duplicate_paper_hash_count,
                "paper_sha256_set_digest": self.paper_sha256_set_digest,
            },
            "durable_data": {
                "archive": "paper_data.tar",
                "manifest": "paper_data.json",
                "roots": ["library/mobile", "library/raw"],
            },
            "consistency": {
                "mode": "quiesced",
                "services": list(quiesced_services),
            },
        }


@dataclass(frozen=True)
class BackupResult:
    backup_id: str
    backup_dir: Path
    removed_backup_ids: tuple[str, ...]
    uploaded: bool
    dry_run: bool


@dataclass(frozen=True)
class MaterializedRestoreResult:
    database_volume: str
    data_volume: str
    dry_run: bool


class CommandRunner:
    """Small subprocess boundary that never exposes captured output on error."""

    def run(
        self,
        command: Sequence[str],
        *,
        stage: str,
        stdin_path: Path | None = None,
        stdout_path: Path | None = None,
        capture: bool = False,
        check: bool = True,
    ) -> subprocess.CompletedProcess[bytes]:
        stdin_handle = None
        stdout_handle = None
        try:
            if stdin_path is not None:
                stdin_handle = stdin_path.open("rb")
            if stdout_path is not None:
                descriptor = os.open(
                    stdout_path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
                stdout_handle = os.fdopen(descriptor, "wb")
            completed = subprocess.run(
                list(command),
                stdin=stdin_handle,
                stdout=(
                    subprocess.PIPE
                    if capture
                    else stdout_handle
                    if stdout_handle is not None
                    else subprocess.DEVNULL
                ),
                stderr=subprocess.PIPE,
                check=False,
                timeout=3600,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise OpsError(f"{stage} could not run") from exc
        finally:
            if stdin_handle is not None:
                stdin_handle.close()
            if stdout_handle is not None:
                stdout_handle.close()
        if check and completed.returncode != 0:
            raise OpsError(f"{stage} failed with exit code {completed.returncode}")
        return completed


def _absolute_path(value: str, label: str, *, must_exist: bool) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise OpsError(f"{label} must be an absolute path")
    try:
        return path.resolve(strict=must_exist)
    except FileNotFoundError as exc:
        raise OpsError(f"{label} does not exist") from exc


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def validate_service_paths(
    service_root: str,
    compose_file: str,
    env_file: str,
    backup_root: str,
) -> ServicePaths:
    raw_backup_root = Path(backup_root).expanduser()
    if raw_backup_root.is_symlink():
        raise OpsError("backup root must be a real directory, not a link")
    root = _absolute_path(service_root, "service root", must_exist=True)
    compose = _absolute_path(compose_file, "compose file", must_exist=True)
    environment = _absolute_path(env_file, "environment file", must_exist=True)
    backups = _absolute_path(backup_root, "backup root", must_exist=False)

    if not root.is_dir():
        raise OpsError("service root must be a directory")
    for path, label in ((compose, "compose file"), (environment, "environment file")):
        if not path.is_file():
            raise OpsError(f"{label} must be a regular file")
        if not _is_within(path, root):
            raise OpsError(f"{label} must be inside the service root")
    if stat.S_IMODE(environment.stat().st_mode) & 0o077:
        raise OpsError("environment file permissions must be 600 or stricter")

    # Reject broad roots and any path that could encompass the service itself.
    if backups == Path("/") or len(backups.parts) < 4:
        raise OpsError("backup root is too broad")
    if backups == root or _is_within(root, backups):
        raise OpsError("backup root must not contain the service root")
    if backups.exists() and (backups.is_symlink() or not backups.is_dir()):
        raise OpsError("backup root must be a real directory, not a link")
    if "," in str(backups):
        raise OpsError("backup root cannot contain a comma")
    return ServicePaths(root, compose, environment, backups)


def compose_command(paths: ServicePaths, *arguments: str) -> list[str]:
    return [
        "docker",
        "compose",
        "--project-directory",
        # Preserve the live stack's `papers` project identity and the Compose
        # file's ../.. path base while still requiring a separately validated
        # service root.
        str(paths.compose_file.parent),
        "--env-file",
        str(paths.env_file),
        "--file",
        str(paths.compose_file),
        *arguments,
    ]


def pg_dump_command(paths: ServicePaths) -> list[str]:
    return compose_command(
        paths,
        "exec",
        "-T",
        "postgres",
        "pg_dump",
        "--format=custom",
        "--no-owner",
        "--no-privileges",
        "--username=paper_os",
        "--dbname=paper_os",
    )


def pg_restore_list_command(paths: ServicePaths) -> list[str]:
    return compose_command(paths, "exec", "-T", "postgres", "pg_restore", "--list")


def psql_compose_command(paths: ServicePaths, query: str) -> list[str]:
    return compose_command(
        paths,
        "exec",
        "-T",
        "postgres",
        "psql",
        "--no-psqlrc",
        "--tuples-only",
        "--no-align",
        "--set=ON_ERROR_STOP=1",
        "--username=paper_os",
        "--dbname=paper_os",
        "--command",
        query,
    )


def _captured_text(
    runner: CommandRunner,
    command: Sequence[str],
    *,
    stage: str,
    maximum: int = 1_000_000,
) -> str:
    completed = runner.run(command, stage=stage, capture=True)
    output = completed.stdout or b""
    if len(output) > maximum:
        raise OpsError(f"{stage} returned too much data")
    try:
        return output.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise OpsError(f"{stage} returned invalid text") from exc


def _parse_nonnegative_pair(value: str, label: str) -> tuple[int, int]:
    fields = value.split("|")
    if len(fields) != 2 or any(not field.isdigit() for field in fields):
        raise OpsError(f"{label} returned an invalid result")
    return int(fields[0]), int(fields[1])


def collect_database_snapshot(
    runner: CommandRunner, paths: ServicePaths
) -> DatabaseSnapshot:
    raw_tables = _captured_text(
        runner,
        psql_compose_command(paths, TABLE_QUERY),
        stage="table inventory",
    )
    tables = tuple(line for line in raw_tables.splitlines() if line)
    if not tables or any(not TABLE_NAME_PATTERN.fullmatch(name) for name in tables):
        raise OpsError("table inventory returned invalid names")
    if not set(CORE_TABLES).issubset(tables):
        raise OpsError("database is missing required core tables")

    row_counts: list[tuple[str, int]] = []
    for table in tables:
        raw_count = _captured_text(
            runner,
            psql_compose_command(paths, f'SELECT count(*) FROM "{table}"'),
            stage=f"row count inventory for {table}",
        )
        if not raw_count.isdigit():
            raise OpsError("row count inventory returned an invalid result")
        row_counts.append((table, int(raw_count)))
    counts = dict(row_counts)
    paper_count = counts["papers"]
    sync_job_count = counts["sync_jobs"]
    invalid_hashes, duplicate_hashes = _parse_nonnegative_pair(
        _captured_text(
            runner,
            psql_compose_command(paths, INTEGRITY_QUERY),
            stage="source integrity check",
        ),
        "source integrity check",
    )
    if invalid_hashes or duplicate_hashes:
        raise OpsError("source database integrity check failed")
    raw_hashes = _captured_text(
        runner,
        psql_compose_command(paths, PAPER_SHA_QUERY),
        stage="paper source hash inventory",
        maximum=100_000_000,
    )
    hashes = tuple(line for line in raw_hashes.splitlines() if line)
    if (
        len(hashes) != paper_count
        or tuple(sorted(hashes)) != hashes
        or len(set(hashes)) != len(hashes)
        or any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in hashes)
    ):
        raise OpsError("paper source hash inventory returned an invalid result")
    digest = hashlib.sha256()
    for value in hashes:
        digest.update(value.encode("ascii"))
        digest.update(b"\n")
    return DatabaseSnapshot(
        tables=tables,
        paper_count=paper_count,
        sync_job_count=sync_job_count,
        invalid_paper_hash_count=invalid_hashes,
        duplicate_paper_hash_count=duplicate_hashes,
        row_counts=tuple(row_counts),
        paper_sha256_set_digest=digest.hexdigest(),
    )


def backup_root_is_initialized(root: Path) -> bool:
    """Validate a root without changing it; return whether its marker exists."""
    if not root.exists():
        return False
    if root.is_symlink() or not root.is_dir():
        raise OpsError("backup root must be a real directory")
    if stat.S_IMODE(root.stat().st_mode) & 0o077:
        raise OpsError("backup root permissions must be 700 or stricter")
    marker = root / BACKUP_MARKER_NAME
    if marker.exists():
        if marker.is_symlink() or not marker.is_file():
            raise OpsError("backup root marker is invalid")
        if marker.read_text(encoding="utf-8") != BACKUP_MARKER_CONTENT:
            raise OpsError("backup root marker does not match Paper OS")
        if stat.S_IMODE(marker.stat().st_mode) & 0o077:
            raise OpsError("backup root marker permissions must be 600 or stricter")
        return True
    if any(root.iterdir()):
        raise OpsError("an unmarked non-empty backup root cannot be initialized")
    return False


def initialize_backup_root(root: Path) -> None:
    initialized = backup_root_is_initialized(root)
    if initialized:
        return
    if not root.exists():
        root.mkdir(parents=True, mode=0o700)
    marker = root / BACKUP_MARKER_NAME
    descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(BACKUP_MARKER_CONTENT)
        handle.flush()
        os.fsync(handle.fileno())


def require_initialized_backup_root(root: Path) -> None:
    if root.is_symlink() or not root.is_dir():
        raise OpsError("backup root is not an initialized directory")
    if stat.S_IMODE(root.stat().st_mode) & 0o077:
        raise OpsError("backup root permissions must be 700 or stricter")
    marker = root / BACKUP_MARKER_NAME
    if (
        marker.is_symlink()
        or not marker.is_file()
        or marker.read_text(encoding="utf-8") != BACKUP_MARKER_CONTENT
    ):
        raise OpsError("backup root marker is missing or invalid")
    if stat.S_IMODE(marker.stat().st_mode) & 0o077:
        raise OpsError("backup root marker permissions must be 600 or stricter")


def backup_id_for(created_at: datetime, random_hex: str | None = None) -> str:
    stamp = created_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    suffix = secrets.token_hex(4) if random_hex is None else random_hex
    if not re.fullmatch(r"[0-9a-f]{8}", suffix):
        raise OpsError("backup identifier suffix is invalid")
    return f"{stamp}-{suffix}"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_private_json(path: Path, value: dict[str, object]) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(value, handle, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def write_manifest(directory: Path) -> Path:
    manifest = directory / "SHA256SUMS"
    if all((directory / name).is_file() for name in PAYLOAD_FILES):
        payloads = PAYLOAD_FILES
    elif all((directory / name).is_file() for name in LEGACY_PAYLOAD_FILES):
        payloads = LEGACY_PAYLOAD_FILES
    else:
        raise OpsError("backup payload is incomplete")
    lines = [
        f"{_sha256(directory / name)}  {name}\n"
        for name in payloads
    ]
    descriptor = os.open(manifest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="ascii") as handle:
        handle.writelines(lines)
        handle.flush()
        os.fsync(handle.fileno())
    return manifest


def fsync_file_and_directory(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def verify_backup_directory(directory: Path, backup_root: Path) -> dict[str, object]:
    require_initialized_backup_root(backup_root)
    if directory.is_symlink() or not directory.is_dir():
        raise OpsError("backup directory must be a real directory")
    if directory.parent.resolve() != backup_root.resolve():
        raise OpsError("backup directory must be an immediate child of the backup root")
    if not BACKUP_ID_PATTERN.fullmatch(directory.name):
        raise OpsError("backup directory name is invalid")
    names = {entry.name for entry in directory.iterdir()}
    if names == EXPECTED_FILES:
        expected_files = EXPECTED_FILES
        payload_files = PAYLOAD_FILES
    elif names == LEGACY_EXPECTED_FILES:
        expected_files = LEGACY_EXPECTED_FILES
        payload_files = LEGACY_PAYLOAD_FILES
    else:
        raise OpsError("backup directory contents are incomplete or unexpected")
    if any(entry.is_symlink() for entry in directory.iterdir()):
        raise OpsError("backup directory contents are incomplete or unexpected")
    for name in expected_files:
        path = directory / name
        if not path.is_file() or stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise OpsError("backup files must be regular files with mode 600 or stricter")

    manifest_path = directory / "SHA256SUMS"
    if manifest_path.stat().st_size > 4096:
        raise OpsError("backup checksum manifest is unexpectedly large")
    try:
        expected_manifest_lines = manifest_path.read_text(encoding="ascii").splitlines()
    except UnicodeDecodeError as exc:
        raise OpsError("backup checksum manifest is invalid") from exc
    parsed: dict[str, str] = {}
    for line in expected_manifest_lines:
        match = re.fullmatch(
            r"([0-9a-f]{64})  (paper_os\.dump|paper_data\.tar|paper_data\.json|backup\.json)",
            line,
        )
        if not match or match.group(2) in parsed:
            raise OpsError("backup checksum manifest is invalid")
        parsed[match.group(2)] = match.group(1)
    if set(parsed) != set(payload_files):
        raise OpsError("backup checksum manifest is incomplete")
    for name, expected_hash in parsed.items():
        if _sha256(directory / name) != expected_hash:
            raise OpsError("backup checksum verification failed")

    metadata_path = directory / "backup.json"
    if metadata_path.stat().st_size > 65_536:
        raise OpsError("backup metadata is unexpectedly large")
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OpsError("backup metadata is invalid") from exc
    validate_backup_metadata(metadata)
    if metadata.get("format_version") == 2:
        try:
            data_manifest = load_manifest(directory / "paper_data.json")
        except DataArchiveError as exc:
            raise OpsError("durable data manifest is invalid") from exc
        if data_manifest["roots"] != metadata["durable_data"]["roots"]:
            raise OpsError("durable data manifest does not match backup metadata")
    return metadata


def validate_backup_metadata(metadata: object) -> None:
    if not isinstance(metadata, dict) or metadata.get("format_version") not in {1, 2}:
        raise OpsError("backup metadata version is invalid")
    format_version = metadata["format_version"]
    expected_top_level = (
        {
            "format_version",
            "created_at",
            "postgres_image",
            "expected_tables",
            "counts",
            "integrity",
        }
        if format_version == 1
        else {
            "format_version",
            "created_at",
            "postgres_image",
            "archive_tool_image",
            "expected_tables",
            "counts",
            "integrity",
            "durable_data",
            "consistency",
        }
    )
    if set(metadata) != expected_top_level:
        raise OpsError("backup metadata shape is invalid")
    if metadata.get("postgres_image") != POSTGRES_IMAGE:
        raise OpsError("backup metadata PostgreSQL image is unexpected")
    tables = metadata.get("expected_tables")
    counts = metadata.get("counts")
    integrity = metadata.get("integrity")
    created_at = metadata.get("created_at")
    if (
        not isinstance(tables, list)
        or not tables
        or any(not isinstance(name, str) or not TABLE_NAME_PATTERN.fullmatch(name) for name in tables)
        or not set(CORE_TABLES).issubset(tables)
        or not isinstance(counts, dict)
        or not isinstance(integrity, dict)
    ):
        raise OpsError("backup metadata shape is invalid")
    if tables != sorted(set(tables)):
        raise OpsError("backup metadata table inventory is not canonical")
    if not isinstance(created_at, str):
        raise OpsError("backup metadata timestamp is invalid")
    try:
        parsed_created_at = datetime.fromisoformat(created_at)
    except ValueError as exc:
        raise OpsError("backup metadata timestamp is invalid") from exc
    if parsed_created_at.tzinfo is None:
        raise OpsError("backup metadata timestamp must include a timezone")
    count_names_valid = (
        set(counts) == set(tables)
        if format_version == 2
        else {"papers", "sync_jobs"}.issubset(counts)
    )
    if not count_names_valid:
        raise OpsError("backup metadata counts are invalid")
    if any(
        not isinstance(name, str)
        or not TABLE_NAME_PATTERN.fullmatch(name)
        or type(value) is not int
        or value < 0
        for name, value in counts.items()
    ):
        raise OpsError("backup metadata counts are invalid")
    for field in ("invalid_paper_hashes", "duplicate_paper_hashes"):
        if type(integrity.get(field)) is not int or integrity[field] < 0:
            raise OpsError("backup metadata counts are invalid")
    if integrity["invalid_paper_hashes"] or integrity["duplicate_paper_hashes"]:
        raise OpsError("backup metadata records a failed integrity check")
    if format_version == 1:
        if set(integrity) != {
            "invalid_paper_hashes",
            "duplicate_paper_hashes",
        }:
            raise OpsError("backup metadata shape is invalid")
        return
    digest = integrity.get("paper_sha256_set_digest")
    durable = metadata.get("durable_data")
    consistency = metadata.get("consistency")
    archive_tool_image = metadata.get("archive_tool_image")
    services = consistency.get("services") if isinstance(consistency, dict) else None
    if (
        not isinstance(digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", digest)
        or archive_tool_image != ARCHIVE_TOOL_IMAGE
        or set(integrity)
        != {
            "invalid_paper_hashes",
            "duplicate_paper_hashes",
            "paper_sha256_set_digest",
        }
        or durable
        != {
            "archive": "paper_data.tar",
            "manifest": "paper_data.json",
            "roots": ["library/mobile", "library/raw"],
        }
        or not isinstance(consistency, dict)
        or set(consistency) != {"mode", "services"}
        or consistency.get("mode") != "quiesced"
        or not isinstance(services, list)
        or any(not isinstance(service, str) for service in services)
        or services != sorted(set(services))
        or any(service not in QUIESCE_SERVICES for service in services)
    ):
        raise OpsError("backup metadata shape is invalid")


def _backup_timestamp(backup_id: str) -> datetime:
    match = BACKUP_ID_PATTERN.fullmatch(backup_id)
    if not match:
        raise OpsError("backup identifier is invalid")
    return datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(
        tzinfo=timezone.utc
    )


def retention_candidates(
    backup_root: Path, *, now: datetime, retention_days: int
) -> tuple[Path, ...]:
    if retention_days < 1:
        raise OpsError("retention days must be at least 1")
    require_initialized_backup_root(backup_root)
    cutoff = now.astimezone(timezone.utc) - timedelta(days=retention_days)
    candidates: list[Path] = []
    for entry in backup_root.iterdir():
        if entry.name == BACKUP_MARKER_NAME or not BACKUP_ID_PATTERN.fullmatch(entry.name):
            continue
        if entry.is_symlink() or not entry.is_dir():
            continue
        if _backup_timestamp(entry.name) >= cutoff:
            continue
        try:
            verify_backup_directory(entry, backup_root)
        except OpsError:
            # Corrupt or surprising backups require operator review, never deletion.
            continue
        candidates.append(entry)
    return tuple(sorted(candidates, key=lambda path: path.name))


def remove_retention_candidates(candidates: Iterable[Path], backup_root: Path) -> tuple[str, ...]:
    require_initialized_backup_root(backup_root)
    removed: list[str] = []
    for candidate in candidates:
        metadata = verify_backup_directory(candidate, backup_root)
        expected_files = (
            EXPECTED_FILES
            if metadata.get("format_version") == 2
            else LEGACY_EXPECTED_FILES
        )
        # The directory was validated as a direct, non-link child with an exact
        # file set. Delete individual files, then the empty directory.
        for name in sorted(expected_files):
            (candidate / name).unlink()
        candidate.rmdir()
        removed.append(candidate.name)
    return tuple(removed)


def parse_crypt_remote(remote: str) -> tuple[str, str]:
    name, separator, remote_path = remote.partition(":")
    if not separator or not REMOTE_NAME_PATTERN.fullmatch(name):
        raise OpsError("rclone crypt remote must use the form name:non-empty/path")
    if remote_path.startswith("/") or not remote_path.strip("/"):
        raise OpsError("rclone crypt remote must use the form name:non-empty/path")
    components = [component for component in remote_path.strip("/").split("/") if component]
    if not components or any(component in {".", ".."} for component in components):
        raise OpsError("rclone crypt remote path must be specific and traversal-free")
    if any("\x00" in component for component in components):
        raise OpsError("rclone crypt remote path is invalid")
    return name, "/".join(components)


def validate_rclone_config(path_value: str) -> Path:
    raw_path = Path(path_value).expanduser()
    if raw_path.is_symlink():
        raise OpsError("rclone config must be a real file")
    path = _absolute_path(path_value, "rclone config", must_exist=True)
    if path.is_symlink() or not path.is_file():
        raise OpsError("rclone config must be a real file")
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise OpsError("rclone config permissions must be 600 or stricter")
    return path


def verify_crypt_remote(
    runner: CommandRunner, *, config: Path, remote_name: str
) -> None:
    output = _captured_text(
        runner,
        [
            "rclone",
            "--ask-password=false",
            "--config",
            str(config),
            "config",
            "redacted",
            remote_name,
        ],
        stage="rclone crypt remote validation",
    )
    if not re.search(r"(?m)^type\s*=\s*crypt\s*$", output):
        raise OpsError("configured rclone remote is not a crypt remote")


def upload_backup(
    runner: CommandRunner,
    backup_dir: Path,
    *,
    config: Path,
    remote: str,
) -> None:
    metadata = verify_backup_directory(backup_dir, backup_dir.parent)
    payloads = (
        PAYLOAD_FILES
        if metadata.get("format_version") == 2
        else LEGACY_PAYLOAD_FILES
    )
    remote_name, remote_path = parse_crypt_remote(remote)
    verify_crypt_remote(runner, config=config, remote_name=remote_name)
    destination = f"{remote_name}:{remote_path}/{backup_dir.name}"
    # Upload the manifest last so its presence denotes a complete offsite copy.
    for name in (*payloads, "SHA256SUMS"):
        runner.run(
            [
                "rclone",
                "--ask-password=false",
                "--config",
                str(config),
                "copyto",
                "--checksum",
                "--immutable",
                str(backup_dir / name),
                f"{destination}/{name}",
            ],
            stage=f"offsite upload of {name}",
        )


def _cleanup_incomplete(directory: Path) -> None:
    if not directory.exists() or directory.is_symlink() or not directory.is_dir():
        return
    for entry in directory.iterdir():
        if (
            entry.name in EXPECTED_FILES | LEGACY_EXPECTED_FILES
            or entry.name.startswith(".paper_os.dump.partial-")
            or entry.name.startswith(".paper_data.tar.partial-")
            or entry.name.startswith(".paper_data.json.partial-")
        ):
            if entry.is_file() and not entry.is_symlink():
                entry.unlink()
    try:
        directory.rmdir()
    except OSError:
        pass


def running_services(runner: CommandRunner, paths: ServicePaths) -> tuple[str, ...]:
    raw = _captured_text(
        runner,
        compose_command(
            paths,
            "ps",
            "--services",
            "--filter",
            "status=running",
        ),
        stage="running service inventory",
    )
    services = tuple(line for line in raw.splitlines() if line)
    if (
        len(set(services)) != len(services)
        or any(not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,99}", item) for item in services)
    ):
        raise OpsError("running service inventory returned invalid names")
    return tuple(sorted(services))


def quiesce_application_services(
    runner: CommandRunner, paths: ServicePaths
) -> tuple[str, ...]:
    running = set(running_services(runner, paths))
    selected = tuple(service for service in QUIESCE_SERVICES if service in running)
    try:
        if selected:
            runner.run(
                compose_command(
                    paths,
                    "stop",
                    "--timeout",
                    "120",
                    *selected,
                ),
                stage="application write quiescence",
            )
        remaining = set(running_services(runner, paths)) & set(QUIESCE_SERVICES)
        if remaining:
            raise OpsError("application services did not quiesce")
    except Exception:
        if selected:
            runner.run(
                compose_command(paths, "start", *selected),
                stage="application service resume after quiescence failure",
                check=False,
            )
        raise
    return selected


def resume_application_services(
    runner: CommandRunner,
    paths: ServicePaths,
    services: Sequence[str],
) -> None:
    if not services:
        return
    if tuple(services) != tuple(
        service for service in QUIESCE_SERVICES if service in set(services)
    ):
        raise OpsError("resume service inventory is invalid")
    runner.run(
        compose_command(paths, "start", *services),
        stage="application service resume",
    )
    running = set(running_services(runner, paths))
    if not set(services).issubset(running):
        raise OpsError("application services did not resume")


def artifact_runtime(
    runner: CommandRunner, paths: ServicePaths
) -> str:
    container_id = _captured_text(
        runner,
        compose_command(paths, "ps", "--all", "--quiet", "paper-api"),
        stage="paper API container lookup",
    )
    if not CONTAINER_ID_PATTERN.fullmatch(container_id):
        raise OpsError("paper API container lookup returned an invalid identifier")
    return container_id


def create_durable_data_archive(
    runner: CommandRunner,
    *,
    service_root: Path,
    container_id: str,
    backup_dir: Path,
    archive_path: Path,
    manifest_path: Path,
) -> None:
    if (
        not CONTAINER_ID_PATTERN.fullmatch(container_id)
        or archive_path.parent != backup_dir
        or manifest_path.parent != backup_dir
    ):
        raise OpsError("durable data archive boundary is invalid")
    tool_root = service_root / "paper_service"
    if tool_root.is_symlink() or not tool_root.is_dir():
        raise OpsError("durable data archive tool directory is invalid")
    runner.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--user",
            "1000:1000",
            "--volumes-from",
            f"{container_id}:ro",
            "--mount",
            f"type=bind,src={backup_dir},dst=/backup",
            "--mount",
            f"type=bind,src={tool_root},dst=/tool/paper_service,readonly",
            "--workdir",
            "/tool",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=64m",
            ARCHIVE_TOOL_IMAGE,
            "python",
            "-m",
            "paper_service.data_archive",
            "create",
            "--data-root",
            "/data",
            "--archive",
            f"/backup/{archive_path.name}",
            "--manifest",
            f"/backup/{manifest_path.name}",
        ],
        stage="durable paper data archive",
    )


def prove_durable_data_restore(
    runner: CommandRunner,
    *,
    service_root: Path,
    backup_dir: Path,
    volume_name: str,
    preserve_on_success: bool = False,
) -> None:
    if (
        not RESTORE_VOLUME_PATTERN.fullmatch(volume_name)
        and not ROLLBACK_DATA_VOLUME_PATTERN.fullmatch(volume_name)
    ) or type(preserve_on_success) is not bool:
        raise OpsError("durable data restore boundary is invalid")
    tool_root = service_root / "paper_service"
    if tool_root.is_symlink() or not tool_root.is_dir():
        raise OpsError("durable data archive tool directory is invalid")
    created = False
    succeeded = False
    try:
        runner.run(
            ["docker", "volume", "create", volume_name],
            stage="durable data restore volume create",
        )
        created = True
        runner.run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--read-only",
                "--user",
                "0:0",
                "--mount",
                f"type=volume,src={volume_name},dst=/restore",
                ARCHIVE_TOOL_IMAGE,
                "python",
                "-c",
                "import os; os.chown('/restore', 1000, 1000); os.chmod('/restore', 0o700)",
            ],
            stage="durable data restore volume initialization",
        )
        runner.run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--read-only",
                "--user",
                "1000:1000",
                "--mount",
                f"type=bind,src={backup_dir},dst=/backup,readonly",
                "--mount",
                f"type=volume,src={volume_name},dst=/restore",
                "--mount",
                f"type=bind,src={tool_root},dst=/tool/paper_service,readonly",
                "--workdir",
                "/tool",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,size=64m",
                ARCHIVE_TOOL_IMAGE,
                "python",
                "-m",
                "paper_service.data_archive",
                "verify",
                "--archive",
                "/backup/paper_data.tar",
                "--manifest",
                "/backup/paper_data.json",
                "--restore-root",
                "/restore",
            ],
            stage="isolated durable data restore",
        )
        succeeded = True
    finally:
        if created and (not preserve_on_success or not succeeded):
            runner.run(
                ["docker", "volume", "rm", "--force", volume_name],
                stage="durable data restore volume cleanup",
                check=False,
            )


def create_backup(
    paths: ServicePaths,
    *,
    retention_days: int,
    offsite: bool,
    rclone_config: Path | None,
    rclone_remote: str | None,
    dry_run: bool,
    runner: CommandRunner | None = None,
    now: datetime | None = None,
    random_hex: str | None = None,
) -> BackupResult:
    runner = CommandRunner() if runner is None else runner
    created_at = datetime.now(timezone.utc) if now is None else now.astimezone(timezone.utc)
    backup_id = backup_id_for(created_at, random_hex)
    backup_dir = paths.backup_root / backup_id
    if retention_days < 1:
        raise OpsError("retention days must be at least 1")
    if offsite and (rclone_config is None or not rclone_remote):
        raise OpsError("offsite upload requires an explicit config and crypt remote")
    if rclone_remote:
        parse_crypt_remote(rclone_remote)

    if dry_run:
        backup_root_is_initialized(paths.backup_root)
        return BackupResult(backup_id, backup_dir, (), offsite, True)

    initialize_backup_root(paths.backup_root)
    if backup_dir.exists():
        raise OpsError("generated backup directory already exists")
    backup_dir.mkdir(mode=0o700)
    partial_dump = backup_dir / f".paper_os.dump.partial-{os.getpid()}"
    partial_data_archive = backup_dir / f".paper_data.tar.partial-{os.getpid()}"
    partial_data_manifest = backup_dir / f".paper_data.json.partial-{os.getpid()}"
    final_dump = backup_dir / "paper_os.dump"
    final_data_archive = backup_dir / "paper_data.tar"
    final_data_manifest = backup_dir / "paper_data.json"
    local_complete = False
    try:
        api_container_id = artifact_runtime(runner, paths)
        stopped_services = quiesce_application_services(runner, paths)
        try:
            before = collect_database_snapshot(runner, paths)
            runner.run(
                pg_dump_command(paths),
                stage="PostgreSQL custom-format backup",
                stdout_path=partial_dump,
            )
            os.chmod(partial_dump, 0o600)
            runner.run(
                pg_restore_list_command(paths),
                stage="PostgreSQL archive verification",
                stdin_path=partial_dump,
            )
            create_durable_data_archive(
                runner,
                service_root=paths.service_root,
                container_id=api_container_id,
                backup_dir=backup_dir,
                archive_path=partial_data_archive,
                manifest_path=partial_data_manifest,
            )
            for path in (partial_data_archive, partial_data_manifest):
                if path.is_symlink() or not path.is_file():
                    raise OpsError("durable data archive did not produce expected files")
                os.chmod(path, 0o600)
            after = collect_database_snapshot(runner, paths)
            if before != after:
                raise OpsError(
                    "database inventory changed during backup; retry required"
                )
        finally:
            resume_application_services(runner, paths, stopped_services)
        partial_dump.replace(final_dump)
        partial_data_archive.replace(final_data_archive)
        partial_data_manifest.replace(final_data_manifest)
        os.chmod(final_dump, 0o600)
        os.chmod(final_data_archive, 0o600)
        os.chmod(final_data_manifest, 0o600)
        for path in (final_dump, final_data_archive, final_data_manifest):
            fsync_file_and_directory(path)
        write_private_json(
            backup_dir / "backup.json",
            before.to_v2_json(
                created_at,
                quiesced_services=stopped_services,
            ),
        )
        write_manifest(backup_dir)
        fsync_file_and_directory(backup_dir / "SHA256SUMS")
        verify_backup_directory(backup_dir, paths.backup_root)
        local_complete = True
        if offsite:
            assert rclone_config is not None and rclone_remote is not None
            upload_backup(
                runner,
                backup_dir,
                config=rclone_config,
                remote=rclone_remote,
            )
        candidates = retention_candidates(
            paths.backup_root, now=created_at, retention_days=retention_days
        )
        removed = remove_retention_candidates(candidates, paths.backup_root)
        return BackupResult(backup_id, backup_dir, removed, offsite, False)
    except Exception:
        if not local_complete:
            _cleanup_incomplete(backup_dir)
        raise


def load_snapshot_from_metadata(metadata: dict[str, object]) -> DatabaseSnapshot:
    validate_backup_metadata(metadata)
    counts = metadata["counts"]
    integrity = metadata["integrity"]
    assert isinstance(counts, dict) and isinstance(integrity, dict)
    format_version = int(metadata["format_version"])
    expected_tables = metadata["expected_tables"]
    assert isinstance(expected_tables, list)
    row_counts = (
        tuple((str(name), int(counts[str(name)])) for name in expected_tables)
        if format_version == 2
        else ()
    )
    digest = (
        str(integrity["paper_sha256_set_digest"])
        if format_version == 2
        else None
    )
    return DatabaseSnapshot(
        tables=tuple(expected_tables),
        paper_count=int(counts["papers"]),
        sync_job_count=int(counts["sync_jobs"]),
        invalid_paper_hash_count=int(integrity["invalid_paper_hashes"]),
        duplicate_paper_hash_count=int(integrity["duplicate_paper_hashes"]),
        row_counts=row_counts,
        paper_sha256_set_digest=digest,
    )


def restore_container_name(now: datetime, random_hex: str | None = None) -> str:
    return f"paper-os-restore-drill-{backup_id_for(now, random_hex)}"


def docker_exec_psql(container_name: str, query: str) -> list[str]:
    if not CONTAINER_NAME_PATTERN.fullmatch(container_name):
        raise OpsError("restore container name is invalid")
    return [
        "docker",
        "exec",
        container_name,
        "psql",
        "--no-psqlrc",
        "--tuples-only",
        "--no-align",
        "--set=ON_ERROR_STOP=1",
        "--username=postgres",
        "--dbname=paper_restore",
        "--command",
        query,
    ]


def collect_restored_snapshot(
    runner: CommandRunner,
    container_name: str,
    *,
    extended: bool,
) -> DatabaseSnapshot:
    def query(sql: str, stage: str) -> str:
        return _captured_text(
            runner, docker_exec_psql(container_name, sql), stage=stage
        )

    tables = tuple(line for line in query(TABLE_QUERY, "restored table inventory").splitlines() if line)
    if any(not TABLE_NAME_PATTERN.fullmatch(name) for name in tables):
        raise OpsError("restored table inventory is invalid")
    row_counts: tuple[tuple[str, int], ...] = ()
    digest_value: str | None = None
    if extended:
        collected: list[tuple[str, int]] = []
        for table in tables:
            raw_count = query(
                f'SELECT count(*) FROM "{table}"',
                f"restored row count check for {table}",
            )
            if not raw_count.isdigit():
                raise OpsError("restored row count check is invalid")
            collected.append((table, int(raw_count)))
        row_counts = tuple(collected)
        counts = dict(row_counts)
        paper_count = counts.get("papers", -1)
        sync_job_count = counts.get("sync_jobs", -1)
        raw_hashes = query(PAPER_SHA_QUERY, "restored paper source hash inventory")
        hashes = tuple(line for line in raw_hashes.splitlines() if line)
        if (
            len(hashes) != paper_count
            or tuple(sorted(hashes)) != hashes
            or len(set(hashes)) != len(hashes)
            or any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in hashes)
        ):
            raise OpsError("restored paper source hash inventory is invalid")
        digest = hashlib.sha256()
        for value in hashes:
            digest.update(value.encode("ascii"))
            digest.update(b"\n")
        digest_value = digest.hexdigest()
    else:
        paper_count, sync_job_count = _parse_nonnegative_pair(
            query(COUNT_QUERY, "restored row count check"),
            "restored row count check",
        )
    invalid_hashes, duplicate_hashes = _parse_nonnegative_pair(
        query(INTEGRITY_QUERY, "restored integrity check"),
        "restored integrity check",
    )
    constraint_count = query(CONSTRAINT_QUERY, "restored constraint check")
    if not constraint_count.isdigit() or int(constraint_count) != 0:
        raise OpsError("restored database has unvalidated constraints")
    return DatabaseSnapshot(
        tables=tables,
        paper_count=paper_count,
        sync_job_count=sync_job_count,
        invalid_paper_hash_count=invalid_hashes,
        duplicate_paper_hash_count=duplicate_hashes,
        row_counts=row_counts,
        paper_sha256_set_digest=digest_value,
    )


def restore_drill(
    paths: ServicePaths,
    *,
    backup_dir: Path,
    dry_run: bool,
    runner: CommandRunner | None = None,
    now: datetime | None = None,
    random_hex: str | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    runner = CommandRunner() if runner is None else runner
    metadata = verify_backup_directory(backup_dir, paths.backup_root)
    expected = load_snapshot_from_metadata(metadata)
    created_at = datetime.now(timezone.utc) if now is None else now.astimezone(timezone.utc)
    container_name = restore_container_name(created_at, random_hex)
    if dry_run:
        return container_name

    inspection = runner.run(
        [
            "docker",
            "container",
            "ls",
            "--all",
            "--filter",
            f"name=^/{container_name}$",
            "--format",
            "{{.Names}}",
        ],
        stage="restore container collision check",
        capture=True,
    )
    try:
        existing_name = (inspection.stdout or b"").decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise OpsError("restore container collision check returned invalid text") from exc
    if existing_name:
        raise OpsError("restore container name is already in use")

    created = False
    try:
        runner.run(
            [
                "docker",
                "run",
                "--detach",
                "--name",
                container_name,
                "--network",
                "none",
                "--tmpfs",
                "/var/lib/postgresql/data:rw,nosuid,nodev,size=1g",
                "--env",
                "POSTGRES_HOST_AUTH_METHOD=trust",
                "--env",
                "POSTGRES_DB=paper_restore",
                POSTGRES_IMAGE,
            ],
            stage="isolated PostgreSQL restore container start",
        )
        created = True
        ready = False
        for _attempt in range(60):
            result = runner.run(
                [
                    "docker",
                    "exec",
                    container_name,
                    "pg_isready",
                    "--username=postgres",
                    "--dbname=paper_restore",
                ],
                stage="restore database readiness",
                check=False,
            )
            if result.returncode == 0:
                ready = True
                break
            sleep(1)
        if not ready:
            raise OpsError("isolated restore database did not become ready")

        runner.run(
            [
                "docker",
                "exec",
                "--interactive",
                container_name,
                "pg_restore",
                "--exit-on-error",
                "--no-owner",
                "--no-privileges",
                "--username=postgres",
                "--dbname=paper_restore",
            ],
            stage="isolated PostgreSQL restore",
            stdin_path=backup_dir / "paper_os.dump",
        )
        actual = collect_restored_snapshot(
            runner,
            container_name,
            extended=metadata.get("format_version") == 2,
        )
        if actual != expected:
            raise OpsError("restored tables, counts, or integrity do not match the backup")
        if metadata.get("format_version") == 2:
            prove_durable_data_restore(
                runner,
                service_root=paths.service_root,
                backup_dir=backup_dir,
                volume_name=container_name.replace(
                    "paper-os-restore-drill-", "paper-os-data-restore-", 1
                ),
            )
        return container_name
    finally:
        if created:
            runner.run(
                ["docker", "rm", "--force", container_name],
                stage="restore container cleanup",
                check=False,
            )


def materialize_restore(
    paths: ServicePaths,
    *,
    backup_dir: Path,
    dry_run: bool,
    confirm_create_new_volumes: bool,
    runner: CommandRunner | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> MaterializedRestoreResult:
    """Restore into new named volumes without switching or deleting live state."""

    runner = CommandRunner() if runner is None else runner
    metadata = verify_backup_directory(backup_dir, paths.backup_root)
    if metadata.get("format_version") != 2:
        raise OpsError("materialized restore requires a format-version-2 backup")
    if type(confirm_create_new_volumes) is not bool or (
        not dry_run and not confirm_create_new_volumes
    ):
        raise OpsError("materialized restore requires explicit new-volume confirmation")
    backup_id = backup_dir.name
    database_volume = f"paper-os-db-rollback-{backup_id}"
    data_volume = f"paper-os-data-rollback-{backup_id}"
    container_name = f"paper-os-rollback-db-{backup_id}"
    if (
        not ROLLBACK_DB_VOLUME_PATTERN.fullmatch(database_volume)
        or not ROLLBACK_DATA_VOLUME_PATTERN.fullmatch(data_volume)
        or not CONTAINER_NAME_PATTERN.fullmatch(container_name)
    ):
        raise OpsError("materialized restore identifiers are invalid")
    if dry_run:
        return MaterializedRestoreResult(database_volume, data_volume, True)

    for volume in (database_volume, data_volume):
        inspection = runner.run(
            ["docker", "volume", "inspect", volume],
            stage="rollback volume collision check",
            capture=True,
            check=False,
        )
        if inspection.returncode == 0:
            raise OpsError("rollback volume name is already in use")
        if inspection.returncode != 1:
            raise OpsError("rollback volume collision check failed")

    database_created = False
    container_created = False
    completed = False
    try:
        runner.run(
            ["docker", "volume", "create", database_volume],
            stage="rollback database volume create",
        )
        database_created = True
        runner.run(
            [
                "docker",
                "run",
                "--detach",
                "--name",
                container_name,
                "--network",
                "none",
                "--mount",
                f"type=volume,src={database_volume},dst=/var/lib/postgresql/data",
                "--env",
                "POSTGRES_HOST_AUTH_METHOD=trust",
                "--env",
                "POSTGRES_DB=paper_restore",
                POSTGRES_IMAGE,
            ],
            stage="rollback database container start",
        )
        container_created = True
        ready = False
        for _attempt in range(60):
            result = runner.run(
                [
                    "docker",
                    "exec",
                    container_name,
                    "pg_isready",
                    "--username=postgres",
                    "--dbname=paper_restore",
                ],
                stage="rollback database readiness",
                check=False,
            )
            if result.returncode == 0:
                ready = True
                break
            sleep(1)
        if not ready:
            raise OpsError("rollback database did not become ready")
        runner.run(
            [
                "docker",
                "exec",
                "--interactive",
                container_name,
                "pg_restore",
                "--exit-on-error",
                "--no-owner",
                "--no-privileges",
                "--username=postgres",
                "--dbname=paper_restore",
            ],
            stage="rollback database restore",
            stdin_path=backup_dir / "paper_os.dump",
        )
        expected = load_snapshot_from_metadata(metadata)
        actual = collect_restored_snapshot(
            runner,
            container_name,
            extended=True,
        )
        if actual != expected:
            raise OpsError("rollback database does not match the backup")
        prove_durable_data_restore(
            runner,
            service_root=paths.service_root,
            backup_dir=backup_dir,
            volume_name=data_volume,
            preserve_on_success=True,
        )
        completed = True
        return MaterializedRestoreResult(database_volume, data_volume, False)
    finally:
        if container_created:
            runner.run(
                ["docker", "rm", "--force", container_name],
                stage="rollback database container cleanup",
                check=False,
            )
        if database_created and not completed:
            runner.run(
                ["docker", "volume", "rm", "--force", database_volume],
                stage="failed rollback database volume cleanup",
                check=False,
            )
        if not completed:
            runner.run(
                ["docker", "volume", "rm", "--force", data_volume],
                stage="failed rollback data volume cleanup",
                check=False,
            )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create or test a safe Paper OS database and durable-data backup."
    )
    parser.add_argument("--service-root", required=True)
    parser.add_argument("--compose-file", required=True)
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--backup-root", required=True)
    subparsers = parser.add_subparsers(dest="command", required=True)

    backup = subparsers.add_parser("backup", help="create a verified local backup")
    backup.add_argument("--retention-days", type=int, default=14)
    backup.add_argument("--offsite", action="store_true")
    backup.add_argument("--rclone-config")
    backup.add_argument("--rclone-crypt-remote")
    backup.add_argument("--dry-run", action="store_true")

    drill = subparsers.add_parser(
        "restore-drill", help="restore one backup into an isolated PostgreSQL 17 container"
    )
    drill.add_argument("--backup-dir", required=True)
    drill.add_argument("--dry-run", action="store_true")
    materialize = subparsers.add_parser(
        "materialize-restore",
        help="restore a v2 backup into new, unselected rollback volumes",
    )
    materialize.add_argument("--backup-dir", required=True)
    materialize.add_argument("--confirm-create-new-volumes", action="store_true")
    materialize.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    os.umask(0o077)
    arguments = build_parser().parse_args(argv)
    try:
        paths = validate_service_paths(
            arguments.service_root,
            arguments.compose_file,
            arguments.env_file,
            arguments.backup_root,
        )
        if arguments.command == "backup":
            rclone_config = None
            if arguments.offsite:
                if not arguments.rclone_config or not arguments.rclone_crypt_remote:
                    raise OpsError(
                        "--offsite requires --rclone-config and --rclone-crypt-remote"
                    )
                rclone_config = validate_rclone_config(arguments.rclone_config)
            elif arguments.rclone_config or arguments.rclone_crypt_remote:
                raise OpsError("rclone options require --offsite")
            result = create_backup(
                paths,
                retention_days=arguments.retention_days,
                offsite=arguments.offsite,
                rclone_config=rclone_config,
                rclone_remote=arguments.rclone_crypt_remote,
                dry_run=arguments.dry_run,
            )
            prefix = "dry-run" if result.dry_run else "complete"
            print(f"backup {prefix}: {result.backup_id}")
            if result.uploaded:
                print("offsite crypt upload planned" if result.dry_run else "offsite crypt upload complete")
            if result.removed_backup_ids:
                print(f"retention removed {len(result.removed_backup_ids)} verified backup(s)")
        elif arguments.command == "restore-drill":
            backup_dir = _absolute_path(
                arguments.backup_dir, "backup directory", must_exist=True
            )
            container_name = restore_drill(
                paths,
                backup_dir=backup_dir,
                dry_run=arguments.dry_run,
            )
            prefix = "dry-run" if arguments.dry_run else "passed"
            print(f"restore drill {prefix}: {container_name}")
        else:
            backup_dir = _absolute_path(
                arguments.backup_dir, "backup directory", must_exist=True
            )
            materialized = materialize_restore(
                paths,
                backup_dir=backup_dir,
                dry_run=arguments.dry_run,
                confirm_create_new_volumes=arguments.confirm_create_new_volumes,
            )
            prefix = "dry-run" if materialized.dry_run else "complete"
            print(f"materialized restore {prefix}")
            print(f"database volume: {materialized.database_volume}")
            print(f"data volume: {materialized.data_volume}")
        return 0
    except OpsError as exc:
        print(f"paper backup error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

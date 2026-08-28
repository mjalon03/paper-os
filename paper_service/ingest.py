"""Deterministic, content-addressed import of locally staged PDFs."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class PaperRegistry(Protocol):
    def __call__(
        self,
        sha256: str,
        size_bytes: int,
        source_path: str,
        stored_path: str,
        job_id: int,
    ) -> bool: ...


class StorageIntegrityError(RuntimeError):
    """Raised when a content-addressed path does not contain its named content."""


@dataclass(frozen=True)
class IngestStats:
    seen: int = 0
    imported: int = 0
    duplicate: int = 0
    invalid: int = 0


def looks_like_pdf(path: Path) -> bool:
    try:
        with path.open("rb") as source:
            return b"%PDF-" in source.read(1024)
    except OSError:
        return False


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_atomically(source: Path, target: Path, expected_sha256: str) -> None:
    if target.exists():
        if sha256_file(target) != expected_sha256:
            raise StorageIntegrityError("existing raw paper failed hash verification")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as temporary:
            temporary_name = temporary.name
            with source.open("rb") as original:
                shutil.copyfileobj(original, temporary, length=1024 * 1024)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.chmod(temporary_name, 0o640)
        os.replace(temporary_name, target)
    finally:
        if temporary_name and os.path.exists(temporary_name):
            os.unlink(temporary_name)


def ingest_pdfs(
    incoming_dir: Path,
    raw_dir: Path,
    job_id: int,
    register: PaperRegistry,
) -> IngestStats:
    seen = imported = duplicate = invalid = 0
    incoming_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)

    candidates = sorted(
        path
        for path in incoming_dir.rglob("*")
        if path.is_file() and not path.is_symlink() and path.suffix.lower() == ".pdf"
    )
    for source in candidates:
        seen += 1
        if not looks_like_pdf(source):
            invalid += 1
            continue
        digest = sha256_file(source)
        size_bytes = source.stat().st_size
        relative_source = source.relative_to(incoming_dir).as_posix()
        stored_path = f"{digest[:2]}/{digest}.pdf"
        target = raw_dir / stored_path
        _copy_atomically(source, target, digest)
        if register(digest, size_bytes, relative_source, stored_path, job_id):
            imported += 1
        else:
            duplicate += 1

    return IngestStats(seen, imported, duplicate, invalid)

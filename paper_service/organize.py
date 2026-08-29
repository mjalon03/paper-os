"""Validated Drive naming and organization for registered papers."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from .metadata import MetadataFailure, PaperMetadata, classify_with_deepseek, extract_pdf_context


class OrganizationFailure(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ProcessingStats:
    organized: int = 0
    failed: int = 0


def sanitize_name(value: str, max_length: int = 90) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .-")
    return (cleaned or "Untitled")[:max_length].rstrip(" .-")


def _truncate_utf8(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore").rstrip(" .-")


def first_author_name(authors: list[str]) -> str:
    if not authors:
        return "Unknown Author"
    author = authors[0].strip()
    if "," in author:
        return sanitize_name(author.split(",", 1)[0], 40)
    pieces = author.split()
    return sanitize_name(pieces[-1] if pieces else author, 40)


def drive_destination(metadata: PaperMetadata, sha256: str) -> str:
    year = str(metadata.year) if metadata.year else "Unknown Year"
    prefix = " - ".join(
        (year, first_author_name(metadata.authors), sanitize_name(metadata.short_title))
    )
    suffix = f" - {sha256[:8]}.pdf"
    filename = _truncate_utf8(prefix, 220 - len(suffix.encode("utf-8"))) + suffix
    base = os.environ.get("PAPER_DRIVE_ORIGINAL", "paper-drive:Original").rstrip("/")
    return f"{base}/{metadata.category}/{year}/{filename}"


def _safe_source_path(relative_source: str) -> str:
    candidate = PurePosixPath(relative_source)
    if candidate.is_absolute() or ".." in candidate.parts or not candidate.parts:
        raise OrganizationFailure("unsafe_source_path")
    return candidate.as_posix()


def rclone_moveto(source_path: str, destination: str) -> None:
    inbox = os.environ.get("PAPER_DRIVE_INBOX", "paper-drive:Paper Inbox").rstrip("/")
    source = f"{inbox}/{_safe_source_path(source_path)}"
    command = [
        os.environ.get("PAPER_RCLONE_BIN", "rclone"),
        "moveto",
        source,
        destination,
        "--config",
        os.environ.get("PAPER_RCLONE_CONFIG", "/config/rclone/rclone.conf"),
        "--contimeout",
        "30s",
        "--timeout",
        "5m",
    ]
    try:
        result = subprocess.run(
            command,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=int(os.environ.get("PAPER_RCLONE_TIMEOUT_SECONDS", "3600")),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OrganizationFailure("rclone_unavailable") from exc
    if result.returncode != 0:
        raise OrganizationFailure(f"rclone_move_exit_{result.returncode}")


def process_paper(
    paper: dict[str, Any],
    data_dir: Path,
    classify: Callable[[Any], PaperMetadata] = classify_with_deepseek,
    move: Callable[[str, str], None] = rclone_moveto,
) -> tuple[dict[str, Any], str]:
    raw_path = data_dir / "library" / "raw" / paper["stored_path"]
    if not raw_path.is_file():
        raise OrganizationFailure("raw_paper_missing")
    context = extract_pdf_context(raw_path)
    metadata = classify(context)
    destination = drive_destination(metadata, paper["sha256"].strip())
    move(paper["source_path"], destination)
    staged_path = data_dir / "incoming" / _safe_source_path(paper["source_path"])
    try:
        staged_path.unlink(missing_ok=True)
    except OSError:
        pass
    return metadata.to_dict(), destination

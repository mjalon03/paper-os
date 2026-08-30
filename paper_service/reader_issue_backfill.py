"""Idempotently import warnings from existing Reader v2 artifacts."""

from __future__ import annotations

import json
import os
from pathlib import Path

from .database import ensure_schema, import_article_warnings, list_reader_v2_ready_ids
from .reader_v2 import read_article
from .selectors import build_selector_index


def backfill_reader_issues(data_dir: Path) -> dict[str, int]:
    stats = {"papers_seen": 0, "papers_imported": 0, "warnings_seen": 0, "failed": 0}
    for paper_id in list_reader_v2_ready_ids():
        stats["papers_seen"] += 1
        try:
            article = read_article(data_dir, paper_id)
            warnings = article["warnings"]
            imported = import_article_warnings(
                paper_id,
                warnings=warnings,
                article_fingerprint=build_selector_index(article).article_fingerprint,
            )
            stats["warnings_seen"] += len(imported)
            stats["papers_imported"] += 1
        except (FileNotFoundError, ValueError, OSError):
            stats["failed"] += 1
    return stats


def main() -> int:
    ensure_schema()
    stats = backfill_reader_issues(
        Path(os.environ.get("PAPER_DATA_DIR", "/data"))
    )
    print(json.dumps(stats, sort_keys=True))
    return 1 if stats["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

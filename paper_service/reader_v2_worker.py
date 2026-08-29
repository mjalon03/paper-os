"""Resource-bounded worker for staged reader-v2 conversion."""

from __future__ import annotations

import os
import time
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import get_context
from pathlib import Path

from .database import (
    claim_paper_for_reader_v2,
    complete_reader_v2_paper,
    ensure_schema,
    fail_reader_v2_paper,
    recover_reader_v2_papers,
    retry_failed_reader_v2_papers,
)
from .reader_v2 import ReaderV2Failure, convert_paper_v2


def parse_allowed_ids(value: str | None) -> list[int] | None:
    if value is None or not value.strip():
        return None
    if value.strip().casefold() == "all":
        return None
    try:
        values = sorted({int(item.strip()) for item in value.split(",") if item.strip()})
    except ValueError as exc:
        raise RuntimeError("PAPER_READER_V2_ALLOWED_IDS must contain integers") from exc
    if any(value <= 0 for value in values):
        raise RuntimeError("PAPER_READER_V2_ALLOWED_IDS must contain positive integers")
    return values


def convert_isolated(paper: dict, data_dir: Path) -> tuple[str, str]:
    """Release Docling model memory after every paper on the small OVH host."""
    with ProcessPoolExecutor(max_workers=1, mp_context=get_context("spawn")) as executor:
        return executor.submit(convert_paper_v2, paper, data_dir).result()


def run_worker() -> None:
    ensure_schema()
    recover_reader_v2_papers()
    allowed_ids = parse_allowed_ids(os.environ.get("PAPER_READER_V2_ALLOWED_IDS"))
    poll_seconds = max(10, int(os.environ.get("PAPER_READER_V2_POLL_SECONDS", "30")))
    data_dir = Path(os.environ.get("PAPER_DATA_DIR", "/data"))
    while True:
        retry_failed_reader_v2_papers()
        paper = claim_paper_for_reader_v2(allowed_ids)
        if paper:
            try:
                manifest, source = convert_isolated(paper, data_dir)
                complete_reader_v2_paper(int(paper["id"]), manifest, source)
            except ReaderV2Failure as exc:
                fail_reader_v2_paper(int(paper["id"]), exc.code)
            except Exception:
                fail_reader_v2_paper(int(paper["id"]), "reader_v2_failed")
            continue
        time.sleep(poll_seconds)


if __name__ == "__main__":
    run_worker()

"""Credential-free orchestration for legacy Reader v1 artifacts.

PDF parsing itself happens in the separate paper-parser service.  This worker
holds only the internal database connection needed for the existing durable
reader queue and a writable artifact volume; it has no Drive or model secret
and no external-egress network in Compose.
"""

from __future__ import annotations

import os
from pathlib import Path
import time
from typing import Any

from .database import (
    claim_paper_for_reader,
    complete_reader_paper,
    ensure_schema,
    fail_reader_paper,
    recover_reader_papers,
    retry_failed_reader_papers,
)
from .ops_runtime import worker_heartbeat_from_env
from .parser_client import convert_paper
from .reader import READER_VERSION, ReaderFailure, ReaderStats


def _heartbeat_processing(heartbeat: Any | None, paper_id: Any) -> bool:
    if (
        heartbeat is None
        or isinstance(paper_id, bool)
        or not isinstance(paper_id, int)
        or paper_id < 1
    ):
        return False
    try:
        heartbeat.processing(paper_id)
        return True
    except Exception:
        return False


def _heartbeat_idle(
    heartbeat: Any | None,
    active: bool,
    *,
    successful: bool,
) -> None:
    if not active:
        return
    try:
        heartbeat.idle(successful=successful)
    except Exception:
        return


def execute_reader_batch(*, heartbeat: Any | None = None) -> ReaderStats:
    """Drain the existing durable Reader v1 queue without parsing locally."""

    data_dir = Path(os.environ.get("PAPER_DATA_DIR", "/data"))
    retry_failed_reader_papers()
    ready = failed = 0
    while paper := claim_paper_for_reader():
        active = _heartbeat_processing(heartbeat, paper.get("id"))
        successful = False
        try:
            manifest_path = convert_paper(paper, data_dir)
            complete_reader_paper(int(paper["id"]), manifest_path, READER_VERSION)
            ready += 1
            successful = True
        except ReaderFailure as exc:
            fail_reader_paper(int(paper["id"]), exc.code)
            failed += 1
        except Exception:
            fail_reader_paper(int(paper["id"]), "reader_failed")
            failed += 1
        finally:
            _heartbeat_idle(
                heartbeat,
                active,
                successful=successful,
            )
    return ReaderStats(ready, failed)


def run_worker() -> None:
    ensure_schema()
    recover_reader_papers()
    try:
        poll_seconds = int(os.environ.get("PAPER_READER_V1_POLL_SECONDS", "15"))
    except ValueError:
        poll_seconds = 15
    poll_seconds = min(300, max(5, poll_seconds))
    with worker_heartbeat_from_env("reader-v1-worker-1") as heartbeat:
        while True:
            execute_reader_batch(heartbeat=heartbeat)
            time.sleep(poll_seconds)


if __name__ == "__main__":
    run_worker()

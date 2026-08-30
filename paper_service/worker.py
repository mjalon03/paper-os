"""Single worker that schedules, claims, and executes Drive sync jobs."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .database import (
    claim_paper_for_processing,
    claim_sync,
    complete_paper_processing,
    ensure_schema,
    fail_paper_processing,
    fail_sync,
    finish_sync,
    queue_sync,
    recover_processing_papers,
    recover_interrupted_sync,
    register_paper,
    retry_failed_papers,
)
from .ingest import StorageIntegrityError, ingest_pdfs
from .metadata import MetadataFailure, deepseek_api_key
from .organize import OrganizationFailure, ProcessingStats, process_paper
from .ops_runtime import worker_heartbeat_from_env
from .sync import SyncFailure, copy_drive_inbox, latest_due_slot, parse_sync_times


@dataclass(frozen=True)
class CombinedStats:
    seen: int
    imported: int
    duplicate: int
    invalid: int
    organized: int
    processing_failed: int
    reader_ready: int
    reader_failed: int


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


def execute_sync(job_id: int, *, heartbeat: Any | None = None) -> None:
    data_dir = Path(os.environ.get("PAPER_DATA_DIR", "/data"))
    incoming_dir = data_dir / "incoming"
    raw_dir = data_dir / "library" / "raw"
    try:
        copy_drive_inbox(incoming_dir)
        stats = ingest_pdfs(incoming_dir, raw_dir, job_id, register_paper)
        processing = ProcessingStats()
        if deepseek_api_key():
            retry_failed_papers()
            organized = failed = 0
            while paper := claim_paper_for_processing():
                active = _heartbeat_processing(heartbeat, paper.get("id"))
                successful = False
                try:
                    metadata, drive_path = process_paper(paper, data_dir)
                    complete_paper_processing(int(paper["id"]), metadata, drive_path)
                    organized += 1
                    successful = True
                except (MetadataFailure, OrganizationFailure) as exc:
                    fail_paper_processing(int(paper["id"]), exc.code)
                    failed += 1
                except Exception:
                    fail_paper_processing(int(paper["id"]), "processing_failed")
                    failed += 1
                finally:
                    _heartbeat_idle(
                        heartbeat,
                        active,
                        successful=successful,
                    )
            processing = ProcessingStats(organized, failed)
        combined = CombinedStats(
            stats.seen,
            stats.imported,
            stats.duplicate,
            stats.invalid,
            processing.organized,
            processing.failed,
            0,
            0,
        )
        finish_sync(job_id, combined)
    except SyncFailure as exc:
        fail_sync(job_id, exc.code)
    except StorageIntegrityError:
        fail_sync(job_id, "storage_integrity")
    except Exception:
        fail_sync(job_id, "ingest_failed")


def run_worker() -> None:
    ensure_schema()
    recover_interrupted_sync()
    recover_processing_papers()
    sync_times = parse_sync_times(os.environ.get("PAPER_SYNC_TIMES", "07:00,19:00"))
    timezone_name = os.environ.get("PAPER_TIMEZONE", "Asia/Hong_Kong")
    poll_seconds = max(5, int(os.environ.get("PAPER_WORKER_POLL_SECONDS", "15")))

    with worker_heartbeat_from_env("paper-worker-1") as heartbeat:
        while True:
            slot = latest_due_slot(datetime.now(timezone.utc), sync_times, timezone_name)
            queue_sync("scheduled", slot.key)
            job = claim_sync()
            if job:
                execute_sync(int(job["id"]), heartbeat=heartbeat)
                continue
            time.sleep(poll_seconds)


if __name__ == "__main__":
    run_worker()

"""Single worker that schedules, claims, and executes Drive sync jobs."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .database import (
    claim_paper_for_processing,
    claim_paper_for_reader,
    claim_sync,
    complete_paper_processing,
    complete_reader_paper,
    ensure_schema,
    fail_paper_processing,
    fail_reader_paper,
    fail_sync,
    finish_sync,
    queue_sync,
    recover_processing_papers,
    recover_reader_papers,
    recover_interrupted_sync,
    register_paper,
    retry_failed_papers,
    retry_failed_reader_papers,
)
from .ingest import StorageIntegrityError, ingest_pdfs
from .metadata import MetadataFailure, deepseek_api_key
from .organize import OrganizationFailure, ProcessingStats, process_paper
from .reader import READER_VERSION, ReaderFailure, ReaderStats, convert_paper
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


def execute_sync(job_id: int) -> None:
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
                try:
                    metadata, drive_path = process_paper(paper, data_dir)
                    complete_paper_processing(int(paper["id"]), metadata, drive_path)
                    organized += 1
                except (MetadataFailure, OrganizationFailure) as exc:
                    fail_paper_processing(int(paper["id"]), exc.code)
                    failed += 1
                except Exception:
                    fail_paper_processing(int(paper["id"]), "processing_failed")
                    failed += 1
            processing = ProcessingStats(organized, failed)
        retry_failed_reader_papers()
        reader_ready = reader_failed = 0
        while paper := claim_paper_for_reader():
            try:
                manifest_path = convert_paper(paper, data_dir)
                complete_reader_paper(
                    int(paper["id"]), manifest_path, READER_VERSION
                )
                reader_ready += 1
            except ReaderFailure as exc:
                fail_reader_paper(int(paper["id"]), exc.code)
                reader_failed += 1
            except Exception:
                fail_reader_paper(int(paper["id"]), "reader_failed")
                reader_failed += 1
        reader = ReaderStats(reader_ready, reader_failed)
        combined = CombinedStats(
            stats.seen,
            stats.imported,
            stats.duplicate,
            stats.invalid,
            processing.organized,
            processing.failed,
            reader.ready,
            reader.failed,
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
    recover_reader_papers()
    sync_times = parse_sync_times(os.environ.get("PAPER_SYNC_TIMES", "07:00,19:00"))
    timezone_name = os.environ.get("PAPER_TIMEZONE", "Asia/Hong_Kong")
    poll_seconds = max(5, int(os.environ.get("PAPER_WORKER_POLL_SECONDS", "15")))

    while True:
        slot = latest_due_slot(datetime.now(timezone.utc), sync_times, timezone_name)
        queue_sync("scheduled", slot.key)
        job = claim_sync()
        if job:
            execute_sync(int(job["id"]))
            continue
        time.sleep(poll_seconds)


if __name__ == "__main__":
    run_worker()

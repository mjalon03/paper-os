"""PostgreSQL-backed job queue and paper registry."""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterator


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sync_jobs (
    id BIGSERIAL PRIMARY KEY,
    trigger TEXT NOT NULL CHECK (trigger IN ('manual', 'scheduled')),
    schedule_key TEXT UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
    requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    files_seen INTEGER NOT NULL DEFAULT 0,
    files_imported INTEGER NOT NULL DEFAULT 0,
    files_duplicate INTEGER NOT NULL DEFAULT 0,
    files_invalid INTEGER NOT NULL DEFAULT 0,
    files_organized INTEGER NOT NULL DEFAULT 0,
    files_processing_failed INTEGER NOT NULL DEFAULT 0,
    files_reader_ready INTEGER NOT NULL DEFAULT 0,
    files_reader_failed INTEGER NOT NULL DEFAULT 0,
    error_code TEXT
);

ALTER TABLE sync_jobs ADD COLUMN IF NOT EXISTS files_organized INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sync_jobs ADD COLUMN IF NOT EXISTS files_processing_failed INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sync_jobs ADD COLUMN IF NOT EXISTS files_reader_ready INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sync_jobs ADD COLUMN IF NOT EXISTS files_reader_failed INTEGER NOT NULL DEFAULT 0;

CREATE UNIQUE INDEX IF NOT EXISTS sync_jobs_one_active
ON sync_jobs ((TRUE))
WHERE status IN ('queued', 'running');

CREATE TABLE IF NOT EXISTS papers (
    id BIGSERIAL PRIMARY KEY,
    sha256 CHAR(64) NOT NULL UNIQUE,
    size_bytes BIGINT NOT NULL CHECK (size_bytes >= 0),
    source_path TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    first_job_id BIGINT REFERENCES sync_jobs(id),
    imported_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processing_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (processing_status IN ('pending', 'processing', 'organized', 'failed')),
    processing_attempts INTEGER NOT NULL DEFAULT 0,
    title TEXT,
    authors JSONB,
    publication_year INTEGER,
    doi TEXT,
    category TEXT,
    paper_type TEXT,
    confidence DOUBLE PRECISION,
    drive_path TEXT,
    metadata_json JSONB,
    processed_at TIMESTAMPTZ,
    processing_error TEXT,
    reader_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (reader_status IN ('pending', 'processing', 'ready', 'failed')),
    reader_attempts INTEGER NOT NULL DEFAULT 0,
    reader_version INTEGER,
    reader_manifest_path TEXT,
    reader_generated_at TIMESTAMPTZ,
    reader_error TEXT,
    reader_v2_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (reader_v2_status IN ('pending', 'processing', 'ready', 'failed')),
    reader_v2_attempts INTEGER NOT NULL DEFAULT 0,
    reader_v2_source TEXT,
    reader_v2_manifest_path TEXT,
    reader_v2_generated_at TIMESTAMPTZ,
    reader_v2_error TEXT
);

ALTER TABLE papers ADD COLUMN IF NOT EXISTS processing_status TEXT NOT NULL DEFAULT 'pending';
ALTER TABLE papers ADD COLUMN IF NOT EXISTS processing_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE papers ADD COLUMN IF NOT EXISTS title TEXT;
ALTER TABLE papers ADD COLUMN IF NOT EXISTS authors JSONB;
ALTER TABLE papers ADD COLUMN IF NOT EXISTS publication_year INTEGER;
ALTER TABLE papers ADD COLUMN IF NOT EXISTS doi TEXT;
ALTER TABLE papers ADD COLUMN IF NOT EXISTS category TEXT;
ALTER TABLE papers ADD COLUMN IF NOT EXISTS paper_type TEXT;
ALTER TABLE papers ADD COLUMN IF NOT EXISTS confidence DOUBLE PRECISION;
ALTER TABLE papers ADD COLUMN IF NOT EXISTS drive_path TEXT;
ALTER TABLE papers ADD COLUMN IF NOT EXISTS metadata_json JSONB;
ALTER TABLE papers ADD COLUMN IF NOT EXISTS processed_at TIMESTAMPTZ;
ALTER TABLE papers ADD COLUMN IF NOT EXISTS processing_error TEXT;
ALTER TABLE papers ADD COLUMN IF NOT EXISTS reader_status TEXT NOT NULL DEFAULT 'pending';
ALTER TABLE papers ADD COLUMN IF NOT EXISTS reader_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE papers ADD COLUMN IF NOT EXISTS reader_version INTEGER;
ALTER TABLE papers ADD COLUMN IF NOT EXISTS reader_manifest_path TEXT;
ALTER TABLE papers ADD COLUMN IF NOT EXISTS reader_generated_at TIMESTAMPTZ;
ALTER TABLE papers ADD COLUMN IF NOT EXISTS reader_error TEXT;
ALTER TABLE papers ADD COLUMN IF NOT EXISTS reader_v2_status TEXT NOT NULL DEFAULT 'pending';
ALTER TABLE papers ADD COLUMN IF NOT EXISTS reader_v2_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE papers ADD COLUMN IF NOT EXISTS reader_v2_source TEXT;
ALTER TABLE papers ADD COLUMN IF NOT EXISTS reader_v2_manifest_path TEXT;
ALTER TABLE papers ADD COLUMN IF NOT EXISTS reader_v2_generated_at TIMESTAMPTZ;
ALTER TABLE papers ADD COLUMN IF NOT EXISTS reader_v2_error TEXT;

CREATE INDEX IF NOT EXISTS papers_processing_queue
ON papers (processing_status, id);

CREATE INDEX IF NOT EXISTS papers_reader_queue
ON papers (reader_status, id);

CREATE INDEX IF NOT EXISTS papers_reader_v2_queue
ON papers (reader_v2_status, id);
"""

PUBLIC_JOB_COLUMNS = """
id, trigger, status, requested_at, started_at, finished_at,
files_seen, files_imported, files_duplicate, files_invalid,
files_organized, files_processing_failed, files_reader_ready,
files_reader_failed, error_code
"""


def database_config() -> dict[str, str]:
    name = os.environ.get("PAPER_DB_NAME", "paper_os")
    user = os.environ.get("PAPER_DB_USER", "paper_os")
    password = os.environ.get("PAPER_DB_PASSWORD")
    host = os.environ.get("PAPER_DB_HOST", "postgres")
    port = os.environ.get("PAPER_DB_PORT", "5432")
    if not password:
        raise RuntimeError("PAPER_DB_PASSWORD must be set")
    return {
        "dbname": name,
        "user": user,
        "password": password,
        "host": host,
        "port": port,
    }


@contextmanager
def connection() -> Iterator[Any]:
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(**database_config(), row_factory=dict_row) as database:
        yield database


def ensure_schema() -> None:
    with connection() as database:
        database.execute(SCHEMA_SQL)


def queue_sync(trigger: str, schedule_key: str | None = None) -> dict[str, Any]:
    if trigger not in {"manual", "scheduled"}:
        raise ValueError("invalid sync trigger")
    with connection() as database:
        database.execute("SELECT pg_advisory_xact_lock(806244901)")
        if schedule_key:
            existing = database.execute(
                f"SELECT {PUBLIC_JOB_COLUMNS} FROM sync_jobs WHERE schedule_key = %s",
                (schedule_key,),
            ).fetchone()
            if existing:
                return dict(existing)
        active = database.execute(
            f"""SELECT {PUBLIC_JOB_COLUMNS} FROM sync_jobs
                WHERE status IN ('queued', 'running') ORDER BY id LIMIT 1"""
        ).fetchone()
        if active:
            return dict(active)
        created = database.execute(
            f"""INSERT INTO sync_jobs (trigger, schedule_key, status)
                VALUES (%s, %s, 'queued') RETURNING {PUBLIC_JOB_COLUMNS}""",
            (trigger, schedule_key),
        ).fetchone()
        return dict(created)


def latest_sync() -> dict[str, Any] | None:
    with connection() as database:
        row = database.execute(
            f"SELECT {PUBLIC_JOB_COLUMNS} FROM sync_jobs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


def recover_interrupted_sync() -> None:
    with connection() as database:
        database.execute(
            """UPDATE sync_jobs SET status = 'queued', started_at = NULL,
               error_code = NULL WHERE status = 'running'"""
        )


def claim_sync() -> dict[str, Any] | None:
    with connection() as database:
        row = database.execute(
            """UPDATE sync_jobs SET status = 'running', started_at = NOW()
               WHERE id = (
                   SELECT id FROM sync_jobs WHERE status = 'queued'
                   ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1
               )
               RETURNING id, trigger, status"""
        ).fetchone()
        return dict(row) if row else None


def finish_sync(job_id: int, stats: Any) -> None:
    with connection() as database:
        database.execute(
            """UPDATE sync_jobs SET status = 'succeeded', finished_at = NOW(),
               files_seen = %s, files_imported = %s, files_duplicate = %s,
               files_invalid = %s, files_organized = %s,
               files_processing_failed = %s, files_reader_ready = %s,
               files_reader_failed = %s, error_code = NULL WHERE id = %s""",
            (
                stats.seen,
                stats.imported,
                stats.duplicate,
                stats.invalid,
                getattr(stats, "organized", 0),
                getattr(stats, "processing_failed", 0),
                getattr(stats, "reader_ready", 0),
                getattr(stats, "reader_failed", 0),
                job_id,
            ),
        )


def fail_sync(job_id: int, error_code: str) -> None:
    with connection() as database:
        database.execute(
            """UPDATE sync_jobs SET status = 'failed', finished_at = NOW(),
               error_code = %s WHERE id = %s""",
            (error_code[:80], job_id),
        )


def register_paper(
    sha256: str,
    size_bytes: int,
    source_path: str,
    stored_path: str,
    job_id: int,
) -> bool:
    with connection() as database:
        row = database.execute(
            """INSERT INTO papers
               (sha256, size_bytes, source_path, stored_path, first_job_id)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (sha256) DO NOTHING RETURNING id""",
            (sha256, size_bytes, source_path, stored_path, job_id),
        ).fetchone()
        return row is not None


def recover_processing_papers() -> None:
    with connection() as database:
        database.execute(
            """UPDATE papers SET processing_status = 'pending',
               processing_error = 'worker_interrupted'
               WHERE processing_status = 'processing'"""
        )


def retry_failed_papers(max_attempts: int = 3) -> None:
    with connection() as database:
        database.execute(
            """UPDATE papers SET processing_status = 'pending'
               WHERE processing_status = 'failed' AND processing_attempts < %s""",
            (max_attempts,),
        )


def claim_paper_for_processing() -> dict[str, Any] | None:
    with connection() as database:
        row = database.execute(
            """UPDATE papers SET processing_status = 'processing',
               processing_attempts = processing_attempts + 1,
               processing_error = NULL
               WHERE id = (
                   SELECT id FROM papers WHERE processing_status = 'pending'
                   ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1
               )
               RETURNING id, sha256, source_path, stored_path"""
        ).fetchone()
        return dict(row) if row else None


def complete_paper_processing(
    paper_id: int,
    metadata: dict[str, Any],
    drive_path: str,
) -> None:
    import json

    with connection() as database:
        database.execute(
            """UPDATE papers SET processing_status = 'organized', title = %s,
               authors = %s::jsonb, publication_year = %s, doi = %s,
               category = %s, paper_type = %s, confidence = %s,
               drive_path = %s, metadata_json = %s::jsonb,
               processed_at = NOW(), processing_error = NULL WHERE id = %s""",
            (
                metadata["title"],
                json.dumps(metadata["authors"], ensure_ascii=False),
                metadata["year"],
                metadata["doi"],
                metadata["category"],
                metadata["paper_type"],
                metadata["confidence"],
                drive_path,
                json.dumps(metadata, ensure_ascii=False),
                paper_id,
            ),
        )


def fail_paper_processing(paper_id: int, error_code: str) -> None:
    with connection() as database:
        database.execute(
            """UPDATE papers SET processing_status = 'failed',
               processing_error = %s WHERE id = %s""",
            (error_code[:80], paper_id),
        )


def recover_reader_papers() -> None:
    with connection() as database:
        database.execute(
            """UPDATE papers SET reader_status = 'pending',
               reader_error = 'worker_interrupted'
               WHERE reader_status = 'processing'"""
        )


def retry_failed_reader_papers(max_attempts: int = 3) -> None:
    with connection() as database:
        database.execute(
            """UPDATE papers SET reader_status = 'pending'
               WHERE reader_status = 'failed' AND reader_attempts < %s""",
            (max_attempts,),
        )


def claim_paper_for_reader() -> dict[str, Any] | None:
    with connection() as database:
        row = database.execute(
            """UPDATE papers SET reader_status = 'processing',
               reader_attempts = reader_attempts + 1, reader_error = NULL
               WHERE id = (
                   SELECT id FROM papers
                   WHERE processing_status = 'organized' AND reader_status = 'pending'
                   ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1
               )
               RETURNING id, sha256, size_bytes, stored_path, title, authors,
                         publication_year, doi, category, paper_type"""
        ).fetchone()
        return dict(row) if row else None


def complete_reader_paper(paper_id: int, manifest_path: str, version: int) -> None:
    with connection() as database:
        database.execute(
            """UPDATE papers SET reader_status = 'ready', reader_version = %s,
               reader_manifest_path = %s, reader_generated_at = NOW(),
               reader_error = NULL WHERE id = %s""",
            (version, manifest_path, paper_id),
        )


def fail_reader_paper(paper_id: int, error_code: str) -> None:
    with connection() as database:
        database.execute(
            """UPDATE papers SET reader_status = 'failed', reader_error = %s
               WHERE id = %s""",
            (error_code[:80], paper_id),
        )


def recover_reader_v2_papers() -> None:
    with connection() as database:
        database.execute(
            """UPDATE papers SET reader_v2_status = 'pending',
               reader_v2_error = 'worker_interrupted'
               WHERE reader_v2_status = 'processing'"""
        )


def retry_failed_reader_v2_papers(max_attempts: int = 3) -> None:
    with connection() as database:
        database.execute(
            """UPDATE papers SET reader_v2_status = 'pending'
               WHERE reader_v2_status = 'failed' AND reader_v2_attempts < %s""",
            (max_attempts,),
        )


def claim_paper_for_reader_v2(allowed_ids: list[int] | None) -> dict[str, Any] | None:
    with connection() as database:
        parameters: tuple[Any, ...] = ()
        allowed = ""
        if allowed_ids is not None:
            if not allowed_ids:
                return None
            allowed = " AND id = ANY(%s)"
            parameters = (allowed_ids,)
        row = database.execute(
            f"""UPDATE papers SET reader_v2_status = 'processing',
               reader_v2_attempts = reader_v2_attempts + 1,
               reader_v2_error = NULL
               WHERE id = (
                   SELECT id FROM papers
                   WHERE processing_status = 'organized'
                     AND reader_v2_status = 'pending'{allowed}
                   ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1
               )
               RETURNING id, sha256, size_bytes, stored_path, title, authors,
                         publication_year, doi, category, paper_type""",
            parameters,
        ).fetchone()
        return dict(row) if row else None


def complete_reader_v2_paper(paper_id: int, manifest_path: str, source: str) -> None:
    with connection() as database:
        database.execute(
            """UPDATE papers SET reader_v2_status = 'ready',
               reader_v2_source = %s, reader_v2_manifest_path = %s,
               reader_v2_generated_at = NOW(), reader_v2_error = NULL
               WHERE id = %s""",
            (source, manifest_path, paper_id),
        )


def fail_reader_v2_paper(paper_id: int, error_code: str) -> None:
    with connection() as database:
        database.execute(
            """UPDATE papers SET reader_v2_status = 'failed', reader_v2_error = %s
               WHERE id = %s""",
            (error_code[:80], paper_id),
        )


def list_reader_papers() -> list[dict[str, Any]]:
    with connection() as database:
        rows = database.execute(
            """SELECT id, title, authors, publication_year, category, paper_type,
                      reader_status, reader_generated_at
               FROM papers WHERE processing_status = 'organized'
               ORDER BY imported_at DESC, id DESC"""
        ).fetchall()
        return [dict(row) for row in rows]


def get_reader_paper(paper_id: int) -> dict[str, Any] | None:
    with connection() as database:
        row = database.execute(
            """SELECT id, title, authors, publication_year, doi, category,
                      paper_type, reader_status, reader_manifest_path,
                      reader_generated_at, stored_path
               FROM papers WHERE id = %s AND processing_status = 'organized'""",
            (paper_id,),
        ).fetchone()
        return dict(row) if row else None

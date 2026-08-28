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
    error_code TEXT
);

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
    imported_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

PUBLIC_JOB_COLUMNS = """
id, trigger, status, requested_at, started_at, finished_at,
files_seen, files_imported, files_duplicate, files_invalid, error_code
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
               files_invalid = %s, error_code = NULL WHERE id = %s""",
            (stats.seen, stats.imported, stats.duplicate, stats.invalid, job_id),
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

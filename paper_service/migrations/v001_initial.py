"""Migration 001: original paper registry and worker queues."""

SQL = """
CREATE TABLE IF NOT EXISTS sync_jobs (
    id BIGSERIAL PRIMARY KEY,
    trigger TEXT NOT NULL CONSTRAINT sync_jobs_trigger_v001_guard
        CHECK (trigger IN ('manual', 'scheduled')),
    schedule_key TEXT UNIQUE,
    status TEXT NOT NULL CONSTRAINT sync_jobs_status_v001_guard
        CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
    requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    files_seen INTEGER NOT NULL DEFAULT 0
        CONSTRAINT sync_jobs_files_seen_v001_guard CHECK (files_seen >= 0),
    files_imported INTEGER NOT NULL DEFAULT 0
        CONSTRAINT sync_jobs_files_imported_v001_guard CHECK (files_imported >= 0),
    files_duplicate INTEGER NOT NULL DEFAULT 0
        CONSTRAINT sync_jobs_files_duplicate_v001_guard CHECK (files_duplicate >= 0),
    files_invalid INTEGER NOT NULL DEFAULT 0
        CONSTRAINT sync_jobs_files_invalid_v001_guard CHECK (files_invalid >= 0),
    files_organized INTEGER NOT NULL DEFAULT 0
        CONSTRAINT sync_jobs_files_organized_v001_guard CHECK (files_organized >= 0),
    files_processing_failed INTEGER NOT NULL DEFAULT 0
        CONSTRAINT sync_jobs_files_processing_failed_v001_guard
        CHECK (files_processing_failed >= 0),
    files_reader_ready INTEGER NOT NULL DEFAULT 0
        CONSTRAINT sync_jobs_files_reader_ready_v001_guard
        CHECK (files_reader_ready >= 0),
    files_reader_failed INTEGER NOT NULL DEFAULT 0
        CONSTRAINT sync_jobs_files_reader_failed_v001_guard
        CHECK (files_reader_failed >= 0),
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
    sha256 CHAR(64) NOT NULL UNIQUE
        CONSTRAINT papers_sha256_v001_guard
        CHECK (btrim(sha256) ~ '^[0-9a-f]{64}$'),
    size_bytes BIGINT NOT NULL CONSTRAINT papers_size_bytes_v001_guard
        CHECK (size_bytes >= 0),
    source_path TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    first_job_id BIGINT REFERENCES sync_jobs(id),
    imported_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processing_status TEXT NOT NULL DEFAULT 'pending'
        CONSTRAINT papers_processing_status_v001_guard
        CHECK (processing_status IN ('pending', 'processing', 'organized', 'failed')),
    processing_attempts INTEGER NOT NULL DEFAULT 0
        CONSTRAINT papers_processing_attempts_v001_guard
        CHECK (processing_attempts >= 0),
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
        CONSTRAINT papers_reader_status_v001_guard
        CHECK (reader_status IN ('pending', 'processing', 'ready', 'failed')),
    reader_attempts INTEGER NOT NULL DEFAULT 0
        CONSTRAINT papers_reader_attempts_v001_guard CHECK (reader_attempts >= 0),
    reader_version INTEGER,
    reader_manifest_path TEXT,
    reader_generated_at TIMESTAMPTZ,
    reader_error TEXT,
    reader_v2_status TEXT NOT NULL DEFAULT 'pending'
        CONSTRAINT papers_reader_v2_status_v001_guard
        CHECK (reader_v2_status IN ('pending', 'processing', 'ready', 'failed')),
    reader_v2_attempts INTEGER NOT NULL DEFAULT 0
        CONSTRAINT papers_reader_v2_attempts_v001_guard
        CHECK (reader_v2_attempts >= 0),
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

-- The original service added some columns to already-existing tables.  The
-- CREATE clauses above therefore do not establish their checks on that live
-- upgrade path.  Add our own uniquely named guards, then validate all legacy
-- rows inside this migration transaction.  Equivalent older unnamed checks
-- are intentionally not treated as proof.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'sync_jobs'::regclass
          AND conname = 'sync_jobs_trigger_v001_guard'
    ) THEN
        ALTER TABLE sync_jobs ADD CONSTRAINT sync_jobs_trigger_v001_guard
            CHECK (trigger IN ('manual', 'scheduled')) NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'sync_jobs'::regclass
          AND conname = 'sync_jobs_status_v001_guard'
    ) THEN
        ALTER TABLE sync_jobs ADD CONSTRAINT sync_jobs_status_v001_guard
            CHECK (status IN ('queued', 'running', 'succeeded', 'failed')) NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'sync_jobs'::regclass
          AND conname = 'sync_jobs_files_seen_v001_guard'
    ) THEN
        ALTER TABLE sync_jobs ADD CONSTRAINT sync_jobs_files_seen_v001_guard
            CHECK (files_seen >= 0) NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'sync_jobs'::regclass
          AND conname = 'sync_jobs_files_imported_v001_guard'
    ) THEN
        ALTER TABLE sync_jobs ADD CONSTRAINT sync_jobs_files_imported_v001_guard
            CHECK (files_imported >= 0) NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'sync_jobs'::regclass
          AND conname = 'sync_jobs_files_duplicate_v001_guard'
    ) THEN
        ALTER TABLE sync_jobs ADD CONSTRAINT sync_jobs_files_duplicate_v001_guard
            CHECK (files_duplicate >= 0) NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'sync_jobs'::regclass
          AND conname = 'sync_jobs_files_invalid_v001_guard'
    ) THEN
        ALTER TABLE sync_jobs ADD CONSTRAINT sync_jobs_files_invalid_v001_guard
            CHECK (files_invalid >= 0) NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'sync_jobs'::regclass
          AND conname = 'sync_jobs_files_organized_v001_guard'
    ) THEN
        ALTER TABLE sync_jobs ADD CONSTRAINT sync_jobs_files_organized_v001_guard
            CHECK (files_organized >= 0) NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'sync_jobs'::regclass
          AND conname = 'sync_jobs_files_processing_failed_v001_guard'
    ) THEN
        ALTER TABLE sync_jobs
            ADD CONSTRAINT sync_jobs_files_processing_failed_v001_guard
            CHECK (files_processing_failed >= 0) NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'sync_jobs'::regclass
          AND conname = 'sync_jobs_files_reader_ready_v001_guard'
    ) THEN
        ALTER TABLE sync_jobs
            ADD CONSTRAINT sync_jobs_files_reader_ready_v001_guard
            CHECK (files_reader_ready >= 0) NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'sync_jobs'::regclass
          AND conname = 'sync_jobs_files_reader_failed_v001_guard'
    ) THEN
        ALTER TABLE sync_jobs
            ADD CONSTRAINT sync_jobs_files_reader_failed_v001_guard
            CHECK (files_reader_failed >= 0) NOT VALID;
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'papers'::regclass
          AND conname = 'papers_sha256_v001_guard'
    ) THEN
        ALTER TABLE papers ADD CONSTRAINT papers_sha256_v001_guard
            CHECK (btrim(sha256) ~ '^[0-9a-f]{64}$') NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'papers'::regclass
          AND conname = 'papers_size_bytes_v001_guard'
    ) THEN
        ALTER TABLE papers ADD CONSTRAINT papers_size_bytes_v001_guard
            CHECK (size_bytes >= 0) NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'papers'::regclass
          AND conname = 'papers_processing_status_v001_guard'
    ) THEN
        ALTER TABLE papers ADD CONSTRAINT papers_processing_status_v001_guard
            CHECK (processing_status IN ('pending', 'processing', 'organized', 'failed'))
            NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'papers'::regclass
          AND conname = 'papers_processing_attempts_v001_guard'
    ) THEN
        ALTER TABLE papers ADD CONSTRAINT papers_processing_attempts_v001_guard
            CHECK (processing_attempts >= 0) NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'papers'::regclass
          AND conname = 'papers_reader_status_v001_guard'
    ) THEN
        ALTER TABLE papers ADD CONSTRAINT papers_reader_status_v001_guard
            CHECK (reader_status IN ('pending', 'processing', 'ready', 'failed'))
            NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'papers'::regclass
          AND conname = 'papers_reader_attempts_v001_guard'
    ) THEN
        ALTER TABLE papers ADD CONSTRAINT papers_reader_attempts_v001_guard
            CHECK (reader_attempts >= 0) NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'papers'::regclass
          AND conname = 'papers_reader_v2_status_v001_guard'
    ) THEN
        ALTER TABLE papers ADD CONSTRAINT papers_reader_v2_status_v001_guard
            CHECK (reader_v2_status IN ('pending', 'processing', 'ready', 'failed'))
            NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'papers'::regclass
          AND conname = 'papers_reader_v2_attempts_v001_guard'
    ) THEN
        ALTER TABLE papers ADD CONSTRAINT papers_reader_v2_attempts_v001_guard
            CHECK (reader_v2_attempts >= 0) NOT VALID;
    END IF;
END
$$;

ALTER TABLE sync_jobs VALIDATE CONSTRAINT sync_jobs_trigger_v001_guard;
ALTER TABLE sync_jobs VALIDATE CONSTRAINT sync_jobs_status_v001_guard;
ALTER TABLE sync_jobs VALIDATE CONSTRAINT sync_jobs_files_seen_v001_guard;
ALTER TABLE sync_jobs VALIDATE CONSTRAINT sync_jobs_files_imported_v001_guard;
ALTER TABLE sync_jobs VALIDATE CONSTRAINT sync_jobs_files_duplicate_v001_guard;
ALTER TABLE sync_jobs VALIDATE CONSTRAINT sync_jobs_files_invalid_v001_guard;
ALTER TABLE sync_jobs VALIDATE CONSTRAINT sync_jobs_files_organized_v001_guard;
ALTER TABLE sync_jobs
    VALIDATE CONSTRAINT sync_jobs_files_processing_failed_v001_guard;
ALTER TABLE sync_jobs VALIDATE CONSTRAINT sync_jobs_files_reader_ready_v001_guard;
ALTER TABLE sync_jobs VALIDATE CONSTRAINT sync_jobs_files_reader_failed_v001_guard;

ALTER TABLE papers VALIDATE CONSTRAINT papers_sha256_v001_guard;
ALTER TABLE papers VALIDATE CONSTRAINT papers_size_bytes_v001_guard;
ALTER TABLE papers VALIDATE CONSTRAINT papers_processing_status_v001_guard;
ALTER TABLE papers VALIDATE CONSTRAINT papers_processing_attempts_v001_guard;
ALTER TABLE papers VALIDATE CONSTRAINT papers_reader_status_v001_guard;
ALTER TABLE papers VALIDATE CONSTRAINT papers_reader_attempts_v001_guard;
ALTER TABLE papers VALIDATE CONSTRAINT papers_reader_v2_status_v001_guard;
ALTER TABLE papers VALIDATE CONSTRAINT papers_reader_v2_attempts_v001_guard;

CREATE INDEX IF NOT EXISTS papers_processing_queue
ON papers (processing_status, id);

CREATE INDEX IF NOT EXISTS papers_reader_queue
ON papers (reader_status, id);

CREATE INDEX IF NOT EXISTS papers_reader_v2_queue
ON papers (reader_v2_status, id);
"""

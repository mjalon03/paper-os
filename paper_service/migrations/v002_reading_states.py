"""Migration 002: durable per-paper mobile reading state."""

SQL = """
CREATE TABLE IF NOT EXISTS reading_states (
    paper_id BIGINT PRIMARY KEY REFERENCES papers(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'unread'
        CHECK (status IN ('unread', 'reading', 'completed', 'on_hold', 'rereading')),
    progress DOUBLE PRECISION NOT NULL DEFAULT 0
        CHECK (progress >= 0 AND progress <= 1),
    anchor JSONB
        CHECK (anchor IS NULL OR jsonb_typeof(anchor) = 'object'),
    article_fingerprint TEXT
        CHECK (
            article_fingerprint IS NULL
            OR article_fingerprint ~ '^[0-9a-f]{64}$'
        ),
    favorite BOOLEAN NOT NULL DEFAULT FALSE,
    priority SMALLINT NOT NULL DEFAULT 0
        CHECK (priority >= 0 AND priority <= 5),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    revision BIGINT NOT NULL DEFAULT 1 CHECK (revision >= 1)
);

CREATE INDEX IF NOT EXISTS reading_states_status_updated
ON reading_states (status, updated_at DESC);

CREATE INDEX IF NOT EXISTS reading_states_favorite_updated
ON reading_states (updated_at DESC)
WHERE favorite;
"""

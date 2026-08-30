"""Migration 003: durable reader quality issues and warning imports."""

SQL = """
CREATE TABLE reader_issues (
    id UUID PRIMARY KEY,
    paper_id BIGINT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    kind TEXT NOT NULL
        CHECK (kind IN (
            'extraction_text', 'sentence_break', 'section_structure',
            'figure_crop', 'table', 'metadata', 'other'
        )),
    description TEXT
        CHECK (description IS NULL OR char_length(description) <= 4000),
    anchor JSONB NOT NULL
        CHECK (jsonb_typeof(anchor) = 'object'),
    article_fingerprint CHAR(64) NOT NULL
        CHECK (article_fingerprint ~ '^[0-9a-f]{64}$'),
    source TEXT NOT NULL CHECK (source IN ('user', 'system')),
    external_key TEXT
        CHECK (external_key IS NULL OR char_length(external_key) <= 200),
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'resolved', 'dismissed')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at TIMESTAMPTZ,
    revision BIGINT NOT NULL DEFAULT 1 CHECK (revision >= 1),
    CHECK (
        (source = 'system' AND external_key IS NOT NULL)
        OR (source = 'user' AND external_key IS NULL)
    ),
    CHECK (
        (status = 'open' AND resolved_at IS NULL)
        OR (status IN ('resolved', 'dismissed') AND resolved_at IS NOT NULL)
    )
);

CREATE UNIQUE INDEX reader_issues_external_key
ON reader_issues (paper_id, external_key)
WHERE external_key IS NOT NULL;

CREATE INDEX reader_issues_paper_status_updated
ON reader_issues (paper_id, status, updated_at DESC, id);
"""

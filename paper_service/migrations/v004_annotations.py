"""Migration 004: user highlights, notes, and questions."""

SQL = """
CREATE TABLE annotations (
    id UUID PRIMARY KEY,
    paper_id BIGINT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('highlight', 'note', 'question')),
    anchor JSONB NOT NULL
        CHECK (
            jsonb_typeof(anchor) = 'object'
            AND anchor ?& ARRAY[
                'selector_version', 'scope', 'section_id', 'node_id',
                'start_utf16', 'end_utf16', 'exact_quote', 'prefix', 'suffix',
                'article_fingerprint'
            ]
        ),
    body TEXT CHECK (body IS NULL OR char_length(body) <= 20000),
    category TEXT CHECK (category IS NULL OR char_length(category) <= 80),
    question_status TEXT
        CHECK (question_status IN ('open', 'answered', 'dismissed')),
    article_fingerprint CHAR(64) NOT NULL
        CHECK (article_fingerprint ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    trashed_at TIMESTAMPTZ,
    revision BIGINT NOT NULL DEFAULT 1 CHECK (revision >= 1),
    CHECK (
        (kind = 'question' AND question_status IS NOT NULL)
        OR (kind <> 'question' AND question_status IS NULL)
    )
);

CREATE INDEX annotations_paper_active_updated
ON annotations (paper_id, updated_at DESC, id)
WHERE trashed_at IS NULL;

CREATE INDEX annotations_paper_kind_updated
ON annotations (paper_id, kind, updated_at DESC, id)
WHERE trashed_at IS NULL;
"""

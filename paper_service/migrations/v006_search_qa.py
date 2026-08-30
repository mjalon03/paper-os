"""Migration 006: immutable paper search generations and grounded QA jobs."""

SQL = r"""
ALTER TABLE annotations
ADD COLUMN ai_share_allowed BOOLEAN NOT NULL DEFAULT FALSE;

CREATE TABLE paper_search_generations (
    id UUID PRIMARY KEY,
    paper_id BIGINT NOT NULL REFERENCES papers(id) ON DELETE RESTRICT,
    corpus_digest CHAR(64) NOT NULL
        CHECK (corpus_digest ~ '^[0-9a-f]{64}$'),
    source_sha256 CHAR(64) NOT NULL
        CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
    article_fingerprint CHAR(64) NOT NULL
        CHECK (article_fingerprint ~ '^[0-9a-f]{64}$'),
    selector_version INTEGER NOT NULL CHECK (selector_version >= 1),
    document_count INTEGER NOT NULL CHECK (document_count >= 0),
    chunk_count INTEGER NOT NULL CHECK (chunk_count >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (paper_id, corpus_digest),
    UNIQUE (id, paper_id),
    UNIQUE (id, paper_id, selector_version),
    UNIQUE (
        id, paper_id, source_sha256, article_fingerprint, selector_version
    )
);

CREATE TABLE paper_search_documents (
    generation_id UUID NOT NULL,
    paper_id BIGINT NOT NULL,
    document_id TEXT NOT NULL CHECK (document_id ~ '^d-[0-9a-f]{32}$'),
    selector_version INTEGER NOT NULL CHECK (selector_version >= 1),
    source_kind TEXT NOT NULL CHECK (
        source_kind IN ('source_text', 'reviewed_knowledge', 'user_annotation')
    ),
    source_id TEXT NOT NULL CHECK (char_length(source_id) BETWEEN 1 AND 512),
    reviewed_revision_id UUID,
    annotation_id UUID,
    node_id TEXT NOT NULL CHECK (
        node_id ~ '^n-[0-9a-f]{24}(-([2-9]|[1-9][0-9]+))?$'
    ),
    content TEXT NOT NULL CHECK (char_length(content) BETWEEN 1 AND 100000),
    lexical_content TEXT NOT NULL
        CHECK (char_length(lexical_content) BETWEEN 1 AND 100000),
    authority_score SMALLINT NOT NULL CHECK (
        (source_kind = 'source_text' AND authority_score = 100)
        OR (source_kind = 'reviewed_knowledge' AND authority_score = 200)
        OR (source_kind = 'user_annotation' AND authority_score = 300)
    ),
    model_eligible BOOLEAN NOT NULL,
    content_sha256 CHAR(64) NOT NULL
        CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (generation_id, document_id),
    UNIQUE (generation_id, paper_id, document_id),
    UNIQUE (generation_id, paper_id, document_id, selector_version),
    UNIQUE (generation_id, source_kind, source_id, node_id),
    FOREIGN KEY (generation_id, paper_id, selector_version)
        REFERENCES paper_search_generations(
            id, paper_id, selector_version
        ) ON DELETE RESTRICT,
    FOREIGN KEY (reviewed_revision_id, paper_id)
        REFERENCES paper_knowledge_revisions(id, paper_id) ON DELETE RESTRICT,
    FOREIGN KEY (annotation_id)
        REFERENCES annotations(id) ON DELETE RESTRICT,
    CHECK (
        (source_kind = 'source_text'
         AND reviewed_revision_id IS NULL AND annotation_id IS NULL
         AND model_eligible)
        OR (source_kind = 'reviewed_knowledge'
            AND reviewed_revision_id IS NOT NULL AND annotation_id IS NULL
            AND model_eligible)
        OR (source_kind = 'user_annotation'
            AND reviewed_revision_id IS NULL AND annotation_id IS NOT NULL)
    )
);

CREATE TABLE paper_search_chunks (
    generation_id UUID NOT NULL,
    paper_id BIGINT NOT NULL,
    chunk_id TEXT NOT NULL CHECK (chunk_id ~ '^c-[0-9a-f]{32}$'),
    document_id TEXT NOT NULL CHECK (document_id ~ '^d-[0-9a-f]{32}$'),
    selector_version INTEGER NOT NULL CHECK (selector_version >= 1),
    source_kind TEXT NOT NULL CHECK (
        source_kind IN ('source_text', 'reviewed_knowledge', 'user_annotation')
    ),
    source_id TEXT NOT NULL CHECK (char_length(source_id) BETWEEN 1 AND 512),
    reviewed_revision_id UUID,
    annotation_id UUID,
    node_id TEXT NOT NULL CHECK (
        node_id ~ '^n-[0-9a-f]{24}(-([2-9]|[1-9][0-9]+))?$'
    ),
    start_offset INTEGER NOT NULL CHECK (start_offset >= 0),
    end_offset INTEGER NOT NULL CHECK (end_offset > start_offset),
    content TEXT NOT NULL CHECK (
        char_length(content) BETWEEN 1 AND 8000
        AND end_offset - start_offset = char_length(content)
    ),
    lexical_content TEXT NOT NULL
        CHECK (char_length(lexical_content) BETWEEN 1 AND 8000),
    authority_score SMALLINT NOT NULL CHECK (
        (source_kind = 'source_text' AND authority_score = 100)
        OR (source_kind = 'reviewed_knowledge' AND authority_score = 200)
        OR (source_kind = 'user_annotation' AND authority_score = 300)
    ),
    model_eligible BOOLEAN NOT NULL,
    content_sha256 CHAR(64) NOT NULL
        CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    search_vector TSVECTOR GENERATED ALWAYS AS (
        to_tsvector('simple'::regconfig, lexical_content)
    ) STORED,
    PRIMARY KEY (generation_id, chunk_id),
    UNIQUE (
        generation_id, paper_id, chunk_id, source_kind, node_id,
        content_sha256, model_eligible
    ),
    FOREIGN KEY (generation_id, paper_id, document_id, selector_version)
        REFERENCES paper_search_documents(
            generation_id, paper_id, document_id, selector_version
        ) ON DELETE RESTRICT,
    CHECK (
        (source_kind = 'source_text'
         AND reviewed_revision_id IS NULL AND annotation_id IS NULL
         AND model_eligible)
        OR (source_kind = 'reviewed_knowledge'
            AND reviewed_revision_id IS NOT NULL AND annotation_id IS NULL
            AND model_eligible)
        OR (source_kind = 'user_annotation'
            AND reviewed_revision_id IS NULL AND annotation_id IS NOT NULL)
    )
);

CREATE INDEX paper_search_chunks_fts
ON paper_search_chunks USING GIN (search_vector);

CREATE INDEX paper_search_chunks_generation_authority
ON paper_search_chunks (
    generation_id, authority_score DESC, source_kind, source_id, node_id, chunk_id
);

CREATE TABLE paper_search_heads (
    paper_id BIGINT PRIMARY KEY REFERENCES papers(id) ON DELETE RESTRICT,
    generation_id UUID NOT NULL,
    corpus_digest CHAR(64) NOT NULL
        CHECK (corpus_digest ~ '^[0-9a-f]{64}$'),
    source_sha256 CHAR(64) NOT NULL
        CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
    article_fingerprint CHAR(64) NOT NULL
        CHECK (article_fingerprint ~ '^[0-9a-f]{64}$'),
    selector_version INTEGER NOT NULL CHECK (selector_version >= 1),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    FOREIGN KEY (
        generation_id, paper_id, source_sha256,
        article_fingerprint, selector_version
    ) REFERENCES paper_search_generations(
        id, paper_id, source_sha256, article_fingerprint, selector_version
    ) ON DELETE RESTRICT
);

CREATE TABLE paper_qa_jobs (
    id UUID PRIMARY KEY,
    paper_id BIGINT NOT NULL REFERENCES papers(id) ON DELETE RESTRICT,
    idempotency_key_digest CHAR(64) NOT NULL
        CHECK (idempotency_key_digest ~ '^[0-9a-f]{64}$'),
    request_digest CHAR(64) NOT NULL
        CHECK (request_digest ~ '^[0-9a-f]{64}$'),
    question TEXT NOT NULL CHECK (char_length(question) BETWEEN 1 AND 4000),
    provider TEXT NOT NULL CHECK (char_length(provider) BETWEEN 1 AND 80),
    model TEXT NOT NULL CHECK (char_length(model) BETWEEN 1 AND 200),
    request_actor_kind TEXT NOT NULL CHECK (request_actor_kind = 'human'),
    request_actor_id TEXT NOT NULL
        CHECK (
            request_actor_id = btrim(request_actor_id)
            AND char_length(request_actor_id) BETWEEN 1 AND 200
            AND request_actor_id !~ '[[:cntrl:]]'
        ),
    request_acknowledged BOOLEAN NOT NULL CHECK (request_acknowledged),
    include_personal_annotations BOOLEAN NOT NULL DEFAULT FALSE,
    evidence_limit SMALLINT NOT NULL CHECK (evidence_limit BETWEEN 1 AND 100),
    generation_id UUID NOT NULL,
    source_sha256 CHAR(64) NOT NULL
        CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
    article_fingerprint CHAR(64) NOT NULL
        CHECK (article_fingerprint ~ '^[0-9a-f]{64}$'),
    selector_version INTEGER NOT NULL CHECK (selector_version >= 1),
    evidence_snapshot JSONB NOT NULL CHECK (
        jsonb_typeof(evidence_snapshot) = 'object'
        AND evidence_snapshot ?& ARRAY[
            'schema_version', 'selector_version', 'question',
            'source_nodes', 'limits'
        ]
        AND jsonb_typeof(evidence_snapshot -> 'source_nodes') = 'array'
    ),
    evidence_snapshot_sha256 CHAR(64) NOT NULL
        CHECK (evidence_snapshot_sha256 ~ '^[0-9a-f]{64}$'),
    status TEXT NOT NULL DEFAULT 'queued' CHECK (
        status IN ('queued', 'running', 'retry_wait', 'succeeded', 'failed')
    ),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    max_attempts SMALLINT NOT NULL DEFAULT 3 CHECK (max_attempts BETWEEN 1 AND 10),
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    lease_owner TEXT CHECK (
        lease_owner IS NULL OR char_length(lease_owner) BETWEEN 1 AND 200
    ),
    lease_token UUID,
    lease_expires_at TIMESTAMPTZ,
    answer_payload JSONB CHECK (
        answer_payload IS NULL OR jsonb_typeof(answer_payload) = 'object'
    ),
    response_digest CHAR(64) CHECK (
        response_digest IS NULL OR response_digest ~ '^[0-9a-f]{64}$'
    ),
    last_error_code TEXT CHECK (
        last_error_code IS NULL
        OR (char_length(last_error_code) BETWEEN 1 AND 120
            AND last_error_code ~ '^[a-z0-9_]+$')
    ),
    requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    CHECK (
        (status = 'running'
         AND lease_owner IS NOT NULL
         AND lease_token IS NOT NULL
         AND lease_expires_at IS NOT NULL)
        OR (status <> 'running'
            AND lease_owner IS NULL
            AND lease_token IS NULL
            AND lease_expires_at IS NULL)
    ),
    CHECK (
        (status = 'succeeded'
         AND answer_payload IS NOT NULL
         AND response_digest IS NOT NULL
         AND finished_at IS NOT NULL)
        OR (status = 'failed'
            AND answer_payload IS NULL
            AND response_digest IS NULL
            AND finished_at IS NOT NULL)
        OR (status IN ('queued', 'running', 'retry_wait')
            AND answer_payload IS NULL
            AND response_digest IS NULL
            AND finished_at IS NULL)
    ),
    UNIQUE (paper_id, idempotency_key_digest),
    UNIQUE (id, paper_id),
    FOREIGN KEY (
        generation_id, paper_id, source_sha256,
        article_fingerprint, selector_version
    ) REFERENCES paper_search_generations(
        id, paper_id, source_sha256, article_fingerprint, selector_version
    ) ON DELETE RESTRICT
);

CREATE INDEX paper_qa_jobs_claim
ON paper_qa_jobs (next_attempt_at, requested_at, id)
WHERE status IN ('queued', 'retry_wait');

CREATE INDEX paper_qa_jobs_expired_lease
ON paper_qa_jobs (lease_expires_at, id)
WHERE status = 'running';

CREATE FUNCTION reject_paper_qa_job_identity_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'paper QA job identity cannot be deleted'
            USING ERRCODE = '55000';
    END IF;
    IF ROW(
        OLD.id, OLD.paper_id, OLD.idempotency_key_digest,
        OLD.request_digest, OLD.question, OLD.provider, OLD.model,
        OLD.request_actor_kind, OLD.request_actor_id,
        OLD.request_acknowledged, OLD.include_personal_annotations,
        OLD.evidence_limit, OLD.generation_id, OLD.source_sha256,
        OLD.article_fingerprint, OLD.selector_version,
        OLD.evidence_snapshot, OLD.evidence_snapshot_sha256,
        OLD.max_attempts, OLD.requested_at
    ) IS DISTINCT FROM ROW(
        NEW.id, NEW.paper_id, NEW.idempotency_key_digest,
        NEW.request_digest, NEW.question, NEW.provider, NEW.model,
        NEW.request_actor_kind, NEW.request_actor_id,
        NEW.request_acknowledged, NEW.include_personal_annotations,
        NEW.evidence_limit, NEW.generation_id, NEW.source_sha256,
        NEW.article_fingerprint, NEW.selector_version,
        NEW.evidence_snapshot, NEW.evidence_snapshot_sha256,
        NEW.max_attempts, NEW.requested_at
    ) THEN
        RAISE EXCEPTION 'paper QA job identity is immutable'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER paper_qa_jobs_identity_immutable
BEFORE UPDATE OR DELETE ON paper_qa_jobs
FOR EACH ROW EXECUTE FUNCTION reject_paper_qa_job_identity_mutation();

CREATE TABLE paper_qa_job_evidence (
    job_id UUID NOT NULL,
    paper_id BIGINT NOT NULL,
    ordinal SMALLINT NOT NULL CHECK (ordinal BETWEEN 0 AND 99),
    generation_id UUID NOT NULL,
    chunk_id TEXT NOT NULL CHECK (chunk_id ~ '^c-[0-9a-f]{32}$'),
    source_kind TEXT NOT NULL CHECK (
        source_kind IN ('source_text', 'reviewed_knowledge', 'user_annotation')
    ),
    node_id TEXT NOT NULL CHECK (
        node_id ~ '^n-[0-9a-f]{24}(-([2-9]|[1-9][0-9]+))?$'
    ),
    content_sha256 CHAR(64) NOT NULL
        CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    model_eligible BOOLEAN NOT NULL,
    PRIMARY KEY (job_id, ordinal),
    UNIQUE (job_id, chunk_id),
    FOREIGN KEY (job_id, paper_id)
        REFERENCES paper_qa_jobs(id, paper_id) ON DELETE RESTRICT,
    FOREIGN KEY (
        generation_id, paper_id, chunk_id, source_kind, node_id,
        content_sha256, model_eligible
    ) REFERENCES paper_search_chunks(
        generation_id, paper_id, chunk_id, source_kind, node_id,
        content_sha256, model_eligible
    ) ON DELETE RESTRICT,
    CHECK (model_eligible)
);

CREATE INDEX paper_qa_job_evidence_generation
ON paper_qa_job_evidence (generation_id, chunk_id);

CREATE FUNCTION validate_paper_qa_evidence_insert()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    annotations_allowed BOOLEAN;
BEGIN
    SELECT include_personal_annotations INTO annotations_allowed
    FROM paper_qa_jobs
    WHERE id = NEW.job_id AND paper_id = NEW.paper_id;
    IF annotations_allowed IS NULL THEN
        RAISE EXCEPTION 'paper QA job missing for evidence'
            USING ERRCODE = '23503';
    END IF;
    IF NEW.source_kind = 'user_annotation' AND NOT annotations_allowed THEN
        RAISE EXCEPTION 'paper QA annotation evidence requires explicit opt-in'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER paper_qa_job_evidence_validate
BEFORE INSERT ON paper_qa_job_evidence
FOR EACH ROW EXECUTE FUNCTION validate_paper_qa_evidence_insert();

CREATE TABLE paper_qa_usage_ledger (
    id UUID PRIMARY KEY,
    job_id UUID NOT NULL REFERENCES paper_qa_jobs(id) ON DELETE RESTRICT,
    attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
    provider TEXT NOT NULL CHECK (char_length(provider) BETWEEN 1 AND 80),
    model TEXT NOT NULL CHECK (char_length(model) BETWEEN 1 AND 200),
    request_digest CHAR(64) NOT NULL
        CHECK (request_digest ~ '^[0-9a-f]{64}$'),
    response_digest CHAR(64) NOT NULL
        CHECK (response_digest ~ '^[0-9a-f]{64}$'),
    prompt_tokens BIGINT NOT NULL CHECK (prompt_tokens >= 0),
    completion_tokens BIGINT NOT NULL CHECK (completion_tokens >= 0),
    total_tokens BIGINT NOT NULL CHECK (
        total_tokens >= 0 AND total_tokens = prompt_tokens + completion_tokens
    ),
    cost_microusd BIGINT CHECK (cost_microusd IS NULL OR cost_microusd >= 0),
    cost_basis TEXT NOT NULL CHECK (
        cost_basis IN ('unavailable', 'provider-reported', 'reviewed-price-table')
    ),
    pricing_version TEXT CHECK (
        pricing_version IS NULL OR char_length(pricing_version) BETWEEN 1 AND 120
    ),
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (
        (cost_basis = 'unavailable'
         AND cost_microusd IS NULL AND pricing_version IS NULL)
        OR (cost_basis = 'provider-reported'
            AND cost_microusd IS NOT NULL AND pricing_version IS NULL)
        OR (cost_basis = 'reviewed-price-table'
            AND cost_microusd IS NOT NULL AND pricing_version IS NOT NULL)
    ),
    UNIQUE (job_id, attempt_number)
);

CREATE FUNCTION reject_paper_search_append_only_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'paper search history is append-only'
        USING ERRCODE = '55000';
END;
$$;

CREATE TRIGGER paper_search_generations_immutable
BEFORE UPDATE OR DELETE ON paper_search_generations
FOR EACH ROW EXECUTE FUNCTION reject_paper_search_append_only_mutation();

CREATE TRIGGER paper_search_documents_immutable
BEFORE UPDATE OR DELETE ON paper_search_documents
FOR EACH ROW EXECUTE FUNCTION reject_paper_search_append_only_mutation();

CREATE TRIGGER paper_search_chunks_immutable
BEFORE UPDATE OR DELETE ON paper_search_chunks
FOR EACH ROW EXECUTE FUNCTION reject_paper_search_append_only_mutation();

CREATE TRIGGER paper_qa_job_evidence_immutable
BEFORE UPDATE OR DELETE ON paper_qa_job_evidence
FOR EACH ROW EXECUTE FUNCTION reject_paper_search_append_only_mutation();

CREATE TRIGGER paper_qa_usage_ledger_immutable
BEFORE UPDATE OR DELETE ON paper_qa_usage_ledger
FOR EACH ROW EXECUTE FUNCTION reject_paper_search_append_only_mutation();
"""

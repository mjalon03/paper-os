"""Migration 005: reviewed paper-knowledge jobs, revisions, review, and outbox."""

SQL = r"""
CREATE TABLE paper_knowledge_jobs (
    id UUID PRIMARY KEY,
    paper_id BIGINT NOT NULL REFERENCES papers(id) ON DELETE RESTRICT,
    idempotency_key_digest CHAR(64) NOT NULL
        CHECK (idempotency_key_digest ~ '^[0-9a-f]{64}$'),
    request_digest CHAR(64) NOT NULL
        CHECK (request_digest ~ '^[0-9a-f]{64}$'),
    provider TEXT NOT NULL CHECK (char_length(provider) BETWEEN 1 AND 80),
    model TEXT NOT NULL CHECK (char_length(model) BETWEEN 1 AND 200),
    request_actor_kind TEXT NOT NULL CHECK (request_actor_kind = 'human'),
    request_actor_id TEXT NOT NULL
        CHECK (
            request_actor_id = btrim(request_actor_id)
            AND char_length(request_actor_id) BETWEEN 1 AND 200
            AND request_actor_id !~ '[[:cntrl:]]'
        ),
    paper_text_and_cost_acknowledged BOOLEAN NOT NULL
        CHECK (paper_text_and_cost_acknowledged),
    include_personal_annotations BOOLEAN NOT NULL DEFAULT FALSE,
    max_source_pages SMALLINT NOT NULL CHECK (max_source_pages BETWEEN 1 AND 8),
    max_source_characters INTEGER NOT NULL
        CHECK (max_source_characters BETWEEN 1 AND 30000),
    max_output_tokens INTEGER NOT NULL CHECK (max_output_tokens BETWEEN 1 AND 8192),
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'retry_wait', 'succeeded', 'failed')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    max_attempts SMALLINT NOT NULL DEFAULT 3 CHECK (max_attempts BETWEEN 1 AND 10),
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    lease_owner TEXT CHECK (
        lease_owner IS NULL OR char_length(lease_owner) BETWEEN 1 AND 200
    ),
    lease_token UUID,
    lease_expires_at TIMESTAMPTZ,
    generation_request_digest CHAR(64) CHECK (
        generation_request_digest IS NULL
        OR generation_request_digest ~ '^[0-9a-f]{64}$'
    ),
    generation_revision_id UUID,
    generation_source_node_ids JSONB CHECK (
        generation_source_node_ids IS NULL
        OR jsonb_typeof(generation_source_node_ids) = 'array'
    ),
    generation_annotation_ids JSONB CHECK (
        generation_annotation_ids IS NULL
        OR jsonb_typeof(generation_annotation_ids) = 'array'
    ),
    result_revision_id UUID,
    last_error_code TEXT CHECK (
        last_error_code IS NULL OR char_length(last_error_code) BETWEEN 1 AND 120
    ),
    requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    CHECK (
        (status = 'running'
         AND lease_owner IS NOT NULL
         AND lease_token IS NOT NULL
         AND lease_expires_at IS NOT NULL)
        OR
        (status <> 'running'
         AND lease_owner IS NULL
         AND lease_token IS NULL
         AND lease_expires_at IS NULL)
    ),
    CHECK (
        (status IN ('succeeded', 'failed') AND finished_at IS NOT NULL)
        OR (status NOT IN ('succeeded', 'failed') AND finished_at IS NULL)
    ),
    CHECK (
        (status = 'succeeded' AND result_revision_id IS NOT NULL)
        OR (status <> 'succeeded' AND result_revision_id IS NULL)
    ),
    CHECK (
        (generation_request_digest IS NULL
         AND generation_revision_id IS NULL
         AND generation_source_node_ids IS NULL
         AND generation_annotation_ids IS NULL)
        OR
        (generation_request_digest IS NOT NULL
         AND generation_revision_id IS NOT NULL
         AND generation_source_node_ids IS NOT NULL
         AND generation_annotation_ids IS NOT NULL
         AND status IN ('running', 'succeeded'))
    ),
    UNIQUE (paper_id, idempotency_key_digest),
    UNIQUE (id, paper_id)
);

CREATE INDEX paper_knowledge_jobs_claim
ON paper_knowledge_jobs (next_attempt_at, requested_at, id)
WHERE status IN ('queued', 'retry_wait');

CREATE INDEX paper_knowledge_jobs_expired_lease
ON paper_knowledge_jobs (lease_expires_at, id)
WHERE status = 'running';

CREATE FUNCTION reject_paper_knowledge_job_identity_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'paper knowledge job identity cannot be deleted'
            USING ERRCODE = '55000';
    END IF;
    IF ROW(
        OLD.id, OLD.paper_id, OLD.idempotency_key_digest,
        OLD.request_digest, OLD.provider, OLD.model,
        OLD.request_actor_kind, OLD.request_actor_id,
        OLD.paper_text_and_cost_acknowledged,
        OLD.include_personal_annotations, OLD.max_source_pages,
        OLD.max_source_characters, OLD.max_output_tokens,
        OLD.max_attempts, OLD.requested_at
    ) IS DISTINCT FROM ROW(
        NEW.id, NEW.paper_id, NEW.idempotency_key_digest,
        NEW.request_digest, NEW.provider, NEW.model,
        NEW.request_actor_kind, NEW.request_actor_id,
        NEW.paper_text_and_cost_acknowledged,
        NEW.include_personal_annotations, NEW.max_source_pages,
        NEW.max_source_characters, NEW.max_output_tokens,
        NEW.max_attempts, NEW.requested_at
    ) THEN
        RAISE EXCEPTION 'paper knowledge job identity is immutable'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER paper_knowledge_jobs_identity_immutable
BEFORE UPDATE OR DELETE ON paper_knowledge_jobs
FOR EACH ROW EXECUTE FUNCTION reject_paper_knowledge_job_identity_mutation();

CREATE TABLE paper_knowledge_revisions (
    id UUID PRIMARY KEY,
    paper_id BIGINT NOT NULL REFERENCES papers(id) ON DELETE RESTRICT,
    revision_number BIGINT NOT NULL CHECK (revision_number >= 1),
    parent_revision_id UUID,
    source_job_id UUID,
    status TEXT NOT NULL CHECK (status IN ('ai-draft', 'reviewed')),
    canonical_payload JSONB NOT NULL
        CHECK (jsonb_typeof(canonical_payload) = 'object'),
    canonical_payload_sha256 CHAR(64) NOT NULL
        CHECK (canonical_payload_sha256 ~ '^[0-9a-f]{64}$'),
    source_sha256 CHAR(64) NOT NULL
        CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
    article_fingerprint CHAR(64) NOT NULL
        CHECK (article_fingerprint ~ '^[0-9a-f]{64}$'),
    selector_version INTEGER NOT NULL CHECK (selector_version >= 1),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (
        canonical_payload ?& ARRAY[
            'schema_version', 'selector_version', 'revision_id', 'status',
            'paper_identity', 'source_sha256', 'article_fingerprint',
            'summary', 'claims', 'key_methods', 'key_findings', 'limitations'
        ]
    ),
    CHECK (
        jsonb_typeof(canonical_payload -> 'schema_version') = 'number'
        AND (canonical_payload ->> 'schema_version')::integer = 1
    ),
    CHECK (
        jsonb_typeof(canonical_payload -> 'revision_id') = 'string'
        AND canonical_payload ->> 'revision_id' = id::text
    ),
    CHECK (
        jsonb_typeof(canonical_payload -> 'status') = 'string'
        AND canonical_payload ->> 'status' = status
    ),
    CHECK (
        jsonb_typeof(canonical_payload -> 'source_sha256') = 'string'
        AND canonical_payload ->> 'source_sha256' = source_sha256
    ),
    CHECK (
        jsonb_typeof(canonical_payload -> 'article_fingerprint') = 'string'
        AND canonical_payload ->> 'article_fingerprint' = article_fingerprint
    ),
    CHECK (
        jsonb_typeof(canonical_payload -> 'selector_version') = 'number'
        AND (canonical_payload ->> 'selector_version')::integer = selector_version
    ),
    CHECK (
        jsonb_typeof(canonical_payload -> 'paper_identity') = 'object'
        AND jsonb_typeof(canonical_payload #> '{paper_identity,paper_id}') = 'number'
        AND canonical_payload #>> '{paper_identity,paper_id}' = paper_id::text
    ),
    UNIQUE (paper_id, revision_number),
    UNIQUE (paper_id, canonical_payload_sha256),
    UNIQUE (id, paper_id),
    UNIQUE (id, status),
    UNIQUE (id, canonical_payload_sha256, status),
    UNIQUE (id, paper_id, canonical_payload_sha256, status),
    FOREIGN KEY (parent_revision_id, paper_id)
        REFERENCES paper_knowledge_revisions(id, paper_id) ON DELETE RESTRICT,
    FOREIGN KEY (source_job_id, paper_id)
        REFERENCES paper_knowledge_jobs(id, paper_id) ON DELETE RESTRICT
);

ALTER TABLE paper_knowledge_jobs
ADD CONSTRAINT paper_knowledge_jobs_result_revision_fk
FOREIGN KEY (result_revision_id, paper_id)
REFERENCES paper_knowledge_revisions(id, paper_id) ON DELETE RESTRICT;

CREATE INDEX paper_knowledge_revisions_paper_created
ON paper_knowledge_revisions (paper_id, revision_number DESC);

CREATE UNIQUE INDEX paper_knowledge_revisions_source_job_once
ON paper_knowledge_revisions (source_job_id)
WHERE source_job_id IS NOT NULL;

CREATE TABLE paper_knowledge_review_heads (
    paper_id BIGINT PRIMARY KEY REFERENCES papers(id) ON DELETE RESTRICT,
    review_revision BIGINT NOT NULL DEFAULT 0 CHECK (review_revision >= 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE paper_knowledge_review_events (
    id UUID PRIMARY KEY,
    paper_id BIGINT NOT NULL REFERENCES papers(id) ON DELETE RESTRICT,
    review_revision BIGINT NOT NULL CHECK (review_revision >= 1),
    expected_revision BIGINT NOT NULL CHECK (expected_revision >= 0),
    subject_revision_id UUID NOT NULL,
    result_revision_id UUID,
    decision TEXT NOT NULL CHECK (decision IN ('approve', 'reject')),
    actor_kind TEXT NOT NULL CHECK (actor_kind IN ('human', 'service')),
    actor_id TEXT NOT NULL CHECK (char_length(actor_id) BETWEEN 1 AND 200),
    comment TEXT CHECK (comment IS NULL OR char_length(comment) <= 4000),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (review_revision = expected_revision + 1),
    CHECK (
        (decision = 'approve'
         AND result_revision_id IS NOT NULL
         AND result_revision_id <> subject_revision_id)
        OR (decision = 'reject' AND result_revision_id IS NULL)
    ),
    FOREIGN KEY (subject_revision_id, paper_id)
        REFERENCES paper_knowledge_revisions(id, paper_id) ON DELETE RESTRICT,
    FOREIGN KEY (result_revision_id, paper_id)
        REFERENCES paper_knowledge_revisions(id, paper_id) ON DELETE RESTRICT,
    UNIQUE (paper_id, review_revision),
    UNIQUE (subject_revision_id),
    UNIQUE (id, result_revision_id, decision),
    UNIQUE (id, paper_id, result_revision_id, decision)
);

CREATE UNIQUE INDEX paper_knowledge_review_result_once
ON paper_knowledge_review_events (result_revision_id)
WHERE result_revision_id IS NOT NULL;

CREATE INDEX paper_knowledge_review_events_paper_created
ON paper_knowledge_review_events (paper_id, review_revision DESC);

CREATE TABLE paper_knowledge_outbox (
    id UUID PRIMARY KEY,
    paper_id BIGINT NOT NULL REFERENCES papers(id) ON DELETE RESTRICT,
    revision_id UUID NOT NULL,
    revision_status TEXT NOT NULL DEFAULT 'reviewed'
        CHECK (revision_status = 'reviewed'),
    revision_payload_sha256 CHAR(64) NOT NULL
        CHECK (revision_payload_sha256 ~ '^[0-9a-f]{64}$'),
    approval_event_id UUID NOT NULL,
    approval_decision TEXT NOT NULL DEFAULT 'approve'
        CHECK (approval_decision = 'approve'),
    dedupe_key CHAR(64) NOT NULL UNIQUE
        CHECK (dedupe_key ~ '^[0-9a-f]{64}$'),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'leased', 'delivered', 'dead')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    max_attempts SMALLINT NOT NULL DEFAULT 10 CHECK (max_attempts BETWEEN 1 AND 20),
    available_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    lease_owner TEXT CHECK (
        lease_owner IS NULL OR char_length(lease_owner) BETWEEN 1 AND 200
    ),
    lease_token UUID,
    lease_expires_at TIMESTAMPTZ,
    last_error_code TEXT CHECK (
        last_error_code IS NULL OR char_length(last_error_code) BETWEEN 1 AND 120
    ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    delivered_at TIMESTAMPTZ,
    CHECK (
        (status = 'leased'
         AND lease_owner IS NOT NULL
         AND lease_token IS NOT NULL
         AND lease_expires_at IS NOT NULL)
        OR
        (status <> 'leased'
         AND lease_owner IS NULL
         AND lease_token IS NULL
         AND lease_expires_at IS NULL)
    ),
    CHECK (
        (status = 'delivered' AND delivered_at IS NOT NULL)
        OR (status <> 'delivered' AND delivered_at IS NULL)
    ),
    FOREIGN KEY (
        revision_id, paper_id, revision_payload_sha256, revision_status
    )
        REFERENCES paper_knowledge_revisions(
            id, paper_id, canonical_payload_sha256, status
        ) ON DELETE RESTRICT,
    FOREIGN KEY (
        approval_event_id, paper_id, revision_id, approval_decision
    )
        REFERENCES paper_knowledge_review_events(
            id, paper_id, result_revision_id, decision
        ) ON DELETE RESTRICT,
    UNIQUE (revision_id)
);

CREATE INDEX paper_knowledge_outbox_claim
ON paper_knowledge_outbox (available_at, created_at, id)
WHERE status = 'pending';

CREATE INDEX paper_knowledge_outbox_expired_lease
ON paper_knowledge_outbox (lease_expires_at, id)
WHERE status = 'leased';

CREATE TABLE paper_knowledge_usage_ledger (
    id UUID PRIMARY KEY,
    job_id UUID NOT NULL REFERENCES paper_knowledge_jobs(id) ON DELETE RESTRICT,
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
        total_tokens >= 0
        AND total_tokens = prompt_tokens + completion_tokens
    ),
    cost_microusd BIGINT CHECK (cost_microusd IS NULL OR cost_microusd >= 0),
    cost_basis TEXT NOT NULL CHECK (
        cost_basis IN (
            'unavailable', 'provider-reported', 'reviewed-price-table'
        )
    ),
    pricing_version TEXT CHECK (
        pricing_version IS NULL OR char_length(pricing_version) BETWEEN 1 AND 120
    ),
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (
        (cost_basis = 'unavailable'
         AND cost_microusd IS NULL
         AND pricing_version IS NULL)
        OR
        (cost_basis = 'provider-reported'
         AND cost_microusd IS NOT NULL
         AND pricing_version IS NULL)
        OR
        (cost_basis = 'reviewed-price-table'
         AND cost_microusd IS NOT NULL
         AND pricing_version IS NOT NULL)
    ),
    UNIQUE (job_id, attempt_number)
);

CREATE FUNCTION validate_paper_knowledge_review_event()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    subject_status TEXT;
    result_status TEXT;
    result_parent UUID;
BEGIN
    SELECT status INTO subject_status
    FROM paper_knowledge_revisions
    WHERE id = NEW.subject_revision_id AND paper_id = NEW.paper_id;
    IF subject_status IS DISTINCT FROM 'ai-draft' THEN
        RAISE EXCEPTION 'paper knowledge review subject must be an AI draft'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.decision = 'approve' THEN
        IF NEW.actor_kind <> 'human' THEN
            RAISE EXCEPTION 'paper knowledge approval requires a human actor'
                USING ERRCODE = '23514';
        END IF;
        SELECT status, parent_revision_id INTO result_status, result_parent
        FROM paper_knowledge_revisions
        WHERE id = NEW.result_revision_id AND paper_id = NEW.paper_id;
        IF result_status IS DISTINCT FROM 'reviewed'
           OR result_parent IS DISTINCT FROM NEW.subject_revision_id THEN
            RAISE EXCEPTION 'approved result must be a new reviewed child revision'
                USING ERRCODE = '23514';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER paper_knowledge_review_event_validate
BEFORE INSERT ON paper_knowledge_review_events
FOR EACH ROW EXECUTE FUNCTION validate_paper_knowledge_review_event();

CREATE FUNCTION reject_paper_knowledge_immutable_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'paper knowledge history is append-only'
        USING ERRCODE = '55000';
END;
$$;

CREATE TRIGGER paper_knowledge_revisions_immutable
BEFORE UPDATE OR DELETE ON paper_knowledge_revisions
FOR EACH ROW EXECUTE FUNCTION reject_paper_knowledge_immutable_mutation();

CREATE TRIGGER paper_knowledge_review_events_immutable
BEFORE UPDATE OR DELETE ON paper_knowledge_review_events
FOR EACH ROW EXECUTE FUNCTION reject_paper_knowledge_immutable_mutation();

CREATE TRIGGER paper_knowledge_usage_ledger_immutable
BEFORE UPDATE OR DELETE ON paper_knowledge_usage_ledger
FOR EACH ROW EXECUTE FUNCTION reject_paper_knowledge_immutable_mutation();
"""

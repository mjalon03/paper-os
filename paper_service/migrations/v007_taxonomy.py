"""Migration 007: reviewed adaptive paper taxonomy and journaled move jobs."""

SQL = r"""
CREATE TABLE paper_taxonomy_audits (
    id UUID PRIMARY KEY,
    category TEXT NOT NULL CHECK (char_length(category) BETWEEN 1 AND 120),
    category_path TEXT NOT NULL CHECK (
        char_length(category_path) BETWEEN 1 AND 1500
        AND category_path ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}:[^/].*$'
        AND position(chr(92) in category_path) = 0
        AND category_path !~ '(^|/)\.\.?(/|$)'
        AND category_path !~ '//'
        AND category_path !~ '(^|/)(19|20|21)[0-9]{2}(/|$)'
    ),
    source_snapshot JSONB NOT NULL CHECK (
        jsonb_typeof(source_snapshot) = 'object'
        AND source_snapshot ->> 'schema' = 'paper-taxonomy-source-snapshot'
        AND (source_snapshot ->> 'schema_version')::integer = 1
        AND source_snapshot ->> 'category' = category
        AND source_snapshot ->> 'category_path' = category_path
        AND jsonb_typeof(source_snapshot -> 'papers') = 'array'
    ),
    source_snapshot_sha256 CHAR(64) NOT NULL
        CHECK (source_snapshot_sha256 ~ '^[0-9a-f]{64}$'),
    paper_count INTEGER NOT NULL CHECK (
        paper_count BETWEEN 0 AND 1000
        AND paper_count = jsonb_array_length(source_snapshot -> 'papers')
    ),
    new_paper_count INTEGER NOT NULL CHECK (
        new_paper_count BETWEEN 0 AND paper_count
    ),
    metrics JSONB NOT NULL CHECK (
        jsonb_typeof(metrics) = 'object'
        AND metrics ?& ARRAY[
            'paper_count', 'new_paper_count', 'days_since_checkpoint',
            'max_leaf_size', 'leaf_count', 'overcrowding_ratio',
            'heterogeneity'
        ]
        AND (metrics ->> 'paper_count')::integer = paper_count
        AND (metrics ->> 'new_paper_count')::integer = new_paper_count
    ),
    audit_policy JSONB NOT NULL CHECK (
        jsonb_typeof(audit_policy) = 'object'
        AND audit_policy ?& ARRAY[
            'min_category_papers', 'min_new_papers',
            'max_audit_age_days', 'max_leaf_papers',
            'heterogeneity_threshold', 'min_subtheme_size',
            'max_subthemes', 'max_other_fraction'
        ]
    ),
    decision TEXT NOT NULL CHECK (decision IN ('no-op', 'request-proposal')),
    reason_codes JSONB NOT NULL CHECK (jsonb_typeof(reason_codes) = 'array'),
    evaluated_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX paper_taxonomy_audits_category_evaluated
ON paper_taxonomy_audits (category, evaluated_at DESC, id);

CREATE TABLE paper_taxonomy_generation_jobs (
    id UUID PRIMARY KEY,
    audit_id UUID NOT NULL UNIQUE
        REFERENCES paper_taxonomy_audits(id) ON DELETE RESTRICT,
    proposal_id UUID NOT NULL UNIQUE,
    provider TEXT NOT NULL DEFAULT 'deepseek' CHECK (provider = 'deepseek'),
    model TEXT NOT NULL DEFAULT 'deepseek-v4-pro'
        CHECK (model = 'deepseek-v4-pro'),
    request_actor_kind TEXT NOT NULL CHECK (request_actor_kind = 'human'),
    request_actor_id TEXT NOT NULL CHECK (
        request_actor_id = btrim(request_actor_id)
        AND char_length(request_actor_id) BETWEEN 1 AND 200
        AND request_actor_id !~ '[[:cntrl:]]'
    ),
    metadata_and_cost_acknowledged BOOLEAN NOT NULL
        CHECK (metadata_and_cost_acknowledged),
    provider_body_digest CHAR(64) NOT NULL
        CHECK (provider_body_digest ~ '^[0-9a-f]{64}$'),
    request_digest CHAR(64) NOT NULL
        CHECK (request_digest ~ '^[0-9a-f]{64}$'),
    max_output_tokens INTEGER NOT NULL CHECK (
        max_output_tokens BETWEEN 1 AND 16384
    ),
    status TEXT NOT NULL DEFAULT 'queued' CHECK (
        status IN ('queued', 'running', 'retry_wait', 'succeeded', 'failed')
    ),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    max_attempts SMALLINT NOT NULL DEFAULT 3
        CHECK (max_attempts BETWEEN 1 AND 10),
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    lease_owner TEXT CHECK (
        lease_owner IS NULL OR char_length(lease_owner) BETWEEN 1 AND 200
    ),
    lease_token UUID,
    lease_expires_at TIMESTAMPTZ,
    result_proposal_id UUID,
    last_error_code TEXT CHECK (
        last_error_code IS NULL
        OR (char_length(last_error_code) BETWEEN 1 AND 120
            AND last_error_code ~ '^[a-z][a-z0-9_]*$')
    ),
    requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    CHECK (
        (status = 'running' AND lease_owner IS NOT NULL
         AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)
        OR
        (status <> 'running' AND lease_owner IS NULL
         AND lease_token IS NULL AND lease_expires_at IS NULL)
    ),
    CHECK (
        (status = 'succeeded' AND result_proposal_id IS NOT NULL
         AND finished_at IS NOT NULL)
        OR
        (status = 'failed' AND result_proposal_id IS NULL
         AND finished_at IS NOT NULL)
        OR
        (status IN ('queued', 'running', 'retry_wait')
         AND result_proposal_id IS NULL AND finished_at IS NULL)
    )
);

CREATE INDEX paper_taxonomy_generation_claim
ON paper_taxonomy_generation_jobs (next_attempt_at, requested_at, id)
WHERE status IN ('queued', 'retry_wait');

CREATE INDEX paper_taxonomy_generation_expired
ON paper_taxonomy_generation_jobs (lease_expires_at, id)
WHERE status = 'running';

CREATE TABLE paper_taxonomy_proposals (
    id UUID PRIMARY KEY,
    audit_id UUID NOT NULL UNIQUE
        REFERENCES paper_taxonomy_audits(id) ON DELETE RESTRICT,
    generation_job_id UUID NOT NULL UNIQUE
        REFERENCES paper_taxonomy_generation_jobs(id) ON DELETE RESTRICT,
    category TEXT NOT NULL CHECK (char_length(category) BETWEEN 1 AND 120),
    category_path TEXT NOT NULL CHECK (char_length(category_path) BETWEEN 1 AND 1500),
    source_snapshot_sha256 CHAR(64) NOT NULL
        CHECK (source_snapshot_sha256 ~ '^[0-9a-f]{64}$'),
    canonical_payload JSONB NOT NULL CHECK (
        jsonb_typeof(canonical_payload) = 'object'
        AND canonical_payload ->> 'schema' = 'paper-taxonomy-proposal'
        AND (canonical_payload ->> 'schema_version')::integer = 1
        AND canonical_payload ->> 'proposal_id' = id::text
        AND canonical_payload ->> 'category' = category
        AND canonical_payload ->> 'category_path' = category_path
        AND canonical_payload ->> 'source_snapshot_sha256' = source_snapshot_sha256
        AND canonical_payload ->> 'proposal_payload_sha256' =
            canonical_payload_sha256
        AND canonical_payload ->> 'status' = 'ai-draft'
        AND jsonb_typeof(canonical_payload -> 'subthemes') = 'array'
        AND jsonb_array_length(canonical_payload -> 'subthemes') BETWEEN 2 AND 24
        AND jsonb_typeof(canonical_payload -> 'assignments') = 'array'
    ),
    canonical_payload_sha256 CHAR(64) NOT NULL
        CHECK (canonical_payload_sha256 ~ '^[0-9a-f]{64}$'),
    provider TEXT NOT NULL DEFAULT 'deepseek' CHECK (provider = 'deepseek'),
    model TEXT NOT NULL DEFAULT 'deepseek-v4-pro'
        CHECK (model = 'deepseek-v4-pro'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (id, source_snapshot_sha256)
);

ALTER TABLE paper_taxonomy_generation_jobs
ADD CONSTRAINT paper_taxonomy_generation_result_fk
FOREIGN KEY (result_proposal_id)
REFERENCES paper_taxonomy_proposals(id) ON DELETE RESTRICT;

CREATE TABLE paper_taxonomy_proposal_assignments (
    proposal_id UUID NOT NULL
        REFERENCES paper_taxonomy_proposals(id) ON DELETE RESTRICT,
    ordinal INTEGER NOT NULL CHECK (ordinal BETWEEN 0 AND 999),
    paper_id BIGINT NOT NULL REFERENCES papers(id) ON DELETE RESTRICT,
    source_sha256 CHAR(64) NOT NULL CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
    old_drive_path TEXT NOT NULL CHECK (
        char_length(old_drive_path) BETWEEN 1 AND 1500
        AND old_drive_path ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}:[^/].*$'
        AND position(chr(92) in old_drive_path) = 0
        AND old_drive_path !~ '(^|/)\.\.?(/|$)'
        AND old_drive_path !~ '//'
    ),
    new_drive_path TEXT NOT NULL CHECK (
        char_length(new_drive_path) BETWEEN 1 AND 1500
        AND new_drive_path ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}:[^/].*$'
        AND position(chr(92) in new_drive_path) = 0
        AND new_drive_path !~ '(^|/)\.\.?(/|$)'
        AND new_drive_path !~ '//'
    ),
    subtheme TEXT NOT NULL CHECK (
        char_length(subtheme) BETWEEN 1 AND 80
        AND subtheme = btrim(subtheme)
        AND position('/' in subtheme) = 0
        AND position(chr(92) in subtheme) = 0
        AND position(':' in subtheme) = 0
        AND subtheme NOT IN ('.', '..')
        AND subtheme !~ '^(19|20|21)[0-9]{2}$'
        AND lower(subtheme) NOT IN (
            'misc', 'miscellaneous', 'uncategorized', 'unclassified',
            'general', '기타', '미분류'
        )
    ),
    move_job_id UUID NOT NULL UNIQUE,
    idempotency_key_digest CHAR(64) NOT NULL UNIQUE
        CHECK (idempotency_key_digest ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (proposal_id, paper_id),
    UNIQUE (proposal_id, ordinal),
    UNIQUE (proposal_id, new_drive_path)
);

CREATE INDEX paper_taxonomy_assignments_paper
ON paper_taxonomy_proposal_assignments (paper_id, proposal_id);

CREATE TABLE paper_taxonomy_review_heads (
    proposal_id UUID PRIMARY KEY
        REFERENCES paper_taxonomy_proposals(id) ON DELETE RESTRICT,
    review_revision BIGINT NOT NULL DEFAULT 0 CHECK (review_revision >= 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE paper_taxonomy_review_events (
    id UUID PRIMARY KEY,
    proposal_id UUID NOT NULL
        REFERENCES paper_taxonomy_proposals(id) ON DELETE RESTRICT,
    review_revision BIGINT NOT NULL CHECK (review_revision >= 1),
    expected_revision BIGINT NOT NULL CHECK (expected_revision >= 0),
    decision TEXT NOT NULL CHECK (decision IN ('approve', 'reject')),
    actor_kind TEXT NOT NULL CHECK (actor_kind = 'human'),
    actor_id TEXT NOT NULL CHECK (char_length(actor_id) BETWEEN 1 AND 200),
    comment TEXT CHECK (comment IS NULL OR char_length(comment) <= 4000),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (review_revision = expected_revision + 1),
    UNIQUE (proposal_id),
    UNIQUE (proposal_id, review_revision),
    UNIQUE (id, proposal_id, decision)
);

CREATE TABLE paper_taxonomy_move_jobs (
    id UUID PRIMARY KEY,
    direction TEXT NOT NULL CHECK (direction IN ('apply', 'rollback')),
    proposal_id UUID REFERENCES paper_taxonomy_proposals(id) ON DELETE RESTRICT,
    rollback_proposal_id UUID,
    approval_event_id UUID,
    approval_decision TEXT,
    rollback_approval_event_id UUID,
    rollback_approval_decision TEXT,
    paper_id BIGINT NOT NULL REFERENCES papers(id) ON DELETE RESTRICT,
    source_sha256 CHAR(64) NOT NULL CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
    source_drive_file_id TEXT CHECK (
        source_drive_file_id IS NULL
        OR (
            char_length(source_drive_file_id) BETWEEN 10 AND 256
            AND source_drive_file_id ~ '^[A-Za-z0-9_-]+$'
        )
    ),
    old_drive_path TEXT NOT NULL CHECK (
        char_length(old_drive_path) BETWEEN 1 AND 1500
        AND old_drive_path ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}:[^/].*$'
        AND position(chr(92) in old_drive_path) = 0
        AND old_drive_path !~ '(^|/)\.\.?(/|$)'
        AND old_drive_path !~ '//'
    ),
    new_drive_path TEXT NOT NULL CHECK (
        char_length(new_drive_path) BETWEEN 1 AND 1500
        AND new_drive_path <> old_drive_path
        AND new_drive_path ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}:[^/].*$'
        AND position(chr(92) in new_drive_path) = 0
        AND new_drive_path !~ '(^|/)\.\.?(/|$)'
        AND new_drive_path !~ '//'
    ),
    idempotency_key_digest CHAR(64) NOT NULL UNIQUE
        CHECK (idempotency_key_digest ~ '^[0-9a-f]{64}$'),
    status TEXT NOT NULL DEFAULT 'queued' CHECK (
        status IN ('queued', 'running', 'retry_wait', 'succeeded', 'failed')
    ),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    max_attempts SMALLINT NOT NULL DEFAULT 5
        CHECK (max_attempts BETWEEN 1 AND 10),
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    lease_owner TEXT CHECK (
        lease_owner IS NULL OR char_length(lease_owner) BETWEEN 1 AND 200
    ),
    lease_token UUID,
    lease_expires_at TIMESTAMPTZ,
    last_error_code TEXT CHECK (
        last_error_code IS NULL
        OR (char_length(last_error_code) BETWEEN 1 AND 120
            AND last_error_code ~ '^[a-z][a-z0-9_]*$')
    ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    CHECK (
        (direction = 'apply' AND proposal_id IS NOT NULL
         AND approval_event_id IS NOT NULL
         AND approval_decision = 'approve'
         AND rollback_proposal_id IS NULL
         AND rollback_approval_event_id IS NULL
         AND rollback_approval_decision IS NULL)
        OR
        (direction = 'rollback' AND proposal_id IS NULL
         AND approval_event_id IS NULL
         AND approval_decision IS NULL
         AND rollback_proposal_id IS NOT NULL
         AND rollback_approval_event_id IS NOT NULL
         AND rollback_approval_decision = 'approve')
    ),
    CHECK (
        (status = 'running' AND lease_owner IS NOT NULL
         AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)
        OR
        (status <> 'running' AND lease_owner IS NULL
         AND lease_token IS NULL AND lease_expires_at IS NULL)
    ),
    CHECK (
        (status IN ('succeeded', 'failed') AND finished_at IS NOT NULL)
        OR (status NOT IN ('succeeded', 'failed') AND finished_at IS NULL)
    ),
    CHECK (status <> 'succeeded' OR source_drive_file_id IS NOT NULL),
    CHECK (
        direction = 'rollback'
        OR new_drive_path !~ '(^|/)(19|20|21)[0-9]{2}/'
    ),
    FOREIGN KEY (approval_event_id, proposal_id, approval_decision)
        REFERENCES paper_taxonomy_review_events(id, proposal_id, decision)
        ON DELETE RESTRICT,
    UNIQUE (
        id, paper_id, source_sha256, source_drive_file_id,
        old_drive_path, new_drive_path, direction
    )
);

CREATE INDEX paper_taxonomy_move_claim
ON paper_taxonomy_move_jobs (next_attempt_at, created_at, id)
WHERE status IN ('queued', 'retry_wait');

CREATE INDEX paper_taxonomy_move_expired
ON paper_taxonomy_move_jobs (lease_expires_at, id)
WHERE status = 'running';

CREATE INDEX paper_taxonomy_move_paper
ON paper_taxonomy_move_jobs (paper_id, created_at DESC, id);

CREATE TABLE paper_taxonomy_move_journal (
    id UUID PRIMARY KEY,
    move_job_id UUID NOT NULL UNIQUE,
    paper_id BIGINT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('apply', 'rollback')),
    source_sha256 CHAR(64) NOT NULL CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
    source_drive_file_id TEXT NOT NULL CHECK (
        char_length(source_drive_file_id) BETWEEN 10 AND 256
        AND source_drive_file_id ~ '^[A-Za-z0-9_-]+$'
    ),
    old_drive_path TEXT NOT NULL CHECK (char_length(old_drive_path) BETWEEN 1 AND 1500),
    new_drive_path TEXT NOT NULL CHECK (
        char_length(new_drive_path) BETWEEN 1 AND 1500
        AND new_drive_path <> old_drive_path
    ),
    idempotency_key_digest CHAR(64) NOT NULL UNIQUE
        CHECK (idempotency_key_digest ~ '^[0-9a-f]{64}$'),
    worker_id TEXT NOT NULL CHECK (char_length(worker_id) BETWEEN 1 AND 200),
    completed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    FOREIGN KEY (
        move_job_id, paper_id, source_sha256, source_drive_file_id, old_drive_path,
        new_drive_path, direction
    ) REFERENCES paper_taxonomy_move_jobs(
        id, paper_id, source_sha256, source_drive_file_id,
        old_drive_path, new_drive_path, direction
    ) ON DELETE RESTRICT
);

CREATE TABLE paper_taxonomy_rollback_proposals (
    id UUID PRIMARY KEY,
    original_proposal_id UUID NOT NULL
        REFERENCES paper_taxonomy_proposals(id) ON DELETE RESTRICT,
    canonical_payload JSONB NOT NULL CHECK (
        jsonb_typeof(canonical_payload) = 'object'
        AND canonical_payload ->> 'schema' = 'paper-taxonomy-rollback-proposal'
        AND (canonical_payload ->> 'schema_version')::integer = 1
        AND canonical_payload ->> 'rollback_proposal_id' = id::text
        AND canonical_payload ->> 'original_proposal_id' = original_proposal_id::text
        AND canonical_payload ->> 'rollback_payload_sha256' =
            canonical_payload_sha256
        AND canonical_payload ->> 'status' = 'pending-human-review'
        AND jsonb_typeof(canonical_payload -> 'items') = 'array'
        AND jsonb_array_length(canonical_payload -> 'items') BETWEEN 1 AND 1000
    ),
    canonical_payload_sha256 CHAR(64) NOT NULL
        CHECK (canonical_payload_sha256 ~ '^[0-9a-f]{64}$'),
    reason TEXT NOT NULL CHECK (char_length(reason) BETWEEN 1 AND 4000),
    created_by TEXT NOT NULL CHECK (char_length(created_by) BETWEEN 1 AND 200),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE paper_taxonomy_rollback_items (
    rollback_proposal_id UUID NOT NULL
        REFERENCES paper_taxonomy_rollback_proposals(id) ON DELETE RESTRICT,
    ordinal INTEGER NOT NULL CHECK (ordinal BETWEEN 0 AND 999),
    source_move_job_id UUID NOT NULL
        REFERENCES paper_taxonomy_move_jobs(id) ON DELETE RESTRICT,
    move_job_id UUID NOT NULL UNIQUE,
    idempotency_key_digest CHAR(64) NOT NULL UNIQUE
        CHECK (idempotency_key_digest ~ '^[0-9a-f]{64}$'),
    paper_id BIGINT NOT NULL REFERENCES papers(id) ON DELETE RESTRICT,
    source_sha256 CHAR(64) NOT NULL CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
    old_drive_path TEXT NOT NULL CHECK (
        char_length(old_drive_path) BETWEEN 1 AND 1500
        AND old_drive_path ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}:[^/].*$'
        AND position(chr(92) in old_drive_path) = 0
        AND old_drive_path !~ '(^|/)\.\.?(/|$)'
        AND old_drive_path !~ '//'
    ),
    new_drive_path TEXT NOT NULL CHECK (
        char_length(new_drive_path) BETWEEN 1 AND 1500
        AND new_drive_path <> old_drive_path
        AND new_drive_path ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}:[^/].*$'
        AND position(chr(92) in new_drive_path) = 0
        AND new_drive_path !~ '(^|/)\.\.?(/|$)'
        AND new_drive_path !~ '//'
    ),
    PRIMARY KEY (rollback_proposal_id, paper_id),
    UNIQUE (rollback_proposal_id, source_move_job_id),
    UNIQUE (rollback_proposal_id, ordinal),
    UNIQUE (rollback_proposal_id, new_drive_path)
);

CREATE TABLE paper_taxonomy_rollback_review_heads (
    rollback_proposal_id UUID PRIMARY KEY
        REFERENCES paper_taxonomy_rollback_proposals(id) ON DELETE RESTRICT,
    review_revision BIGINT NOT NULL DEFAULT 0 CHECK (review_revision >= 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE paper_taxonomy_rollback_review_events (
    id UUID PRIMARY KEY,
    rollback_proposal_id UUID NOT NULL
        REFERENCES paper_taxonomy_rollback_proposals(id) ON DELETE RESTRICT,
    review_revision BIGINT NOT NULL CHECK (review_revision >= 1),
    expected_revision BIGINT NOT NULL CHECK (expected_revision >= 0),
    decision TEXT NOT NULL CHECK (decision IN ('approve', 'reject')),
    actor_kind TEXT NOT NULL CHECK (actor_kind = 'human'),
    actor_id TEXT NOT NULL CHECK (char_length(actor_id) BETWEEN 1 AND 200),
    comment TEXT CHECK (comment IS NULL OR char_length(comment) <= 4000),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (review_revision = expected_revision + 1),
    UNIQUE (rollback_proposal_id),
    UNIQUE (rollback_proposal_id, review_revision),
    UNIQUE (id, rollback_proposal_id, decision)
);

ALTER TABLE paper_taxonomy_move_jobs
ADD CONSTRAINT paper_taxonomy_move_rollback_proposal_fk
FOREIGN KEY (rollback_proposal_id)
REFERENCES paper_taxonomy_rollback_proposals(id) ON DELETE RESTRICT;

ALTER TABLE paper_taxonomy_move_jobs
ADD CONSTRAINT paper_taxonomy_move_rollback_approval_fk
FOREIGN KEY (
    rollback_approval_event_id, rollback_proposal_id,
    rollback_approval_decision
) REFERENCES paper_taxonomy_rollback_review_events(
    id, rollback_proposal_id, decision
) ON DELETE RESTRICT;

CREATE TABLE paper_taxonomy_usage_ledger (
    id UUID PRIMARY KEY,
    generation_job_id UUID NOT NULL
        REFERENCES paper_taxonomy_generation_jobs(id) ON DELETE RESTRICT,
    attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
    provider TEXT NOT NULL DEFAULT 'deepseek' CHECK (provider = 'deepseek'),
    model TEXT NOT NULL DEFAULT 'deepseek-v4-pro'
        CHECK (model = 'deepseek-v4-pro'),
    request_digest CHAR(64) NOT NULL CHECK (request_digest ~ '^[0-9a-f]{64}$'),
    response_digest CHAR(64) NOT NULL CHECK (response_digest ~ '^[0-9a-f]{64}$'),
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
        (cost_basis = 'unavailable' AND cost_microusd IS NULL
         AND pricing_version IS NULL)
        OR
        (cost_basis = 'provider-reported' AND cost_microusd IS NOT NULL
         AND pricing_version IS NULL)
        OR
        (cost_basis = 'reviewed-price-table' AND cost_microusd IS NOT NULL
         AND pricing_version IS NOT NULL)
    ),
    UNIQUE (generation_job_id, attempt_number)
);

CREATE FUNCTION paper_taxonomy_deny_history_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'paper taxonomy history is append-only'
        USING ERRCODE = '55000';
END;
$$;

CREATE FUNCTION paper_taxonomy_guard_generation_job()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'paper taxonomy generation jobs cannot be deleted'
            USING ERRCODE = '55000';
    END IF;
    IF (OLD.id, OLD.audit_id, OLD.proposal_id, OLD.provider, OLD.model,
        OLD.request_actor_kind, OLD.request_actor_id,
        OLD.metadata_and_cost_acknowledged, OLD.provider_body_digest,
        OLD.request_digest, OLD.max_output_tokens, OLD.max_attempts,
        OLD.requested_at)
       IS DISTINCT FROM
       (NEW.id, NEW.audit_id, NEW.proposal_id, NEW.provider, NEW.model,
        NEW.request_actor_kind, NEW.request_actor_id,
        NEW.metadata_and_cost_acknowledged, NEW.provider_body_digest,
        NEW.request_digest, NEW.max_output_tokens, NEW.max_attempts,
        NEW.requested_at) THEN
        RAISE EXCEPTION 'paper taxonomy generation identity is immutable'
            USING ERRCODE = '55000';
    END IF;
    IF NEW.attempt_count < OLD.attempt_count THEN
        RAISE EXCEPTION 'paper taxonomy generation attempts cannot decrease'
            USING ERRCODE = '55000';
    END IF;
    IF OLD.status IN ('succeeded', 'failed') AND NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION 'terminal paper taxonomy generation job is immutable'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION paper_taxonomy_validate_generation_job()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    audit_decision TEXT;
BEGIN
    SELECT decision INTO audit_decision
    FROM paper_taxonomy_audits WHERE id = NEW.audit_id;
    IF audit_decision IS DISTINCT FROM 'request-proposal' THEN
        RAISE EXCEPTION 'paper taxonomy generation requires an eligible audit'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION paper_taxonomy_validate_usage()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    current_job paper_taxonomy_generation_jobs%ROWTYPE;
BEGIN
    SELECT * INTO current_job
    FROM paper_taxonomy_generation_jobs
    WHERE id = NEW.generation_job_id;
    IF NOT FOUND
       OR NEW.provider <> current_job.provider
       OR NEW.model <> current_job.model
       OR btrim(NEW.request_digest) <> btrim(current_job.provider_body_digest)
       OR NEW.attempt_number > current_job.attempt_count THEN
        RAISE EXCEPTION 'paper taxonomy usage does not match generation job'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION paper_taxonomy_guard_move_job()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'paper taxonomy move jobs cannot be deleted'
            USING ERRCODE = '55000';
    END IF;
    IF (OLD.id, OLD.direction, OLD.proposal_id, OLD.rollback_proposal_id,
        OLD.approval_event_id, OLD.approval_decision,
        OLD.rollback_approval_event_id, OLD.rollback_approval_decision, OLD.paper_id,
        OLD.source_sha256, OLD.old_drive_path, OLD.new_drive_path,
        OLD.idempotency_key_digest, OLD.max_attempts, OLD.created_at)
       IS DISTINCT FROM
       (NEW.id, NEW.direction, NEW.proposal_id, NEW.rollback_proposal_id,
        NEW.approval_event_id, NEW.approval_decision,
        NEW.rollback_approval_event_id, NEW.rollback_approval_decision, NEW.paper_id,
        NEW.source_sha256, NEW.old_drive_path, NEW.new_drive_path,
        NEW.idempotency_key_digest, NEW.max_attempts, NEW.created_at) THEN
        RAISE EXCEPTION 'paper taxonomy move identity is immutable'
            USING ERRCODE = '55000';
    END IF;
    IF OLD.source_drive_file_id IS NOT NULL
       AND NEW.source_drive_file_id IS DISTINCT FROM OLD.source_drive_file_id THEN
        RAISE EXCEPTION 'paper taxonomy Drive file identity is immutable once bound'
            USING ERRCODE = '55000';
    END IF;
    IF OLD.source_drive_file_id IS NULL
       AND NEW.source_drive_file_id IS NOT NULL
       AND (OLD.status <> 'running' OR NEW.status <> 'running') THEN
        RAISE EXCEPTION 'paper taxonomy Drive file identity requires a running lease'
            USING ERRCODE = '55000';
    END IF;
    IF NEW.attempt_count < OLD.attempt_count THEN
        RAISE EXCEPTION 'paper taxonomy move attempts cannot decrease'
            USING ERRCODE = '55000';
    END IF;
    IF OLD.status IN ('succeeded', 'failed') AND NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION 'terminal paper taxonomy move job is immutable'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION paper_taxonomy_validate_review_event()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    current_revision BIGINT;
    expected_count INTEGER;
    assignment_count INTEGER;
    invalid_count INTEGER;
    other_count INTEGER;
BEGIN
    IF NEW.actor_kind <> 'human' THEN
        RAISE EXCEPTION 'paper taxonomy approval requires a human actor'
            USING ERRCODE = '23514';
    END IF;
    INSERT INTO paper_taxonomy_review_heads (proposal_id)
    VALUES (NEW.proposal_id) ON CONFLICT DO NOTHING;
    SELECT review_revision INTO current_revision
    FROM paper_taxonomy_review_heads
    WHERE proposal_id = NEW.proposal_id FOR UPDATE;
    IF current_revision IS DISTINCT FROM NEW.expected_revision THEN
        RAISE EXCEPTION 'paper taxonomy review revision conflict'
            USING ERRCODE = '40001', DETAIL = current_revision::text;
    END IF;
    IF NEW.review_revision <> current_revision + 1 THEN
        RAISE EXCEPTION 'paper taxonomy review revision invalid'
            USING ERRCODE = '23514';
    END IF;

    IF NEW.decision = 'approve' THEN
        SELECT audit.paper_count INTO expected_count
        FROM paper_taxonomy_proposals proposal
        JOIN paper_taxonomy_audits audit ON audit.id = proposal.audit_id
        WHERE proposal.id = NEW.proposal_id;

        SELECT count(*) INTO invalid_count
        FROM paper_taxonomy_proposals proposal
        JOIN paper_taxonomy_audits audit ON audit.id = proposal.audit_id
        JOIN paper_taxonomy_generation_jobs generation
          ON generation.id = proposal.generation_job_id
        WHERE proposal.id = NEW.proposal_id
          AND (
              audit.decision <> 'request-proposal'
              OR audit.category <> proposal.category
              OR audit.category_path <> proposal.category_path
              OR btrim(audit.source_snapshot_sha256) <>
                  btrim(proposal.source_snapshot_sha256)
              OR generation.status <> 'succeeded'
              OR generation.audit_id <> proposal.audit_id
              OR generation.proposal_id <> proposal.id
              OR generation.result_proposal_id <> proposal.id
          );
        IF invalid_count <> 0 THEN
            RAISE EXCEPTION 'paper taxonomy proposal provenance is invalid'
                USING ERRCODE = '23514';
        END IF;

        SELECT count(*) INTO assignment_count
        FROM paper_taxonomy_proposal_assignments
        WHERE proposal_id = NEW.proposal_id;
        IF assignment_count IS DISTINCT FROM expected_count THEN
            RAISE EXCEPTION 'paper taxonomy proposal is incomplete'
                USING ERRCODE = '23514';
        END IF;

        SELECT count(*) INTO invalid_count
        FROM paper_taxonomy_proposal_assignments assignment
        JOIN paper_taxonomy_proposals proposal ON proposal.id = assignment.proposal_id
        JOIN paper_taxonomy_audits audit ON audit.id = proposal.audit_id
        JOIN papers paper ON paper.id = assignment.paper_id
        WHERE assignment.proposal_id = NEW.proposal_id
          AND (
              btrim(paper.sha256) <> btrim(assignment.source_sha256)
              OR paper.drive_path IS DISTINCT FROM assignment.old_drive_path
              OR assignment.new_drive_path <> proposal.category_path || '/' ||
                    assignment.subtheme || '/' ||
                    regexp_replace(assignment.old_drive_path, '^.*/', '')
              OR NOT EXISTS (
                    SELECT 1
                    FROM jsonb_array_elements(audit.source_snapshot -> 'papers') item
                    WHERE (item ->> 'paper_id')::bigint = assignment.paper_id
                      AND item ->> 'source_sha256' = btrim(assignment.source_sha256)
                      AND item ->> 'current_drive_path' = assignment.old_drive_path
              )
              OR NOT EXISTS (
                    SELECT 1
                    FROM jsonb_array_elements(
                        proposal.canonical_payload -> 'assignments'
                    ) item
                    WHERE (item ->> 'paper_id')::bigint = assignment.paper_id
                      AND item ->> 'source_sha256' = btrim(assignment.source_sha256)
                      AND item ->> 'current_drive_path' = assignment.old_drive_path
                      AND item ->> 'proposed_drive_path' = assignment.new_drive_path
                      AND item ->> 'subtheme' = assignment.subtheme
                      AND item ->> 'move_job_id' = assignment.move_job_id::text
                      AND item ->> 'idempotency_key_digest' =
                          btrim(assignment.idempotency_key_digest)
              )
              OR NOT EXISTS (
                    SELECT 1
                    FROM jsonb_array_elements(
                        proposal.canonical_payload -> 'subthemes'
                    ) item
                    WHERE item ->> 'name' = assignment.subtheme
              )
          );
        IF invalid_count <> 0 THEN
            RAISE EXCEPTION 'paper taxonomy proposal source is stale or unsafe'
                USING ERRCODE = '23514';
        END IF;

        SELECT count(*) INTO invalid_count
        FROM (
            SELECT subtheme, count(*) AS members
            FROM paper_taxonomy_proposal_assignments
            WHERE proposal_id = NEW.proposal_id
            GROUP BY subtheme
        ) groups
        WHERE groups.subtheme <> 'Other' AND groups.members < 4;
        IF invalid_count <> 0 THEN
            RAISE EXCEPTION 'paper taxonomy subtheme is too small'
                USING ERRCODE = '23514';
        END IF;

        SELECT count(*) INTO other_count
        FROM paper_taxonomy_proposal_assignments
        WHERE proposal_id = NEW.proposal_id AND subtheme = 'Other';
        IF other_count * 4 > assignment_count THEN
            RAISE EXCEPTION 'paper taxonomy Other catch-all is too large'
                USING ERRCODE = '23514';
        END IF;
        IF (SELECT count(DISTINCT subtheme)
            FROM paper_taxonomy_proposal_assignments
            WHERE proposal_id = NEW.proposal_id) < 2 THEN
            RAISE EXCEPTION 'paper taxonomy requires at least two subthemes'
                USING ERRCODE = '23514';
        END IF;
    END IF;

    UPDATE paper_taxonomy_review_heads
    SET review_revision = NEW.review_revision, updated_at = NOW()
    WHERE proposal_id = NEW.proposal_id;
    RETURN NEW;
END;
$$;

CREATE FUNCTION paper_taxonomy_enqueue_approved_moves()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.decision = 'approve' THEN
        INSERT INTO paper_taxonomy_move_jobs
            (id, direction, proposal_id, approval_event_id,
             approval_decision, paper_id,
             source_sha256, old_drive_path, new_drive_path,
             idempotency_key_digest)
        SELECT assignment.move_job_id, 'apply', NEW.proposal_id, NEW.id,
               'approve',
               assignment.paper_id, assignment.source_sha256,
               assignment.old_drive_path, assignment.new_drive_path,
               assignment.idempotency_key_digest
        FROM paper_taxonomy_proposal_assignments assignment
        WHERE assignment.proposal_id = NEW.proposal_id
          AND assignment.old_drive_path <> assignment.new_drive_path
        ORDER BY assignment.ordinal;
    END IF;
    RETURN NULL;
END;
$$;

CREATE FUNCTION paper_taxonomy_validate_rollback_review_event()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    current_revision BIGINT;
    item_count INTEGER;
    invalid_count INTEGER;
BEGIN
    IF NEW.actor_kind <> 'human' THEN
        RAISE EXCEPTION 'paper taxonomy rollback requires a human actor'
            USING ERRCODE = '23514';
    END IF;
    INSERT INTO paper_taxonomy_rollback_review_heads (rollback_proposal_id)
    VALUES (NEW.rollback_proposal_id) ON CONFLICT DO NOTHING;
    SELECT review_revision INTO current_revision
    FROM paper_taxonomy_rollback_review_heads
    WHERE rollback_proposal_id = NEW.rollback_proposal_id FOR UPDATE;
    IF current_revision IS DISTINCT FROM NEW.expected_revision THEN
        RAISE EXCEPTION 'paper taxonomy rollback review revision conflict'
            USING ERRCODE = '40001', DETAIL = current_revision::text;
    END IF;
    IF NEW.review_revision <> current_revision + 1 THEN
        RAISE EXCEPTION 'paper taxonomy rollback review revision invalid'
            USING ERRCODE = '23514';
    END IF;

    IF NEW.decision = 'approve' THEN
        SELECT count(*) INTO item_count
        FROM paper_taxonomy_rollback_items
        WHERE rollback_proposal_id = NEW.rollback_proposal_id;
        IF item_count = 0 THEN
            RAISE EXCEPTION 'paper taxonomy rollback has no items'
                USING ERRCODE = '23514';
        END IF;
        SELECT count(*) INTO invalid_count
        FROM paper_taxonomy_rollback_items item
        JOIN paper_taxonomy_rollback_proposals rollback
          ON rollback.id = item.rollback_proposal_id
        LEFT JOIN paper_taxonomy_move_jobs original ON original.id = item.source_move_job_id
        LEFT JOIN paper_taxonomy_move_journal journal
          ON journal.move_job_id = original.id
        LEFT JOIN papers paper ON paper.id = item.paper_id
        WHERE item.rollback_proposal_id = NEW.rollback_proposal_id
          AND (
              original.id IS NULL
              OR journal.id IS NULL
              OR paper.id IS NULL
              OR original.status <> 'succeeded'
              OR original.direction <> 'apply'
              OR original.proposal_id <> rollback.original_proposal_id
              OR journal.paper_id <> item.paper_id
              OR btrim(journal.source_sha256) <> btrim(item.source_sha256)
              OR journal.new_drive_path <> item.old_drive_path
              OR journal.old_drive_path <> item.new_drive_path
              OR btrim(paper.sha256) <> btrim(item.source_sha256)
              OR paper.drive_path IS DISTINCT FROM item.old_drive_path
          );
        IF invalid_count <> 0 THEN
            RAISE EXCEPTION 'paper taxonomy rollback source is stale or invalid'
                USING ERRCODE = '23514';
        END IF;
    END IF;

    UPDATE paper_taxonomy_rollback_review_heads
    SET review_revision = NEW.review_revision, updated_at = NOW()
    WHERE rollback_proposal_id = NEW.rollback_proposal_id;
    RETURN NEW;
END;
$$;

CREATE FUNCTION paper_taxonomy_enqueue_approved_rollbacks()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.decision = 'approve' THEN
        INSERT INTO paper_taxonomy_move_jobs
            (id, direction, rollback_proposal_id,
             rollback_approval_event_id, rollback_approval_decision,
             paper_id, source_sha256,
             old_drive_path, new_drive_path, idempotency_key_digest)
        SELECT item.move_job_id, 'rollback', NEW.rollback_proposal_id, NEW.id,
               'approve',
               item.paper_id, item.source_sha256, item.old_drive_path,
               item.new_drive_path, item.idempotency_key_digest
        FROM paper_taxonomy_rollback_items item
        WHERE item.rollback_proposal_id = NEW.rollback_proposal_id
        ORDER BY item.ordinal;
    END IF;
    RETURN NULL;
END;
$$;

CREATE FUNCTION paper_taxonomy_validate_move_journal()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    current_job paper_taxonomy_move_jobs%ROWTYPE;
BEGIN
    SELECT * INTO current_job
    FROM paper_taxonomy_move_jobs WHERE id = NEW.move_job_id FOR UPDATE;
    IF NOT FOUND OR current_job.status <> 'running' THEN
        RAISE EXCEPTION 'paper taxonomy journal requires a running move job'
            USING ERRCODE = '23514';
    END IF;
    IF (NEW.paper_id, NEW.direction, NEW.source_sha256,
        NEW.source_drive_file_id,
        NEW.old_drive_path, NEW.new_drive_path, NEW.idempotency_key_digest)
       IS DISTINCT FROM
       (current_job.paper_id, current_job.direction, current_job.source_sha256,
        current_job.source_drive_file_id,
        current_job.old_drive_path, current_job.new_drive_path,
        current_job.idempotency_key_digest) THEN
        RAISE EXCEPTION 'paper taxonomy journal does not match move job'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER paper_taxonomy_generation_guard
BEFORE UPDATE OR DELETE ON paper_taxonomy_generation_jobs
FOR EACH ROW EXECUTE FUNCTION paper_taxonomy_guard_generation_job();

CREATE TRIGGER paper_taxonomy_generation_validate
BEFORE INSERT ON paper_taxonomy_generation_jobs
FOR EACH ROW EXECUTE FUNCTION paper_taxonomy_validate_generation_job();

CREATE TRIGGER paper_taxonomy_usage_validate
BEFORE INSERT ON paper_taxonomy_usage_ledger
FOR EACH ROW EXECUTE FUNCTION paper_taxonomy_validate_usage();

CREATE TRIGGER paper_taxonomy_move_guard
BEFORE UPDATE OR DELETE ON paper_taxonomy_move_jobs
FOR EACH ROW EXECUTE FUNCTION paper_taxonomy_guard_move_job();

CREATE TRIGGER paper_taxonomy_review_validate
BEFORE INSERT ON paper_taxonomy_review_events
FOR EACH ROW EXECUTE FUNCTION paper_taxonomy_validate_review_event();

CREATE TRIGGER paper_taxonomy_review_enqueue
AFTER INSERT ON paper_taxonomy_review_events
FOR EACH ROW EXECUTE FUNCTION paper_taxonomy_enqueue_approved_moves();

CREATE TRIGGER paper_taxonomy_rollback_review_validate
BEFORE INSERT ON paper_taxonomy_rollback_review_events
FOR EACH ROW EXECUTE FUNCTION paper_taxonomy_validate_rollback_review_event();

CREATE TRIGGER paper_taxonomy_rollback_review_enqueue
AFTER INSERT ON paper_taxonomy_rollback_review_events
FOR EACH ROW EXECUTE FUNCTION paper_taxonomy_enqueue_approved_rollbacks();

CREATE TRIGGER paper_taxonomy_move_journal_validate
BEFORE INSERT ON paper_taxonomy_move_journal
FOR EACH ROW EXECUTE FUNCTION paper_taxonomy_validate_move_journal();

CREATE TRIGGER paper_taxonomy_audits_append_only
BEFORE UPDATE OR DELETE ON paper_taxonomy_audits
FOR EACH ROW EXECUTE FUNCTION paper_taxonomy_deny_history_mutation();

CREATE TRIGGER paper_taxonomy_proposals_append_only
BEFORE UPDATE OR DELETE ON paper_taxonomy_proposals
FOR EACH ROW EXECUTE FUNCTION paper_taxonomy_deny_history_mutation();

CREATE TRIGGER paper_taxonomy_assignments_append_only
BEFORE UPDATE OR DELETE ON paper_taxonomy_proposal_assignments
FOR EACH ROW EXECUTE FUNCTION paper_taxonomy_deny_history_mutation();

CREATE TRIGGER paper_taxonomy_reviews_append_only
BEFORE UPDATE OR DELETE ON paper_taxonomy_review_events
FOR EACH ROW EXECUTE FUNCTION paper_taxonomy_deny_history_mutation();

CREATE TRIGGER paper_taxonomy_journal_append_only
BEFORE UPDATE OR DELETE ON paper_taxonomy_move_journal
FOR EACH ROW EXECUTE FUNCTION paper_taxonomy_deny_history_mutation();

CREATE TRIGGER paper_taxonomy_rollback_proposals_append_only
BEFORE UPDATE OR DELETE ON paper_taxonomy_rollback_proposals
FOR EACH ROW EXECUTE FUNCTION paper_taxonomy_deny_history_mutation();

CREATE TRIGGER paper_taxonomy_rollback_items_append_only
BEFORE UPDATE OR DELETE ON paper_taxonomy_rollback_items
FOR EACH ROW EXECUTE FUNCTION paper_taxonomy_deny_history_mutation();

CREATE TRIGGER paper_taxonomy_rollback_reviews_append_only
BEFORE UPDATE OR DELETE ON paper_taxonomy_rollback_review_events
FOR EACH ROW EXECUTE FUNCTION paper_taxonomy_deny_history_mutation();

CREATE TRIGGER paper_taxonomy_usage_append_only
BEFORE UPDATE OR DELETE ON paper_taxonomy_usage_ledger
FOR EACH ROW EXECUTE FUNCTION paper_taxonomy_deny_history_mutation();

CREATE TRIGGER paper_taxonomy_review_heads_no_delete
BEFORE DELETE ON paper_taxonomy_review_heads
FOR EACH ROW EXECUTE FUNCTION paper_taxonomy_deny_history_mutation();

CREATE TRIGGER paper_taxonomy_rollback_heads_no_delete
BEFORE DELETE ON paper_taxonomy_rollback_review_heads
FOR EACH ROW EXECUTE FUNCTION paper_taxonomy_deny_history_mutation();
"""

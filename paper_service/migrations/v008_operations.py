"""Migration 008: observable operations and approval-gated artifact changes."""

SQL = r"""
CREATE TABLE paper_expected_workers (
    service_id TEXT NOT NULL CHECK (
        service_id ~ '^[a-z0-9][a-z0-9._-]{0,63}$'
    ),
    worker_id TEXT NOT NULL CHECK (
        worker_id ~ '^[a-z0-9][a-z0-9._-]{0,63}$'
    ),
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (service_id, worker_id)
);

CREATE TABLE paper_worker_heartbeats (
    service_id TEXT NOT NULL,
    worker_id TEXT NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    sequence BIGINT NOT NULL CHECK (sequence >= 0),
    state TEXT NOT NULL CHECK (
        state IN ('idle', 'processing', 'degraded', 'stopped')
    ),
    active_paper_id BIGINT REFERENCES papers(id) ON DELETE RESTRICT,
    last_success_at TIMESTAMPTZ,
    error_code TEXT CHECK (
        error_code IS NULL OR error_code ~ '^[a-z][a-z0-9_]{0,63}$'
    ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (service_id, worker_id),
    FOREIGN KEY (service_id, worker_id)
        REFERENCES paper_expected_workers(service_id, worker_id)
        ON DELETE RESTRICT,
    CHECK (last_success_at IS NULL OR last_success_at <= observed_at),
    CHECK (
        (state = 'processing' AND active_paper_id IS NOT NULL)
        OR (state IN ('idle', 'stopped') AND active_paper_id IS NULL)
        OR state = 'degraded'
    ),
    CHECK (
        (state = 'degraded' AND error_code IS NOT NULL)
        OR (state IN ('idle', 'processing') AND error_code IS NULL)
        OR state = 'stopped'
    )
);

CREATE INDEX paper_worker_heartbeats_observed
ON paper_worker_heartbeats (service_id, observed_at DESC, worker_id);

CREATE TABLE paper_incident_heads (
    service_id TEXT PRIMARY KEY CHECK (
        service_id ~ '^[a-z0-9][a-z0-9._-]{0,63}$'
    ),
    generation BIGINT NOT NULL DEFAULT 0 CHECK (generation >= 0),
    current_incident_id TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (
        (generation = 0 AND current_incident_id IS NULL)
        OR (generation > 0 AND current_incident_id IS NOT NULL)
    )
);

CREATE TABLE paper_incidents (
    incident_id TEXT PRIMARY KEY CHECK (
        incident_id ~ '^incident-[0-9a-f]{32}$'
    ),
    service_id TEXT NOT NULL CHECK (
        service_id ~ '^[a-z0-9][a-z0-9._-]{0,63}$'
    ),
    generation BIGINT NOT NULL CHECK (generation >= 1),
    status TEXT NOT NULL CHECK (status IN ('open', 'recovered')),
    health_dedupe_key TEXT NOT NULL CHECK (
        health_dedupe_key ~ '^health-[0-9a-f]{32}$'
    ),
    opened_at TIMESTAMPTZ NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL,
    recovered_at TIMESTAMPTZ,
    reason_codes TEXT[] NOT NULL CHECK (
        cardinality(reason_codes) BETWEEN 1 AND 32
        AND reason_codes <@ ARRAY[
            'worker_degraded', 'worker_missing',
            'worker_stale', 'worker_stopped'
        ]::TEXT[]
    ),
    affected_worker_count SMALLINT NOT NULL CHECK (
        affected_worker_count BETWEEN 1 AND 64
    ),
    CHECK (last_seen_at >= opened_at),
    CHECK (
        (status = 'open' AND recovered_at IS NULL)
        OR (status = 'recovered' AND recovered_at >= last_seen_at)
    ),
    UNIQUE (service_id, generation),
    UNIQUE (incident_id, service_id, generation),
    UNIQUE (incident_id, service_id)
);

ALTER TABLE paper_incident_heads
ADD CONSTRAINT paper_incident_heads_current_fk
FOREIGN KEY (current_incident_id, service_id, generation)
REFERENCES paper_incidents(incident_id, service_id, generation)
DEFERRABLE INITIALLY DEFERRED;

CREATE INDEX paper_incidents_current
ON paper_incidents (service_id, status, last_seen_at DESC);

CREATE TABLE paper_incident_events (
    id UUID PRIMARY KEY,
    service_id TEXT NOT NULL,
    incident_id TEXT NOT NULL,
    event_type TEXT NOT NULL CHECK (
        event_type IN ('incident_opened', 'incident_recovered')
    ),
    dedupe_key TEXT NOT NULL UNIQUE CHECK (
        dedupe_key ~ '^event-[0-9a-f]{32}$'
    ),
    occurred_at TIMESTAMPTZ NOT NULL,
    severity TEXT NOT NULL CHECK (
        (event_type = 'incident_opened' AND severity = 'warning')
        OR (event_type = 'incident_recovered' AND severity = 'info')
    ),
    reason_codes TEXT[] NOT NULL CHECK (
        cardinality(reason_codes) BETWEEN 1 AND 32
        AND reason_codes <@ ARRAY[
            'worker_degraded', 'worker_missing',
            'worker_stale', 'worker_stopped'
        ]::TEXT[]
    ),
    affected_worker_count SMALLINT NOT NULL CHECK (
        affected_worker_count BETWEEN 1 AND 64
    ),
    canonical_event JSONB NOT NULL CHECK (
        jsonb_typeof(canonical_event) = 'object'
        AND canonical_event ?& ARRAY[
            'schema', 'schema_version', 'service_id', 'event_type',
            'incident_id', 'dedupe_key', 'occurred_at', 'severity',
            'reason_codes', 'affected_worker_count'
        ]
    ),
    canonical_event_sha256 CHAR(64) NOT NULL CHECK (
        canonical_event_sha256 ~ '^[0-9a-f]{64}$'
    ),
    FOREIGN KEY (incident_id, service_id)
        REFERENCES paper_incidents(incident_id, service_id) ON DELETE RESTRICT,
    UNIQUE (incident_id, event_type),
    UNIQUE (id, dedupe_key)
);

CREATE TABLE paper_webhook_outbox (
    id UUID PRIMARY KEY,
    incident_event_id UUID NOT NULL,
    event_dedupe_key TEXT NOT NULL,
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    payload_sha256 CHAR(64) NOT NULL CHECK (
        payload_sha256 ~ '^[0-9a-f]{64}$'
    ),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending', 'leased', 'delivered', 'dead')
    ),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    max_attempts SMALLINT NOT NULL DEFAULT 8 CHECK (
        max_attempts BETWEEN 1 AND 20
    ),
    available_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    lease_owner TEXT CHECK (
        lease_owner IS NULL OR char_length(lease_owner) BETWEEN 1 AND 200
    ),
    lease_token UUID,
    lease_expires_at TIMESTAMPTZ,
    last_error_code TEXT CHECK (
        last_error_code IS NULL
        OR last_error_code ~ '^[a-z][a-z0-9_]{0,119}$'
    ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    delivered_at TIMESTAMPTZ,
    CHECK (
        (status = 'leased' AND lease_owner IS NOT NULL
         AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)
        OR (status <> 'leased' AND lease_owner IS NULL
            AND lease_token IS NULL AND lease_expires_at IS NULL)
    ),
    CHECK (
        (status = 'delivered' AND delivered_at IS NOT NULL)
        OR (status <> 'delivered' AND delivered_at IS NULL)
    ),
    FOREIGN KEY (incident_event_id, event_dedupe_key)
        REFERENCES paper_incident_events(id, dedupe_key) ON DELETE RESTRICT,
    UNIQUE (incident_event_id)
);

CREATE INDEX paper_webhook_outbox_claim
ON paper_webhook_outbox (available_at, created_at, id)
WHERE status = 'pending';

CREATE INDEX paper_webhook_outbox_expired
ON paper_webhook_outbox (lease_expires_at, id)
WHERE status = 'leased';

CREATE TABLE paper_reprocess_requests (
    id UUID PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE CHECK (
        idempotency_key ~ '^rr-[0-9a-f]{32}$'
    ),
    request_fingerprint CHAR(64) NOT NULL CHECK (
        request_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    target_snapshot_sha256 CHAR(64) NOT NULL CHECK (
        target_snapshot_sha256 ~ '^[0-9a-f]{64}$'
    ),
    scope TEXT NOT NULL CHECK (scope IN ('selected', 'all')),
    reason_code TEXT NOT NULL CHECK (
        reason_code ~ '^[a-z][a-z0-9_]{0,63}$'
    ),
    requested_at TIMESTAMPTZ NOT NULL,
    global_confirmed BOOLEAN NOT NULL,
    target_count SMALLINT NOT NULL CHECK (target_count BETWEEN 1 AND 50),
    canonical_request JSONB NOT NULL CHECK (
        jsonb_typeof(canonical_request) = 'object'
        AND canonical_request ?& ARRAY[
            'schema', 'schema_version', 'scope', 'reason_code',
            'requested_at', 'targets', 'idempotency_key'
        ]
    ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (
        canonical_request ->> 'scope' = scope
        AND canonical_request ->> 'reason_code' = reason_code
        AND canonical_request ->> 'idempotency_key' = idempotency_key
        AND jsonb_typeof(canonical_request -> 'schema_version') = 'number'
        AND (canonical_request ->> 'schema_version')::integer = 1
        AND (canonical_request ->> 'requested_at')::timestamptz = requested_at
        AND jsonb_typeof(canonical_request -> 'targets') = 'array'
        AND (
        (scope = 'selected' AND NOT global_confirmed
         AND NOT canonical_request ? 'global_confirmation'
         AND jsonb_array_length(canonical_request -> 'targets') = target_count)
        OR (scope = 'all' AND global_confirmed
            AND canonical_request ? 'global_confirmation'
            AND jsonb_array_length(canonical_request -> 'targets') = 0
            AND canonical_request ->> 'global_confirmation'
                = 'REPROCESS_ALL_PAPERS')
        )
    ),
    UNIQUE (id, target_count)
);

CREATE TABLE paper_reprocess_targets (
    request_id UUID NOT NULL REFERENCES paper_reprocess_requests(id)
        ON DELETE RESTRICT,
    ordinal SMALLINT NOT NULL CHECK (ordinal BETWEEN 0 AND 49),
    paper_id BIGINT NOT NULL REFERENCES papers(id) ON DELETE RESTRICT,
    expected_source_sha256 CHAR(64) NOT NULL CHECK (
        expected_source_sha256 ~ '^[0-9a-f]{64}$'
    ),
    expected_current_version_id TEXT CHECK (
        expected_current_version_id IS NULL
        OR expected_current_version_id ~ '^rv-[0-9a-f]{32}$'
    ),
    PRIMARY KEY (request_id, paper_id),
    UNIQUE (request_id, ordinal)
);

CREATE TABLE paper_reprocess_review_heads (
    request_id UUID PRIMARY KEY REFERENCES paper_reprocess_requests(id)
        ON DELETE RESTRICT,
    review_revision BIGINT NOT NULL DEFAULT 0 CHECK (review_revision >= 0),
    decision TEXT NOT NULL DEFAULT 'pending' CHECK (
        decision IN ('pending', 'approved', 'rejected')
    ),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE paper_reprocess_review_events (
    id UUID PRIMARY KEY,
    request_id UUID NOT NULL REFERENCES paper_reprocess_requests(id)
        ON DELETE RESTRICT,
    review_revision BIGINT NOT NULL CHECK (review_revision >= 1),
    expected_revision BIGINT NOT NULL CHECK (expected_revision >= 0),
    decision TEXT NOT NULL CHECK (decision IN ('approve', 'reject')),
    actor_kind TEXT NOT NULL DEFAULT 'human' CHECK (actor_kind = 'human'),
    actor_id TEXT NOT NULL CHECK (char_length(actor_id) BETWEEN 1 AND 200),
    comment TEXT CHECK (comment IS NULL OR char_length(comment) <= 4000),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (review_revision = expected_revision + 1),
    UNIQUE (request_id),
    UNIQUE (request_id, review_revision),
    UNIQUE (id, request_id, decision, actor_kind)
);

CREATE TABLE paper_reprocess_execution_jobs (
    id UUID PRIMARY KEY,
    request_id UUID NOT NULL,
    paper_id BIGINT NOT NULL,
    approval_event_id UUID NOT NULL,
    approval_decision TEXT NOT NULL DEFAULT 'approve'
        CHECK (approval_decision = 'approve'),
    approval_actor_kind TEXT NOT NULL DEFAULT 'human'
        CHECK (approval_actor_kind = 'human'),
    status TEXT NOT NULL DEFAULT 'queued' CHECK (
        status IN ('queued', 'leased', 'running', 'retry_wait',
                   'succeeded', 'failed')
    ),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    max_attempts SMALLINT NOT NULL DEFAULT 3 CHECK (
        max_attempts BETWEEN 1 AND 10
    ),
    available_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    lease_owner TEXT CHECK (
        lease_owner IS NULL OR char_length(lease_owner) BETWEEN 1 AND 200
    ),
    lease_token UUID,
    lease_expires_at TIMESTAMPTZ,
    planned_result_version_id TEXT CHECK (
        planned_result_version_id IS NULL
        OR planned_result_version_id ~ '^rv-[0-9a-f]{32}$'
    ),
    result_version_id TEXT CHECK (
        result_version_id IS NULL
        OR result_version_id ~ '^rv-[0-9a-f]{32}$'
    ),
    last_error_code TEXT CHECK (
        last_error_code IS NULL
        OR last_error_code ~ '^[a-z][a-z0-9_]{0,119}$'
    ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    CHECK (
        (status IN ('leased', 'running') AND lease_owner IS NOT NULL
         AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)
        OR (status NOT IN ('leased', 'running') AND lease_owner IS NULL
            AND lease_token IS NULL AND lease_expires_at IS NULL)
    ),
    CHECK (
        (status = 'succeeded' AND result_version_id IS NOT NULL
         AND finished_at IS NOT NULL)
        OR (status = 'failed' AND result_version_id IS NULL
            AND finished_at IS NOT NULL)
        OR (status NOT IN ('succeeded', 'failed')
            AND result_version_id IS NULL AND finished_at IS NULL)
    ),
    FOREIGN KEY (request_id, paper_id)
        REFERENCES paper_reprocess_targets(request_id, paper_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (
        approval_event_id, request_id,
        approval_decision, approval_actor_kind
    ) REFERENCES paper_reprocess_review_events(
        id, request_id, decision, actor_kind
    ) ON DELETE RESTRICT,
    UNIQUE (request_id, paper_id)
);

CREATE INDEX paper_reprocess_execution_claim
ON paper_reprocess_execution_jobs (available_at, created_at, id)
WHERE status IN ('queued', 'retry_wait');

CREATE INDEX paper_reprocess_execution_expired
ON paper_reprocess_execution_jobs (lease_expires_at, id)
WHERE status IN ('leased', 'running');

CREATE TABLE paper_rollback_requests (
    id UUID PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE CHECK (
        idempotency_key ~ '^rb-[0-9a-f]{32}$'
    ),
    request_fingerprint CHAR(64) NOT NULL CHECK (
        request_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    reason_code TEXT NOT NULL CHECK (
        reason_code ~ '^[a-z][a-z0-9_]{0,63}$'
    ),
    requested_at TIMESTAMPTZ NOT NULL,
    target_count SMALLINT NOT NULL CHECK (target_count BETWEEN 1 AND 50),
    canonical_request JSONB NOT NULL CHECK (
        jsonb_typeof(canonical_request) = 'object'
        AND canonical_request ?& ARRAY[
            'schema_version', 'reason_code', 'requested_at',
            'targets', 'idempotency_key'
        ]
    ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (
        canonical_request ->> 'reason_code' = reason_code
        AND canonical_request ->> 'idempotency_key' = idempotency_key
        AND jsonb_typeof(canonical_request -> 'schema_version') = 'number'
        AND (canonical_request ->> 'schema_version')::integer = 1
        AND (canonical_request ->> 'requested_at')::timestamptz = requested_at
        AND jsonb_typeof(canonical_request -> 'targets') = 'array'
        AND jsonb_array_length(canonical_request -> 'targets') = target_count
    )
);

CREATE TABLE paper_rollback_targets (
    request_id UUID NOT NULL REFERENCES paper_rollback_requests(id)
        ON DELETE RESTRICT,
    ordinal SMALLINT NOT NULL CHECK (ordinal BETWEEN 0 AND 49),
    paper_id BIGINT NOT NULL REFERENCES papers(id) ON DELETE RESTRICT,
    expected_source_sha256 CHAR(64) NOT NULL CHECK (
        expected_source_sha256 ~ '^[0-9a-f]{64}$'
    ),
    expected_current_version_id TEXT NOT NULL CHECK (
        expected_current_version_id ~ '^rv-[0-9a-f]{32}$'
    ),
    rollback_to_version_id TEXT NOT NULL CHECK (
        rollback_to_version_id ~ '^rv-[0-9a-f]{32}$'
    ),
    CHECK (expected_current_version_id <> rollback_to_version_id),
    PRIMARY KEY (request_id, paper_id),
    UNIQUE (request_id, ordinal)
);

CREATE TABLE paper_rollback_review_heads (
    request_id UUID PRIMARY KEY REFERENCES paper_rollback_requests(id)
        ON DELETE RESTRICT,
    review_revision BIGINT NOT NULL DEFAULT 0 CHECK (review_revision >= 0),
    decision TEXT NOT NULL DEFAULT 'pending' CHECK (
        decision IN ('pending', 'approved', 'rejected')
    ),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE paper_rollback_review_events (
    id UUID PRIMARY KEY,
    request_id UUID NOT NULL REFERENCES paper_rollback_requests(id)
        ON DELETE RESTRICT,
    review_revision BIGINT NOT NULL CHECK (review_revision >= 1),
    expected_revision BIGINT NOT NULL CHECK (expected_revision >= 0),
    decision TEXT NOT NULL CHECK (decision IN ('approve', 'reject')),
    actor_kind TEXT NOT NULL DEFAULT 'human' CHECK (actor_kind = 'human'),
    actor_id TEXT NOT NULL CHECK (char_length(actor_id) BETWEEN 1 AND 200),
    comment TEXT CHECK (comment IS NULL OR char_length(comment) <= 4000),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (review_revision = expected_revision + 1),
    UNIQUE (request_id),
    UNIQUE (request_id, review_revision),
    UNIQUE (id, request_id, decision, actor_kind)
);

CREATE TABLE paper_rollback_execution_jobs (
    id UUID PRIMARY KEY,
    request_id UUID NOT NULL,
    paper_id BIGINT NOT NULL,
    approval_event_id UUID NOT NULL,
    approval_decision TEXT NOT NULL DEFAULT 'approve'
        CHECK (approval_decision = 'approve'),
    approval_actor_kind TEXT NOT NULL DEFAULT 'human'
        CHECK (approval_actor_kind = 'human'),
    status TEXT NOT NULL DEFAULT 'queued' CHECK (
        status IN ('queued', 'leased', 'running', 'retry_wait',
                   'succeeded', 'failed')
    ),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    max_attempts SMALLINT NOT NULL DEFAULT 3 CHECK (
        max_attempts BETWEEN 1 AND 10
    ),
    available_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    lease_owner TEXT CHECK (
        lease_owner IS NULL OR char_length(lease_owner) BETWEEN 1 AND 200
    ),
    lease_token UUID,
    lease_expires_at TIMESTAMPTZ,
    result_version_id TEXT CHECK (
        result_version_id IS NULL
        OR result_version_id ~ '^rv-[0-9a-f]{32}$'
    ),
    last_error_code TEXT CHECK (
        last_error_code IS NULL
        OR last_error_code ~ '^[a-z][a-z0-9_]{0,119}$'
    ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    CHECK (
        (status IN ('leased', 'running') AND lease_owner IS NOT NULL
         AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)
        OR (status NOT IN ('leased', 'running') AND lease_owner IS NULL
            AND lease_token IS NULL AND lease_expires_at IS NULL)
    ),
    CHECK (
        (status = 'succeeded' AND result_version_id IS NOT NULL
         AND finished_at IS NOT NULL)
        OR (status = 'failed' AND result_version_id IS NULL
            AND finished_at IS NOT NULL)
        OR (status NOT IN ('succeeded', 'failed')
            AND result_version_id IS NULL AND finished_at IS NULL)
    ),
    FOREIGN KEY (request_id, paper_id)
        REFERENCES paper_rollback_targets(request_id, paper_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (
        approval_event_id, request_id,
        approval_decision, approval_actor_kind
    ) REFERENCES paper_rollback_review_events(
        id, request_id, decision, actor_kind
    ) ON DELETE RESTRICT,
    UNIQUE (request_id, paper_id)
);

CREATE INDEX paper_rollback_execution_claim
ON paper_rollback_execution_jobs (available_at, created_at, id)
WHERE status IN ('queued', 'retry_wait');

CREATE INDEX paper_rollback_execution_expired
ON paper_rollback_execution_jobs (lease_expires_at, id)
WHERE status IN ('leased', 'running');

CREATE FUNCTION apply_paper_operation_review_event()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    current_revision BIGINT;
    current_decision TEXT;
    head_table REGCLASS;
BEGIN
    IF TG_TABLE_NAME = 'paper_reprocess_review_events' THEN
        SELECT review_revision, decision
        INTO current_revision, current_decision
        FROM paper_reprocess_review_heads
        WHERE request_id = NEW.request_id FOR UPDATE;
        IF NOT FOUND OR current_decision <> 'pending'
           OR current_revision <> NEW.expected_revision THEN
            RAISE EXCEPTION 'reprocess review revision conflict'
                USING ERRCODE = '40001';
        END IF;
        UPDATE paper_reprocess_review_heads
        SET review_revision = NEW.review_revision,
            decision = CASE WHEN NEW.decision = 'approve'
                            THEN 'approved' ELSE 'rejected' END,
            updated_at = NEW.created_at
        WHERE request_id = NEW.request_id;
    ELSIF TG_TABLE_NAME = 'paper_rollback_review_events' THEN
        SELECT review_revision, decision
        INTO current_revision, current_decision
        FROM paper_rollback_review_heads
        WHERE request_id = NEW.request_id FOR UPDATE;
        IF NOT FOUND OR current_decision <> 'pending'
           OR current_revision <> NEW.expected_revision THEN
            RAISE EXCEPTION 'rollback review revision conflict'
                USING ERRCODE = '40001';
        END IF;
        UPDATE paper_rollback_review_heads
        SET review_revision = NEW.review_revision,
            decision = CASE WHEN NEW.decision = 'approve'
                            THEN 'approved' ELSE 'rejected' END,
            updated_at = NEW.created_at
        WHERE request_id = NEW.request_id;
    ELSE
        RAISE EXCEPTION 'unexpected operation review table'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER paper_reprocess_review_event_apply
BEFORE INSERT ON paper_reprocess_review_events
FOR EACH ROW EXECUTE FUNCTION apply_paper_operation_review_event();

CREATE TRIGGER paper_rollback_review_event_apply
BEFORE INSERT ON paper_rollback_review_events
FOR EACH ROW EXECUTE FUNCTION apply_paper_operation_review_event();

CREATE FUNCTION reject_paper_operation_history_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'paper operation history is append-only'
        USING ERRCODE = '55000';
END;
$$;

CREATE TRIGGER paper_incident_events_immutable
BEFORE UPDATE OR DELETE ON paper_incident_events
FOR EACH ROW EXECUTE FUNCTION reject_paper_operation_history_mutation();

CREATE TRIGGER paper_reprocess_requests_immutable
BEFORE UPDATE OR DELETE ON paper_reprocess_requests
FOR EACH ROW EXECUTE FUNCTION reject_paper_operation_history_mutation();

CREATE TRIGGER paper_reprocess_targets_immutable
BEFORE UPDATE OR DELETE ON paper_reprocess_targets
FOR EACH ROW EXECUTE FUNCTION reject_paper_operation_history_mutation();

CREATE TRIGGER paper_reprocess_review_events_immutable
BEFORE UPDATE OR DELETE ON paper_reprocess_review_events
FOR EACH ROW EXECUTE FUNCTION reject_paper_operation_history_mutation();

CREATE TRIGGER paper_rollback_requests_immutable
BEFORE UPDATE OR DELETE ON paper_rollback_requests
FOR EACH ROW EXECUTE FUNCTION reject_paper_operation_history_mutation();

CREATE TRIGGER paper_rollback_targets_immutable
BEFORE UPDATE OR DELETE ON paper_rollback_targets
FOR EACH ROW EXECUTE FUNCTION reject_paper_operation_history_mutation();

CREATE TRIGGER paper_rollback_review_events_immutable
BEFORE UPDATE OR DELETE ON paper_rollback_review_events
FOR EACH ROW EXECUTE FUNCTION reject_paper_operation_history_mutation();
"""

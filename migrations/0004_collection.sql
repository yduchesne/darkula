-- SPDX-License-Identifier: AGPL-3.0-only
-- Darkula PR 9 migration: managed-source collection lifecycle.
--
-- Adds durable CollectionPolicy / CollectionRun / policy-endpoint
-- authorization plus the versioned stored functions the collection
-- scheduler, worker, and service invoke. 0001-0003 are immutable; all new
-- production database behavior lives here as versioned *_v1 functions.
--
-- Semantics:
--   * collection_policy owns durable schedule/authorization state for one
--     Source: active flag, positive bounded interval_seconds, explicit
--     next_due_at occurrence clock, revision counter, budget fields that
--     mirror the PR 7 CrawlRequest bounds, and a credential REFERENCE only
--     (never a secret value);
--   * collection_policy_endpoint is the relational authorization list
--     (managed endpoint identities, never URIs);
--   * collection_run is a historical execution record: (policy_id,
--     scheduled_for) is the semantic occurrence identity with a UNIQUE
--     constraint so concurrent schedulers can never admit the same
--     occurrence twice; policy_snapshot freezes the exact authorization
--     this run executed under so later policy edits never rewrite history;
--   * expected-state transitions (QUEUED -> RUNNING -> terminal) are
--     atomic WHERE-clause guards, not application checks;
--   * a crash after RUNNING is recovered by the execution lease: reclaim
--     is permitted only when the lease expired AND attempts remain;
--   * failure codes/summaries are the bounded sanitized vocabulary, never
--     raw exceptions/URIs/content/credentials.

CREATE TABLE collection_policy (
    policy_id       uuid PRIMARY KEY,
    source_id       uuid NOT NULL REFERENCES source(id) ON DELETE CASCADE,
    active          boolean NOT NULL,
    created_at      timestamptz NOT NULL,
    updated_at      timestamptz NOT NULL,
    revision        integer NOT NULL CHECK (revision >= 1),
    interval_seconds integer NOT NULL CHECK (
        interval_seconds >= 60 AND interval_seconds <= 31536000),
    next_due_at     timestamptz NOT NULL,
    allowed_paths   jsonb NOT NULL,
    max_pages       integer NOT NULL CHECK (max_pages >= 1 AND max_pages <= 10000),
    max_requests    integer NOT NULL CHECK (max_requests >= 1 AND max_requests <= 100000),
    max_depth       integer NOT NULL CHECK (max_depth >= 1 AND max_depth <= 64),
    timeout_seconds double precision NOT NULL
                    CHECK (timeout_seconds > 0 AND timeout_seconds <= 86400),
    authentication_reference text CHECK (
        authentication_reference IS NULL
        OR length(btrim(authentication_reference)) > 0),
    CHECK (updated_at >= created_at)
);

-- Relational endpoint authorization: managed endpoint identities only.
CREATE TABLE collection_policy_endpoint (
    policy_id   uuid NOT NULL REFERENCES collection_policy(policy_id)
                ON DELETE CASCADE,
    endpoint_id uuid NOT NULL REFERENCES source_endpoint(endpoint_id),
    PRIMARY KEY (policy_id, endpoint_id)
);

-- Deterministic due-scheduling order: next_due_at, policy identity.
CREATE INDEX collection_policy_due_idx
    ON collection_policy (next_due_at, policy_id);

CREATE TABLE collection_run (
    run_id                   uuid PRIMARY KEY,
    policy_id                uuid NOT NULL REFERENCES collection_policy(policy_id),
    policy_revision          integer NOT NULL CHECK (policy_revision >= 1),
    policy_snapshot          jsonb NOT NULL,
    source_id                uuid NOT NULL REFERENCES source(id),
    scheduled_for            timestamptz NOT NULL,
    created_at               timestamptz NOT NULL,
    started_at               timestamptz,
    completed_at             timestamptz,
    status                   text NOT NULL CHECK (status IN (
        'QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED'
    )),
    execution_id             uuid,
    lease_expires_at         timestamptz,
    attempt_count            integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    crawl_requests_attempted integer NOT NULL DEFAULT 0
                              CHECK (crawl_requests_attempted >= 0),
    pages_observed           integer NOT NULL DEFAULT 0 CHECK (pages_observed >= 0),
    content_observations     integer NOT NULL DEFAULT 0
                              CHECK (content_observations >= 0),
    content_created          integer NOT NULL DEFAULT 0 CHECK (content_created >= 0),
    content_deduplicated     integer NOT NULL DEFAULT 0
                              CHECK (content_deduplicated >= 0),
    failure_code             text CHECK (
        failure_code IS NULL OR failure_code IN (
            'SOURCE_INACTIVE',
            'POLICY_INACTIVE',
            'NO_ACTIVE_ENDPOINT',
            'CRAWL_FAILED',
            'NORMALIZATION_FAILED',
            'CONTENT_PERSISTENCE_FAILED',
            'ATTEMPTS_EXHAUSTED',
            'INVALID_STATE'
        )),
    failure_summary          text CHECK (
        failure_summary IS NULL OR length(btrim(failure_summary)) > 0),
    -- One run per semantic occurrence; the uniqueness is the scheduler race
    -- arbiter (concurrent schedulers -> exactly one run + one command).
    UNIQUE (policy_id, scheduled_for)
);

CREATE INDEX collection_run_status_idx
    ON collection_run (status, created_at, run_id);

-- ========================================================================
-- Versioned stored functions (production collection persistence API).
-- ========================================================================

-- Create one policy. Returns 'added' / 'duplicate' / 'unknown_source'.
CREATE FUNCTION collection_policy_create_v1(
    p_policy_id               uuid,
    p_source_id               uuid,
    p_active                  boolean,
    p_created_at              timestamptz,
    p_updated_at              timestamptz,
    p_revision                integer,
    p_interval_seconds        integer,
    p_next_due_at             timestamptz,
    p_allowed_paths           jsonb,
    p_max_pages               integer,
    p_max_requests            integer,
    p_max_depth               integer,
    p_timeout_seconds         double precision,
    p_authentication_reference text
) RETURNS text
LANGUAGE plpgsql
AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM source WHERE id = p_source_id) THEN
        RETURN 'unknown_source';
    END IF;
    IF EXISTS (SELECT 1 FROM collection_policy WHERE policy_id = p_policy_id) THEN
        RETURN 'duplicate';
    END IF;
    INSERT INTO collection_policy
        (policy_id, source_id, active, created_at, updated_at, revision,
         interval_seconds, next_due_at, allowed_paths, max_pages,
         max_requests, max_depth, timeout_seconds, authentication_reference)
    VALUES
        (p_policy_id, p_source_id, p_active, p_created_at, p_updated_at,
         p_revision, p_interval_seconds, p_next_due_at, p_allowed_paths,
         p_max_pages, p_max_requests, p_max_depth, p_timeout_seconds,
         p_authentication_reference);
    RETURN 'added';
END;
$$;

-- Authorize one managed endpoint for a policy. The endpoint must exist and
-- must belong to the policy's Source. Returns 'added' / 'duplicate' /
-- 'unknown_policy' / 'unknown_endpoint' / 'endpoint_not_owned'.
CREATE FUNCTION collection_policy_endpoint_add_v1(
    p_policy_id   uuid,
    p_endpoint_id uuid
) RETURNS text
LANGUAGE plpgsql
AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM collection_policy WHERE policy_id = p_policy_id) THEN
        RETURN 'unknown_policy';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM source_endpoint WHERE endpoint_id = p_endpoint_id) THEN
        RETURN 'unknown_endpoint';
    END IF;
    IF NOT EXISTS (
        SELECT 1
          FROM collection_policy p
          JOIN source_endpoint e ON e.source_id = p.source_id
         WHERE p.policy_id = p_policy_id
           AND e.endpoint_id = p_endpoint_id
    ) THEN
        RETURN 'endpoint_not_owned';
    END IF;
    INSERT INTO collection_policy_endpoint (policy_id, endpoint_id)
    VALUES (p_policy_id, p_endpoint_id)
    ON CONFLICT (policy_id, endpoint_id) DO NOTHING;
    RETURN 'added';
END;
$$;

-- Load one policy plus its authorized endpoint identities.
CREATE FUNCTION collection_policy_get_v1(p_policy_id uuid)
RETURNS TABLE (
    policy_id                uuid,
    source_id                uuid,
    active                   boolean,
    created_at               timestamptz,
    updated_at               timestamptz,
    revision                 integer,
    interval_seconds         integer,
    next_due_at              timestamptz,
    allowed_paths            jsonb,
    max_pages                integer,
    max_requests             integer,
    max_depth                integer,
    timeout_seconds          double precision,
    authentication_reference text,
    endpoint_ids             uuid[]
)
LANGUAGE sql
AS $$
    SELECT p.policy_id, p.source_id, p.active, p.created_at, p.updated_at,
           p.revision, p.interval_seconds, p.next_due_at, p.allowed_paths,
           p.max_pages, p.max_requests, p.max_depth, p.timeout_seconds,
           p.authentication_reference,
           COALESCE((
               SELECT array_agg(pe.endpoint_id ORDER BY pe.endpoint_id)
                 FROM collection_policy_endpoint pe
                WHERE pe.policy_id = p.policy_id
           ), ARRAY[]::uuid[])
      FROM collection_policy p
     WHERE p.policy_id = p_policy_id;
$$;

-- Edit a policy: bumps the revision counter and replaces the endpoint
-- authorization list atomically. Returns 'updated' / 'unknown_policy' /
-- 'unknown_endpoint' / 'endpoint_not_owned'.
CREATE FUNCTION collection_policy_update_v1(
    p_policy_id                uuid,
    p_active                   boolean,
    p_updated_at               timestamptz,
    p_interval_seconds         integer,
    p_next_due_at              timestamptz,
    p_allowed_paths            jsonb,
    p_max_pages                integer,
    p_max_requests             integer,
    p_max_depth                integer,
    p_timeout_seconds          double precision,
    p_authentication_reference text,
    p_endpoint_ids             uuid[]
) RETURNS text
LANGUAGE plpgsql
AS $$
DECLARE
    v_source_id uuid;
    v_endpoint uuid;
    v_new_revision integer;
BEGIN
    SELECT p.source_id, p.revision + 1 INTO v_source_id, v_new_revision
      FROM collection_policy p
     WHERE p.policy_id = p_policy_id;
    IF NOT FOUND THEN
        RETURN 'unknown_policy';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM source_endpoint
         WHERE endpoint_id = ANY(p_endpoint_ids)
    ) THEN
        RETURN 'unknown_endpoint';
    END IF;
    FOR v_endpoint IN SELECT unnest(p_endpoint_ids) LOOP
        IF v_endpoint IS NULL THEN
            CONTINUE;
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM source_endpoint e
             WHERE e.endpoint_id = v_endpoint
               AND e.source_id = v_source_id
        ) THEN
            RETURN 'endpoint_not_owned';
        END IF;
    END LOOP;
    UPDATE collection_policy
       SET active = p_active,
           updated_at = p_updated_at,
           revision = v_new_revision,
           interval_seconds = p_interval_seconds,
           next_due_at = p_next_due_at,
           allowed_paths = p_allowed_paths,
           max_pages = p_max_pages,
           max_requests = p_max_requests,
           max_depth = p_max_depth,
           timeout_seconds = p_timeout_seconds,
           authentication_reference = p_authentication_reference
     WHERE policy_id = p_policy_id;
    DELETE FROM collection_policy_endpoint WHERE policy_id = p_policy_id;
    INSERT INTO collection_policy_endpoint (policy_id, endpoint_id)
    SELECT p_policy_id, unnest(p_endpoint_ids)
     WHERE p_endpoint_ids IS NOT NULL
       AND cardinality(p_endpoint_ids) > 0;
    RETURN 'updated';
END;
$$;

-- List the endpoints currently authorized by a policy (source_endpoint view).
CREATE FUNCTION collection_policy_endpoint_list_v1(p_policy_id uuid)
RETURNS TABLE (
    endpoint_id        uuid,
    source_id          uuid,
    uri                text,
    endpoint_type      text,
    status             text,
    first_observed_at  timestamptz,
    last_observed_at   timestamptz,
    metadata           jsonb
)
LANGUAGE sql
AS $$
    SELECT e.endpoint_id, e.source_id, e.uri, e.endpoint_type, e.status,
           e.first_observed_at, e.last_observed_at, e.metadata
      FROM collection_policy_endpoint pe
      JOIN source_endpoint e ON e.endpoint_id = pe.endpoint_id
     WHERE pe.policy_id = p_policy_id
     ORDER BY e.endpoint_id;
$$;

-- Load one endpoint by identity (the service resolves frozen snapshot
-- endpoint ids against current endpoint state at execution start).
CREATE FUNCTION collection_endpoint_get_v1(p_endpoint_id uuid)
RETURNS TABLE (
    endpoint_id        uuid,
    source_id          uuid,
    uri                text,
    endpoint_type      text,
    status             text,
    first_observed_at  timestamptz,
    last_observed_at   timestamptz,
    metadata           jsonb
)
LANGUAGE sql
AS $$
    SELECT e.endpoint_id, e.source_id, e.uri, e.endpoint_type, e.status,
           e.first_observed_at, e.last_observed_at, e.metadata
      FROM source_endpoint e
     WHERE e.endpoint_id = p_endpoint_id;
$$;

-- Directly create one run (test/manual path). Returns 'added' /
-- 'duplicate' (run id) / 'occurrence_exists' (a run for the same
-- (policy_id, scheduled_for) already exists) / 'unknown_policy'.
CREATE FUNCTION collection_run_create_v1(
    p_run_id            uuid,
    p_policy_id         uuid,
    p_policy_revision   integer,
    p_policy_snapshot   jsonb,
    p_source_id         uuid,
    p_scheduled_for     timestamptz,
    p_created_at        timestamptz
) RETURNS text
LANGUAGE plpgsql
AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM collection_policy WHERE policy_id = p_policy_id) THEN
        RETURN 'unknown_policy';
    END IF;
    IF EXISTS (SELECT 1 FROM collection_run WHERE run_id = p_run_id) THEN
        RETURN 'duplicate';
    END IF;
    IF EXISTS (
        SELECT 1 FROM collection_run
         WHERE policy_id = p_policy_id AND scheduled_for = p_scheduled_for
    ) THEN
        RETURN 'occurrence_exists';
    END IF;
    INSERT INTO collection_run
        (run_id, policy_id, policy_revision, policy_snapshot, source_id,
         scheduled_for, created_at, status)
    VALUES
        (p_run_id, p_policy_id, p_policy_revision, p_policy_snapshot,
         p_source_id, p_scheduled_for, p_created_at, 'QUEUED');
    RETURN 'added';
END;
$$;

-- Atomic due-work admission (PR 9 invariant 1.8): in ONE statement the
-- function claims the earliest due active policy (row lock, SKIP LOCKED),
-- creates the QUEUED run for the occurrence, and advances the policy's
-- next_due_at strictly beyond ``p_now``. The scheduler persists the
-- collection.execute command in the outbox IN THE SAME TRANSACTION, so a
-- scheduled occurrence has a durable queued run iff its command is durably
-- in the outbox. Duplicate occurrences (a concurrent scheduler won) return
-- 'occurrence_exists' with no state change; 'none' when nothing is due.
CREATE FUNCTION collection_schedule_due_v1(
    p_now        timestamptz,
    p_run_id     uuid,
    p_created_at timestamptz
) RETURNS TABLE (
    run_id           uuid,
    policy_id        uuid,
    policy_revision  integer,
    policy_snapshot  jsonb,
    source_id        uuid,
    scheduled_for    timestamptz,
    result           text
)
LANGUAGE plpgsql
AS $$
DECLARE
    v_policy        collection_policy%ROWTYPE;
    v_scheduled     timestamptz;
    v_next          timestamptz;
    v_snapshot      jsonb;
BEGIN
    SELECT p.* INTO v_policy
      FROM collection_policy p
     WHERE p.active = true
       AND p.next_due_at <= p_now
     ORDER BY p.next_due_at, p.policy_id
     LIMIT 1
       FOR UPDATE SKIP LOCKED;
    IF NOT FOUND THEN
        RETURN QUERY SELECT p_run_id, NULL::uuid, NULL::integer, NULL::jsonb,
                            NULL::uuid, NULL::timestamptz, 'none'::text;
        RETURN;
    END IF;
    v_scheduled := v_policy.next_due_at;
    v_snapshot := jsonb_build_object(
        'policy_revision', v_policy.revision,
        'interval_seconds', v_policy.interval_seconds,
        'allowed_endpoint_ids', COALESCE((
            SELECT jsonb_agg(pe.endpoint_id ORDER BY pe.endpoint_id)
              FROM collection_policy_endpoint pe
             WHERE pe.policy_id = v_policy.policy_id
        ), '[]'::jsonb),
        'allowed_paths', v_policy.allowed_paths,
        'max_pages', v_policy.max_pages,
        'max_requests', v_policy.max_requests,
        'max_depth', v_policy.max_depth,
        'timeout_seconds', v_policy.timeout_seconds,
        'authentication_reference', v_policy.authentication_reference
    );
    BEGIN
        INSERT INTO collection_run
            (run_id, policy_id, policy_revision, policy_snapshot, source_id,
             scheduled_for, created_at, status)
        VALUES
            (p_run_id, v_policy.policy_id, v_policy.revision, v_snapshot,
             v_policy.source_id, v_scheduled, p_created_at, 'QUEUED');
    EXCEPTION WHEN unique_violation THEN
        RETURN QUERY SELECT p_run_id, v_policy.policy_id, v_policy.revision,
                            NULL::jsonb, v_policy.source_id, v_scheduled,
                            'occurrence_exists'::text;
        RETURN;
    END;
    v_next := v_scheduled + make_interval(secs => v_policy.interval_seconds);
    WHILE v_next <= p_now LOOP
        v_next := v_next + make_interval(secs => v_policy.interval_seconds);
    END LOOP;
    UPDATE collection_policy
       SET next_due_at = v_next
     WHERE collection_policy.policy_id = v_policy.policy_id;
    RETURN QUERY SELECT p_run_id, v_policy.policy_id, v_policy.revision,
                        v_snapshot, v_policy.source_id, v_scheduled,
                        'scheduled'::text;
END;
$$;

-- Load one run with its frozen snapshot.
CREATE FUNCTION collection_run_get_v1(p_run_id uuid)
RETURNS TABLE (
    run_id                   uuid,
    policy_id                uuid,
    policy_revision          integer,
    policy_snapshot          jsonb,
    source_id                uuid,
    scheduled_for            timestamptz,
    created_at               timestamptz,
    started_at               timestamptz,
    completed_at             timestamptz,
    status                   text,
    execution_id             uuid,
    lease_expires_at         timestamptz,
    attempt_count            integer,
    crawl_requests_attempted integer,
    pages_observed           integer,
    content_observations     integer,
    content_created          integer,
    content_deduplicated     integer,
    failure_code             text,
    failure_summary          text
)
LANGUAGE sql
AS $$
    SELECT r.run_id, r.policy_id, r.policy_revision, r.policy_snapshot,
           r.source_id, r.scheduled_for, r.created_at, r.started_at,
           r.completed_at, r.status, r.execution_id, r.lease_expires_at,
           r.attempt_count, r.crawl_requests_attempted, r.pages_observed,
           r.content_observations, r.content_created, r.content_deduplicated,
           r.failure_code, r.failure_summary
      FROM collection_run r
     WHERE r.run_id = p_run_id;
$$;

-- Claim a QUEUED run for execution: QUEUED -> RUNNING, first lease, attempt
-- increment (0 -> 1). Returns 'claimed' / 'not_queued' / 'unknown_run'.
CREATE FUNCTION collection_run_claim_v1(
    p_run_id           uuid,
    p_execution_id     uuid,
    p_started_at       timestamptz,
    p_lease_expires_at timestamptz
) RETURNS text
LANGUAGE plpgsql
AS $$
BEGIN
    UPDATE collection_run
       SET status = 'RUNNING',
           execution_id = p_execution_id,
           started_at = COALESCE(started_at, p_started_at),
           lease_expires_at = p_lease_expires_at,
           attempt_count = attempt_count + 1
     WHERE run_id = p_run_id
       AND status = 'QUEUED';
    IF FOUND THEN
        RETURN 'claimed';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM collection_run WHERE run_id = p_run_id) THEN
        RETURN 'unknown_run';
    END IF;
    RETURN 'not_queued';
END;
$$;

-- Reclaim a crashed RUNNING run whose lease expired, incrementing the
-- attempt. Returns 'reclaimed' / 'lease_active' / 'attempts_exhausted' /
-- 'terminal' / 'unknown_run'.
CREATE FUNCTION collection_run_reclaim_v1(
    p_run_id           uuid,
    p_execution_id     uuid,
    p_now              timestamptz,
    p_lease_expires_at timestamptz,
    p_max_attempts     integer
) RETURNS text
LANGUAGE plpgsql
AS $$
DECLARE
    v_status text;
    v_lease  timestamptz;
    v_attempts integer;
BEGIN
    UPDATE collection_run
       SET execution_id = p_execution_id,
           lease_expires_at = p_lease_expires_at,
           attempt_count = attempt_count + 1
     WHERE run_id = p_run_id
       AND status = 'RUNNING'
       AND lease_expires_at IS NOT NULL
       AND lease_expires_at < p_now
       AND attempt_count < p_max_attempts;
    IF FOUND THEN
        RETURN 'reclaimed';
    END IF;
    SELECT r.status, r.lease_expires_at, r.attempt_count
      INTO v_status, v_lease, v_attempts
      FROM collection_run r
     WHERE r.run_id = p_run_id;
    IF NOT FOUND THEN
        RETURN 'unknown_run';
    END IF;
    IF v_status <> 'RUNNING' THEN
        RETURN 'terminal';
    END IF;
    IF v_lease IS NOT NULL AND v_lease >= p_now THEN
        RETURN 'lease_active';
    END IF;
    RETURN 'attempts_exhausted';
END;
$$;

-- Finalize a RUNNING run into its terminal state with accurate counters.
-- Only RUNNING runs transition; returns 'updated' / 'wrong_state' /
-- 'unknown_run'.
CREATE FUNCTION collection_run_complete_v1(
    p_run_id                   uuid,
    p_new_status               text,
    p_completed_at             timestamptz,
    p_crawl_requests_attempted integer,
    p_pages_observed           integer,
    p_content_observations     integer,
    p_content_created          integer,
    p_content_deduplicated     integer,
    p_failure_code             text,
    p_failure_summary          text
) RETURNS text
LANGUAGE plpgsql
AS $$
BEGIN
    UPDATE collection_run
       SET status = p_new_status,
           completed_at = p_completed_at,
           crawl_requests_attempted = p_crawl_requests_attempted,
           pages_observed = p_pages_observed,
           content_observations = p_content_observations,
           content_created = p_content_created,
           content_deduplicated = p_content_deduplicated,
           failure_code = p_failure_code,
           failure_summary = p_failure_summary,
           execution_id = NULL,
           lease_expires_at = NULL
     WHERE run_id = p_run_id
       AND status = 'RUNNING'
       AND p_new_status IN ('SUCCEEDED', 'FAILED', 'CANCELLED');
    IF FOUND THEN
        RETURN 'updated';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM collection_run WHERE run_id = p_run_id) THEN
        RETURN 'unknown_run';
    END IF;
    RETURN 'wrong_state';
END;
$$;

-- Cancel a QUEUED or RUNNING run (authorization withdrawn before start,
-- or best-effort caller cancellation). Returns 'cancelled' /
-- 'wrong_state' / 'unknown_run'.
CREATE FUNCTION collection_run_cancel_v1(
    p_run_id       uuid,
    p_completed_at timestamptz
) RETURNS text
LANGUAGE plpgsql
AS $$
BEGIN
    UPDATE collection_run
       SET status = 'CANCELLED',
           completed_at = p_completed_at,
           execution_id = NULL,
           lease_expires_at = NULL
     WHERE run_id = p_run_id
       AND status IN ('QUEUED', 'RUNNING');
    IF FOUND THEN
        RETURN 'cancelled';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM collection_run WHERE run_id = p_run_id) THEN
        RETURN 'unknown_run';
    END IF;
    RETURN 'wrong_state';
END;
$$;

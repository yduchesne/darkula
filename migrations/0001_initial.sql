-- SPDX-License-Identifier: AGPL-3.0-only
-- Darkula PR 4 initial migration: source-domain schema and versioned
-- stored functions.
--
-- This artifact OWNS all production SQL. Production Python repository code
-- contains no data-access SQL: it only invokes the versioned functions
-- defined here (the driver's stored-function call mechanism). Future
-- changes MUST arrive as new numbered migrations with new function
-- versions (for example ..._v2); merged historical migrations are
-- immutable.
--
-- Versioning convention: every production function is named <op>_v1 and
-- lives in the default (public) schema. Signatures and result column order
-- are explicit and centralized in
-- src/darkula/infrastructure/persistence/postgresql/mapping.py.
--
-- Semantic rules enforced structurally:
--   * candidate / source / endpoint / assessment identities are UUID PKs;
--   * every child row references its owner through an explicit FK;
--   * deliberately open structured fields use jsonb (never relationalized
--     speculative ontology);
--   * endpoint URIs are locators, NOT global identity: no unique constraint
--     is asserted on uri (no accidental global URI -> Source uniqueness);
--   * no multi-tenant columns, no soft deletion, no workflow triggers.

CREATE TABLE source_candidate (
    id               uuid PRIMARY KEY,
    discovered_at    timestamptz NOT NULL,
    discovery_method text        NOT NULL CHECK (length(btrim(discovery_method)) > 0),
    entrypoint       text        NOT NULL CHECK (length(btrim(entrypoint)) > 0),
    status           text        NOT NULL CHECK (status IN (
        'DISCOVERED',
        'RECONNAISSANCE_PENDING',
        'UNDER_RECONNAISSANCE',
        'QUALIFIED',
        'REJECTED',
        'PROMOTED'
    )),
    discovery_context jsonb
);

CREATE TABLE source_candidate_event_history (
    event_id     uuid PRIMARY KEY,
    candidate_id uuid NOT NULL REFERENCES source_candidate(id)
                 ON DELETE CASCADE,
    event_type   text NOT NULL CHECK (event_type IN (
        'DISCOVERED',
        'RECONNAISSANCE_STARTED',
        'RECONNAISSANCE_COMPLETED',
        'NEEDS_MORE_RECON',
        'QUALIFIED',
        'REJECTED',
        'PROMOTED'
    )),
    occurred_at timestamptz NOT NULL,
    context     jsonb,
    reason      text CHECK (reason IS NULL OR length(btrim(reason)) > 0),
    provenance  text CHECK (provenance IS NULL OR length(btrim(provenance)) > 0)
);

-- Deterministic candidate history order: candidate_id + occurred_at +
-- stable tie-breaker (event_id).
CREATE INDEX source_candidate_event_history_order_idx
    ON source_candidate_event_history (candidate_id, occurred_at, event_id);

CREATE TABLE recon_assessment (
    assessment_id       uuid PRIMARY KEY,
    candidate_id        uuid NOT NULL REFERENCES source_candidate(id)
                        ON DELETE CASCADE,
    assessed_at         timestamptz NOT NULL,
    disposition         text NOT NULL CHECK (disposition IN (
        'QUALIFY',
        'NEEDS_MORE_RECON',
        'REJECT'
    )),
    confidence          double precision NOT NULL
                        CHECK (confidence >= 0 AND confidence <= 1),
    evidence_references jsonb NOT NULL,
    characteristics     jsonb
);

-- Deterministic recon assessment order: candidate_id + assessed_at +
-- stable tie-breaker (assessment_id).
CREATE INDEX recon_assessment_order_idx
    ON recon_assessment (candidate_id, assessed_at, assessment_id);

CREATE TABLE source (
    id         uuid PRIMARY KEY,
    status     text NOT NULL CHECK (status IN ('ACTIVE', 'INACTIVE')),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    name       text CHECK (name IS NULL OR length(btrim(name)) > 0),
    CHECK (updated_at >= created_at)
);

CREATE TABLE source_endpoint (
    endpoint_id        uuid PRIMARY KEY,
    source_id          uuid NOT NULL REFERENCES source(id) ON DELETE CASCADE,
    uri                text NOT NULL CHECK (length(btrim(uri)) > 0),
    endpoint_type      text NOT NULL CHECK (endpoint_type IN (
        'ONION',
        'CLEARNET',
        'MIRROR',
        'API',
        'FEED'
    )),
    status             text NOT NULL CHECK (status IN ('ACTIVE', 'INACTIVE')),
    first_observed_at  timestamptz NOT NULL,
    last_observed_at   timestamptz NOT NULL,
    metadata           jsonb,
    CHECK (last_observed_at >= first_observed_at)
);

-- Deterministic endpoint order per source.
CREATE INDEX source_endpoint_order_idx
    ON source_endpoint (source_id, endpoint_id);

CREATE TABLE source_assessment (
    assessment_id       uuid PRIMARY KEY,
    source_id           uuid NOT NULL REFERENCES source(id) ON DELETE CASCADE,
    assessed_at         timestamptz NOT NULL,
    window_start        timestamptz NOT NULL,
    window_end          timestamptz NOT NULL,
    confidence          double precision NOT NULL
                        CHECK (confidence >= 0 AND confidence <= 1),
    relevance           double precision CHECK (relevance IS NULL OR (relevance >= 0 AND relevance <= 1)),
    activity            double precision CHECK (activity IS NULL OR (activity >= 0 AND activity <= 1)),
    novelty             double precision CHECK (novelty IS NULL OR (novelty >= 0 AND novelty <= 1)),
    evidence_references jsonb NOT NULL,
    characteristics     jsonb,
    CHECK (window_end >= window_start)
);

-- Deterministic source assessment order: source_id + assessed_at +
-- stable tie-breaker (assessment_id).
CREATE INDEX source_assessment_order_idx
    ON source_assessment (source_id, assessed_at, assessment_id);

-- ========================================================================
-- Versioned stored functions (production persistence API).
-- Convention: every controlled conflict returns a sentinel value instead
-- of raising, so the enclosing transaction stays correctly resolvable.
-- Real constraint violations remain DB-enforced and surface through their
-- SQLSTATE classes as the defensive fallback.
-- ========================================================================

CREATE FUNCTION candidate_create_v1(
    p_candidate_id      uuid,
    p_discovered_at     timestamptz,
    p_discovery_method  text,
    p_entrypoint        text,
    p_status            text,
    p_discovery_context jsonb,
    p_event_id          uuid,
    p_event_type        text,
    p_occurred_at       timestamptz,
    p_event_context     jsonb,
    p_event_reason      text,
    p_event_provenance  text
) RETURNS boolean
LANGUAGE plpgsql
AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM source_candidate WHERE id = p_candidate_id)
       OR EXISTS (SELECT 1 FROM source_candidate_event_history WHERE event_id = p_event_id) THEN
        RETURN false;
    END IF;
    INSERT INTO source_candidate
        (id, discovered_at, discovery_method, entrypoint, status, discovery_context)
    VALUES
        (p_candidate_id, p_discovered_at, p_discovery_method, p_entrypoint,
         p_status, p_discovery_context);
    INSERT INTO source_candidate_event_history
        (event_id, candidate_id, event_type, occurred_at, context, reason, provenance)
    VALUES
        (p_event_id, p_candidate_id, p_event_type, p_occurred_at,
         p_event_context, p_event_reason, p_event_provenance);
    RETURN true;
END;
$$;

CREATE FUNCTION candidate_get_v1(p_candidate_id uuid)
RETURNS TABLE (
    id               uuid,
    discovered_at    timestamptz,
    discovery_method text,
    entrypoint       text,
    status           text,
    discovery_context jsonb
)
LANGUAGE sql
AS $$
    SELECT c.id, c.discovered_at, c.discovery_method, c.entrypoint,
           c.status, c.discovery_context
      FROM source_candidate c
     WHERE c.id = p_candidate_id;
$$;

-- Atomic transition: updates status and appends exactly one event in one
-- database operation. Returns false (no update, no event) when the current
-- status differs from the expected status.
CREATE FUNCTION candidate_transition_v1(
    p_candidate_id     uuid,
    p_expected_status  text,
    p_new_status       text,
    p_event_id         uuid,
    p_event_type       text,
    p_occurred_at      timestamptz,
    p_event_context    jsonb,
    p_event_reason     text,
    p_event_provenance text
) RETURNS boolean
LANGUAGE plpgsql
AS $$
BEGIN
    UPDATE source_candidate
       SET status = p_new_status
     WHERE id = p_candidate_id AND status = p_expected_status;
    IF NOT FOUND THEN
        RETURN false;
    END IF;
    INSERT INTO source_candidate_event_history
        (event_id, candidate_id, event_type, occurred_at, context, reason, provenance)
    VALUES
        (p_event_id, p_candidate_id, p_event_type, p_occurred_at,
         p_event_context, p_event_reason, p_event_provenance);
    RETURN true;
END;
$$;

CREATE FUNCTION candidate_event_append_v1(
    p_event_id         uuid,
    p_candidate_id     uuid,
    p_event_type       text,
    p_occurred_at      timestamptz,
    p_context          jsonb,
    p_reason           text,
    p_provenance       text
) RETURNS text
LANGUAGE plpgsql
AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM source_candidate WHERE id = p_candidate_id) THEN
        RETURN 'unknown_candidate';
    END IF;
    IF EXISTS (SELECT 1 FROM source_candidate_event_history WHERE event_id = p_event_id) THEN
        RETURN 'duplicate';
    END IF;
    INSERT INTO source_candidate_event_history
        (event_id, candidate_id, event_type, occurred_at, context, reason, provenance)
    VALUES
        (p_event_id, p_candidate_id, p_event_type, p_occurred_at,
         p_context, p_reason, p_provenance);
    RETURN 'added';
END;
$$;

CREATE FUNCTION candidate_history_list_v1(p_candidate_id uuid)
RETURNS TABLE (
    event_id     uuid,
    candidate_id uuid,
    event_type   text,
    occurred_at  timestamptz,
    context      jsonb,
    reason       text,
    provenance   text
)
LANGUAGE sql
AS $$
    SELECT h.event_id, h.candidate_id, h.event_type, h.occurred_at,
           h.context, h.reason, h.provenance
      FROM source_candidate_event_history h
     WHERE h.candidate_id = p_candidate_id
     ORDER BY h.occurred_at, h.event_id;
$$;

CREATE FUNCTION recon_assessment_append_v1(
    p_assessment_id      uuid,
    p_candidate_id       uuid,
    p_assessed_at        timestamptz,
    p_disposition        text,
    p_confidence         double precision,
    p_evidence_references jsonb,
    p_characteristics    jsonb
) RETURNS text
LANGUAGE plpgsql
AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM source_candidate WHERE id = p_candidate_id) THEN
        RETURN 'unknown_candidate';
    END IF;
    IF EXISTS (SELECT 1 FROM recon_assessment WHERE assessment_id = p_assessment_id) THEN
        RETURN 'duplicate';
    END IF;
    INSERT INTO recon_assessment
        (assessment_id, candidate_id, assessed_at, disposition, confidence,
         evidence_references, characteristics)
    VALUES
        (p_assessment_id, p_candidate_id, p_assessed_at, p_disposition,
         p_confidence, p_evidence_references, p_characteristics);
    RETURN 'added';
END;
$$;

CREATE FUNCTION recon_assessment_list_v1(p_candidate_id uuid)
RETURNS TABLE (
    assessment_id       uuid,
    candidate_id        uuid,
    assessed_at         timestamptz,
    disposition         text,
    confidence          double precision,
    evidence_references jsonb,
    characteristics     jsonb
)
LANGUAGE sql
AS $$
    SELECT r.assessment_id, r.candidate_id, r.assessed_at, r.disposition,
           r.confidence, r.evidence_references, r.characteristics
      FROM recon_assessment r
     WHERE r.candidate_id = p_candidate_id
     ORDER BY r.assessed_at, r.assessment_id;
$$;

CREATE FUNCTION source_create_v1(
    p_source_id uuid,
    p_status    text,
    p_created_at timestamptz,
    p_updated_at timestamptz,
    p_name      text
) RETURNS boolean
LANGUAGE plpgsql
AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM source WHERE id = p_source_id) THEN
        RETURN false;
    END IF;
    INSERT INTO source (id, status, created_at, updated_at, name)
    VALUES (p_source_id, p_status, p_created_at, p_updated_at, p_name);
    RETURN true;
END;
$$;

CREATE FUNCTION source_get_v1(p_source_id uuid)
RETURNS TABLE (
    id         uuid,
    status     text,
    created_at timestamptz,
    updated_at timestamptz,
    name       text
)
LANGUAGE sql
AS $$
    SELECT s.id, s.status, s.created_at, s.updated_at, s.name
      FROM source s
     WHERE s.id = p_source_id;
$$;

CREATE FUNCTION source_endpoint_add_v1(
    p_endpoint_id       uuid,
    p_source_id         uuid,
    p_uri               text,
    p_endpoint_type     text,
    p_status            text,
    p_first_observed_at timestamptz,
    p_last_observed_at  timestamptz,
    p_metadata          jsonb
) RETURNS text
LANGUAGE plpgsql
AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM source WHERE id = p_source_id) THEN
        RETURN 'unknown_source';
    END IF;
    IF EXISTS (SELECT 1 FROM source_endpoint WHERE endpoint_id = p_endpoint_id) THEN
        RETURN 'duplicate';
    END IF;
    INSERT INTO source_endpoint
        (endpoint_id, source_id, uri, endpoint_type, status,
         first_observed_at, last_observed_at, metadata)
    VALUES
        (p_endpoint_id, p_source_id, p_uri, p_endpoint_type, p_status,
         p_first_observed_at, p_last_observed_at, p_metadata);
    RETURN 'added';
END;
$$;

CREATE FUNCTION source_endpoint_list_v1(p_source_id uuid)
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
     WHERE e.source_id = p_source_id
     ORDER BY e.endpoint_id;
$$;

CREATE FUNCTION source_assessment_append_v1(
    p_assessment_id      uuid,
    p_source_id          uuid,
    p_assessed_at        timestamptz,
    p_window_start       timestamptz,
    p_window_end         timestamptz,
    p_confidence         double precision,
    p_relevance          double precision,
    p_activity           double precision,
    p_novelty            double precision,
    p_evidence_references jsonb,
    p_characteristics    jsonb
) RETURNS text
LANGUAGE plpgsql
AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM source WHERE id = p_source_id) THEN
        RETURN 'unknown_source';
    END IF;
    IF EXISTS (SELECT 1 FROM source_assessment WHERE assessment_id = p_assessment_id) THEN
        RETURN 'duplicate';
    END IF;
    INSERT INTO source_assessment
        (assessment_id, source_id, assessed_at, window_start, window_end,
         confidence, relevance, activity, novelty,
         evidence_references, characteristics)
    VALUES
        (p_assessment_id, p_source_id, p_assessed_at, p_window_start,
         p_window_end, p_confidence, p_relevance, p_activity, p_novelty,
         p_evidence_references, p_characteristics);
    RETURN 'added';
END;
$$;

CREATE FUNCTION source_assessment_list_v1(p_source_id uuid)
RETURNS TABLE (
    assessment_id       uuid,
    source_id           uuid,
    assessed_at         timestamptz,
    window_start        timestamptz,
    window_end          timestamptz,
    confidence          double precision,
    relevance           double precision,
    activity            double precision,
    novelty             double precision,
    evidence_references jsonb,
    characteristics     jsonb
)
LANGUAGE sql
AS $$
    SELECT a.assessment_id, a.source_id, a.assessed_at, a.window_start,
           a.window_end, a.confidence, a.relevance, a.activity, a.novelty,
           a.evidence_references, a.characteristics
      FROM source_assessment a
     WHERE a.source_id = p_source_id
     ORDER BY a.assessed_at, a.assessment_id;
$$;
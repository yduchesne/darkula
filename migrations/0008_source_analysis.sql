-- SPDX-License-Identifier: AGPL-3.0-only
-- Darkula PR 14 migration: immutable/versioned source-analysis assessments.
--
-- Additive and immutable. 0001-0007 remain unchanged. Every new database
-- behavior lives here as versioned stored functions invoked by the Python
-- adapter; production Python contains no SQL.
--
-- Semantics:
--   * source_assessment gains bounded developer-controlled profile provenance
--     (profile_name, profile_version). The semantic/idempotency identity is
--     (source_id, window_start, window_end, profile_name, profile_version) and
--     is UNIQUE, so concurrent/duplicate analysis can never create a second
--     durable assessment for the same Source/window/profile. A new profile
--     version coexists immutably with the old one.
--   * pre-PR14 rows are preserved and deterministically backfilled with the
--     explicit legacy profile; assessment history is never destroyed.
--   * source_assessment_get_by_profile_v1 supports a replay short-circuit
--     without a model call.
--   * normalized_content_list_for_requests_v1 selects observations only by
--     deterministic crawl-request identity (provenance ownership), never by
--     URI matching.
--   * collection_run_list_for_source_window_v1 lists the persisted runs whose
--     execution may overlap a historical window.

-- Bounded profile provenance. NULL is allowed only transiently for the
-- additive backfill below.
ALTER TABLE source_assessment ADD COLUMN profile_name text;
ALTER TABLE source_assessment ADD COLUMN profile_version text;

-- Preserve pre-PR14 history with one explicit deterministic legacy profile.
UPDATE source_assessment
   SET profile_name = 'legacy',
       profile_version = 'v0'
 WHERE profile_name IS NULL;

ALTER TABLE source_assessment ALTER COLUMN profile_name SET NOT NULL;
ALTER TABLE source_assessment ALTER COLUMN profile_version SET NOT NULL;

ALTER TABLE source_assessment
    ADD CONSTRAINT source_assessment_profile_nonblank CHECK (
        length(btrim(profile_name)) > 0
        AND length(btrim(profile_version)) > 0
    );

-- Semantic idempotency key: one durable assessment per source/window/profile.
ALTER TABLE source_assessment
    ADD CONSTRAINT source_assessment_semantic_key UNIQUE (
        source_id, window_start, window_end, profile_name, profile_version
    );

-- Deterministic lookup order for the semantic key.
CREATE INDEX source_assessment_profile_idx
    ON source_assessment (source_id, window_start, window_end,
                          profile_name, profile_version);

-- ========================================================================
-- Versioned stored functions (production persistence API).
-- ========================================================================

-- Create one immutable source assessment with profile provenance. Returns
-- 'added' / 'duplicate' (assessment identity) / 'semantic_duplicate'
-- (source/window/profile already assessed) / 'unknown_source'.
CREATE FUNCTION source_assessment_append_v2(
    p_assessment_id       uuid,
    p_source_id           uuid,
    p_assessed_at         timestamptz,
    p_window_start        timestamptz,
    p_window_end          timestamptz,
    p_confidence          double precision,
    p_relevance           double precision,
    p_activity            double precision,
    p_novelty             double precision,
    p_evidence_references jsonb,
    p_characteristics     jsonb,
    p_profile_name        text,
    p_profile_version     text
) RETURNS text
LANGUAGE plpgsql
AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM source WHERE id = p_source_id) THEN
        RETURN 'unknown_source';
    END IF;
    IF EXISTS (
        SELECT 1 FROM source_assessment WHERE assessment_id = p_assessment_id
    ) THEN
        RETURN 'duplicate';
    END IF;
    IF EXISTS (
        SELECT 1 FROM source_assessment
         WHERE source_id = p_source_id
           AND window_start = p_window_start
           AND window_end = p_window_end
           AND profile_name = p_profile_name
           AND profile_version = p_profile_version
    ) THEN
        RETURN 'semantic_duplicate';
    END IF;
    BEGIN
        INSERT INTO source_assessment
            (assessment_id, source_id, assessed_at, window_start, window_end,
             confidence, relevance, activity, novelty, evidence_references,
             characteristics, profile_name, profile_version)
        VALUES
            (p_assessment_id, p_source_id, p_assessed_at, p_window_start,
             p_window_end, p_confidence, p_relevance, p_activity, p_novelty,
             p_evidence_references, p_characteristics, p_profile_name,
             p_profile_version);
    EXCEPTION WHEN unique_violation THEN
        -- A concurrent caller won the same semantic key: report it and keep
        -- the enclosing transaction resolvable.
        RETURN 'semantic_duplicate';
    END;
    RETURN 'added';
END;
$$;

-- Load the assessment for a semantic source/window/profile key.
CREATE FUNCTION source_assessment_get_by_profile_v1(
    p_source_id       uuid,
    p_window_start    timestamptz,
    p_window_end      timestamptz,
    p_profile_name    text,
    p_profile_version text
)
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
    characteristics     jsonb,
    profile_name        text,
    profile_version     text
)
LANGUAGE sql
AS $$
    SELECT a.assessment_id, a.source_id, a.assessed_at, a.window_start,
           a.window_end, a.confidence, a.relevance, a.activity, a.novelty,
           a.evidence_references, a.characteristics, a.profile_name,
           a.profile_version
      FROM source_assessment a
     WHERE a.source_id = p_source_id
       AND a.window_start = p_window_start
       AND a.window_end = p_window_end
       AND a.profile_name = p_profile_name
       AND a.profile_version = p_profile_version;
$$;

-- List a source's assessments (all windows/profiles) in deterministic order.
CREATE FUNCTION source_assessment_list_v2(p_source_id uuid)
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
    characteristics     jsonb,
    profile_name        text,
    profile_version     text
)
LANGUAGE sql
AS $$
    SELECT a.assessment_id, a.source_id, a.assessed_at, a.window_start,
           a.window_end, a.confidence, a.relevance, a.activity, a.novelty,
           a.evidence_references, a.characteristics, a.profile_name,
           a.profile_version
      FROM source_assessment a
     WHERE a.source_id = p_source_id
     ORDER BY a.assessed_at, a.assessment_id;
$$;

-- Select normalized observations proven to belong to the given deterministic
-- crawl-request identities inside an inclusive observation window. The
-- result is deterministically ordered and bounded by p_limit.
CREATE FUNCTION normalized_content_list_for_requests_v1(
    p_request_ids  text[],
    p_window_start timestamptz,
    p_window_end   timestamptz,
    p_limit        integer
)
RETURNS TABLE (
    content_id             uuid,
    artifact_id            uuid,
    source_uri             text,
    title                  text,
    text_preview           text,
    content_type           text,
    observed_at            timestamptz,
    crawl_request_id       text,
    observation_index      integer,
    normalization_version  text,
    content_hash_algorithm text,
    content_hash_digest    text,
    completeness           text,
    structural_metadata    jsonb,
    created_at             timestamptz
)
LANGUAGE sql
AS $$
    SELECT n.content_id, n.artifact_id, n.source_uri, n.title, n.text_preview,
           n.content_type, n.observed_at, n.crawl_request_id,
           n.observation_index, n.normalization_version,
           n.content_hash_algorithm, n.content_hash_digest, n.completeness,
           n.structural_metadata, n.created_at
      FROM normalized_content n
     WHERE n.crawl_request_id = ANY(p_request_ids)
       AND n.observed_at >= p_window_start
       AND n.observed_at <= p_window_end
     ORDER BY n.observed_at, n.content_id
     LIMIT p_limit;
$$;

-- List the runs of one source whose execution may overlap the window. A run
-- can only produce observations between its creation and completion, so the
-- bounds are provenance-based, never URI/content based.
CREATE FUNCTION collection_run_list_for_source_window_v1(
    p_source_id    uuid,
    p_window_start timestamptz,
    p_window_end   timestamptz
)
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
     WHERE r.source_id = p_source_id
       AND r.created_at <= p_window_end
       AND (r.completed_at IS NULL OR r.completed_at >= p_window_start)
     ORDER BY r.created_at, r.run_id;
$$;

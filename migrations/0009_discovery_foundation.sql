-- SPDX-License-Identifier: AGPL-3.0-only
-- Darkula PR 17 migration: discovery, provenance and qualification foundation.
--
-- Additive and immutable. 0001-0008 remain unchanged. Every new database
-- behavior lives here as versioned stored functions invoked by the Python
-- adapter; production Python contains no data-access SQL.
--
-- Semantics:
--   * discovery claims are UNTRUSTED EVIDENCE. An observation never creates,
--     reconciles, or promotes a candidate/source/endpoint/policy/run, and
--     never grants collection/access authority;
--   * the semantic replay tuple is
--     (provider_id, provider_record_id, provider_record_version,
--      detection_method, reference_kind) and is UNIQUE: a replay with an
--     identical normalized payload returns the original immutable row, a
--     same-key payload difference is a conflict and never an overwrite;
--   * a name-only reference carries no locator (NULL) and cannot become a
--     crawl target;
--   * access_evidence is orthogonal to candidate/source lifecycle. PR17's
--     public write path only records CREDENTIALS_PENDING for an already
--     existing candidate, and never claims PUBLIC/READY;
--   * qualification_decision is an immutable decision that commits in one
--     transaction/function with exactly one existing candidate status
--     transition and one existing candidate event. REGISTRY_POLICY decisions
--     never reference a fabricated ReconAssessment; AGENT_RECON decisions
--     must reference a real assessment of the same candidate.
--
-- Replay identity never includes generated UUIDs, acceptance timestamps, a
-- broker position, or a URI-as-identity.

-- ========================================================================
-- Tables and constraints
-- ========================================================================

CREATE TABLE discovery_observation (
    observation_id          uuid PRIMARY KEY,
    provider_id             text NOT NULL CHECK (
        char_length(provider_id) BETWEEN 1 AND 100
        AND provider_id ~ '^[a-z0-9][a-z0-9._-]*$'
    ),
    provider_record_id      text NOT NULL CHECK (
        char_length(provider_record_id) BETWEEN 1 AND 256
        AND provider_record_id = btrim(provider_record_id)
    ),
    provider_record_version text NOT NULL CHECK (
        char_length(provider_record_version) BETWEEN 1 AND 128
        AND provider_record_version = btrim(provider_record_version)
    ),
    detection_method        text NOT NULL CHECK (detection_method IN (
        'REGISTRY',
        'CONTENT_LINK',
        'CONTENT_SEMANTIC',
        'MANUAL'
    )),
    reference_kind          text NOT NULL CHECK (reference_kind IN (
        'LOCATOR',
        'NAME_ONLY'
    )),
    asserted_reference      text NOT NULL CHECK (
        char_length(asserted_reference) BETWEEN 1 AND 2048
    ),
    normalized_locator      text NULL CHECK (
        normalized_locator IS NULL
        OR char_length(normalized_locator) BETWEEN 1 AND 2048
    ),
    validation_status       text NOT NULL CHECK (validation_status IN (
        'VALID_LOCATOR',
        'UNRESOLVED_NAME'
    )),
    source_kind_hint        text NULL CHECK (source_kind_hint IN (
        'ONION',
        'CLEARNET',
        'MIRROR',
        'API',
        'FEED'
    )),
    requires_auth_claim     boolean NULL,
    origin_reference        text NULL CHECK (
        origin_reference IS NULL
        OR char_length(origin_reference) BETWEEN 1 AND 2048
    ),
    evidence_reference      text NOT NULL CHECK (
        char_length(evidence_reference) BETWEEN 1 AND 2048
    ),
    evidence_excerpt        text NULL CHECK (
        evidence_excerpt IS NULL
        OR char_length(evidence_excerpt) BETWEEN 1 AND 512
    ),
    observed_at             timestamptz NOT NULL,
    ingested_at             timestamptz NOT NULL,
    candidate_id            uuid NULL REFERENCES source_candidate(id),
    semantic_key            text NOT NULL CHECK (
        semantic_key ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT discovery_observation_semantic_tuple UNIQUE (
        provider_id,
        provider_record_id,
        provider_record_version,
        detection_method,
        reference_kind
    ),
    CONSTRAINT discovery_observation_semantic_key_key UNIQUE (semantic_key),
    CONSTRAINT discovery_observation_reference_classification CHECK (
        (
            reference_kind = 'LOCATOR'
            AND normalized_locator IS NOT NULL
            AND validation_status = 'VALID_LOCATOR'
        )
        OR (
            reference_kind = 'NAME_ONLY'
            AND normalized_locator IS NULL
            AND validation_status = 'UNRESOLVED_NAME'
        )
    )
);

-- Deterministic provider listing order (observed_at, observation_id).
CREATE INDEX discovery_observation_provider_order_idx
    ON discovery_observation (provider_id, observed_at, observation_id);

-- Access evidence: exactly one target (candidate OR managed endpoint), and
-- immutable per (observation, target, status).
CREATE TABLE access_evidence (
    access_evidence_id  uuid PRIMARY KEY,
    observation_id      uuid NOT NULL
                        REFERENCES discovery_observation(observation_id)
                        ON DELETE CASCADE,
    candidate_id        uuid NULL REFERENCES source_candidate(id)
                        ON DELETE CASCADE,
    source_endpoint_id  uuid NULL REFERENCES source_endpoint(endpoint_id)
                        ON DELETE CASCADE,
    status              text NOT NULL CHECK (status IN (
        'UNKNOWN',
        'PUBLIC',
        'CREDENTIALS_PENDING',
        'READY',
        'AUTH_FAILED',
        'ACCESS_REVOKED'
    )),
    reason_code         text NOT NULL CHECK (
        char_length(reason_code) BETWEEN 1 AND 100
        AND reason_code ~ '^[A-Z0-9][A-Z0-9_]*$'
    ),
    recorded_at         timestamptz NOT NULL,
    evidence_reference  text NOT NULL CHECK (
        char_length(evidence_reference) BETWEEN 1 AND 2048
    ),
    CONSTRAINT access_evidence_exactly_one_target CHECK (
        (candidate_id IS NOT NULL) <> (source_endpoint_id IS NOT NULL)
    )
);

-- One immutable evidence row per (observation, candidate, status). Two
-- partial unique indexes because PostgreSQL treats NULLs as distinct in a
-- single nullable composite constraint.
CREATE UNIQUE INDEX access_evidence_observation_candidate_status_key
    ON access_evidence (observation_id, candidate_id, status)
    WHERE candidate_id IS NOT NULL;
CREATE UNIQUE INDEX access_evidence_observation_endpoint_status_key
    ON access_evidence (observation_id, source_endpoint_id, status)
    WHERE source_endpoint_id IS NOT NULL;

-- Deterministic immutable-evidence order per target.
CREATE INDEX access_evidence_candidate_order_idx
    ON access_evidence (candidate_id, recorded_at, access_evidence_id)
    WHERE candidate_id IS NOT NULL;
CREATE INDEX access_evidence_endpoint_order_idx
    ON access_evidence (source_endpoint_id, recorded_at, access_evidence_id)
    WHERE source_endpoint_id IS NOT NULL;

-- Immutable qualification decision; commits with the candidate transition
-- and exactly one existing candidate event.
CREATE TABLE qualification_decision (
    decision_id             uuid PRIMARY KEY,
    candidate_id            uuid NOT NULL REFERENCES source_candidate(id)
                            ON DELETE CASCADE,
    method                  text NOT NULL CHECK (method IN (
        'REGISTRY_POLICY',
        'AGENT_RECON'
    )),
    outcome                 text NOT NULL CHECK (outcome IN (
        'QUALIFIED',
        'RECONNAISSANCE_PENDING',
        'REJECTED'
    )),
    evaluated_at            timestamptz NOT NULL,
    policy_id               text NOT NULL CHECK (
        char_length(policy_id) BETWEEN 1 AND 100
        AND policy_id ~ '^[A-Za-z0-9][A-Za-z0-9._-]*$'
    ),
    policy_version           text NOT NULL CHECK (
        char_length(policy_version) BETWEEN 1 AND 100
        AND policy_version ~ '^[A-Za-z0-9][A-Za-z0-9._-]*$'
    ),
    provider_id             text NULL CHECK (
        provider_id IS NULL
        OR (
            char_length(provider_id) BETWEEN 1 AND 100
            AND provider_id ~ '^[a-z0-9][a-z0-9._-]*$'
        )
    ),
    provider_record_id      text NULL CHECK (
        provider_record_id IS NULL
        OR char_length(provider_record_id) BETWEEN 1 AND 256
    ),
    recon_assessment_id     uuid NULL REFERENCES recon_assessment(assessment_id)
                            ON DELETE CASCADE,
    event_id                uuid NOT NULL UNIQUE
                            REFERENCES source_candidate_event_history(event_id)
                            ON DELETE CASCADE,
    expected_status         text NOT NULL CHECK (expected_status IN (
        'DISCOVERED',
        'RECONNAISSANCE_PENDING',
        'UNDER_RECONNAISSANCE',
        'QUALIFIED',
        'REJECTED',
        'PROMOTED'
    )),
    new_status              text NOT NULL CHECK (new_status IN (
        'DISCOVERED',
        'RECONNAISSANCE_PENDING',
        'UNDER_RECONNAISSANCE',
        'QUALIFIED',
        'REJECTED',
        'PROMOTED'
    )),
    decision_key            text NOT NULL UNIQUE CHECK (
        decision_key ~ '^[0-9a-f]{64}$'
    ),
    evidence_observation_ids uuid[] NOT NULL CHECK (
        cardinality(evidence_observation_ids) BETWEEN 1 AND 64
    ),
    CONSTRAINT qualification_decision_outcome_status CHECK (
        (outcome = 'QUALIFIED' AND new_status = 'QUALIFIED')
        OR (outcome = 'RECONNAISSANCE_PENDING'
            AND new_status = 'RECONNAISSANCE_PENDING')
        OR (outcome = 'REJECTED' AND new_status = 'REJECTED')
    ),
    CONSTRAINT qualification_decision_expected_differs CHECK (
        expected_status <> new_status
    ),
    CONSTRAINT qualification_decision_method_provenance CHECK (
        (
            method = 'REGISTRY_POLICY'
            AND provider_id IS NOT NULL
            AND provider_record_id IS NOT NULL
            AND recon_assessment_id IS NULL
        )
        OR (
            method = 'AGENT_RECON'
            AND recon_assessment_id IS NOT NULL
        )
    )
);

-- Deterministic immutable decision history order per candidate.
CREATE INDEX qualification_decision_candidate_order_idx
    ON qualification_decision (candidate_id, evaluated_at, decision_id);

-- ========================================================================
-- Versioned stored functions (production persistence API).
-- ========================================================================

-- Insert one discovery observation, or return the stored row's identity for
-- an exact replay. Returns 'added' / 'replayed' / 'conflict'.
--
-- Replay identity excludes the generated observation_id and the acceptance
-- ingested_at (a retry regenerates both). Every semantic payload field,
-- including the provider observed_at, must match exactly; a same-tuple
-- payload difference is a conflict and is never an overwrite.
CREATE FUNCTION discovery_observation_insert_v1(
    p_observation_id          uuid,
    p_provider_id             text,
    p_provider_record_id      text,
    p_provider_record_version text,
    p_detection_method        text,
    p_reference_kind          text,
    p_asserted_reference      text,
    p_normalized_locator      text,
    p_validation_status       text,
    p_source_kind_hint        text,
    p_requires_auth_claim     boolean,
    p_origin_reference        text,
    p_evidence_reference      text,
    p_evidence_excerpt        text,
    p_observed_at             timestamptz,
    p_ingested_at             timestamptz,
    p_candidate_id            uuid,
    p_semantic_key            text
) RETURNS text
LANGUAGE plpgsql
AS $$
DECLARE
    existing discovery_observation%ROWTYPE;
BEGIN
    SELECT * INTO existing
      FROM discovery_observation
     WHERE provider_id = p_provider_id
       AND provider_record_id = p_provider_record_id
       AND provider_record_version = p_provider_record_version
       AND detection_method = p_detection_method
       AND reference_kind = p_reference_kind;
    IF FOUND THEN
        IF existing.asserted_reference = p_asserted_reference
           AND existing.normalized_locator IS NOT DISTINCT FROM p_normalized_locator
           AND existing.validation_status = p_validation_status
           AND existing.source_kind_hint IS NOT DISTINCT FROM p_source_kind_hint
           AND existing.requires_auth_claim
               IS NOT DISTINCT FROM p_requires_auth_claim
           AND existing.origin_reference IS NOT DISTINCT FROM p_origin_reference
           AND existing.evidence_reference = p_evidence_reference
           AND existing.evidence_excerpt IS NOT DISTINCT FROM p_evidence_excerpt
           AND existing.observed_at = p_observed_at
           AND existing.candidate_id IS NOT DISTINCT FROM p_candidate_id
           AND existing.semantic_key = p_semantic_key
        THEN
            RETURN 'replayed';
        END IF;
        RETURN 'conflict';
    END IF;
    BEGIN
        INSERT INTO discovery_observation (
            observation_id, provider_id, provider_record_id,
            provider_record_version, detection_method, reference_kind,
            asserted_reference, normalized_locator, validation_status,
            source_kind_hint, requires_auth_claim, origin_reference,
            evidence_reference, evidence_excerpt, observed_at, ingested_at,
            candidate_id, semantic_key
        ) VALUES (
            p_observation_id, p_provider_id, p_provider_record_id,
            p_provider_record_version, p_detection_method, p_reference_kind,
            p_asserted_reference, p_normalized_locator, p_validation_status,
            p_source_kind_hint, p_requires_auth_claim, p_origin_reference,
            p_evidence_reference, p_evidence_excerpt, p_observed_at,
            p_ingested_at, p_candidate_id, p_semantic_key
        );
    EXCEPTION WHEN unique_violation THEN
        -- A concurrent caller won the same semantic tuple. Report conflict
        -- unless its persisted payload is exactly this attempt's.
        SELECT * INTO existing
          FROM discovery_observation
         WHERE provider_id = p_provider_id
           AND provider_record_id = p_provider_record_id
           AND provider_record_version = p_provider_record_version
           AND detection_method = p_detection_method
           AND reference_kind = p_reference_kind;
        IF FOUND
           AND existing.asserted_reference = p_asserted_reference
           AND existing.normalized_locator IS NOT DISTINCT FROM p_normalized_locator
           AND existing.validation_status = p_validation_status
           AND existing.source_kind_hint IS NOT DISTINCT FROM p_source_kind_hint
           AND existing.requires_auth_claim
               IS NOT DISTINCT FROM p_requires_auth_claim
           AND existing.origin_reference IS NOT DISTINCT FROM p_origin_reference
           AND existing.evidence_reference = p_evidence_reference
           AND existing.evidence_excerpt IS NOT DISTINCT FROM p_evidence_excerpt
           AND existing.observed_at = p_observed_at
           AND existing.candidate_id IS NOT DISTINCT FROM p_candidate_id
           AND existing.semantic_key = p_semantic_key
        THEN
            RETURN 'replayed';
        END IF;
        RETURN 'conflict';
    END;
    RETURN 'added';
END;
$$;

CREATE FUNCTION discovery_observation_get_v1(p_observation_id uuid)
RETURNS TABLE (
    observation_id          uuid,
    provider_id             text,
    provider_record_id      text,
    provider_record_version text,
    detection_method        text,
    reference_kind          text,
    asserted_reference      text,
    normalized_locator      text,
    validation_status       text,
    source_kind_hint        text,
    requires_auth_claim     boolean,
    origin_reference        text,
    evidence_reference      text,
    evidence_excerpt        text,
    observed_at             timestamptz,
    ingested_at             timestamptz,
    candidate_id            uuid,
    semantic_key            text
)
LANGUAGE sql
AS $$
    SELECT o.observation_id, o.provider_id, o.provider_record_id,
           o.provider_record_version, o.detection_method, o.reference_kind,
           o.asserted_reference, o.normalized_locator, o.validation_status,
           o.source_kind_hint, o.requires_auth_claim, o.origin_reference,
           o.evidence_reference, o.evidence_excerpt, o.observed_at,
           o.ingested_at, o.candidate_id, o.semantic_key
      FROM discovery_observation o
     WHERE o.observation_id = p_observation_id;
$$;

CREATE FUNCTION discovery_observation_get_by_key_v1(
    p_provider_id             text,
    p_provider_record_id      text,
    p_provider_record_version text,
    p_detection_method        text,
    p_reference_kind          text
)
RETURNS TABLE (
    observation_id          uuid,
    provider_id             text,
    provider_record_id      text,
    provider_record_version text,
    detection_method        text,
    reference_kind          text,
    asserted_reference      text,
    normalized_locator      text,
    validation_status       text,
    source_kind_hint        text,
    requires_auth_claim     boolean,
    origin_reference        text,
    evidence_reference      text,
    evidence_excerpt        text,
    observed_at             timestamptz,
    ingested_at             timestamptz,
    candidate_id            uuid,
    semantic_key            text
)
LANGUAGE sql
AS $$
    SELECT o.observation_id, o.provider_id, o.provider_record_id,
           o.provider_record_version, o.detection_method, o.reference_kind,
           o.asserted_reference, o.normalized_locator, o.validation_status,
           o.source_kind_hint, o.requires_auth_claim, o.origin_reference,
           o.evidence_reference, o.evidence_excerpt, o.observed_at,
           o.ingested_at, o.candidate_id, o.semantic_key
      FROM discovery_observation o
     WHERE o.provider_id = p_provider_id
       AND o.provider_record_id = p_provider_record_id
       AND o.provider_record_version = p_provider_record_version
       AND o.detection_method = p_detection_method
       AND o.reference_kind = p_reference_kind;
$$;

CREATE FUNCTION discovery_observation_list_for_provider_v1(
    p_provider_id text,
    p_limit       integer,
    p_offset      integer
)
RETURNS TABLE (
    observation_id          uuid,
    provider_id             text,
    provider_record_id      text,
    provider_record_version text,
    detection_method        text,
    reference_kind          text,
    asserted_reference      text,
    normalized_locator      text,
    validation_status       text,
    source_kind_hint        text,
    requires_auth_claim     boolean,
    origin_reference        text,
    evidence_reference      text,
    evidence_excerpt        text,
    observed_at             timestamptz,
    ingested_at             timestamptz,
    candidate_id            uuid,
    semantic_key            text
)
LANGUAGE plpgsql
AS $$
BEGIN
    IF p_limit < 1 OR p_limit > 200 OR p_offset < 0 THEN
        RAISE EXCEPTION 'invalid discovery observation pagination'
            USING ERRCODE = '23514';
    END IF;
    RETURN QUERY
        SELECT o.observation_id, o.provider_id, o.provider_record_id,
               o.provider_record_version, o.detection_method, o.reference_kind,
               o.asserted_reference, o.normalized_locator, o.validation_status,
               o.source_kind_hint, o.requires_auth_claim, o.origin_reference,
               o.evidence_reference, o.evidence_excerpt, o.observed_at,
               o.ingested_at, o.candidate_id, o.semantic_key
          FROM discovery_observation o
         WHERE o.provider_id = p_provider_id
         ORDER BY o.observed_at, o.observation_id
         LIMIT p_limit OFFSET p_offset;
END;
$$;

-- Append one CREDENTIALS_PENDING access evidence for an EXISTING candidate
-- whose observation actually claims authentication is required.
--
-- Returns 'added' / 'replayed' / 'conflict' / 'unknown_observation' /
-- 'unknown_candidate' / 'claim_absent' / 'wrong_candidate'. No other status
-- can be written through this PR17 primitive.
CREATE FUNCTION access_evidence_append_pending_v1(
    p_access_evidence_id uuid,
    p_observation_id     uuid,
    p_candidate_id       uuid,
    p_reason_code        text,
    p_recorded_at        timestamptz,
    p_evidence_reference text
) RETURNS text
LANGUAGE plpgsql
AS $$
DECLARE
    observed_claim   boolean;
    observed_candidate uuid;
    existing access_evidence%ROWTYPE;
BEGIN
    SELECT o.requires_auth_claim, o.candidate_id
      INTO observed_claim, observed_candidate
      FROM discovery_observation o
     WHERE o.observation_id = p_observation_id;
    IF NOT FOUND THEN
        RETURN 'unknown_observation';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM source_candidate c WHERE c.id = p_candidate_id) THEN
        RETURN 'unknown_candidate';
    END IF;
    IF observed_candidate IS NOT NULL AND observed_candidate <> p_candidate_id THEN
        RETURN 'wrong_candidate';
    END IF;
    IF observed_claim IS NOT TRUE THEN
        RETURN 'claim_absent';
    END IF;
    SELECT * INTO existing
      FROM access_evidence
     WHERE observation_id = p_observation_id
       AND candidate_id = p_candidate_id
       AND status = 'CREDENTIALS_PENDING';
    IF FOUND THEN
        IF existing.reason_code = p_reason_code
           AND existing.evidence_reference = p_evidence_reference
        THEN
            RETURN 'replayed';
        END IF;
        RETURN 'conflict';
    END IF;
    IF EXISTS (
        SELECT 1 FROM access_evidence
         WHERE observation_id = p_observation_id
           AND candidate_id = p_candidate_id
           AND status <> 'CREDENTIALS_PENDING'
    ) THEN
        RETURN 'conflict';
    END IF;
    INSERT INTO access_evidence (
        access_evidence_id, observation_id, candidate_id,
        source_endpoint_id, status, reason_code, recorded_at,
        evidence_reference
    ) VALUES (
        p_access_evidence_id, p_observation_id, p_candidate_id,
        NULL, 'CREDENTIALS_PENDING', p_reason_code, p_recorded_at,
        p_evidence_reference
    );
    RETURN 'added';
EXCEPTION WHEN unique_violation THEN
    SELECT * INTO existing
      FROM access_evidence
     WHERE observation_id = p_observation_id
       AND candidate_id = p_candidate_id
       AND status = 'CREDENTIALS_PENDING';
    IF FOUND
       AND existing.reason_code = p_reason_code
       AND existing.evidence_reference = p_evidence_reference
    THEN
        RETURN 'replayed';
    END IF;
    RETURN 'conflict';
END;
$$;

CREATE FUNCTION access_evidence_get_pending_v1(
    p_observation_id uuid,
    p_candidate_id   uuid
)
RETURNS TABLE (
    access_evidence_id uuid,
    observation_id     uuid,
    candidate_id       uuid,
    source_endpoint_id uuid,
    status             text,
    reason_code        text,
    recorded_at        timestamptz,
    evidence_reference text
)
LANGUAGE sql
AS $$
    SELECT e.access_evidence_id, e.observation_id, e.candidate_id,
           e.source_endpoint_id, e.status, e.reason_code, e.recorded_at,
           e.evidence_reference
      FROM access_evidence e
     WHERE e.observation_id = p_observation_id
       AND e.candidate_id = p_candidate_id
       AND e.status = 'CREDENTIALS_PENDING';
$$;

CREATE FUNCTION access_evidence_list_for_candidate_v1(p_candidate_id uuid)
RETURNS TABLE (
    access_evidence_id uuid,
    observation_id     uuid,
    candidate_id       uuid,
    source_endpoint_id uuid,
    status             text,
    reason_code        text,
    recorded_at        timestamptz,
    evidence_reference text
)
LANGUAGE sql
AS $$
    SELECT e.access_evidence_id, e.observation_id, e.candidate_id,
           e.source_endpoint_id, e.status, e.reason_code, e.recorded_at,
           e.evidence_reference
      FROM access_evidence e
     WHERE e.candidate_id = p_candidate_id
     ORDER BY e.recorded_at, e.access_evidence_id;
$$;

CREATE FUNCTION access_evidence_list_for_endpoint_v1(p_source_endpoint_id uuid)
RETURNS TABLE (
    access_evidence_id uuid,
    observation_id     uuid,
    candidate_id       uuid,
    source_endpoint_id uuid,
    status             text,
    reason_code        text,
    recorded_at        timestamptz,
    evidence_reference text
)
LANGUAGE sql
AS $$
    SELECT e.access_evidence_id, e.observation_id, e.candidate_id,
           e.source_endpoint_id, e.status, e.reason_code, e.recorded_at,
           e.evidence_reference
      FROM access_evidence e
     WHERE e.source_endpoint_id = p_source_endpoint_id
     ORDER BY e.recorded_at, e.access_evidence_id;
$$;

CREATE FUNCTION access_evidence_latest_for_candidate_v1(p_candidate_id uuid)
RETURNS TABLE (
    access_evidence_id uuid,
    observation_id     uuid,
    candidate_id       uuid,
    source_endpoint_id uuid,
    status             text,
    reason_code        text,
    recorded_at        timestamptz,
    evidence_reference text
)
LANGUAGE sql
AS $$
    SELECT e.access_evidence_id, e.observation_id, e.candidate_id,
           e.source_endpoint_id, e.status, e.reason_code, e.recorded_at,
           e.evidence_reference
      FROM access_evidence e
     WHERE e.candidate_id = p_candidate_id
     ORDER BY e.recorded_at DESC, e.access_evidence_id DESC
     LIMIT 1;
$$;

CREATE FUNCTION access_evidence_latest_for_endpoint_v1(p_source_endpoint_id uuid)
RETURNS TABLE (
    access_evidence_id uuid,
    observation_id     uuid,
    candidate_id       uuid,
    source_endpoint_id uuid,
    status             text,
    reason_code        text,
    recorded_at        timestamptz,
    evidence_reference text
)
LANGUAGE sql
AS $$
    SELECT e.access_evidence_id, e.observation_id, e.candidate_id,
           e.source_endpoint_id, e.status, e.reason_code, e.recorded_at,
           e.evidence_reference
      FROM access_evidence e
     WHERE e.source_endpoint_id = p_source_endpoint_id
     ORDER BY e.recorded_at DESC, e.access_evidence_id DESC
     LIMIT 1;
$$;

-- One atomic qualification decision + candidate status transition + exactly
-- one candidate event. Returns 'decided' / 'replayed' / 'conflict' /
-- 'state_conflict' / 'unknown_candidate' / 'unknown_evidence' /
-- 'invalid_provenance'.
--
-- Replay comparison exempts only a regenerated decision_id: every persisted
-- semantic field, the evaluation timestamp, and all immutable fields of the
-- linked candidate event must match exactly (JSONB compares by content).
CREATE FUNCTION qualification_decide_and_transition_v1(
    p_decision_id            uuid,
    p_candidate_id           uuid,
    p_method                 text,
    p_outcome                text,
    p_evaluated_at           timestamptz,
    p_policy_id              text,
    p_policy_version         text,
    p_provider_id            text,
    p_provider_record_id     text,
    p_recon_assessment_id    uuid,
    p_event_id               uuid,
    p_expected_status        text,
    p_new_status             text,
    p_decision_key           text,
    p_evidence_observation_ids uuid[],
    p_event_type             text,
    p_event_occurred_at      timestamptz,
    p_event_context          jsonb,
    p_event_reason           text,
    p_event_provenance       text
) RETURNS text
LANGUAGE plpgsql
AS $$
DECLARE
    current_status     text;
    existing           qualification_decision%ROWTYPE;
    stored_event       source_candidate_event_history%ROWTYPE;
    expected_event     text;
    expected_new       text;
    unsorted_evidence  uuid[];
    sorted_evidence    uuid[];
BEGIN
    -- Serialize competing decisions for the same candidate.
    SELECT c.status INTO current_status
      FROM source_candidate c
     WHERE c.id = p_candidate_id
       FOR UPDATE;
    IF NOT FOUND THEN
        RETURN 'unknown_candidate';
    END IF;

    SELECT * INTO existing
      FROM qualification_decision d
     WHERE d.decision_key = p_decision_key;
    IF FOUND THEN
        SELECT h.* INTO stored_event
          FROM source_candidate_event_history h
         WHERE h.event_id = existing.event_id
           AND h.candidate_id = existing.candidate_id;
        IF NOT FOUND THEN
            RETURN 'conflict';
        END IF;
        SELECT array_agg(v ORDER BY v) INTO unsorted_evidence
          FROM unnest(existing.evidence_observation_ids) v;
        SELECT array_agg(v ORDER BY v) INTO sorted_evidence
          FROM unnest(p_evidence_observation_ids) v;
        IF existing.candidate_id = p_candidate_id
           AND existing.method = p_method
           AND existing.outcome = p_outcome
           AND existing.policy_id = p_policy_id
           AND existing.policy_version = p_policy_version
           AND existing.provider_id IS NOT DISTINCT FROM p_provider_id
           AND existing.provider_record_id
               IS NOT DISTINCT FROM p_provider_record_id
           AND existing.recon_assessment_id
               IS NOT DISTINCT FROM p_recon_assessment_id
           AND existing.expected_status = p_expected_status
           AND existing.new_status = p_new_status
           AND unsorted_evidence IS NOT DISTINCT FROM sorted_evidence
           AND existing.evaluated_at = p_evaluated_at
           AND existing.event_id = p_event_id
           AND stored_event.event_type = p_event_type
           AND stored_event.occurred_at = p_event_occurred_at
           AND stored_event.context IS NOT DISTINCT FROM p_event_context
           AND stored_event.reason IS NOT DISTINCT FROM p_event_reason
           AND stored_event.provenance IS NOT DISTINCT FROM p_event_provenance
        THEN
            RETURN 'replayed';
        END IF;
        RETURN 'conflict';
    END IF;

    IF p_evidence_observation_ids IS NULL
       OR cardinality(p_evidence_observation_ids) < 1
       OR cardinality(p_evidence_observation_ids) > 64
    THEN
        RETURN 'invalid_provenance';
    END IF;
    IF cardinality(p_evidence_observation_ids) <> (
        SELECT cardinality(array_agg(DISTINCT v))
          FROM unnest(p_evidence_observation_ids) v
    ) THEN
        RETURN 'invalid_provenance';
    END IF;
    IF EXISTS (
        SELECT 1
          FROM unnest(p_evidence_observation_ids) e
         WHERE NOT EXISTS (
             SELECT 1 FROM discovery_observation o
              WHERE o.observation_id = e
         )
    ) THEN
        RETURN 'unknown_evidence';
    END IF;

    IF p_method = 'REGISTRY_POLICY' THEN
        IF p_provider_id IS NULL
           OR p_provider_record_id IS NULL
           OR p_recon_assessment_id IS NOT NULL
        THEN
            RETURN 'invalid_provenance';
        END IF;
        -- At least one *cited* observation must itself be a registry
        -- detection of the claimed provider record; a matching observation
        -- elsewhere in the database does not establish the claim.
        IF NOT EXISTS (
            SELECT 1
              FROM unnest(p_evidence_observation_ids) AS cited(observation_id)
              JOIN discovery_observation o
                ON o.observation_id = cited.observation_id
             WHERE o.provider_id = p_provider_id
               AND o.provider_record_id = p_provider_record_id
               AND o.detection_method = 'REGISTRY'
        ) THEN
            RETURN 'invalid_provenance';
        END IF;
    ELSIF p_method = 'AGENT_RECON' THEN
        IF p_recon_assessment_id IS NULL THEN
            RETURN 'invalid_provenance';
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM recon_assessment r
             WHERE r.assessment_id = p_recon_assessment_id
               AND r.candidate_id = p_candidate_id
        ) THEN
            RETURN 'invalid_provenance';
        END IF;
    ELSE
        RETURN 'invalid_provenance';
    END IF;

    expected_event := CASE p_outcome
        WHEN 'QUALIFIED' THEN 'QUALIFIED'
        WHEN 'RECONNAISSANCE_PENDING' THEN 'NEEDS_MORE_RECON'
        WHEN 'REJECTED' THEN 'REJECTED'
        ELSE NULL
    END;
    expected_new := CASE p_outcome
        WHEN 'QUALIFIED' THEN 'QUALIFIED'
        WHEN 'RECONNAISSANCE_PENDING' THEN 'RECONNAISSANCE_PENDING'
        WHEN 'REJECTED' THEN 'REJECTED'
        ELSE NULL
    END;
    IF expected_event IS NULL
       OR p_event_type <> expected_event
       OR p_new_status <> expected_new
    THEN
        RETURN 'invalid_provenance';
    END IF;

    IF current_status <> p_expected_status THEN
        RETURN 'state_conflict';
    END IF;

    IF EXISTS (
        SELECT 1 FROM source_candidate_event_history h
         WHERE h.event_id = p_event_id
    ) THEN
        RETURN 'conflict';
    END IF;

    BEGIN
        UPDATE source_candidate
           SET status = p_new_status
         WHERE id = p_candidate_id
           AND status = p_expected_status;
        IF NOT FOUND THEN
            RETURN 'state_conflict';
        END IF;

        INSERT INTO source_candidate_event_history (
            event_id, candidate_id, event_type, occurred_at, context, reason,
            provenance
        ) VALUES (
            p_event_id, p_candidate_id, p_event_type, p_event_occurred_at,
            p_event_context, p_event_reason, p_event_provenance
        );

        INSERT INTO qualification_decision (
            decision_id, candidate_id, method, outcome, evaluated_at,
            policy_id, policy_version, provider_id, provider_record_id,
            recon_assessment_id, event_id, expected_status, new_status,
            decision_key, evidence_observation_ids
        ) VALUES (
            p_decision_id, p_candidate_id, p_method, p_outcome, p_evaluated_at,
            p_policy_id, p_policy_version, p_provider_id, p_provider_record_id,
            p_recon_assessment_id, p_event_id, p_expected_status, p_new_status,
            p_decision_key, p_evidence_observation_ids
        );
    EXCEPTION WHEN unique_violation THEN
        -- Roll back the transition/event/decision together; a competing
        -- decision can never leave a partial durable effect.
        RETURN 'conflict';
    END;

    RETURN 'decided';
END;
$$;

CREATE FUNCTION qualification_decision_get_v1(p_decision_id uuid)
RETURNS TABLE (
    decision_id              uuid,
    candidate_id             uuid,
    method                   text,
    outcome                  text,
    evaluated_at             timestamptz,
    policy_id                text,
    policy_version           text,
    provider_id              text,
    provider_record_id       text,
    recon_assessment_id      uuid,
    event_id                 uuid,
    expected_status          text,
    new_status               text,
    decision_key             text,
    evidence_observation_ids uuid[]
)
LANGUAGE sql
AS $$
    SELECT d.decision_id, d.candidate_id, d.method, d.outcome,
           d.evaluated_at, d.policy_id, d.policy_version, d.provider_id,
           d.provider_record_id, d.recon_assessment_id, d.event_id,
           d.expected_status, d.new_status, d.decision_key,
           d.evidence_observation_ids
      FROM qualification_decision d
     WHERE d.decision_id = p_decision_id;
$$;

CREATE FUNCTION qualification_decision_get_by_key_v1(p_decision_key text)
RETURNS TABLE (
    decision_id              uuid,
    candidate_id             uuid,
    method                   text,
    outcome                  text,
    evaluated_at             timestamptz,
    policy_id                text,
    policy_version           text,
    provider_id              text,
    provider_record_id       text,
    recon_assessment_id      uuid,
    event_id                 uuid,
    expected_status          text,
    new_status               text,
    decision_key             text,
    evidence_observation_ids uuid[]
)
LANGUAGE sql
AS $$
    SELECT d.decision_id, d.candidate_id, d.method, d.outcome,
           d.evaluated_at, d.policy_id, d.policy_version, d.provider_id,
           d.provider_record_id, d.recon_assessment_id, d.event_id,
           d.expected_status, d.new_status, d.decision_key,
           d.evidence_observation_ids
      FROM qualification_decision d
     WHERE d.decision_key = p_decision_key;
$$;

CREATE FUNCTION qualification_decision_list_for_candidate_v1(p_candidate_id uuid)
RETURNS TABLE (
    decision_id              uuid,
    candidate_id             uuid,
    method                   text,
    outcome                  text,
    evaluated_at             timestamptz,
    policy_id                text,
    policy_version           text,
    provider_id              text,
    provider_record_id       text,
    recon_assessment_id      uuid,
    event_id                 uuid,
    expected_status          text,
    new_status               text,
    decision_key             text,
    evidence_observation_ids uuid[]
)
LANGUAGE sql
AS $$
    SELECT d.decision_id, d.candidate_id, d.method, d.outcome,
           d.evaluated_at, d.policy_id, d.policy_version, d.provider_id,
           d.provider_record_id, d.recon_assessment_id, d.event_id,
           d.expected_status, d.new_status, d.decision_key,
           d.evidence_observation_ids
      FROM qualification_decision d
     WHERE d.candidate_id = p_candidate_id
     ORDER BY d.evaluated_at, d.decision_id;
$$;

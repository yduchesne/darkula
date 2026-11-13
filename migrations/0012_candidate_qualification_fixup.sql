-- SPDX-License-Identifier: AGPL-3.0-only
-- Darkula PR 19-F1 migration: trusted registry provenance, current endpoint
-- guard and bounded access-readiness probes.
--
-- Additive and immutable. 0001-0011 remain byte-for-byte unchanged. Every
-- new database behavior lives here as a versioned stored function invoked by
-- the Python adapter; production Python contains no data-access SQL.
--
-- Semantics:
--   * a discovery observation is UNVERIFIED unless a separate immutable
--     trusted_registry_observation marker records that trusted application
--     code observed the claim through the concrete fixed-origin RansomLook
--     poller/transport. Generic ingest never creates this marker;
--   * the marker records WHICH application ingestion path was used. It is a
--     trusted-pipeline provenance marker, never a cryptographic attestation
--     from the remote registry and never proof that the remote claims are
--     true;
--   * the marker is not a Source, candidate, access, endorsement or crawl
--     authorization. It is append-only: an exact replay returns the original
--     marker and verified_at, never an update;
--   * deterministic registry qualification additionally requires that the
--     candidate's exact normalized locator does not collide with an existing
--     managed SourceEndpoint at decision time. Source identity is never
--     inferred or mutated here;
--   * CREDENTIALS_PENDING readiness is answered by a constant-shape,
--     indexed EXISTS probe, never by materializing access-evidence history.

-- ========================================================================
-- Tables and constraints
-- ========================================================================

-- One permanently immutable approved-ingestion marker per observation.
CREATE TABLE trusted_registry_observation (
    observation_id uuid PRIMARY KEY
        REFERENCES discovery_observation(observation_id),
    adapter_code text NOT NULL
        CHECK (adapter_code = 'ransomlook-fixed-origin-v1'),
    verified_at timestamptz NOT NULL
);

-- Constant-shape readiness lookup: candidate-scoped pending evidence only.
CREATE INDEX access_evidence_pending_candidate_idx
    ON access_evidence(candidate_id)
    WHERE candidate_id IS NOT NULL
      AND status = 'CREDENTIALS_PENDING';

-- ========================================================================
-- Versioned stored functions (production persistence API).
-- ========================================================================

-- The only versioned verified-ingestion marker writer. The adapter code is a
-- fixed SQL literal; a caller can never choose or broaden it. A replay is a
-- no-op that preserves the original verified_at.
CREATE FUNCTION trusted_registry_observation_record_v1(
    p_observation_id uuid,
    p_verified_at    timestamptz
)
RETURNS text
LANGUAGE plpgsql
AS $$
DECLARE
    v_observation discovery_observation%ROWTYPE;
BEGIN
    SELECT * INTO v_observation
      FROM discovery_observation o
     WHERE o.observation_id = p_observation_id;
    IF NOT FOUND THEN
        RETURN 'unknown_observation';
    END IF;
    IF p_verified_at IS NULL
       OR v_observation.provider_id <> 'ransomlook'
       OR v_observation.detection_method <> 'REGISTRY'
    THEN
        RETURN 'invalid_provenance';
    END IF;
    INSERT INTO trusted_registry_observation
        (observation_id, adapter_code, verified_at)
    VALUES
        (p_observation_id, 'ransomlook-fixed-origin-v1', p_verified_at)
    ON CONFLICT (observation_id) DO NOTHING;
    IF FOUND THEN
        RETURN 'added';
    END IF;
    RETURN 'replayed';
END;
$$;

-- Bounded evidence verification: admitted, linked observations that also
-- carry the approved-ingestion marker. Only opaque IDs are returned; never a
-- URL, excerpt, or raw provider value.
CREATE FUNCTION trusted_registry_candidate_ids_v1(
    p_candidate_id uuid,
    p_limit        integer
)
RETURNS TABLE (observation_id uuid)
LANGUAGE plpgsql
AS $$
BEGIN
    IF p_limit IS NULL OR p_limit < 1 OR p_limit > 65 THEN
        RAISE EXCEPTION 'invalid trusted-evidence limit'
            USING ERRCODE = '23514';
    END IF;
    RETURN QUERY
        SELECT o.observation_id
          FROM discovery_candidate_admission a
          JOIN discovery_observation o
            ON o.observation_id = a.observation_id
          JOIN trusted_registry_observation v
            ON v.observation_id = o.observation_id
         WHERE a.candidate_id = p_candidate_id
           AND a.outcome IN ('ADMITTED_NEW', 'ADMITTED_EXISTING')
           AND v.adapter_code = 'ransomlook-fixed-origin-v1'
         ORDER BY o.ingested_at, o.observation_id
         LIMIT p_limit;
END;
$$;

-- Bounded current-state guard: does this candidate's exact persisted locator
-- slot currently collide with an existing managed SourceEndpoint? Returns
-- one row with two booleans. A missing slot is an unsafe state, never "zero
-- endpoints". INACTIVE endpoints still count: the check concerns existing
-- managed ownership, not reachability or collection eligibility.
CREATE FUNCTION candidate_current_endpoint_match_v1(p_candidate_id uuid)
RETURNS TABLE (slot_found boolean, endpoint_present boolean)
LANGUAGE plpgsql
AS $$
DECLARE
    v_locator text;
BEGIN
    SELECT s.normalized_locator INTO v_locator
      FROM candidate_locator_slot s
     WHERE s.candidate_id = p_candidate_id;
    IF NOT FOUND THEN
        RETURN QUERY SELECT false, false;
        RETURN;
    END IF;
    RETURN QUERY
        SELECT true,
               EXISTS (
                   SELECT 1
                     FROM source_endpoint se
                    WHERE md5(se.uri) = md5(v_locator)
                      AND se.uri = v_locator
               );
END;
$$;

-- Constant-shape readiness probe. Any historical CREDENTIALS_PENDING row
-- blocks unauthenticated recon and automatic qualification in PR 19.
CREATE FUNCTION access_evidence_has_pending_v1(p_candidate_id uuid)
RETURNS boolean
LANGUAGE sql
STABLE
AS $$
    SELECT EXISTS (
        SELECT 1 FROM access_evidence a
         WHERE a.candidate_id = p_candidate_id
           AND a.status = 'CREDENTIALS_PENDING'
    );
$$;

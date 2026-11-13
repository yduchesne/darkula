-- SPDX-License-Identifier: AGPL-3.0-only
-- Darkula PR 19 migration: candidate reconciliation, deduplication and
-- policy-routed admission.
--
-- Additive and immutable. 0001-0010 remain byte-for-byte unchanged. Every
-- new database behavior lives here as a versioned stored function invoked
-- by the Python adapter; production Python contains no data-access SQL.
--
-- Semantics frozen here:
--   * admission is an explicit post-ingest step over an already persisted,
--     immutable DiscoveryObservation (PR 17). It never fetches, crawls,
--     resolves DNS, calls an LLM, promotes a Source, creates an endpoint, or
--     grants collection/access authority;
--   * a normalized locator is a SYNTACTIC lookup key, never a logical source
--     identity. `candidate_locator_slot` maps
--     sha256(normalized_locator) -> one candidate so the same exact locator
--     cannot create duplicate candidates. It never merges two different
--     locators and never asserts that equal URLs are equal Sources;
--   * matching is exact text equality only (no case folding, slash guessing,
--     ILIKE, fuzzy matching, or URI shortener expansion). More than 32 exact
--     managed-endpoint matches and more than one exact legacy candidate stay
--     conservative REVIEW_REQUIRED outcomes;
--   * `discovery_candidate_admission` is the durable ledger for EVERY
--     handled observation (including unresolved-name and review outcomes) so
--     pending selection never re-processes a handled claim forever;
--   * `discovery_endpoint_match` records evidence-only exact managed-endpoint
--     matches. It is written exclusively by this migration's admission
--     function; no Python repository creates or deletes rows;
--   * possible mirrors/aliases are read-only claims. There is no source
--     merge, endpoint rotation, candidate promotion, or trust signal here;
--   * no CASCADE deletion of immutable observation provenance.
--
-- Replay identity never includes generated UUIDs, acceptance timestamps, a
-- broker position, or a URI-as-identity.

-- ========================================================================
-- Tables and constraints
-- ========================================================================

-- One admission slot per exact normalized locator. The digest is a lookup
-- key only (never candidate/Source identity); the retained locator is
-- re-validated on every lookup so an equal digest with an unequal locator
-- becomes a fatal HASH_COLLISION instead of aliasing.
CREATE TABLE candidate_locator_slot (
    locator_key         text PRIMARY KEY
        CHECK (locator_key ~ '^[0-9a-f]{64}$'),
    normalized_locator  text NOT NULL
        CHECK (char_length(normalized_locator) BETWEEN 1 AND 2048),
    candidate_id        uuid NOT NULL UNIQUE
        REFERENCES source_candidate(id),
    first_observation_id uuid NOT NULL
        REFERENCES discovery_observation(observation_id)
);

-- Durable admission ledger: exactly one row per handled observation.
CREATE TABLE discovery_candidate_admission (
    observation_id uuid PRIMARY KEY
        REFERENCES discovery_observation(observation_id),
    outcome text NOT NULL CHECK (outcome IN (
        'UNRESOLVED_NAME',
        'KNOWN_ENDPOINT',
        'ADMITTED_NEW',
        'ADMITTED_EXISTING',
        'REVIEW_REQUIRED'
    )),
    reason text NOT NULL CHECK (reason IN (
        'NAME_ONLY',
        'EXACT_MANAGED_ENDPOINT',
        'NEW_LOCATOR',
        'EXISTING_LOCATOR_SLOT',
        'EXISTING_LEGACY_CANDIDATE',
        'MULTIPLE_LEGACY_CANDIDATES',
        'EXCESS_ENDPOINT_MATCHES'
    )),
    candidate_id uuid NULL REFERENCES source_candidate(id),
    processed_at timestamptz NOT NULL,
    CONSTRAINT discovery_candidate_admission_shape CHECK (
        (
            outcome IN ('ADMITTED_NEW', 'ADMITTED_EXISTING')
            AND candidate_id IS NOT NULL
        )
        OR (
            outcome IN ('UNRESOLVED_NAME', 'KNOWN_ENDPOINT', 'REVIEW_REQUIRED')
            AND candidate_id IS NULL
        )
    )
);

-- Evidence-only exact managed-endpoint matches for one KNOWN_ENDPOINT
-- admission. Written exclusively by discovery_admit_observation_v1.
CREATE TABLE discovery_endpoint_match (
    observation_id uuid NOT NULL
        REFERENCES discovery_candidate_admission(observation_id),
    endpoint_id    uuid NOT NULL REFERENCES source_endpoint(endpoint_id),
    match_reason   text NOT NULL DEFAULT 'EXACT_LOCATOR'
        CHECK (match_reason = 'EXACT_LOCATOR'),
    PRIMARY KEY (observation_id, endpoint_id)
);

-- Deterministic candidate -> ledger reads.
CREATE INDEX discovery_candidate_admission_candidate_idx
    ON discovery_candidate_admission (candidate_id, observation_id);

-- Deterministic pending-observation selection order.
CREATE INDEX discovery_observation_pending_admission_idx
    ON discovery_observation (ingested_at, observation_id);

-- Exact-locator equality lookups (md5 only narrows; the exact text equality
-- predicate decides). No global UNIQUE on source_endpoint.uri: an endpoint
-- URI is a locator, not a Source identity.
CREATE INDEX source_endpoint_uri_exact_hash_idx
    ON source_endpoint ((md5(uri)));
CREATE INDEX source_candidate_entrypoint_exact_hash_idx
    ON source_candidate ((md5(entrypoint)));

-- ========================================================================
-- Versioned stored functions (production persistence API).
-- Convention: controlled outcomes/conflicts return bounded sentinels instead
-- of raising, so the enclosing transaction stays correctly resolvable.
-- Real constraint violations remain DB-enforced.
-- ========================================================================

-- Bounded pending-observation selection: observations for one provider that
-- have no admission ledger row yet, in deterministic ascending order.
CREATE FUNCTION discovery_admission_pending_v1(
    p_provider_id text,
    p_limit       integer
)
RETURNS TABLE (observation_id uuid)
LANGUAGE plpgsql
AS $$
BEGIN
    IF p_provider_id IS NULL OR btrim(p_provider_id) = '' THEN
        RAISE EXCEPTION 'invalid admission provider filter'
            USING ERRCODE = '23514';
    END IF;
    IF p_limit IS NULL OR p_limit < 1 OR p_limit > 50 THEN
        RAISE EXCEPTION 'invalid pending-admission limit'
            USING ERRCODE = '23514';
    END IF;
    RETURN QUERY
        SELECT o.observation_id
          FROM discovery_observation o
          LEFT JOIN discovery_candidate_admission a
                 ON a.observation_id = o.observation_id
         WHERE o.provider_id = p_provider_id
           AND a.observation_id IS NULL
         ORDER BY o.ingested_at, o.observation_id
         LIMIT p_limit;
END;
$$;

-- Exactly zero or one stored admission ledger row.
CREATE FUNCTION discovery_admission_get_v1(p_observation_id uuid)
RETURNS TABLE (
    observation_id uuid,
    outcome        text,
    reason         text,
    candidate_id   uuid,
    processed_at   timestamptz
)
LANGUAGE sql
AS $$
    SELECT a.observation_id, a.outcome, a.reason, a.candidate_id,
           a.processed_at
      FROM discovery_candidate_admission a
     WHERE a.observation_id = p_observation_id;
$$;

-- Bounded exact managed-endpoint match evidence for one ledger row. Never
-- returns raw endpoint URIs.
CREATE FUNCTION discovery_admission_endpoints_v1(p_observation_id uuid)
RETURNS TABLE (endpoint_id uuid)
LANGUAGE sql
AS $$
    SELECT m.endpoint_id
      FROM discovery_endpoint_match m
     WHERE m.observation_id = p_observation_id
     ORDER BY m.endpoint_id
     LIMIT 32;
$$;

-- Immutable discovery claims linked to one admitted candidate, in stable
-- (ingested_at, observation_id) order. Columns are the full immutable
-- observation projection (the documented qualification subset first) so the
-- adapter can materialize a DiscoveryObservation without a second read.
-- p_limit is bounded to 1..65 so a caller can detect an over-64 claim set.
CREATE FUNCTION discovery_admission_claims_for_candidate_v1(
    p_candidate_id uuid,
    p_limit        integer
)
RETURNS TABLE (
    observation_id          uuid,
    provider_id             text,
    provider_record_id      text,
    origin_reference        text,
    normalized_locator      text,
    detection_method        text,
    reference_kind          text,
    validation_status       text,
    source_kind_hint        text,
    requires_auth_claim     boolean,
    ingested_at             timestamptz,
    provider_record_version text,
    asserted_reference      text,
    evidence_reference      text,
    evidence_excerpt        text,
    observed_at             timestamptz,
    candidate_id            uuid,
    semantic_key            text
)
LANGUAGE plpgsql
AS $$
BEGIN
    IF p_limit IS NULL OR p_limit < 1 OR p_limit > 65 THEN
        RAISE EXCEPTION 'invalid admission claim limit'
            USING ERRCODE = '23514';
    END IF;
    RETURN QUERY
        SELECT o.observation_id, o.provider_id, o.provider_record_id,
               o.origin_reference, o.normalized_locator,
               o.detection_method, o.reference_kind, o.validation_status,
               o.source_kind_hint, o.requires_auth_claim, o.ingested_at,
               o.provider_record_version, o.asserted_reference,
               o.evidence_reference, o.evidence_excerpt, o.observed_at,
               o.candidate_id, o.semantic_key
          FROM discovery_candidate_admission a
          JOIN discovery_observation o
            ON o.observation_id = a.observation_id
         WHERE a.candidate_id = p_candidate_id
           AND a.outcome IN ('ADMITTED_NEW', 'ADMITTED_EXISTING')
         ORDER BY o.ingested_at, o.observation_id
         LIMIT p_limit;
END;
$$;

-- Read-only possible mirror/alias claims: two distinct admitted candidates
-- that share an exact non-null (provider_id, origin_reference) while keeping
-- different normalized locators. Returns p_limit + 1 rows so the caller can
-- report an explicit overflow instead of a truncated claim list.
CREATE FUNCTION discovery_possible_aliases_v1(
    p_candidate_id uuid,
    p_limit        integer
)
RETURNS TABLE (
    other_candidate_id  uuid,
    our_observation_id  uuid,
    other_observation_id uuid
)
LANGUAGE plpgsql
AS $$
BEGIN
    IF p_limit IS NULL OR p_limit < 1 OR p_limit > 16 THEN
        RAISE EXCEPTION 'invalid possible-alias limit'
            USING ERRCODE = '23514';
    END IF;
    RETURN QUERY
        SELECT x.other_candidate_id, x.our_observation_id,
               x.other_observation_id
          FROM (
              SELECT DISTINCT ON (t.other_candidate_id)
                     t.other_candidate_id,
                     o.observation_id AS our_observation_id,
                     t.observation_id AS other_observation_id
                FROM discovery_candidate_admission a
                JOIN discovery_observation o
                  ON o.observation_id = a.observation_id
                JOIN (
                    SELECT a2.candidate_id AS other_candidate_id,
                           o2.provider_id,
                           o2.origin_reference,
                           o2.observation_id,
                           o2.normalized_locator
                      FROM discovery_candidate_admission a2
                      JOIN discovery_observation o2
                        ON o2.observation_id = a2.observation_id
                     WHERE a2.candidate_id IS NOT NULL
                       AND a2.candidate_id <> p_candidate_id
                       AND a2.outcome IN ('ADMITTED_NEW', 'ADMITTED_EXISTING')
                       AND o2.origin_reference IS NOT NULL
                       AND o2.normalized_locator IS NOT NULL
                ) t
                  ON t.provider_id = o.provider_id
                 AND t.origin_reference = o.origin_reference
                 AND t.normalized_locator <> o.normalized_locator
               WHERE a.candidate_id = p_candidate_id
                 AND a.outcome IN ('ADMITTED_NEW', 'ADMITTED_EXISTING')
                 AND o.origin_reference IS NOT NULL
                 AND o.normalized_locator IS NOT NULL
               ORDER BY t.other_candidate_id, o.observation_id,
                        t.observation_id
          ) x
         ORDER BY x.other_candidate_id, x.our_observation_id,
                  x.other_observation_id
         LIMIT p_limit + 1;
END;
$$;

-- The ONLY production admission writer.
--
-- Deterministic algorithm (first matching step wins):
--   1. unknown observation                 -> UNKNOWN_OBSERVATION
--   2. ledger replay                       -> stored result, replayed=true
--   3. name-only observation               -> UNRESOLVED_NAME/NAME_ONLY
--   4. malformed locator classification    -> UNEXPECTED_STATE
--   5. exact normalized-locator digest key (advisory-serialized)
--   6. >=33 exact managed endpoints        -> REVIEW_REQUIRED
--   7. 1..32 exact managed endpoints       -> KNOWN_ENDPOINT + match rows
--   8. existing locator slot               -> ADMITTED_EXISTING (or HASH_COLLISION)
--   9. >=2 or PROMOTED exact legacy match  -> REVIEW_REQUIRED
--  10. exactly 1 exact legacy candidate    -> ADMITTED_EXISTING + slot
--  11. otherwise new DISCOVERED candidate  -> ADMITTED_NEW + slot
--
-- A unique violation rolls back the whole subtransaction and permits exactly
-- one bounded committed-winner reread; there is no broad EXCEPTION fallback.
CREATE FUNCTION discovery_admit_observation_v1(
    p_observation_id uuid,
    p_candidate_id   uuid,
    p_event_id       uuid,
    p_processed_at   timestamptz
)
RETURNS TABLE (
    outcome      text,
    reason       text,
    candidate_id uuid,
    replayed     boolean,
    error_code   text
)
LANGUAGE plpgsql
AS $$
DECLARE
    v_observation    discovery_observation%ROWTYPE;
    v_existing       discovery_candidate_admission%ROWTYPE;
    v_slot           candidate_locator_slot%ROWTYPE;
    v_locator_key    text;
    v_endpoint_ids   uuid[];
    v_endpoint_count integer;
    v_legacy_count   integer;
    v_legacy_id      uuid;
    v_legacy_status  text;
    v_created        boolean;
BEGIN
    -- 1. Read the immutable observation.
    SELECT * INTO v_observation
      FROM discovery_observation o
     WHERE o.observation_id = p_observation_id;
    IF NOT FOUND THEN
        RETURN QUERY
            SELECT NULL::text, NULL::text, NULL::uuid, false,
                   'UNKNOWN_OBSERVATION'::text;
        RETURN;
    END IF;

    -- 2. Exact ledger replay: no mutation at all.
    SELECT * INTO v_existing
      FROM discovery_candidate_admission a
     WHERE a.observation_id = p_observation_id;
    IF FOUND THEN
        RETURN QUERY
            SELECT v_existing.outcome, v_existing.reason,
                   v_existing.candidate_id, true, 'NONE'::text;
        RETURN;
    END IF;

    -- 3. Name-only claims never become candidates.
    IF v_observation.reference_kind = 'NAME_ONLY' THEN
        INSERT INTO discovery_candidate_admission
            (observation_id, outcome, reason, candidate_id, processed_at)
        VALUES
            (p_observation_id, 'UNRESOLVED_NAME', 'NAME_ONLY', NULL,
             p_processed_at);
        RETURN QUERY
            SELECT 'UNRESOLVED_NAME'::text, 'NAME_ONLY'::text, NULL::uuid,
                   false, 'NONE'::text;
        RETURN;
    END IF;

    -- 4. Fail closed on malformed persisted classification: never
    -- reinterpret an invalid locator as a name-only claim.
    IF v_observation.reference_kind <> 'LOCATOR'
       OR v_observation.validation_status <> 'VALID_LOCATOR'
       OR v_observation.normalized_locator IS NULL
    THEN
        RETURN QUERY
            SELECT NULL::text, NULL::text, NULL::uuid, false,
                   'UNEXPECTED_STATE'::text;
        RETURN;
    END IF;

    -- 5. Server-side digest of the persisted normalized locator only.
    v_locator_key := encode(
        sha256(convert_to(v_observation.normalized_locator, 'UTF8')), 'hex');

    -- 6. Serialize same-locator admissions (no network/long call under lock).
    PERFORM pg_advisory_xact_lock(hashtextextended(v_locator_key, 0));

    -- Re-check the ledger after acquiring the lock.
    SELECT * INTO v_existing
      FROM discovery_candidate_admission a
     WHERE a.observation_id = p_observation_id;
    IF FOUND THEN
        RETURN QUERY
            SELECT v_existing.outcome, v_existing.reason,
                   v_existing.candidate_id, true, 'NONE'::text;
        RETURN;
    END IF;

    -- 7. Exact managed-endpoint matches (evidence only; never a Source write).
    SELECT array_agg(e.endpoint_id ORDER BY e.endpoint_id)
      INTO v_endpoint_ids
      FROM (
          SELECT se.endpoint_id
            FROM source_endpoint se
           WHERE md5(se.uri) = md5(v_observation.normalized_locator)
             AND se.uri = v_observation.normalized_locator
           ORDER BY se.endpoint_id
           LIMIT 33
      ) e;
    v_endpoint_count := COALESCE(cardinality(v_endpoint_ids), 0);

    IF v_endpoint_count >= 33 THEN
        INSERT INTO discovery_candidate_admission
            (observation_id, outcome, reason, candidate_id, processed_at)
        VALUES
            (p_observation_id, 'REVIEW_REQUIRED', 'EXCESS_ENDPOINT_MATCHES',
             NULL, p_processed_at);
        RETURN QUERY
            SELECT 'REVIEW_REQUIRED'::text, 'EXCESS_ENDPOINT_MATCHES'::text,
                   NULL::uuid, false, 'NONE'::text;
        RETURN;
    ELSIF v_endpoint_count >= 1 THEN
        INSERT INTO discovery_candidate_admission
            (observation_id, outcome, reason, candidate_id, processed_at)
        VALUES
            (p_observation_id, 'KNOWN_ENDPOINT', 'EXACT_MANAGED_ENDPOINT',
             NULL, p_processed_at);
        INSERT INTO discovery_endpoint_match (observation_id, endpoint_id)
            SELECT p_observation_id, matched.endpoint_id
              FROM unnest(v_endpoint_ids) AS matched(endpoint_id);
        RETURN QUERY
            SELECT 'KNOWN_ENDPOINT'::text, 'EXACT_MANAGED_ENDPOINT'::text,
                   NULL::uuid, false, 'NONE'::text;
        RETURN;
    END IF;

    -- 8. Existing locator slot: re-validate the retained exact locator.
    SELECT * INTO v_slot
      FROM candidate_locator_slot s
     WHERE s.locator_key = v_locator_key;
    IF FOUND THEN
        IF v_slot.normalized_locator <> v_observation.normalized_locator THEN
            RETURN QUERY
                SELECT NULL::text, NULL::text, NULL::uuid, false,
                       'HASH_COLLISION'::text;
            RETURN;
        END IF;
        INSERT INTO discovery_candidate_admission
            (observation_id, outcome, reason, candidate_id, processed_at)
        VALUES
            (p_observation_id, 'ADMITTED_EXISTING',
             'EXISTING_LOCATOR_SLOT', v_slot.candidate_id, p_processed_at);
        RETURN QUERY
            SELECT 'ADMITTED_EXISTING'::text, 'EXISTING_LOCATOR_SLOT'::text,
                   v_slot.candidate_id, false, 'NONE'::text;
        RETURN;
    END IF;

    -- 9. Exact legacy candidate entrypoint matches (query at most 2).
    SELECT count(*) INTO v_legacy_count
      FROM (
          SELECT c.id
            FROM source_candidate c
           WHERE md5(c.entrypoint) = md5(v_observation.normalized_locator)
             AND c.entrypoint = v_observation.normalized_locator
           ORDER BY c.id
           LIMIT 2
      ) c;

    IF v_legacy_count >= 2 THEN
        INSERT INTO discovery_candidate_admission
            (observation_id, outcome, reason, candidate_id, processed_at)
        VALUES
            (p_observation_id, 'REVIEW_REQUIRED',
             'MULTIPLE_LEGACY_CANDIDATES', NULL, p_processed_at);
        RETURN QUERY
            SELECT 'REVIEW_REQUIRED'::text,
                   'MULTIPLE_LEGACY_CANDIDATES'::text, NULL::uuid, false,
                   'NONE'::text;
        RETURN;
    ELSIF v_legacy_count = 1 THEN
        SELECT c.id, c.status INTO v_legacy_id, v_legacy_status
          FROM source_candidate c
         WHERE md5(c.entrypoint) = md5(v_observation.normalized_locator)
           AND c.entrypoint = v_observation.normalized_locator
         ORDER BY c.id
         LIMIT 1;
        IF v_legacy_status = 'PROMOTED' THEN
            -- A promoted candidate already became a managed Source; never
            -- reanimate it from an untrusted claim.
            INSERT INTO discovery_candidate_admission
                (observation_id, outcome, reason, candidate_id, processed_at)
            VALUES
                (p_observation_id, 'REVIEW_REQUIRED',
                 'MULTIPLE_LEGACY_CANDIDATES', NULL, p_processed_at);
            RETURN QUERY
                SELECT 'REVIEW_REQUIRED'::text,
                       'MULTIPLE_LEGACY_CANDIDATES'::text, NULL::uuid, false,
                       'NONE'::text;
            RETURN;
        END IF;
        INSERT INTO candidate_locator_slot
            (locator_key, normalized_locator, candidate_id,
             first_observation_id)
        VALUES
            (v_locator_key, v_observation.normalized_locator, v_legacy_id,
             p_observation_id);
        INSERT INTO discovery_candidate_admission
            (observation_id, outcome, reason, candidate_id, processed_at)
        VALUES
            (p_observation_id, 'ADMITTED_EXISTING',
             'EXISTING_LEGACY_CANDIDATE', v_legacy_id, p_processed_at);
        RETURN QUERY
            SELECT 'ADMITTED_EXISTING'::text,
                   'EXISTING_LEGACY_CANDIDATE'::text, v_legacy_id, false,
                   'NONE'::text;
        RETURN;
    END IF;

    -- 10/11. New candidate + DISCOVERED event + slot + ledger atomically.
    BEGIN
        SELECT candidate_create_v1(
            p_candidate_id,
            v_observation.ingested_at,
            v_observation.detection_method,
            v_observation.normalized_locator,
            'DISCOVERED',
            jsonb_build_object(
                'observation_id', p_observation_id,
                'locator_key', v_locator_key
            ),
            p_event_id,
            'DISCOVERED',
            v_observation.ingested_at,
            jsonb_build_object('observation_id', p_observation_id),
            'discovery observation admitted',
            'discovery_observation:' || p_observation_id::text
        ) INTO v_created;

        IF v_created IS NOT TRUE THEN
            RETURN QUERY
                SELECT NULL::text, NULL::text, NULL::uuid, false,
                       'CREATION_CONFLICT'::text;
            RETURN;
        END IF;

        INSERT INTO candidate_locator_slot
            (locator_key, normalized_locator, candidate_id,
             first_observation_id)
        VALUES
            (v_locator_key, v_observation.normalized_locator, p_candidate_id,
             p_observation_id);
        INSERT INTO discovery_candidate_admission
            (observation_id, outcome, reason, candidate_id, processed_at)
        VALUES
            (p_observation_id, 'ADMITTED_NEW', 'NEW_LOCATOR', p_candidate_id,
             p_processed_at);
    EXCEPTION WHEN unique_violation THEN
        -- The subtransaction (candidate/event/slot/ledger) rolled back as a
        -- unit. Re-read the committed winner exactly once.
        SELECT * INTO v_existing
          FROM discovery_candidate_admission a
         WHERE a.observation_id = p_observation_id;
        IF FOUND THEN
            RETURN QUERY
                SELECT v_existing.outcome, v_existing.reason,
                       v_existing.candidate_id, true, 'NONE'::text;
            RETURN;
        END IF;
        SELECT * INTO v_slot
          FROM candidate_locator_slot s
         WHERE s.locator_key = v_locator_key;
        IF FOUND
           AND v_slot.normalized_locator = v_observation.normalized_locator
        THEN
            INSERT INTO discovery_candidate_admission
                (observation_id, outcome, reason, candidate_id, processed_at)
            VALUES
                (p_observation_id, 'ADMITTED_EXISTING',
                 'EXISTING_LOCATOR_SLOT', v_slot.candidate_id, p_processed_at);
            RETURN QUERY
                SELECT 'ADMITTED_EXISTING'::text,
                       'EXISTING_LOCATOR_SLOT'::text, v_slot.candidate_id,
                       false, 'NONE'::text;
            RETURN;
        END IF;
        RETURN QUERY
            SELECT NULL::text, NULL::text, NULL::uuid, false,
                   'CREATION_CONFLICT'::text;
        RETURN;
    END;

    RETURN QUERY
        SELECT 'ADMITTED_NEW'::text, 'NEW_LOCATOR'::text, p_candidate_id,
               false, 'NONE'::text;
END;
$$;

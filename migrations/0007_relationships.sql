-- SPDX-License-Identifier: AGPL-3.0-only
-- Darkula PR 13 migration: content-derived relationship assertions.
--
-- Additive and immutable. 0001-0006 remain unchanged. All new database
-- behavior lives here as versioned stored functions
-- (relationship_extraction_result_*_v1, extracted_relationship_*_v1) invoked
-- by the Python adapter; production Python contains no SQL.
--
-- Semantics:
--   * A persisted relationship asserts only that ONE normalized content
--     observation asserted a predicate between TWO extracted-entity
--     occurrences. It is never a global graph edge, an entity-resolution
--     result, or source-independent truth.
--   * relationship_extraction_result is append-only/versioned. The
--     semantic/idempotency identity is (content_id, profile_name,
--     profile_version) and is UNIQUE, so a concurrent/duplicate extraction
--     can never create a second result for the same content+profile. A new
--     relationship profile version coexists immutably with the old one.
--   * extracted_relationship references extracted_entity occurrences, never
--     endpoint values. Both endpoint occurrences must belong to the same
--     content as the relationship, and self-edges are rejected.
--   * support_text is the exact canonical text slice at the recorded support
--     span (the DB cannot verify ObjectStore bytes; trusted application
--     grounding owns that). The span length must equal the text length.
--   * no predicate is arbitrary: the finite relationship-assertions/v1
--     vocabulary is enforced by a CHECK constraint.

CREATE TABLE relationship_extraction_result (
    id                 uuid PRIMARY KEY,
    content_id         uuid NOT NULL REFERENCES normalized_content(content_id),
    profile_name       text NOT NULL CHECK (length(btrim(profile_name)) > 0),
    profile_version    text NOT NULL CHECK (length(btrim(profile_version)) > 0),
    extractor_manifest jsonb NOT NULL,
    extracted_at       timestamptz NOT NULL,
    relationship_count integer NOT NULL CHECK (relationship_count >= 0),
    -- Semantic idempotency key: one durable result per content+profile.
    UNIQUE (content_id, profile_name, profile_version)
);

CREATE INDEX relationship_extraction_result_profile_idx
    ON relationship_extraction_result (content_id, profile_name, profile_version);

CREATE TABLE extracted_relationship (
    id                               uuid PRIMARY KEY,
    relationship_extraction_result_id uuid NOT NULL
        REFERENCES relationship_extraction_result(id) ON DELETE CASCADE,
    content_id                       uuid NOT NULL
        REFERENCES normalized_content(content_id),
    source_entity_id                 uuid NOT NULL
        REFERENCES extracted_entity(id),
    predicate                        text NOT NULL CHECK (predicate IN (
        'AFFILIATED_WITH',
        'USES',
        'OPERATES',
        'TARGETS',
        'IMPERSONATES',
        'SELLS',
        'OFFERS_ACCESS_TO',
        'HAS_ACCESS_TO',
        'LOCATED_IN',
        'AFFECTS'
    )),
    target_entity_id                 uuid NOT NULL
        REFERENCES extracted_entity(id),
    support_text                     text NOT NULL CHECK (length(support_text) > 0),
    support_span_start               integer NOT NULL CHECK (support_span_start >= 0),
    support_span_end                 integer NOT NULL CHECK (support_span_end > support_span_start),
    extractor_name                   text NOT NULL CHECK (length(btrim(extractor_name)) > 0),
    extractor_version                text NOT NULL CHECK (length(btrim(extractor_version)) > 0),
    extraction_confidence            double precision NOT NULL CHECK (
        extraction_confidence >= 0.0 AND extraction_confidence <= 1.0
    ),
    -- Explicit direction: never a self-edge and never auto-sorted.
    CHECK (source_entity_id <> target_entity_id),
    -- Exact support text length matches the recorded half-open span.
    CHECK (support_span_end - support_span_start = length(support_text)),
    -- Exact assertion-occurrence identity: a retry cannot duplicate a row.
    UNIQUE (
        relationship_extraction_result_id, source_entity_id, predicate,
        target_entity_id, support_span_start, support_span_end,
        extractor_name, extractor_version
    )
);

-- Deterministic result listing order.
CREATE INDEX extracted_relationship_result_order_idx
    ON extracted_relationship (relationship_extraction_result_id,
                               support_span_start, support_span_end, id);

-- Deterministic content-level listing order across results/versions.
CREATE INDEX extracted_relationship_content_order_idx
    ON extracted_relationship (content_id, support_span_start, support_span_end, id);

-- Endpoint occurrence lookup (no speculative graph indexing).
CREATE INDEX extracted_relationship_source_idx
    ON extracted_relationship (source_entity_id, id);

CREATE INDEX extracted_relationship_target_idx
    ON extracted_relationship (target_entity_id, id);

-- ========================================================================
-- Versioned stored functions (production relationship persistence API).
-- ========================================================================

-- Create one immutable relationship-extraction result. Returns 'added' /
-- 'duplicate' / 'unknown_content'.
CREATE FUNCTION relationship_extraction_result_create_v1(
    p_id                 uuid,
    p_content_id         uuid,
    p_profile_name       text,
    p_profile_version    text,
    p_extractor_manifest jsonb,
    p_extracted_at       timestamptz,
    p_relationship_count integer
) RETURNS text
LANGUAGE plpgsql
AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM normalized_content WHERE content_id = p_content_id
    ) THEN
        RETURN 'unknown_content';
    END IF;
    IF EXISTS (
        SELECT 1 FROM relationship_extraction_result
         WHERE content_id = p_content_id
           AND profile_name = p_profile_name
           AND profile_version = p_profile_version
    ) THEN
        RETURN 'duplicate';
    END IF;
    INSERT INTO relationship_extraction_result
        (id, content_id, profile_name, profile_version, extractor_manifest,
         extracted_at, relationship_count)
    VALUES
        (p_id, p_content_id, p_profile_name, p_profile_version,
         p_extractor_manifest, p_extracted_at, p_relationship_count);
    RETURN 'added';
END;
$$;

-- Load one result by identity.
CREATE FUNCTION relationship_extraction_result_get_v1(p_id uuid)
RETURNS TABLE (
    id                 uuid,
    content_id         uuid,
    profile_name       text,
    profile_version    text,
    extractor_manifest jsonb,
    extracted_at       timestamptz,
    relationship_count integer
)
LANGUAGE sql
AS $$
    SELECT r.id, r.content_id, r.profile_name, r.profile_version,
           r.extractor_manifest, r.extracted_at, r.relationship_count
      FROM relationship_extraction_result r
     WHERE r.id = p_id;
$$;

-- Load one result by its semantic content/profile key.
CREATE FUNCTION relationship_extraction_result_get_by_profile_v1(
    p_content_id      uuid,
    p_profile_name    text,
    p_profile_version text
)
RETURNS TABLE (
    id                 uuid,
    content_id         uuid,
    profile_name       text,
    profile_version    text,
    extractor_manifest jsonb,
    extracted_at       timestamptz,
    relationship_count integer
)
LANGUAGE sql
AS $$
    SELECT r.id, r.content_id, r.profile_name, r.profile_version,
           r.extractor_manifest, r.extracted_at, r.relationship_count
      FROM relationship_extraction_result r
     WHERE r.content_id = p_content_id
       AND r.profile_name = p_profile_name
       AND r.profile_version = p_profile_version;
$$;

-- Create one relationship assertion. Returns 'added' / 'duplicate' /
-- 'unknown_result' / 'content_mismatch' / 'unknown_source' / 'unknown_target' /
-- 'source_content_mismatch' / 'target_content_mismatch' / 'self_edge' /
-- 'invalid_predicate' / 'invalid_span' / 'invalid_support' /
-- 'invalid_confidence'.
CREATE FUNCTION extracted_relationship_create_v1(
    p_id                               uuid,
    p_relationship_extraction_result_id uuid,
    p_content_id                       uuid,
    p_source_entity_id                 uuid,
    p_predicate                        text,
    p_target_entity_id                 uuid,
    p_support_text                     text,
    p_support_span_start               integer,
    p_support_span_end                 integer,
    p_extractor_name                   text,
    p_extractor_version                text,
    p_extraction_confidence            double precision
) RETURNS text
LANGUAGE plpgsql
AS $$
DECLARE
    v_result_content uuid;
    v_source_content uuid;
    v_target_content uuid;
BEGIN
    SELECT r.content_id INTO v_result_content
      FROM relationship_extraction_result r
     WHERE r.id = p_relationship_extraction_result_id;
    IF NOT FOUND THEN
        RETURN 'unknown_result';
    END IF;
    IF v_result_content <> p_content_id THEN
        RETURN 'content_mismatch';
    END IF;
    SELECT e.content_id INTO v_source_content
      FROM extracted_entity e
     WHERE e.id = p_source_entity_id;
    IF NOT FOUND THEN
        RETURN 'unknown_source';
    END IF;
    IF v_source_content <> p_content_id THEN
        RETURN 'source_content_mismatch';
    END IF;
    SELECT e.content_id INTO v_target_content
      FROM extracted_entity e
     WHERE e.id = p_target_entity_id;
    IF NOT FOUND THEN
        RETURN 'unknown_target';
    END IF;
    IF v_target_content <> p_content_id THEN
        RETURN 'target_content_mismatch';
    END IF;
    IF p_source_entity_id = p_target_entity_id THEN
        RETURN 'self_edge';
    END IF;
    IF p_predicate NOT IN (
        'AFFILIATED_WITH', 'USES', 'OPERATES', 'TARGETS', 'IMPERSONATES',
        'SELLS', 'OFFERS_ACCESS_TO', 'HAS_ACCESS_TO', 'LOCATED_IN', 'AFFECTS'
    ) THEN
        RETURN 'invalid_predicate';
    END IF;
    IF p_support_span_start < 0 OR p_support_span_end <= p_support_span_start THEN
        RETURN 'invalid_span';
    END IF;
    IF p_support_text IS NULL
       OR length(p_support_text) = 0
       OR (p_support_span_end - p_support_span_start) <> length(p_support_text)
    THEN
        RETURN 'invalid_support';
    END IF;
    IF p_extraction_confidence IS NULL
       OR p_extraction_confidence < 0.0
       OR p_extraction_confidence > 1.0
    THEN
        RETURN 'invalid_confidence';
    END IF;
    IF EXISTS (
        SELECT 1 FROM extracted_relationship r
         WHERE r.relationship_extraction_result_id =
               p_relationship_extraction_result_id
           AND r.source_entity_id = p_source_entity_id
           AND r.predicate = p_predicate
           AND r.target_entity_id = p_target_entity_id
           AND r.support_span_start = p_support_span_start
           AND r.support_span_end = p_support_span_end
           AND r.extractor_name = p_extractor_name
           AND r.extractor_version = p_extractor_version
    ) THEN
        RETURN 'duplicate';
    END IF;
    INSERT INTO extracted_relationship
        (id, relationship_extraction_result_id, content_id, source_entity_id,
         predicate, target_entity_id, support_text, support_span_start,
         support_span_end, extractor_name, extractor_version,
         extraction_confidence)
    VALUES
        (p_id, p_relationship_extraction_result_id, p_content_id,
         p_source_entity_id, p_predicate, p_target_entity_id, p_support_text,
         p_support_span_start, p_support_span_end, p_extractor_name,
         p_extractor_version, p_extraction_confidence);
    RETURN 'added';
END;
$$;

-- List a result's assertions in deterministic order.
CREATE FUNCTION extracted_relationship_list_for_result_v1(
    p_relationship_extraction_result_id uuid
)
RETURNS TABLE (
    id                               uuid,
    relationship_extraction_result_id uuid,
    content_id                       uuid,
    source_entity_id                 uuid,
    predicate                        text,
    target_entity_id                 uuid,
    support_text                     text,
    support_span_start               integer,
    support_span_end                 integer,
    extractor_name                   text,
    extractor_version                text,
    extraction_confidence            double precision
)
LANGUAGE sql
AS $$
    SELECT r.id, r.relationship_extraction_result_id, r.content_id,
           r.source_entity_id, r.predicate, r.target_entity_id, r.support_text,
           r.support_span_start, r.support_span_end, r.extractor_name,
           r.extractor_version, r.extraction_confidence
      FROM extracted_relationship r
     WHERE r.relationship_extraction_result_id =
           p_relationship_extraction_result_id
     ORDER BY r.support_span_start, r.support_span_end, r.source_entity_id,
              r.predicate, r.target_entity_id, r.id;
$$;

-- List a content's assertions across all results/profile versions.
CREATE FUNCTION extracted_relationship_list_for_content_v1(p_content_id uuid)
RETURNS TABLE (
    id                               uuid,
    relationship_extraction_result_id uuid,
    content_id                       uuid,
    source_entity_id                 uuid,
    predicate                        text,
    target_entity_id                 uuid,
    support_text                     text,
    support_span_start               integer,
    support_span_end                 integer,
    extractor_name                   text,
    extractor_version                text,
    extraction_confidence            double precision
)
LANGUAGE sql
AS $$
    SELECT r.id, r.relationship_extraction_result_id, r.content_id,
           r.source_entity_id, r.predicate, r.target_entity_id, r.support_text,
           r.support_span_start, r.support_span_end, r.extractor_name,
           r.extractor_version, r.extraction_confidence
      FROM extracted_relationship r
     WHERE r.content_id = p_content_id
     ORDER BY r.support_span_start, r.support_span_end, r.source_entity_id,
              r.predicate, r.target_entity_id, r.id;
$$;

-- Load one assertion by identity.
CREATE FUNCTION extracted_relationship_get_v1(p_id uuid)
RETURNS TABLE (
    id                               uuid,
    relationship_extraction_result_id uuid,
    content_id                       uuid,
    source_entity_id                 uuid,
    predicate                        text,
    target_entity_id                 uuid,
    support_text                     text,
    support_span_start               integer,
    support_span_end                 integer,
    extractor_name                   text,
    extractor_version                text,
    extraction_confidence            double precision
)
LANGUAGE sql
AS $$
    SELECT r.id, r.relationship_extraction_result_id, r.content_id,
           r.source_entity_id, r.predicate, r.target_entity_id, r.support_text,
           r.support_span_start, r.support_span_end, r.extractor_name,
           r.extractor_version, r.extraction_confidence
      FROM extracted_relationship r
     WHERE r.id = p_id;
$$;

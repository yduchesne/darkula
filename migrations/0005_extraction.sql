-- SPDX-License-Identifier: AGPL-3.0-only
-- Darkula PR 11 migration: deterministic extraction results and occurrences.
--
-- Immutable 0001-0004 remain unchanged. All new database behavior lives here
-- as versioned stored functions (extraction_result_*_v1, extracted_entity_*_v1)
-- invoked by the Python adapter; production Python contains no SQL.
--
-- Semantics:
--   * extraction_result is an append-only/versioned historical record. The
--     semantic/idempotency identity is (content_id, profile_name,
--     profile_version) and is UNIQUE, so a concurrent/duplicate extraction
--     can never create a second result for the same content+profile. A new
--     normalized observation with identical bytes still gets its own result;
--     a new profile version coexists immutably with the old one.
--   * extracted_entity is one provenance-bearing occurrence, NOT a global
--     IOC table. The same value at two spans is two rows; the same value in
--     two normalized observations is two provenance-bearing rows. The exact
--     occurrence uniqueness prevents a retry from duplicating a row.
--   * extractor_manifest freezes the exact historical extractor set (never
--     reconstructed from current code).
--   * no entity value, content, URI, object key, or hash is ever combined
--     into telemetry; PostgreSQL stores the value as data for inspection.

CREATE TABLE extraction_result (
    id                 uuid PRIMARY KEY,
    content_id         uuid NOT NULL REFERENCES normalized_content(content_id),
    profile_name       text NOT NULL CHECK (length(btrim(profile_name)) > 0),
    profile_version    text NOT NULL CHECK (length(btrim(profile_version)) > 0),
    extractor_manifest jsonb NOT NULL,
    extracted_at       timestamptz NOT NULL,
    entity_count       integer NOT NULL CHECK (entity_count >= 0),
    -- Semantic idempotency key: one durable result per content+profile.
    UNIQUE (content_id, profile_name, profile_version)
);

CREATE INDEX extraction_result_profile_idx
    ON extraction_result (content_id, profile_name, profile_version);

CREATE TABLE extracted_entity (
    id                  uuid PRIMARY KEY,
    extraction_result_id uuid NOT NULL REFERENCES extraction_result(id)
                         ON DELETE CASCADE,
    content_id          uuid NOT NULL REFERENCES normalized_content(content_id),
    entity_type         text NOT NULL CHECK (entity_type IN (
        'IP_ADDRESS',
        'DOMAIN',
        'URL',
        'EMAIL',
        'HASH'
    )),
    raw_value           text NOT NULL CHECK (length(raw_value) > 0),
    normalized_value    text NOT NULL CHECK (length(normalized_value) > 0),
    span_start          integer NOT NULL CHECK (span_start >= 0),
    span_end            integer NOT NULL CHECK (span_end > span_start),
    extractor_name      text NOT NULL CHECK (length(btrim(extractor_name)) > 0),
    extractor_version   text NOT NULL CHECK (length(btrim(extractor_version)) > 0),
    subtype             text CHECK (subtype IS NULL OR length(btrim(subtype)) > 0),
    -- Exact occurrence identity: a retry cannot duplicate the same occurrence.
    UNIQUE (
        extraction_result_id, entity_type, span_start, span_end,
        extractor_name, extractor_version, normalized_value
    )
);

-- Deterministic result listing order.
CREATE INDEX extracted_entity_result_order_idx
    ON extracted_entity (extraction_result_id, span_start, span_end, id);

-- Deterministic content-level listing order across results/versions.
CREATE INDEX extracted_entity_content_order_idx
    ON extracted_entity (content_id, entity_type, span_start, id);

-- ========================================================================
-- Versioned stored functions (production extraction persistence API).
-- ========================================================================

-- Create one immutable extraction result. Returns 'added' / 'duplicate' /
-- 'unknown_content'.
CREATE FUNCTION extraction_result_create_v1(
    p_id                 uuid,
    p_content_id         uuid,
    p_profile_name       text,
    p_profile_version    text,
    p_extractor_manifest jsonb,
    p_extracted_at       timestamptz,
    p_entity_count       integer
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
        SELECT 1 FROM extraction_result
         WHERE content_id = p_content_id
           AND profile_name = p_profile_name
           AND profile_version = p_profile_version
    ) THEN
        RETURN 'duplicate';
    END IF;
    INSERT INTO extraction_result
        (id, content_id, profile_name, profile_version, extractor_manifest,
         extracted_at, entity_count)
    VALUES
        (p_id, p_content_id, p_profile_name, p_profile_version,
         p_extractor_manifest, p_extracted_at, p_entity_count);
    RETURN 'added';
END;
$$;

-- Load one result by identity.
CREATE FUNCTION extraction_result_get_v1(p_id uuid)
RETURNS TABLE (
    id                 uuid,
    content_id         uuid,
    profile_name       text,
    profile_version    text,
    extractor_manifest jsonb,
    extracted_at       timestamptz,
    entity_count       integer
)
LANGUAGE sql
AS $$
    SELECT r.id, r.content_id, r.profile_name, r.profile_version,
           r.extractor_manifest, r.extracted_at, r.entity_count
      FROM extraction_result r
     WHERE r.id = p_id;
$$;

-- Load one result by its semantic content/profile key.
CREATE FUNCTION extraction_result_get_by_profile_v1(
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
    entity_count       integer
)
LANGUAGE sql
AS $$
    SELECT r.id, r.content_id, r.profile_name, r.profile_version,
           r.extractor_manifest, r.extracted_at, r.entity_count
      FROM extraction_result r
     WHERE r.content_id = p_content_id
       AND r.profile_name = p_profile_name
       AND r.profile_version = p_profile_version;
$$;

-- Create one extracted-entity occurrence. Returns 'added' / 'duplicate' /
-- 'unknown_result' / 'content_mismatch'.
CREATE FUNCTION extracted_entity_create_v1(
    p_id                  uuid,
    p_extraction_result_id uuid,
    p_content_id          uuid,
    p_entity_type         text,
    p_raw_value           text,
    p_normalized_value    text,
    p_span_start          integer,
    p_span_end            integer,
    p_extractor_name      text,
    p_extractor_version   text,
    p_subtype             text
) RETURNS text
LANGUAGE plpgsql
AS $$
DECLARE
    v_result_content uuid;
BEGIN
    SELECT r.content_id INTO v_result_content
      FROM extraction_result r
     WHERE r.id = p_extraction_result_id;
    IF NOT FOUND THEN
        RETURN 'unknown_result';
    END IF;
    IF v_result_content <> p_content_id THEN
        RETURN 'content_mismatch';
    END IF;
    IF EXISTS (
        SELECT 1 FROM extracted_entity e
         WHERE e.extraction_result_id = p_extraction_result_id
           AND e.entity_type = p_entity_type
           AND e.span_start = p_span_start
           AND e.span_end = p_span_end
           AND e.extractor_name = p_extractor_name
           AND e.extractor_version = p_extractor_version
           AND e.normalized_value = p_normalized_value
    ) THEN
        RETURN 'duplicate';
    END IF;
    INSERT INTO extracted_entity
        (id, extraction_result_id, content_id, entity_type, raw_value,
         normalized_value, span_start, span_end, extractor_name,
         extractor_version, subtype)
    VALUES
        (p_id, p_extraction_result_id, p_content_id, p_entity_type,
         p_raw_value, p_normalized_value, p_span_start, p_span_end,
         p_extractor_name, p_extractor_version, p_subtype);
    RETURN 'added';
END;
$$;

-- List a result's occurrences in deterministic order.
CREATE FUNCTION extracted_entity_list_for_result_v1(p_extraction_result_id uuid)
RETURNS TABLE (
    id                 uuid,
    extraction_result_id uuid,
    content_id         uuid,
    entity_type        text,
    raw_value          text,
    normalized_value   text,
    span_start         integer,
    span_end           integer,
    extractor_name     text,
    extractor_version  text,
    subtype            text
)
LANGUAGE sql
AS $$
    SELECT e.id, e.extraction_result_id, e.content_id, e.entity_type,
           e.raw_value, e.normalized_value, e.span_start, e.span_end,
           e.extractor_name, e.extractor_version, e.subtype
      FROM extracted_entity e
     WHERE e.extraction_result_id = p_extraction_result_id
     ORDER BY e.span_start, e.span_end, e.entity_type, e.normalized_value,
              e.extractor_name, e.extractor_version, e.id;
$$;

-- List a content's occurrences across all results/profile versions.
CREATE FUNCTION extracted_entity_list_for_content_v1(p_content_id uuid)
RETURNS TABLE (
    id                 uuid,
    extraction_result_id uuid,
    content_id         uuid,
    entity_type        text,
    raw_value          text,
    normalized_value   text,
    span_start         integer,
    span_end           integer,
    extractor_name     text,
    extractor_version  text,
    subtype            text
)
LANGUAGE sql
AS $$
    SELECT e.id, e.extraction_result_id, e.content_id, e.entity_type,
           e.raw_value, e.normalized_value, e.span_start, e.span_end,
           e.extractor_name, e.extractor_version, e.subtype
      FROM extracted_entity e
     WHERE e.content_id = p_content_id
     ORDER BY e.span_start, e.span_end, e.entity_type, e.normalized_value,
              e.extractor_name, e.extractor_version, e.id;
$$;

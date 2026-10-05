-- SPDX-License-Identifier: AGPL-3.0-only
-- Darkula PR 12 migration: semantic entity confidence + geographic resolution.
--
-- Additive and immutable. 0001-0005 remain unchanged. Existing PR 11
-- deterministic rows keep extraction_confidence NULL (no fabricated
-- probabilistic confidence). The PR 11 v1 stored functions remain available
-- and unchanged; a v2 create/list family adds the nullable confidence column.
--
-- Geographic resolution is a separate, provider-neutral interpretation of one
-- LOCATION occurrence. Geometry uses a bounded WGS84 latitude/longitude pair
-- (no PostGIS extension is enabled by default). Resolutions are immutable and
-- versioned: (extracted_entity_id, resolver_name, resolver_version) is UNIQUE.

-- ========================================================================
-- Semantic extraction confidence on extracted_entity (additive).
-- ========================================================================

ALTER TABLE extracted_entity
    ADD COLUMN extraction_confidence double precision;

ALTER TABLE extracted_entity
    ADD CONSTRAINT extracted_entity_extraction_confidence_check
    CHECK (
        extraction_confidence IS NULL
        OR (extraction_confidence >= 0.0 AND extraction_confidence <= 1.0)
    );

-- Widen the finite entity-type vocabulary to the PR 12 semantic set without
-- touching any existing row or the historical 0005 definition.
ALTER TABLE extracted_entity
    DROP CONSTRAINT IF EXISTS extracted_entity_entity_type_check;
ALTER TABLE extracted_entity
    ADD CONSTRAINT extracted_entity_entity_type_check
    CHECK (entity_type IN (
        'IP_ADDRESS', 'DOMAIN', 'URL', 'EMAIL', 'HASH',
        'PERSON', 'ORGANIZATION', 'ONLINE_IDENTITY', 'THREAT_ACTOR',
        'MALWARE', 'LOCATION', 'INDUSTRY', 'ORGANIZATION_TYPE',
        'CREDENTIAL_TYPE', 'ACCESS_TYPE', 'CRYPTO_ADDRESS'
    ));

-- Create one extracted-entity occurrence carrying semantic confidence.
-- Returns 'added' / 'duplicate' / 'unknown_result' / 'content_mismatch'.
CREATE FUNCTION extracted_entity_create_v2(
    p_id                    uuid,
    p_extraction_result_id  uuid,
    p_content_id            uuid,
    p_entity_type           text,
    p_raw_value             text,
    p_normalized_value      text,
    p_span_start            integer,
    p_span_end              integer,
    p_extractor_name        text,
    p_extractor_version     text,
    p_subtype               text,
    p_extraction_confidence double precision
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
         extractor_version, subtype, extraction_confidence)
    VALUES
        (p_id, p_extraction_result_id, p_content_id, p_entity_type,
         p_raw_value, p_normalized_value, p_span_start, p_span_end,
         p_extractor_name, p_extractor_version, p_subtype,
         p_extraction_confidence);
    RETURN 'added';
END;
$$;

-- List a result's occurrences (with confidence) in deterministic order.
CREATE FUNCTION extracted_entity_list_for_result_v2(p_extraction_result_id uuid)
RETURNS TABLE (
    id                    uuid,
    extraction_result_id  uuid,
    content_id            uuid,
    entity_type           text,
    raw_value             text,
    normalized_value      text,
    span_start            integer,
    span_end              integer,
    extractor_name        text,
    extractor_version     text,
    subtype               text,
    extraction_confidence double precision
)
LANGUAGE sql
AS $$
    SELECT e.id, e.extraction_result_id, e.content_id, e.entity_type,
           e.raw_value, e.normalized_value, e.span_start, e.span_end,
           e.extractor_name, e.extractor_version, e.subtype,
           e.extraction_confidence
      FROM extracted_entity e
     WHERE e.extraction_result_id = p_extraction_result_id
     ORDER BY e.span_start, e.span_end, e.entity_type, e.normalized_value,
              e.extractor_name, e.extractor_version, e.id;
$$;

-- List a content's occurrences (with confidence) in deterministic order.
CREATE FUNCTION extracted_entity_list_for_content_v2(p_content_id uuid)
RETURNS TABLE (
    id                    uuid,
    extraction_result_id  uuid,
    content_id            uuid,
    entity_type           text,
    raw_value             text,
    normalized_value      text,
    span_start            integer,
    span_end              integer,
    extractor_name        text,
    extractor_version     text,
    subtype               text,
    extraction_confidence double precision
)
LANGUAGE sql
AS $$
    SELECT e.id, e.extraction_result_id, e.content_id, e.entity_type,
           e.raw_value, e.normalized_value, e.span_start, e.span_end,
           e.extractor_name, e.extractor_version, e.subtype,
           e.extraction_confidence
      FROM extracted_entity e
     WHERE e.content_id = p_content_id
     ORDER BY e.span_start, e.span_end, e.entity_type, e.normalized_value,
              e.extractor_name, e.extractor_version, e.id;
$$;

-- Load one occurrence by identity (with confidence).
CREATE FUNCTION extracted_entity_get_v1(p_id uuid)
RETURNS TABLE (
    id                    uuid,
    extraction_result_id  uuid,
    content_id            uuid,
    entity_type           text,
    raw_value             text,
    normalized_value      text,
    span_start            integer,
    span_end              integer,
    extractor_name        text,
    extractor_version     text,
    subtype               text,
    extraction_confidence double precision
)
LANGUAGE sql
AS $$
    SELECT e.id, e.extraction_result_id, e.content_id, e.entity_type,
           e.raw_value, e.normalized_value, e.span_start, e.span_end,
           e.extractor_name, e.extractor_version, e.subtype,
           e.extraction_confidence
      FROM extracted_entity e
     WHERE e.id = p_id;
$$;

-- ========================================================================
-- Geographic resolution (immutable/versioned interpretation).
-- ========================================================================

CREATE TABLE geographic_resolution (
    id                  uuid PRIMARY KEY,
    extracted_entity_id uuid NOT NULL REFERENCES extracted_entity(id),
    status              text NOT NULL CHECK (status IN (
        'RESOLVED', 'AMBIGUOUS', 'UNRESOLVED'
    )),
    resolver_name       text NOT NULL CHECK (length(btrim(resolver_name)) > 0),
    resolver_version    text NOT NULL CHECK (length(btrim(resolver_version)) > 0),
    resolved_at         timestamptz NOT NULL,
    canonical_name      text CHECK (
        canonical_name IS NULL OR length(btrim(canonical_name)) > 0),
    country_code        text CHECK (
        country_code IS NULL OR country_code ~ '^[A-Z]{2}$'),
    administrative_area text CHECK (
        administrative_area IS NULL OR length(btrim(administrative_area)) > 0),
    locality            text CHECK (
        locality IS NULL OR length(btrim(locality)) > 0),
    latitude            double precision CHECK (
        latitude IS NULL OR (latitude >= -90.0 AND latitude <= 90.0)),
    longitude           double precision CHECK (
        longitude IS NULL OR (longitude >= -180.0 AND longitude <= 180.0)),
    confidence          double precision CHECK (
        confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)),
    resolver_reference  text CHECK (
        resolver_reference IS NULL OR length(btrim(resolver_reference)) > 0),
    CHECK ((latitude IS NULL) = (longitude IS NULL)),
    -- RESOLVED requires a canonical name + confidence.
    CHECK (
        status <> 'RESOLVED'
        OR (canonical_name IS NOT NULL AND confidence IS NOT NULL)
    ),
    -- AMBIGUOUS/UNRESOLVED must not pretend a canonical place.
    CHECK (
        status = 'RESOLVED'
        OR (canonical_name IS NULL AND country_code IS NULL
            AND administrative_area IS NULL AND locality IS NULL
            AND latitude IS NULL AND longitude IS NULL)
    ),
    -- One immutable resolution per occurrence + resolver version.
    UNIQUE (extracted_entity_id, resolver_name, resolver_version)
);

CREATE INDEX geographic_resolution_entity_idx
    ON geographic_resolution (extracted_entity_id, resolver_name,
                              resolver_version, id);

CREATE INDEX geographic_resolution_status_idx
    ON geographic_resolution (status, resolved_at, id);

-- Create one immutable resolution. Returns 'added' / 'duplicate' /
-- 'unknown_entity'.
CREATE FUNCTION geographic_resolution_create_v1(
    p_id                  uuid,
    p_extracted_entity_id uuid,
    p_status              text,
    p_resolver_name       text,
    p_resolver_version    text,
    p_resolved_at         timestamptz,
    p_canonical_name      text,
    p_country_code        text,
    p_administrative_area text,
    p_locality            text,
    p_latitude            double precision,
    p_longitude           double precision,
    p_confidence          double precision,
    p_resolver_reference  text
) RETURNS text
LANGUAGE plpgsql
AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM extracted_entity WHERE id = p_extracted_entity_id
    ) THEN
        RETURN 'unknown_entity';
    END IF;
    IF EXISTS (
        SELECT 1 FROM geographic_resolution r
         WHERE r.id = p_id
            OR (r.extracted_entity_id = p_extracted_entity_id
                AND r.resolver_name = p_resolver_name
                AND r.resolver_version = p_resolver_version)
    ) THEN
        RETURN 'duplicate';
    END IF;
    INSERT INTO geographic_resolution
        (id, extracted_entity_id, status, resolver_name, resolver_version,
         resolved_at, canonical_name, country_code, administrative_area,
         locality, latitude, longitude, confidence, resolver_reference)
    VALUES
        (p_id, p_extracted_entity_id, p_status, p_resolver_name,
         p_resolver_version, p_resolved_at, p_canonical_name, p_country_code,
         p_administrative_area, p_locality, p_latitude, p_longitude,
         p_confidence, p_resolver_reference);
    RETURN 'added';
END;
$$;

CREATE FUNCTION geographic_resolution_get_v1(p_id uuid)
RETURNS TABLE (
    id                  uuid,
    extracted_entity_id uuid,
    status              text,
    resolver_name       text,
    resolver_version    text,
    resolved_at         timestamptz,
    canonical_name      text,
    country_code        text,
    administrative_area text,
    locality            text,
    latitude            double precision,
    longitude           double precision,
    confidence          double precision,
    resolver_reference  text
)
LANGUAGE sql
AS $$
    SELECT r.id, r.extracted_entity_id, r.status, r.resolver_name,
           r.resolver_version, r.resolved_at, r.canonical_name,
           r.country_code, r.administrative_area, r.locality, r.latitude,
           r.longitude, r.confidence, r.resolver_reference
      FROM geographic_resolution r
     WHERE r.id = p_id;
$$;

CREATE FUNCTION geographic_resolution_get_for_entity_v1(
    p_extracted_entity_id uuid,
    p_resolver_name       text,
    p_resolver_version    text
)
RETURNS TABLE (
    id                  uuid,
    extracted_entity_id uuid,
    status              text,
    resolver_name       text,
    resolver_version    text,
    resolved_at         timestamptz,
    canonical_name      text,
    country_code        text,
    administrative_area text,
    locality            text,
    latitude            double precision,
    longitude           double precision,
    confidence          double precision,
    resolver_reference  text
)
LANGUAGE sql
AS $$
    SELECT r.id, r.extracted_entity_id, r.status, r.resolver_name,
           r.resolver_version, r.resolved_at, r.canonical_name,
           r.country_code, r.administrative_area, r.locality, r.latitude,
           r.longitude, r.confidence, r.resolver_reference
      FROM geographic_resolution r
     WHERE r.extracted_entity_id = p_extracted_entity_id
       AND r.resolver_name = p_resolver_name
       AND r.resolver_version = p_resolver_version;
$$;

CREATE FUNCTION geographic_resolution_list_for_content_v1(p_content_id uuid)
RETURNS TABLE (
    id                  uuid,
    extracted_entity_id uuid,
    status              text,
    resolver_name       text,
    resolver_version    text,
    resolved_at         timestamptz,
    canonical_name      text,
    country_code        text,
    administrative_area text,
    locality            text,
    latitude            double precision,
    longitude           double precision,
    confidence          double precision,
    resolver_reference  text
)
LANGUAGE sql
AS $$
    SELECT r.id, r.extracted_entity_id, r.status, r.resolver_name,
           r.resolver_version, r.resolved_at, r.canonical_name,
           r.country_code, r.administrative_area, r.locality, r.latitude,
           r.longitude, r.confidence, r.resolver_reference
      FROM geographic_resolution r
      JOIN extracted_entity e ON e.id = r.extracted_entity_id
     WHERE e.content_id = p_content_id
     ORDER BY r.resolved_at, r.id;
$$;

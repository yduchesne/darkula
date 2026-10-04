-- SPDX-License-Identifier: AGPL-3.0-only
-- Darkula PR 8 migration: normalized-content and content-artifact metadata.
--
-- Immutable 0003 artifact. Consistent with the stored-function-only rule,
-- PostgreSQL owns the schema/behavior and production Python invokes only
-- versioned stored functions (content_artifact_*_v1, normalized_content_*_v1).
--
-- ObjectStore owns bytes; this schema owns structured identity, metadata and
-- provenance. The physical ObjectKey is stored once per logical artifact
-- record; a normalized observation references the artifact record. Physical
-- deduplication (one ObjectKey) never merges provenance: many normalized
-- observations and, where appropriate, many artifact records may reference
-- the same stored bytes while remaining distinct historical records.
--
-- Freezed semantics:
--   * content_artifact: one record per (hash_algorithm, hash_digest, kind,
--     completeness) -- the logical artifact reference for a representation.
--     Multiple observations may reference one artifact; retries of the same
--     representation reuse it.
--   * normalized_content is append-only/immutable history; a new observation
--     for the same URI at a new time is a new immutable row.
--   * a unique provenance key (crawl_request_id, observation_index,
--     source_uri) makes the durable idempotency of a retried observation
--     explicit and prevents uncontrolled duplicate logical records.
--   * full normalized text / artifact bytes live in ObjectStore; PostgreSQL
--     stores only a small bounded text preview.
--   * no object key, hash, or content ever leaks into log-relevant columns.

CREATE TABLE content_artifact (
    id                     uuid PRIMARY KEY,
    object_key             text NOT NULL CHECK (length(btrim(object_key)) > 0),
    content_type           text CHECK (content_type IS NULL OR length(btrim(content_type)) > 0),
    size_bytes             bigint NOT NULL CHECK (size_bytes >= 0),
    content_hash_algorithm text,
    content_hash_digest    text,
    artifact_kind          text NOT NULL CHECK (artifact_kind IN (
        'NORMALIZED_TEXT',
        'SOURCE_BODY',
        'DOWNLOAD_SAMPLE'
    )),
    completeness           text NOT NULL CHECK (completeness IN (
        'COMPLETE',
        'SAMPLE'
    )),
    created_at             timestamptz NOT NULL,
    CHECK ((content_hash_algorithm IS NULL) = (content_hash_digest IS NULL))
);

-- One logical artifact per representation identity (algorithm+digest+kind+
-- completeness). Two different artifact kinds may intentionally use the same
-- physical bytes but remain separate logical artifacts.
CREATE UNIQUE INDEX content_artifact_representation_uniq
    ON content_artifact (content_hash_algorithm, content_hash_digest,
                         artifact_kind, completeness)
    WHERE content_hash_algorithm IS NOT NULL;

CREATE TABLE normalized_content (
    content_id             uuid PRIMARY KEY,
    artifact_id            uuid REFERENCES content_artifact(id),
    source_uri             text NOT NULL CHECK (length(btrim(source_uri)) > 0),
    title                  text CHECK (title IS NULL OR length(btrim(title)) > 0),
    text_preview           text CHECK (text_preview IS NULL OR length(text_preview) <= 2000),
    content_type           text CHECK (content_type IS NULL OR length(btrim(content_type)) > 0),
    observed_at            timestamptz NOT NULL,
    crawl_request_id       text NOT NULL CHECK (length(btrim(crawl_request_id)) > 0),
    observation_index      integer NOT NULL CHECK (observation_index >= 1),
    normalization_version  text NOT NULL CHECK (length(btrim(normalization_version)) > 0),
    content_hash_algorithm text,
    content_hash_digest    text,
    completeness           text NOT NULL CHECK (completeness IN (
        'COMPLETE',
        'SAMPLE'
    )),
    structural_metadata    jsonb,
    created_at             timestamptz NOT NULL,
    CHECK ((content_hash_algorithm IS NULL) = (content_hash_digest IS NULL))
);

-- Durable idempotency for a retried observation: the same provenance
-- identity can never create a duplicate logical record.
CREATE UNIQUE INDEX normalized_content_provenance_uniq
    ON normalized_content (crawl_request_id, observation_index, source_uri);

-- Deterministic per-crawl observation display order.
CREATE INDEX normalized_content_crawl_order_idx
    ON normalized_content (crawl_request_id, observation_index);

-- ========================================================================
-- Versioned stored functions (production persistence API).
-- ========================================================================

-- Find an existing logical artifact by its representation identity.
CREATE FUNCTION content_artifact_find_v1(
    p_hash_algorithm text,
    p_hash_digest    text,
    p_artifact_kind  text,
    p_completeness   text
) RETURNS TABLE (
    id                     uuid,
    object_key             text,
    content_type           text,
    size_bytes             bigint,
    content_hash_algorithm text,
    content_hash_digest    text,
    artifact_kind          text,
    completeness           text,
    created_at             timestamptz
)
LANGUAGE sql
AS $$
    SELECT a.id, a.object_key, a.content_type, a.size_bytes,
           a.content_hash_algorithm, a.content_hash_digest,
           a.artifact_kind, a.completeness, a.created_at
      FROM content_artifact a
     WHERE a.content_hash_algorithm = p_hash_algorithm
       AND a.content_hash_digest = p_hash_digest
       AND a.artifact_kind = p_artifact_kind
       AND a.completeness = p_completeness;
$$;

CREATE FUNCTION content_artifact_get_v1(p_id uuid)
RETURNS TABLE (
    id                     uuid,
    object_key             text,
    content_type           text,
    size_bytes             bigint,
    content_hash_algorithm text,
    content_hash_digest    text,
    artifact_kind          text,
    completeness           text,
    created_at             timestamptz
)
LANGUAGE sql
AS $$
    SELECT a.id, a.object_key, a.content_type, a.size_bytes,
           a.content_hash_algorithm, a.content_hash_digest,
           a.artifact_kind, a.completeness, a.created_at
      FROM content_artifact a
     WHERE a.id = p_id;
$$;

-- Create one logical artifact record, or signal an existing one.
CREATE FUNCTION content_artifact_create_v1(
    p_id                uuid,
    p_object_key        text,
    p_content_type      text,
    p_size_bytes        bigint,
    p_hash_algorithm    text,
    p_hash_digest       text,
    p_artifact_kind     text,
    p_completeness      text,
    p_created_at        timestamptz
) RETURNS text
LANGUAGE plpgsql
AS $$
BEGIN
    INSERT INTO content_artifact
        (id, object_key, content_type, size_bytes, content_hash_algorithm,
         content_hash_digest, artifact_kind, completeness, created_at)
    VALUES
        (p_id, p_object_key, p_content_type, p_size_bytes, p_hash_algorithm,
         p_hash_digest, p_artifact_kind, p_completeness, p_created_at)
    ON CONFLICT (id) DO NOTHING;
    IF FOUND THEN
        RETURN 'added';
    END IF;
    RETURN 'duplicate';
END;
$$;

-- Create one normalized observation; provenance idempotent.
CREATE FUNCTION normalized_content_create_v1(
    p_content_id           uuid,
    p_artifact_id          uuid,
    p_source_uri           text,
    p_title                text,
    p_text_preview         text,
    p_content_type         text,
    p_observed_at          timestamptz,
    p_crawl_request_id     text,
    p_observation_index    integer,
    p_normalization_version text,
    p_hash_algorithm       text,
    p_hash_digest          text,
    p_completeness         text,
    p_structural_metadata  jsonb,
    p_created_at           timestamptz
) RETURNS text
LANGUAGE plpgsql
AS $$
BEGIN
    -- Duplicate provenance identity (retry) is reported before any insert so
    -- the transaction stays resolvable and the idempotency rule is explicit.
    IF EXISTS (
        SELECT 1 FROM normalized_content
         WHERE crawl_request_id = p_crawl_request_id
           AND observation_index = p_observation_index
           AND source_uri = p_source_uri
    ) THEN
        RETURN 'duplicate_provenance';
    END IF;
    IF p_artifact_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM content_artifact WHERE id = p_artifact_id
    ) THEN
        RETURN 'unknown_artifact';
    END IF;
    INSERT INTO normalized_content
        (content_id, artifact_id, source_uri, title, text_preview,
         content_type, observed_at, crawl_request_id, observation_index,
         normalization_version, content_hash_algorithm, content_hash_digest,
         completeness, structural_metadata, created_at)
    VALUES
        (p_content_id, p_artifact_id, p_source_uri, p_title, p_text_preview,
         p_content_type, p_observed_at, p_crawl_request_id, p_observation_index,
         p_normalization_version, p_hash_algorithm, p_hash_digest,
         p_completeness, p_structural_metadata, p_created_at);
    RETURN 'added';
END;
$$;

CREATE FUNCTION normalized_content_get_v1(p_content_id uuid)
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
           n.content_type, n.observed_at, n.crawl_request_id, n.observation_index,
           n.normalization_version, n.content_hash_algorithm,
           n.content_hash_digest, n.completeness, n.structural_metadata,
           n.created_at
      FROM normalized_content n
     WHERE n.content_id = p_content_id;
$$;

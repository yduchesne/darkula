-- SPDX-License-Identifier: AGPL-3.0-only
-- Darkula PR 5 migration: reliable messaging persistence.
--
-- Adds the transactional outbox and the durable processed-message
-- idempotency table behind the existing stored-function-only invariant.
-- 0001_initial.sql is immutable; all new production database behavior
-- lives here as new versioned functions (outbox_*_v1, processed_*_v*).
--
-- Semantics:
--   * message_outbox makes state+event atomic: one application
--     transaction persists the business state and appends the outbox row
--     together; a broker publish happens only AFTER that commit;
--   * published_at is delivery state, never broker identity: Kafka
--     partition/offset are never persisted here;
--   * claim/lease fields give bounded concurrent publishers safe,
--     retryable ownership without holding a database transaction open
--     during broker I/O:
--         short UoW: claim -> commit
--         broker publish outside any transaction
--         short UoW: mark published -> commit
--   * a crash after broker publish but before mark leaves the row
--     retryable; consumers deduplicate by stable message_id;
--   * processed_message deduplicates durable processing by
--     (stream_name, consumer_id, message_id) -- never by broker offset.

CREATE TABLE message_outbox (
    outbox_id        uuid PRIMARY KEY,
    message_id       uuid NOT NULL UNIQUE,
    stream_name      text NOT NULL CHECK (length(btrim(stream_name)) > 0),
    message_type     text NOT NULL CHECK (length(btrim(message_type)) > 0),
    schema_version   integer NOT NULL CHECK (schema_version >= 1),
    occurred_at      timestamptz NOT NULL,
    payload          jsonb NOT NULL,
    correlation_id   uuid,
    causation_id     uuid,
    routing_key      text CHECK (routing_key IS NULL OR length(btrim(routing_key)) > 0),
    created_at       timestamptz NOT NULL DEFAULT now(),
    published_at     timestamptz,
    attempt_count    integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    last_attempt_at  timestamptz,
    claim_id         uuid,
    lease_until      timestamptz CHECK (lease_until IS NULL OR published_at IS NULL)
);

-- Deterministic pending-claim order: stream, creation, outbox identity.
CREATE INDEX message_outbox_pending_idx
    ON message_outbox (stream_name, created_at, outbox_id)
    WHERE published_at IS NULL;

CREATE TABLE processed_message (
    stream_name  text NOT NULL,
    consumer_id  text NOT NULL CHECK (length(btrim(consumer_id)) > 0),
    message_id   uuid NOT NULL,
    processed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (stream_name, consumer_id, message_id)
);

-- ========================================================================
-- Versioned stored functions (production persistence API).
-- ========================================================================

-- Append one outbox record. Duplicate message_id returns 'duplicate'
-- (frozen idempotent behavior: the original record is never overwritten).
CREATE FUNCTION outbox_append_v1(
    p_outbox_id      uuid,
    p_message_id     uuid,
    p_stream_name    text,
    p_message_type   text,
    p_schema_version integer,
    p_occurred_at    timestamptz,
    p_payload        jsonb,
    p_correlation_id uuid,
    p_causation_id   uuid,
    p_routing_key    text
) RETURNS text
LANGUAGE plpgsql
AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM message_outbox WHERE message_id = p_message_id) THEN
        RETURN 'duplicate';
    END IF;
    INSERT INTO message_outbox
        (outbox_id, message_id, stream_name, message_type, schema_version,
         occurred_at, payload, correlation_id, causation_id, routing_key)
    VALUES
        (p_outbox_id, p_message_id, p_stream_name, p_message_type,
         p_schema_version, p_occurred_at, p_payload, p_correlation_id,
         p_causation_id, p_routing_key);
    RETURN 'added';
END;
$$;

-- Claim up to p_limit pending rows for one stream whose lease is free or
-- expired. Row locks (FOR UPDATE SKIP LOCKED) prevent two concurrent
-- claimers from owning the same rows; the deterministic order
-- (created_at, outbox_id) is preserved. attempt_count / last_attempt_at
-- record every retry attempt explicitly (no hidden retries).
CREATE FUNCTION outbox_claim_v1(
    p_stream_name    text,
    p_limit          integer,
    p_claim_id       uuid,
    p_lease_seconds  double precision
) RETURNS TABLE (
    outbox_id      uuid,
    message_id     uuid,
    stream_name    text,
    message_type   text,
    schema_version integer,
    occurred_at    timestamptz,
    payload        jsonb,
    correlation_id uuid,
    causation_id   uuid,
    routing_key    text
)
LANGUAGE plpgsql
AS $$
BEGIN
    RETURN QUERY
    UPDATE message_outbox o
       SET claim_id = p_claim_id,
           lease_until = now() + make_interval(secs => p_lease_seconds),
           attempt_count = o.attempt_count + 1,
           last_attempt_at = now()
     WHERE o.outbox_id IN (
        SELECT x.outbox_id
          FROM message_outbox x
         WHERE x.stream_name = p_stream_name
           AND x.published_at IS NULL
           AND (x.lease_until IS NULL OR x.lease_until < now())
         ORDER BY x.created_at, x.outbox_id
         LIMIT p_limit
           FOR UPDATE SKIP LOCKED
     )
     RETURNING o.outbox_id, o.message_id, o.stream_name, o.message_type,
               o.schema_version, o.occurred_at, o.payload,
               o.correlation_id, o.causation_id, o.routing_key;
END;
$$;

-- Mark claimed rows published. Only rows claimed by p_claim_id that are
-- still unpublished are marked; a partial mark (some rows failed) keeps
-- the remaining rows retryable. Returns the number of rows marked.
CREATE FUNCTION outbox_mark_published_v1(
    p_claim_id    uuid,
    p_outbox_ids  uuid[]
) RETURNS bigint
LANGUAGE plpgsql
AS $$
DECLARE
    v_marked bigint;
BEGIN
    UPDATE message_outbox
       SET published_at = now(),
           claim_id = NULL,
           lease_until = NULL
     WHERE claim_id = p_claim_id
       AND published_at IS NULL
       AND outbox_id = ANY(p_outbox_ids);
    GET DIAGNOSTICS v_marked = ROW_COUNT;
    RETURN v_marked;
END;
$$;

-- Atomically record one processed message (durable consumer idempotency).
-- Returns 'recorded' on first delivery, 'duplicate' when this consumer has
-- already processed this message id on this stream. Never an offset.
CREATE FUNCTION processed_message_record_v1(
    p_stream_name  text,
    p_consumer_id  text,
    p_message_id   uuid
) RETURNS text
LANGUAGE plpgsql
AS $$
BEGIN
    INSERT INTO processed_message (stream_name, consumer_id, message_id)
    VALUES (p_stream_name, p_consumer_id, p_message_id)
    ON CONFLICT (stream_name, consumer_id, message_id) DO NOTHING;
    IF FOUND THEN
        RETURN 'recorded';
    END IF;
    RETURN 'duplicate';
END;
$$;

-- SPDX-License-Identifier: AGPL-3.0-only
-- Darkula PR 18 migration: durable registry-poll checkpoints.
--
-- Additive and immutable. 0001-0009 remain byte-for-byte unchanged.
--
-- Semantics:
--   * a checkpoint records polling POSITION only: the last exact upstream
--     registry entry name whose complete set of accepted observations was
--     durably ingested, plus an optional cooldown and the optimistic
--     generation used for compare-and-swap;
--   * `generation` 0 is never stored: the first durable write creates
--     generation 1. A NULL cursor is the explicit end-of-pass state, not
--     "not yet persisted";
--   * `cursor` is an exact bounded upstream entry name (never a URL, broker
--     offset, source/candidate identity, or credential). It is control-free
--     and whitespace-trimmed by check constraint;
--   * advancing the cursor and deferring a provider failure are separate
--     compare-and-swap operations. Defer preserves the cursor and
--     last_completed_at, sets/raises the cooldown, and still increments the
--     generation;
--   * no stored function performs network I/O, creates a candidate/source/
--     endpoint/policy/run, appends an event or outbox record, or grants any
--     authorization.

-- ========================================================================
-- Table and constraints
-- ========================================================================

CREATE TABLE registry_poll_checkpoint (
    provider_id       text PRIMARY KEY,
    cursor            text NULL,
    generation        bigint NOT NULL DEFAULT 1 CHECK (generation >= 1),
    cooldown_until    timestamptz NULL,
    last_completed_at timestamptz NULL,
    CONSTRAINT registry_poll_checkpoint_provider_id_check CHECK (
        char_length(provider_id) BETWEEN 1 AND 100
        AND provider_id ~ '^[a-z0-9][a-z0-9._-]*$'
    ),
    CONSTRAINT registry_poll_checkpoint_cursor_check CHECK (
        cursor IS NULL OR (
            char_length(cursor) BETWEEN 1 AND 200
            AND cursor = btrim(cursor)
            AND cursor !~ '[[:cntrl:]]'
        )
    )
);

-- ========================================================================
-- Versioned stored functions (production persistence API).
-- ========================================================================

-- Read one provider checkpoint (0 or 1 row).
CREATE FUNCTION registry_poll_checkpoint_get_v1(p_provider_id text)
RETURNS TABLE (
    provider_id       text,
    cursor            text,
    generation        bigint,
    cooldown_until    timestamptz,
    last_completed_at timestamptz
)
LANGUAGE sql
AS $$
    SELECT c.provider_id, c.cursor, c.generation, c.cooldown_until,
           c.last_completed_at
      FROM registry_poll_checkpoint c
     WHERE c.provider_id = p_provider_id;
$$;

-- Compare-and-swap advance. Returns 'advanced' or 'conflict'.
--
-- expected_generation = 0 creates generation 1 if and only if no row exists
-- (insert-or-conflict). A positive expected generation updates only the row
-- whose generation matches; a NULL p_cursor is the explicit end-of-pass
-- reset. A successful advance clears any cooldown.
CREATE FUNCTION registry_poll_checkpoint_advance_v1(
    p_provider_id         text,
    p_expected_generation bigint,
    p_cursor              text,
    p_completed_at        timestamptz
) RETURNS text
LANGUAGE plpgsql
AS $$
DECLARE
    affected integer;
BEGIN
    IF p_expected_generation < 0 THEN
        RAISE EXCEPTION 'invalid registry checkpoint generation'
            USING ERRCODE = '23514';
    END IF;
    IF p_expected_generation = 0 THEN
        INSERT INTO registry_poll_checkpoint (
            provider_id, cursor, generation, cooldown_until, last_completed_at
        ) VALUES (
            p_provider_id, p_cursor, 1, NULL, p_completed_at
        )
        ON CONFLICT (provider_id) DO NOTHING;
        GET DIAGNOSTICS affected = ROW_COUNT;
        IF affected = 1 THEN
            RETURN 'advanced';
        END IF;
        RETURN 'conflict';
    END IF;
    UPDATE registry_poll_checkpoint
       SET generation = generation + 1,
           cursor = p_cursor,
           cooldown_until = NULL,
           last_completed_at = p_completed_at
     WHERE provider_id = p_provider_id
       AND generation = p_expected_generation;
    GET DIAGNOSTICS affected = ROW_COUNT;
    IF affected = 1 THEN
        RETURN 'advanced';
    END IF;
    RETURN 'conflict';
END;
$$;

-- Compare-and-swap cooldown deferral. Returns 'deferred' or 'conflict'.
--
-- The cursor and last_completed_at are preserved; cooldown_until becomes the
-- later of the existing value and p_not_before (a NULL existing cooldown
-- becomes p_not_before), and the generation still increments.
CREATE FUNCTION registry_poll_checkpoint_defer_v1(
    p_provider_id         text,
    p_expected_generation bigint,
    p_not_before          timestamptz
) RETURNS text
LANGUAGE plpgsql
AS $$
DECLARE
    affected integer;
BEGIN
    IF p_expected_generation < 0 THEN
        RAISE EXCEPTION 'invalid registry checkpoint generation'
            USING ERRCODE = '23514';
    END IF;
    IF p_expected_generation = 0 THEN
        INSERT INTO registry_poll_checkpoint (
            provider_id, cursor, generation, cooldown_until, last_completed_at
        ) VALUES (
            p_provider_id, NULL, 1, p_not_before, NULL
        )
        ON CONFLICT (provider_id) DO NOTHING;
        GET DIAGNOSTICS affected = ROW_COUNT;
        IF affected = 1 THEN
            RETURN 'deferred';
        END IF;
        RETURN 'conflict';
    END IF;
    UPDATE registry_poll_checkpoint
       SET generation = generation + 1,
           cooldown_until = GREATEST(cooldown_until, p_not_before)
     WHERE provider_id = p_provider_id
       AND generation = p_expected_generation;
    GET DIAGNOSTICS affected = ROW_COUNT;
    IF affected = 1 THEN
        RETURN 'deferred';
    END IF;
    RETURN 'conflict';
END;
$$;

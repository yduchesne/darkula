-- SPDX-License-Identifier: AGPL-3.0-only
-- Darkula PR 19-F2 migration: bounded historical registry v1 decision lookup.
--
-- Additive and read-only. 0001-0012 remain byte-for-byte unchanged. This
-- migration adds no table, index, trigger, privilege, or writer. It exists so
-- that replay of a historical REGISTRY_POLICY decision resolves by candidate
-- and the frozen (method, policy_id, policy_version) tuple, independent of
-- the current observation list, its ordering, its size, or its newest member.
--
-- At most two rows are returned: zero means "no historical decision", one
-- means "possible immutable replay", and two means "ambiguous invariant".
-- The existing qualification_decision_candidate_order_idx supports the
-- candidate/time ordering; no new index is introduced.

CREATE FUNCTION qualification_registry_v1_decisions_for_candidate_v1(
    p_candidate_id uuid
)
RETURNS TABLE (
    decision_id               uuid,
    candidate_id              uuid,
    method                    text,
    outcome                   text,
    evaluated_at              timestamptz,
    policy_id                 text,
    policy_version            text,
    provider_id               text,
    provider_record_id        text,
    recon_assessment_id       uuid,
    event_id                  uuid,
    expected_status           text,
    new_status                text,
    decision_key              text,
    evidence_observation_ids  uuid[]
)
LANGUAGE sql
STABLE
AS $$
    SELECT d.decision_id, d.candidate_id, d.method, d.outcome,
           d.evaluated_at, d.policy_id, d.policy_version, d.provider_id,
           d.provider_record_id, d.recon_assessment_id, d.event_id,
           d.expected_status, d.new_status, d.decision_key,
           d.evidence_observation_ids
      FROM qualification_decision d
     WHERE d.candidate_id = p_candidate_id
       AND d.method = 'REGISTRY_POLICY'
       AND d.policy_id = 'candidate-qualification'
       AND d.policy_version = 'v1'
     ORDER BY d.evaluated_at, d.decision_id
     LIMIT 2;
$$;

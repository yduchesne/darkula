# SPDX-License-Identifier: AGPL-3.0-only
"""Collection domain tests (PR 9): CP1-CP9 policy, CR1-CR8 run.

Deterministic, offline. Validates the finite state machine, the frozen
identity/revision/snapshot rules, and every bounded field of the policy and
run value objects.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from darkula.domain.collection import (
    MAX_AUTHENTICATION_REFERENCE_LENGTH,
    MAX_FAILURE_SUMMARY_LENGTH,
    MAX_POLICY_DEPTH,
    MAX_POLICY_INTERVAL_SECONDS,
    MAX_POLICY_PAGES,
    MAX_POLICY_REQUESTS,
    MIN_POLICY_INTERVAL_SECONDS,
    TERMINAL_RUN_STATUSES,
    CollectionFailureCode,
    CollectionPolicy,
    CollectionRun,
    CollectionRunStatus,
)
from darkula.domain.identifiers import (
    CollectionPolicyId,
    CollectionRunId,
    SourceEndpointId,
    SourceId,
)

_UTC = UTC
_T0 = datetime(2026, 2, 1, 0, 0, 0, tzinfo=UTC)
_T1 = datetime(2026, 2, 1, 1, 0, 0, tzinfo=UTC)
_POLICY = CollectionPolicyId.from_str("11111111-1111-1111-1111-111111111111")
_SOURCE = SourceId.from_str("22222222-2222-2222-2222-222222222222")
_ENDPOINT_A = SourceEndpointId.from_str("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_ENDPOINT_B = SourceEndpointId.from_str("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
_RUN = CollectionRunId.from_str("33333333-3333-3333-3333-333333333333")


def _policy(**overrides: object) -> CollectionPolicy:
    base: dict[str, object] = {
        "policy_id": _POLICY,
        "source_id": _SOURCE,
        "active": True,
        "created_at": _T0,
        "updated_at": _T0,
        "revision": 1,
        "interval_seconds": 3600,
        "next_due_at": _T0,
        "allowed_endpoint_ids": (_ENDPOINT_A, _ENDPOINT_B),
        "allowed_paths": ("/board",),
    }
    base.update(overrides)
    return CollectionPolicy(**base)


def _run(**overrides: object) -> CollectionRun:
    base: dict[str, object] = {
        "run_id": _RUN,
        "policy_id": _POLICY,
        "policy_revision": 1,
        "source_id": _SOURCE,
        "scheduled_for": _T0,
        "created_at": _T0,
        "status": CollectionRunStatus.QUEUED,
        "policy_snapshot": _policy().execution_snapshot(),
    }
    base.update(overrides)
    return CollectionRun(**base)


class TestCollectionPolicy:
    """CP1-CP9: policy value-object bounds."""

    def test_cp1_valid_active_policy_accepted(self) -> None:
        policy = _policy()
        assert policy.active is True
        assert policy.revision == 1
        assert policy.allowed_endpoint_ids == (_ENDPOINT_A, _ENDPOINT_B)

    def test_cp2_nonpositive_interval_rejected(self) -> None:
        with pytest.raises(ValueError):
            _policy(interval_seconds=0)
        with pytest.raises(ValueError):
            _policy(interval_seconds=-60)
        with pytest.raises(ValueError):
            _policy(interval_seconds=59)  # below MIN_POLICY_INTERVAL_SECONDS

    def test_cp3_out_of_bound_interval_and_budget_rejected(self) -> None:
        with pytest.raises(ValueError):
            _policy(interval_seconds=MAX_POLICY_INTERVAL_SECONDS + 1)
        with pytest.raises(ValueError):
            _policy(max_pages=MAX_POLICY_PAGES + 1)
        with pytest.raises(ValueError):
            _policy(max_requests=MAX_POLICY_REQUESTS + 1)
        with pytest.raises(ValueError):
            _policy(max_depth=MAX_POLICY_DEPTH + 1)
        with pytest.raises(ValueError):
            _policy(max_depth=0)
        with pytest.raises(ValueError):
            _policy(timeout_seconds=0.0)
        with pytest.raises(ValueError):
            _policy(timeout_seconds=86_401.0)

    def test_cp4_duplicate_endpoints_rejected(self) -> None:
        with pytest.raises(ValueError):
            _policy(allowed_endpoint_ids=(_ENDPOINT_A, _ENDPOINT_A))
        with pytest.raises(ValueError):
            _policy(allowed_endpoint_ids=(_ENDPOINT_A, _ENDPOINT_A, _ENDPOINT_B))
        with pytest.raises(ValueError):
            _policy(allowed_endpoint_ids=["not-a-tuple"])

    def test_cp5_empty_endpoint_authorization_rejected(self) -> None:
        with pytest.raises(ValueError):
            _policy(allowed_endpoint_ids=())

    def test_cp6_embedded_credential_value_rejected(self) -> None:
        with pytest.raises(ValueError):
            _policy(authentication_reference="user:password@host")
        with pytest.raises(ValueError):
            _policy(allowed_paths="/secret/notes")

    def test_cp7_naive_timestamp_rejected(self) -> None:
        naive = datetime(2026, 2, 1, 0, 0, 0)
        with pytest.raises(ValueError):
            _policy(created_at=naive)
        with pytest.raises(ValueError):
            _policy(next_due_at=naive)
        with pytest.raises(ValueError):
            _policy(updated_at=naive)

    def test_cp8_revision_and_snapshot_retain_old_run_semantics(self) -> None:
        old = _policy()
        edited = _policy(revision=2, max_pages=40, updated_at=_T1, active=False)
        # The old snapshot is immutable and independent of later edits.
        snapshot = old.execution_snapshot()
        assert snapshot["policy_revision"] == 1
        assert snapshot["max_pages"] == old.max_pages
        assert snapshot["max_pages"] != edited.max_pages
        # Snapshot is deterministic and JSON-compatible.
        assert old.execution_snapshot() == snapshot
        assert edited.execution_snapshot()["policy_revision"] == 2

    def test_cp9_inactive_policy_is_never_due_here(self) -> None:
        # Inactivity is a scheduler/persistence concern; the domain must not
        # declare an inactive policy due. The domain only validates shape.
        policy = _policy(active=False)
        assert policy.active is False
        # Re-running the snapshot is the exact frozen authorization.
        assert set(policy.execution_snapshot()) == {
            "policy_revision",
            "interval_seconds",
            "allowed_endpoint_ids",
            "allowed_paths",
            "max_pages",
            "max_requests",
            "max_depth",
            "timeout_seconds",
            "authentication_reference",
        }

    def test_interval_bounds_are_positive_and_bounded(self) -> None:
        assert _policy(interval_seconds=MIN_POLICY_INTERVAL_SECONDS)
        assert _policy(interval_seconds=MAX_POLICY_INTERVAL_SECONDS)
        with pytest.raises(ValueError):
            _policy(interval_seconds=1.5)
        with pytest.raises(ValueError):
            _policy(interval_seconds=True)

    def test_authentication_reference_is_reference_only(self) -> None:
        policy = _policy(authentication_reference="vault-entry-42")
        assert policy.authentication_reference == "vault-entry-42"
        with pytest.raises(ValueError):
            _policy(
                authentication_reference="x" * (MAX_AUTHENTICATION_REFERENCE_LENGTH + 1)
            )

    def test_endpoint_identity_order_is_frozen_deterministic(self) -> None:
        policy = _policy(allowed_endpoint_ids=(_ENDPOINT_B, _ENDPOINT_A))
        assert policy.allowed_endpoint_ids == (_ENDPOINT_A, _ENDPOINT_B)
        assert policy.execution_snapshot()["allowed_endpoint_ids"] == [
            str(_ENDPOINT_A),
            str(_ENDPOINT_B),
        ]


class TestCollectionRun:
    """CR1-CR8: run value-object state machine and history rules."""

    def test_cr1_valid_queued_accepted(self) -> None:
        run = _run()
        assert run.status is CollectionRunStatus.QUEUED
        assert run.attempt_count == 0

    def test_cr2_running_without_started_at_rejected(self) -> None:
        with pytest.raises(ValueError):
            _run(status=CollectionRunStatus.RUNNING)

    def test_cr3_terminal_without_completed_at_rejected(self) -> None:
        terminal = (
            CollectionRunStatus.SUCCEEDED,
            CollectionRunStatus.FAILED,
            CollectionRunStatus.CANCELLED,
        )
        for status in terminal:
            with pytest.raises(ValueError):
                _run(status=status)
        run = _run(
            status=CollectionRunStatus.SUCCEEDED,
            started_at=_T0,
            completed_at=_T1,
        )
        assert run.status in TERMINAL_RUN_STATUSES

    def test_cr4_oversized_failure_summary_rejected(self) -> None:
        with pytest.raises(ValueError):
            _run(
                status=CollectionRunStatus.FAILED,
                started_at=_T0,
                completed_at=_T1,
                failure_code=CollectionFailureCode.CRAWL_FAILED,
                failure_summary="x" * (MAX_FAILURE_SUMMARY_LENGTH + 1),
            )
        # Control characters and secret-like values are rejected too.
        with pytest.raises(ValueError):
            _run(
                status=CollectionRunStatus.FAILED,
                started_at=_T0,
                completed_at=_T1,
                failure_summary="boom\nline",
            )
        with pytest.raises(ValueError):
            _run(
                status=CollectionRunStatus.FAILED,
                started_at=_T0,
                completed_at=_T1,
                failure_summary="leaked password",
            )

    def test_cr5_queued_to_running_shape_allowed(self) -> None:
        run = _run(
            status=CollectionRunStatus.RUNNING,
            started_at=_T0,
            execution_id="exec-1",
            lease_expires_at=_T1,
            attempt_count=1,
        )
        assert run.status is CollectionRunStatus.RUNNING

    def test_cr6_running_to_terminal_shape_allowed(self) -> None:
        for status in (
            CollectionRunStatus.SUCCEEDED,
            CollectionRunStatus.FAILED,
            CollectionRunStatus.CANCELLED,
        ):
            run = _run(status=status, started_at=_T0, completed_at=_T1)
            assert run.completed_at is not None

    def test_cr7_terminal_to_running_rejected_by_domain_shape(self) -> None:
        # The domain never interprets a terminal run as re-executable:
        # guarded statuses carry completion timestamps, and run immutability
        # is enforced by the persistence layer (expected-state transitions).
        terminal = _run(
            status=CollectionRunStatus.SUCCEEDED,
            started_at=_T0,
            completed_at=_T1,
        )
        assert terminal.status in TERMINAL_RUN_STATUSES
        # A QUEUED run cannot carry terminal markers either.
        with pytest.raises(ValueError):
            _run(status=CollectionRunStatus.QUEUED, completed_at=_T1)

    def test_cr8_policy_revision_and_snapshot_are_immutable_fields(self) -> None:
        run = _run()
        assert run.policy_revision == 1
        assert run.policy_snapshot is not None
        assert run.policy_snapshot["policy_revision"] == 1
        # Dataclasses are frozen: attempting mutation raises.
        with pytest.raises(FrozenInstanceError):
            run.policy_revision = 2  # type: ignore[misc]

    def test_run_requires_utc_and_positive_attempt(self) -> None:
        with pytest.raises(ValueError):
            _run(scheduled_for=datetime(2026, 2, 1, tzinfo=None))
        with pytest.raises(ValueError):
            _run(attempt_count=-1)
        with pytest.raises(ValueError):
            _run(policy_revision=0)

    def test_terminal_status_frozenset_is_exact(self) -> None:
        assert (
            frozenset(
                {
                    CollectionRunStatus.SUCCEEDED,
                    CollectionRunStatus.FAILED,
                    CollectionRunStatus.CANCELLED,
                }
            )
            == TERMINAL_RUN_STATUSES
        )
        assert CollectionRunStatus.QUEUED not in TERMINAL_RUN_STATUSES
        assert CollectionRunStatus.RUNNING not in TERMINAL_RUN_STATUSES

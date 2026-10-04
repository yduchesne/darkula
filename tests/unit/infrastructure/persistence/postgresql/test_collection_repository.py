# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic invocation tests for the PR 9 collection repository.

Mocks only the psycopg cursor (``_RecordingCursor``) so parameter
marshalling, fixed stored-function invocation, and result mapping are
exercised offline exactly as in the existing source/outbox repository tests.
The SQL-boundary guard (``test_sql_boundary.py``) additionally proves the
real module contains only fixed parameterized stored-function calls.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, Self
from uuid import UUID

import pytest

from darkula.app.persistence import ConflictError, NotFoundError
from darkula.app.repositories import ClaimOutcome, ReclaimOutcome
from darkula.domain.collection import (
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
from darkula.infrastructure.persistence.postgresql.collection_repository import (
    PostgresCollectionRepository,
)
from darkula.infrastructure.persistence.postgresql.scope import ExecutionScope

_T0 = datetime(2026, 2, 1, 0, 0, 0, tzinfo=UTC)
_POLICY = CollectionPolicyId.from_str("11111111-1111-1111-1111-111111111111")
_SOURCE = SourceId.from_str("22222222-2222-2222-2222-222222222222")
_ENDPOINT = SourceEndpointId.from_str("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_RUN = CollectionRunId.from_str("33333333-3333-3333-3333-333333333333")


class _RecordingCursor:
    """Records executed SQL and serves scripted rows."""

    def __init__(self, rows: list[tuple[Any, ...]] | None = None) -> None:
        self.rows = rows or []
        self.calls: list[tuple[str, tuple[Any, ...] | None]] = []
        self.execute_failure: BaseException | None = None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        self.calls.append((sql, params))
        if self.execute_failure is not None:
            raise self.execute_failure

    async def fetchone(self) -> tuple[Any, ...] | None:
        if not self.rows:
            return None
        return self.rows.pop(0)

    async def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self.rows)


class _Scope(ExecutionScope):
    def __init__(self, cursor: _RecordingCursor) -> None:
        self._calls: list[tuple[Any, ...]] = []
        self.cursor = cursor

    def ensure_open(self) -> None:
        return None

    def connection(self) -> Any:
        class _Connection:
            def __init__(self, owner: _Scope) -> None:
                self.owner = owner

            def cursor(self) -> _RecordingCursor:
                return self.owner.cursor

        return _Connection(self)


def _policy() -> CollectionPolicy:
    return CollectionPolicy(
        policy_id=_POLICY,
        source_id=_SOURCE,
        active=True,
        created_at=_T0,
        updated_at=_T0,
        revision=1,
        interval_seconds=3600,
        next_due_at=_T0,
        allowed_endpoint_ids=(_ENDPOINT,),
        allowed_paths=("/board",),
    )


def _run(**overrides: Any) -> CollectionRun:
    values: dict[str, Any] = {
        "run_id": _RUN,
        "policy_id": _POLICY,
        "policy_revision": 1,
        "source_id": _SOURCE,
        "scheduled_for": _T0,
        "created_at": _T0,
        "status": CollectionRunStatus.QUEUED,
        "policy_snapshot": _policy().execution_snapshot(),
    }
    values.update(overrides)
    return CollectionRun(**values)


class TestPolicyInvocations:
    async def _repo(
        self, rows: list[tuple[Any, ...]] | None = None
    ) -> tuple[PostgresCollectionRepository, _RecordingCursor]:
        cursor = _RecordingCursor(rows)
        return PostgresCollectionRepository(_Scope(cursor)), cursor

    @pytest.mark.asyncio
    async def test_create_policy_invokes_versioned_functions(self) -> None:
        repo, cursor = await self._repo([("added",), ("added",)])
        await repo.create_policy(_policy())
        sqls = [call[0] for call in cursor.calls]
        assert sqls == [
            "SELECT * FROM collection_policy_create_v1("
            "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            "SELECT * FROM collection_policy_endpoint_add_v1(%s, %s)",
        ]
        params = cursor.calls[0][1]
        assert params is not None
        assert params[0] == str(_POLICY)
        assert params[1] == str(_SOURCE)
        assert params[5] == 1
        assert params[8] is not None  # allowed-paths jsonb

    @pytest.mark.asyncio
    async def test_create_policy_duplicate_maps_conflict(self) -> None:
        repo, _ = await self._repo([("duplicate",), ("unknown_policy",)])
        with pytest.raises(ConflictError):
            await repo.create_policy(_policy())

    @pytest.mark.asyncio
    async def test_get_policy_maps_rows(self) -> None:
        row = (
            str(_POLICY),
            str(_SOURCE),
            True,
            _T0,
            _T0,
            1,
            3600,
            _T0,
            ["/board"],
            20,
            60,
            4,
            60.0,
            None,
            [UUID(str(_ENDPOINT))],
        )
        repo, _ = await self._repo([row])
        policy = await repo.get_policy(_POLICY)
        assert policy is not None
        assert policy.policy_id == _POLICY
        assert policy.allowed_endpoint_ids == (_ENDPOINT,)
        assert policy.interval_seconds == 3600

    @pytest.mark.asyncio
    async def test_get_policy_absent_returns_none(self) -> None:
        repo, _ = await self._repo([])
        assert await repo.get_policy(_POLICY) is None

    @pytest.mark.asyncio
    async def test_update_policy_returns_new_revision(self) -> None:
        repo, cursor = await self._repo([("updated",)])
        assert await repo.update_policy(_policy()) == 2
        sql = cursor.calls[0][0]
        assert sql == (
            "SELECT * FROM collection_policy_update_v1("
            "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
        )

    @pytest.mark.asyncio
    async def test_update_policy_unknown_maps_not_found(self) -> None:
        repo, _ = await self._repo([("unknown_policy",)])
        with pytest.raises(NotFoundError):
            await repo.update_policy(_policy())

    @pytest.mark.asyncio
    async def test_cancellation_propagates(self) -> None:
        cursor = _RecordingCursor([("added",), ("added",)])
        cursor.execute_failure = asyncio.CancelledError()
        repo = PostgresCollectionRepository(_Scope(cursor))
        with pytest.raises(asyncio.CancelledError):
            await repo.create_policy(_policy())


class TestRunInvocations:
    async def _repo(
        self, rows: list[tuple[Any, ...]] | None = None
    ) -> tuple[PostgresCollectionRepository, _RecordingCursor]:
        cursor = _RecordingCursor(rows)
        return PostgresCollectionRepository(_Scope(cursor)), cursor

    @pytest.mark.asyncio
    async def test_claim_maps_outcome(self) -> None:
        repo, _ = await self._repo([("claimed",)])
        outcome = await repo.claim_run(
            run_id=_RUN,
            execution_id="11111111-1111-1111-1111-111111111111",
            started_at=_T0,
            lease_expires_at=_T0,
        )
        assert outcome is ClaimOutcome.CLAIMED

    @pytest.mark.asyncio
    async def test_reclaim_maps_lease_active(self) -> None:
        repo, _ = await self._repo([("lease_active",)])
        outcome = await repo.reclaim_run(
            run_id=_RUN,
            execution_id="exec",
            now=_T0,
            lease_expires_at=_T0,
            max_attempts=3,
        )
        assert outcome is ReclaimOutcome.LEASE_ACTIVE

    @pytest.mark.asyncio
    async def test_complete_maps_wrong_state_to_conflict(self) -> None:
        repo, _ = await self._repo([("wrong_state",)])
        with pytest.raises(ConflictError):
            await repo.complete_run(
                run_id=_RUN,
                new_status=CollectionRunStatus.SUCCEEDED,
                completed_at=_T0,
                crawl_requests_attempted=0,
                pages_observed=0,
                content_observations=0,
                content_created=0,
                content_deduplicated=0,
                failure_code=None,
                failure_summary=None,
            )

    @pytest.mark.asyncio
    async def test_complete_maps_unknown_run_to_not_found(self) -> None:
        repo, _ = await self._repo([("unknown_run",)])
        with pytest.raises(NotFoundError):
            await repo.complete_run(
                run_id=_RUN,
                new_status=CollectionRunStatus.SUCCEEDED,
                completed_at=_T0,
                crawl_requests_attempted=0,
                pages_observed=0,
                content_observations=0,
                content_created=0,
                content_deduplicated=0,
                failure_code=None,
                failure_summary=None,
            )

    @pytest.mark.asyncio
    async def test_get_run_maps_full_row(self) -> None:
        row = (
            str(_RUN),
            str(_POLICY),
            1,
            _policy().execution_snapshot(),
            str(_SOURCE),
            _T0,
            _T0,
            _T0,
            None,
            "RUNNING",
            "11111111-1111-1111-1111-111111111111",
            _T0,
            2,
            3,
            4,
            5,
            6,
            7,
            None,
            None,
        )
        repo, _ = await self._repo([row])
        run = await repo.get_run(_RUN)
        assert run is not None
        assert run.status is CollectionRunStatus.RUNNING
        assert run.attempt_count == 2
        assert run.pages_observed == 4

    @pytest.mark.asyncio
    async def test_create_run_occurrence_conflict(self) -> None:
        repo, _ = await self._repo([("occurrence_exists",)])
        with pytest.raises(ConflictError):
            await repo.create_run(_run())

    @pytest.mark.asyncio
    async def test_create_run_invokes_versioned_function(self) -> None:
        repo, cursor = await self._repo([("added",)])
        await repo.create_run(_run())
        sql = cursor.calls[0][0]
        assert sql == (
            "SELECT * FROM collection_run_create_v1(%s, %s, %s, %s, %s, %s, %s)"
        )


class TestSqlBoundaryCoversCollectionRepository:
    """The production package scan includes the new module automatically."""

    def test_module_contains_only_invocations(self) -> None:
        from tests.unit.infrastructure.persistence.postgresql import (
            test_sql_boundary,
        )

        violations = test_sql_boundary.scan_package(test_sql_boundary._PACKAGE)
        assert "collection_repository.py" not in violations
        assert all(
            "collection_repository.py" not in module
            for module in violations
            if "collection_repository" in module
        )

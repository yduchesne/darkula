# SPDX-License-Identifier: AGPL-3.0-only
"""PR 9 collection persistence matrix (P1-P16) over real PostgreSQL.

Exercises the real ``PostgresDarkulaSpi -> PostgresUnitOfWork ->
CollectionRepository -> stored function -> PostgreSQL`` path: policy and run
round-trips, endpoint authorization, occurrence uniqueness, expected-state
transitions, lease reclaim, scheduler admission atomicity, and coexistence
of the PR 9 schema with prior migrations.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from tests.integration.conftest import _raw_connect

from darkula.app.collection import (
    COLLECTION_WORK_STREAM as _WORK_STREAM,
)
from darkula.app.collection import (
    CollectionExecuteCommand,
)
from darkula.app.collection_scheduler import CollectionScheduler
from darkula.app.persistence import ConflictError, IntegrityError
from darkula.app.repositories import ClaimOutcome, ReclaimOutcome
from darkula.config.settings import DatabaseSettings
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
from darkula.domain.source import (
    EndpointStatus,
    EndpointType,
    Source,
    SourceEndpoint,
    SourceStatus,
)
from darkula.infrastructure.persistence.postgresql.spi import PostgresDarkulaSpi

pytestmark = pytest.mark.integration

_T0 = datetime(2026, 2, 1, 0, 0, 0, tzinfo=UTC)


async def _second_seed(
    spi: PostgresDarkulaSpi,
) -> tuple[CollectionPolicy, Source]:
    """Seed a second due policy for the rollback scenario."""
    source, endpoint = await _seed_source_endpoint(spi)
    policy = _policy(source, endpoint)
    async with spi.unit_of_work() as uow:
        await uow.collection.create_policy(policy)
        await uow.commit()
    return policy, source


def _source() -> Source:
    return Source(
        source_id=SourceId.generate(),
        status=SourceStatus.ACTIVE,
        created_at=_T0,
        updated_at=_T0,
        name="collection-source",
    )


def _endpoint(source: Source) -> SourceEndpoint:
    return SourceEndpoint(
        endpoint_id=SourceEndpointId.generate(),
        source_id=source.source_id,
        uri=f"http://blackgate-{uuid.uuid4().hex[:8]}.example.test/",
        endpoint_type=EndpointType.CLEARNET,
        status=EndpointStatus.ACTIVE,
        first_observed_at=_T0,
        last_observed_at=_T0,
    )


def _policy(source: Source, endpoint: SourceEndpoint) -> CollectionPolicy:
    return CollectionPolicy(
        policy_id=CollectionPolicyId.generate(),
        source_id=source.source_id,
        active=True,
        created_at=_T0,
        updated_at=_T0,
        revision=1,
        interval_seconds=3600,
        next_due_at=_T0,
        allowed_endpoint_ids=(endpoint.endpoint_id,),
        allowed_paths=("/board",),
        max_pages=12,
        max_requests=99,
        max_depth=3,
        timeout_seconds=42.0,
    )


def _run(policy: CollectionPolicy, source: Source) -> CollectionRun:
    return CollectionRun(
        run_id=CollectionRunId.generate(),
        policy_id=policy.policy_id,
        policy_revision=policy.revision,
        source_id=source.source_id,
        scheduled_for=_T0,
        created_at=_T0,
        status=CollectionRunStatus.QUEUED,
        policy_snapshot=policy.execution_snapshot(),
    )


async def _seed_source_endpoint(
    spi: PostgresDarkulaSpi,
) -> tuple[Source, SourceEndpoint]:
    source = _source()
    endpoint = _endpoint(source)
    async with spi.unit_of_work() as uow:
        await uow.sources.create(source)
        await uow.sources.add_endpoint(endpoint)
        await uow.commit()
    return source, endpoint


def _count(conn: Any, sql: str, params: tuple[Any, ...]) -> int:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    assert row is not None
    return int(row[0])


def _ledger_versions(database_settings: DatabaseSettings) -> set[str]:
    conn = _raw_connect(database_settings)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT version FROM darkula_schema_migrations")
            return {str(row[0]) for row in cur.fetchall()}
    finally:
        conn.close()


def _table_exists(database_settings: DatabaseSettings, name: str) -> bool:
    conn = _raw_connect(database_settings)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass(%s) IS NOT NULL", (name,))
            row = cur.fetchone()
    finally:
        conn.close()
    assert row is not None
    return bool(row[0])


class TestMigrationAndDataSurvival:
    """P1/P2/P16: the fresh migration provisions schema and preserves prior."""

    def test_p1_p2_migration_provisions_collection_schema(
        self, database_settings: DatabaseSettings
    ) -> None:
        assert _table_exists(database_settings, "collection_policy")
        assert _table_exists(database_settings, "collection_policy_endpoint")
        assert _table_exists(database_settings, "collection_run")
        versions = _ledger_versions(database_settings)
        assert "0004_collection.sql" in versions
        assert "0001_initial.sql" in versions
        assert "0003_content.sql" in versions

    @pytest.mark.asyncio
    async def test_p16_prior_source_data_survives_alongside_collection(
        self,
        spi: PostgresDarkulaSpi,
        database_settings: DatabaseSettings,
    ) -> None:
        source, endpoint = await _seed_source_endpoint(spi)
        policy = _policy(source, endpoint)
        async with spi.unit_of_work() as uow:
            await uow.collection.create_policy(policy)
            await uow.commit()
        # The PR 4 source repo still reads its data after PR 9 writes.
        async with spi.unit_of_work() as uow:
            reloaded = await uow.sources.get(source.source_id)
            listed = await uow.sources.list_endpoints(source.source_id)
        assert reloaded is not None
        assert reloaded.source_id == source.source_id
        assert [e.endpoint_id for e in listed] == [endpoint.endpoint_id]
        conn = _raw_connect(database_settings)
        try:
            assert (
                _count(
                    conn,
                    "SELECT count(*) FROM collection_policy WHERE policy_id = %s",
                    (str(policy.policy_id),),
                )
                == 1
            )
        finally:
            conn.close()


class TestPolicyRepository:
    """P3/P4: policy round-trip and endpoint authorization."""

    @pytest.mark.asyncio
    async def test_p3_policy_round_trip(self, spi: PostgresDarkulaSpi) -> None:
        source, endpoint = await _seed_source_endpoint(spi)
        policy = _policy(source, endpoint)
        async with spi.unit_of_work() as uow:
            await uow.collection.create_policy(policy)
            await uow.commit()
        async with spi.unit_of_work() as uow:
            loaded = await uow.collection.get_policy(policy.policy_id)
        assert loaded is not None
        assert loaded.source_id == source.source_id
        assert loaded.allowed_endpoint_ids == (endpoint.endpoint_id,)
        assert loaded.max_pages == 12
        assert loaded.revision == 1
        assert loaded.active is True

    @pytest.mark.asyncio
    async def test_p3_uncommitted_policy_absent(self, spi: PostgresDarkulaSpi) -> None:
        source, endpoint = await _seed_source_endpoint(spi)
        policy = _policy(source, endpoint)
        async with spi.unit_of_work() as uow:
            await uow.collection.create_policy(policy)
        async with spi.unit_of_work() as uow:
            assert await uow.collection.get_policy(policy.policy_id) is None

    @pytest.mark.asyncio
    async def test_p4_endpoint_of_other_source_rejected(
        self, spi: PostgresDarkulaSpi
    ) -> None:
        source, endpoint = await _seed_source_endpoint(spi)
        other = _source()
        foreign = _endpoint(other)
        async with spi.unit_of_work() as uow:
            await uow.sources.create(other)
            await uow.sources.add_endpoint(foreign)
            await uow.commit()
        policy = _policy(source, endpoint)
        evil = replace(policy, allowed_endpoint_ids=(foreign.endpoint_id,))
        with pytest.raises(IntegrityError):
            async with spi.unit_of_work() as uow:
                await uow.collection.create_policy(evil)
                await uow.commit()

    @pytest.mark.asyncio
    async def test_p4_duplicate_policy_identity_conflict(
        self, spi: PostgresDarkulaSpi
    ) -> None:
        source, endpoint = await _seed_source_endpoint(spi)
        policy = _policy(source, endpoint)
        async with spi.unit_of_work() as uow:
            await uow.collection.create_policy(policy)
            await uow.commit()
        with pytest.raises(ConflictError):
            async with spi.unit_of_work() as uow:
                await uow.collection.create_policy(policy)
                await uow.commit()

    @pytest.mark.asyncio
    async def test_p4_update_bumps_revision_and_old_runs_hold_snapshot(
        self, spi: PostgresDarkulaSpi
    ) -> None:
        source, endpoint = await _seed_source_endpoint(spi)
        policy = _policy(source, endpoint)
        async with spi.unit_of_work() as uow:
            await uow.collection.create_policy(policy)
            run = _run(policy, source)
            await uow.collection.create_run(run)
            await uow.commit()
        edited = replace(policy, max_pages=1, updated_at=_T0)
        async with spi.unit_of_work() as uow:
            new_revision = await uow.collection.update_policy(edited)
            await uow.commit()
        assert new_revision == 2
        async with spi.unit_of_work() as uow:
            loaded_run = await uow.collection.get_run(run.run_id)
        assert loaded_run is not None
        assert loaded_run.policy_revision == 1
        assert loaded_run.policy_snapshot is not None
        assert loaded_run.policy_snapshot["max_pages"] == 12  # frozen


class TestRunRepository:
    """P5-P11: run round-trips and expected-state transitions."""

    @pytest.mark.asyncio
    async def test_p5_run_round_trip(self, spi: PostgresDarkulaSpi) -> None:
        source, endpoint = await _seed_source_endpoint(spi)
        policy = _policy(source, endpoint)
        async with spi.unit_of_work() as uow:
            await uow.collection.create_policy(policy)
            await uow.commit()
        run = _run(policy, source)
        async with spi.unit_of_work() as uow:
            await uow.collection.create_run(run)
            await uow.commit()
        async with spi.unit_of_work() as uow:
            loaded = await uow.collection.get_run(run.run_id)
        assert loaded is not None
        assert loaded.status is CollectionRunStatus.QUEUED
        assert loaded.policy_revision == 1
        assert loaded.policy_snapshot == policy.execution_snapshot()
        assert loaded.scheduled_for == _T0

    @pytest.mark.asyncio
    async def test_p6_duplicate_occurrence_rejected(
        self, spi: PostgresDarkulaSpi
    ) -> None:
        source, endpoint = await _seed_source_endpoint(spi)
        policy = _policy(source, endpoint)
        async with spi.unit_of_work() as uow:
            await uow.collection.create_policy(policy)
            await uow.commit()
        run_a = _run(policy, source)
        run_b = _run(policy, source)
        async with spi.unit_of_work() as uow:
            await uow.collection.create_run(run_a)
            await uow.commit()
        with pytest.raises(ConflictError):
            async with spi.unit_of_work() as uow:
                await uow.collection.create_run(run_b)
                await uow.commit()

    @pytest.mark.asyncio
    async def test_p7_claim_only_from_queued(self, spi: PostgresDarkulaSpi) -> None:
        source, endpoint = await _seed_source_endpoint(spi)
        policy = _policy(source, endpoint)
        async with spi.unit_of_work() as uow:
            await uow.collection.create_policy(policy)
            await uow.commit()
        run = _run(policy, source)
        async with spi.unit_of_work() as uow:
            await uow.collection.create_run(run)
            await uow.commit()
        async with spi.unit_of_work() as uow:
            first = await uow.collection.claim_run(
                run_id=run.run_id,
                execution_id=str(uuid.uuid4()),
                started_at=_T0,
                lease_expires_at=_T0 + timedelta(seconds=60),
            )
            assert first is ClaimOutcome.CLAIMED
            await uow.commit()
        async with spi.unit_of_work() as uow:
            second = await uow.collection.claim_run(
                run_id=run.run_id,
                execution_id=str(uuid.uuid4()),
                started_at=_T0,
                lease_expires_at=_T0 + timedelta(seconds=60),
            )
            assert second is ClaimOutcome.NOT_QUEUED
        async with spi.unit_of_work() as uow:
            loaded = await uow.collection.get_run(run.run_id)
        assert loaded is not None
        assert loaded.attempt_count == 1
        assert loaded.status is CollectionRunStatus.RUNNING

    @pytest.mark.asyncio
    async def test_p8_concurrent_starts_one_winner(
        self, spi: PostgresDarkulaSpi
    ) -> None:
        source, endpoint = await _seed_source_endpoint(spi)
        policy = _policy(source, endpoint)
        async with spi.unit_of_work() as uow:
            await uow.collection.create_policy(policy)
            await uow.commit()
        run = _run(policy, source)
        async with spi.unit_of_work() as uow:
            await uow.collection.create_run(run)
            await uow.commit()

        async def _claim() -> ClaimOutcome:
            async with spi.unit_of_work() as uow:
                outcome = await uow.collection.claim_run(
                    run_id=run.run_id,
                    execution_id=str(uuid.uuid4()),
                    started_at=_T0,
                    lease_expires_at=_T0 + timedelta(seconds=60),
                )
                await uow.commit()
                return outcome

        outcomes = await asyncio.gather(_claim(), _claim())
        assert sorted(o.value for o in outcomes) == ["claimed", "not_queued"]

    @pytest.mark.asyncio
    async def test_p9_terminal_transitions_require_running(
        self, spi: PostgresDarkulaSpi
    ) -> None:
        source, endpoint = await _seed_source_endpoint(spi)
        policy = _policy(source, endpoint)
        async with spi.unit_of_work() as uow:
            await uow.collection.create_policy(policy)
            await uow.commit()
        run = _run(policy, source)
        async with spi.unit_of_work() as uow:
            await uow.collection.create_run(run)
            await uow.commit()
        with pytest.raises(ConflictError):
            async with spi.unit_of_work() as uow:
                await uow.collection.complete_run(
                    run_id=run.run_id,
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
                await uow.commit()
        async with spi.unit_of_work() as uow:
            await uow.collection.claim_run(
                run_id=run.run_id,
                execution_id=str(uuid.uuid4()),
                started_at=_T0,
                lease_expires_at=_T0 + timedelta(seconds=60),
            )
            await uow.commit()
        async with spi.unit_of_work() as uow:
            await uow.collection.complete_run(
                run_id=run.run_id,
                new_status=CollectionRunStatus.SUCCEEDED,
                completed_at=_T0 + timedelta(seconds=10),
                crawl_requests_attempted=2,
                pages_observed=5,
                content_observations=4,
                content_created=3,
                content_deduplicated=1,
                failure_code=None,
                failure_summary=None,
            )
            await uow.commit()
        async with spi.unit_of_work() as uow:
            loaded = await uow.collection.get_run(run.run_id)
        assert loaded is not None
        assert loaded.status is CollectionRunStatus.SUCCEEDED
        assert loaded.pages_observed == 5
        assert loaded.lease_expires_at is None

    @pytest.mark.asyncio
    async def test_p10_expired_lease_reclaim(self, spi: PostgresDarkulaSpi) -> None:
        source, endpoint = await _seed_source_endpoint(spi)
        policy = _policy(source, endpoint)
        async with spi.unit_of_work() as uow:
            await uow.collection.create_policy(policy)
            await uow.commit()
        run = _run(policy, source)
        async with spi.unit_of_work() as uow:
            await uow.collection.create_run(run)
            await uow.commit()
        # Crash the first worker: QUEUED -> RUNNING with an expired lease.
        async with spi.unit_of_work() as uow:
            claim = await uow.collection.claim_run(
                run_id=run.run_id,
                execution_id=str(uuid.uuid4()),
                started_at=_T0 - timedelta(seconds=30),
                lease_expires_at=_T0 - timedelta(seconds=5),
            )
            assert claim is ClaimOutcome.CLAIMED
            await uow.commit()
        async with spi.unit_of_work() as uow:
            reclaim = await uow.collection.reclaim_run(
                run_id=run.run_id,
                execution_id=str(uuid.uuid4()),
                now=_T0,
                lease_expires_at=_T0 + timedelta(seconds=60),
                max_attempts=3,
            )
            await uow.commit()
        assert reclaim is ReclaimOutcome.RECLAIMED
        async with spi.unit_of_work() as uow:
            loaded = await uow.collection.get_run(run.run_id)
        assert loaded is not None
        assert loaded.attempt_count == 2
        assert loaded.status is CollectionRunStatus.RUNNING

    @pytest.mark.asyncio
    async def test_p11_unexpired_lease_rejects(self, spi: PostgresDarkulaSpi) -> None:
        source, endpoint = await _seed_source_endpoint(spi)
        policy = _policy(source, endpoint)
        async with spi.unit_of_work() as uow:
            await uow.collection.create_policy(policy)
            await uow.commit()
        run = _run(policy, source)
        async with spi.unit_of_work() as uow:
            await uow.collection.create_run(run)
            await uow.commit()
        # A live worker holds an unexpired lease.
        async with spi.unit_of_work() as uow:
            claim = await uow.collection.claim_run(
                run_id=run.run_id,
                execution_id=str(uuid.uuid4()),
                started_at=_T0,
                lease_expires_at=_T0 + timedelta(seconds=600),
            )
            assert claim is ClaimOutcome.CLAIMED
            await uow.commit()
        async with spi.unit_of_work() as uow:
            reclaim = await uow.collection.reclaim_run(
                run_id=run.run_id,
                execution_id=str(uuid.uuid4()),
                now=_T0,
                lease_expires_at=_T0 + timedelta(seconds=60),
                max_attempts=3,
            )
        assert reclaim is ReclaimOutcome.LEASE_ACTIVE

    @pytest.mark.asyncio
    async def test_p10_attempts_exhausted(self, spi: PostgresDarkulaSpi) -> None:
        source, endpoint = await _seed_source_endpoint(spi)
        policy = _policy(source, endpoint)
        async with spi.unit_of_work() as uow:
            await uow.collection.create_policy(policy)
            await uow.commit()
        run = _run(policy, source)
        async with spi.unit_of_work() as uow:
            await uow.collection.create_run(run)
            await uow.commit()
        # Claim (attempt 1), then two reclaims: attempts 2 and 3; with
        # max_attempts=3 the next reclaim is exhausted.
        async with spi.unit_of_work() as uow:
            first = await uow.collection.claim_run(
                run_id=run.run_id,
                execution_id=str(uuid.uuid4()),
                started_at=_T0 - timedelta(seconds=30),
                lease_expires_at=_T0 - timedelta(seconds=5),
            )
            assert first is ClaimOutcome.CLAIMED
            await uow.commit()
        for _ in range(2):
            async with spi.unit_of_work() as uow:
                outcome = await uow.collection.reclaim_run(
                    run_id=run.run_id,
                    execution_id=str(uuid.uuid4()),
                    now=_T0,
                    lease_expires_at=_T0 - timedelta(seconds=5),
                    max_attempts=3,
                )
                await uow.commit()
            assert outcome is ReclaimOutcome.RECLAIMED
        async with spi.unit_of_work() as uow:
            outcome = await uow.collection.reclaim_run(
                run_id=run.run_id,
                execution_id=str(uuid.uuid4()),
                now=_T0,
                lease_expires_at=_T0 + timedelta(seconds=60),
                max_attempts=3,
            )
        assert outcome is ReclaimOutcome.ATTEMPTS_EXHAUSTED


class TestScheduleAdmission:
    """P12-P14: atomic due admission and scheduler path."""

    @pytest.mark.asyncio
    async def test_p12_concurrent_due_claim_one_occurrence(
        self,
        spi: PostgresDarkulaSpi,
        database_settings: DatabaseSettings,
    ) -> None:
        source, endpoint = await _seed_source_endpoint(spi)
        policy = _policy(source, endpoint)
        async with spi.unit_of_work() as uow:
            await uow.collection.create_policy(policy)
            await uow.commit()

        async def _schedule() -> str:
            async with spi.unit_of_work() as uow:
                occurrence = await uow.collection.schedule_due(
                    now=_T0 + timedelta(seconds=1),
                    run_id=CollectionRunId.generate(),
                    created_at=_T0,
                )
                await uow.commit()
                return "none" if occurrence is None else occurrence.outcome.value

        outcomes = await asyncio.gather(_schedule(), _schedule())
        assert sum(1 for o in outcomes if o == "scheduled") == 1
        conn = _raw_connect(database_settings)
        try:
            runs = _count(
                conn,
                "SELECT count(*) FROM collection_run WHERE policy_id = %s",
                (str(policy.policy_id),),
            )
        finally:
            conn.close()
        assert runs == 1

    @pytest.mark.asyncio
    async def test_p13_p14_scheduler_run_outbox_atomic(
        self,
        spi: PostgresDarkulaSpi,
        database_settings: DatabaseSettings,
    ) -> None:
        source, endpoint = await _seed_source_endpoint(spi)
        policy = _policy(source, endpoint)
        async with spi.unit_of_work() as uow:
            await uow.collection.create_policy(policy)
            await uow.commit()
        scheduler = CollectionScheduler(spi=spi)
        assert (
            await scheduler.schedule_due(now=_T0 + timedelta(seconds=1), limit=5) == 1
        )

        conn = _raw_connect(database_settings)
        try:
            runs = _count(
                conn,
                "SELECT count(*) FROM collection_run WHERE policy_id = %s",
                (str(policy.policy_id),),
            )
            outbox = _count(
                conn,
                "SELECT count(*) FROM message_outbox "
                "WHERE message_type = 'collection.execute'",
                (),
            )
        finally:
            conn.close()
        assert runs == 1
        assert outbox == 1

        # P14: a rolled-back manual admission leaves neither run nor outbox.
        second, second_source = await _second_seed(spi)
        async with spi.unit_of_work() as uow:
            occurrence = await uow.collection.schedule_due(
                now=_T0 + timedelta(seconds=1),
                run_id=CollectionRunId.generate(),
                created_at=_T0,
            )
            assert occurrence is not None
            assert occurrence.outcome.value == "scheduled"
            assert occurrence.policy_id == second.policy_id
            assert occurrence.source_id == second_source.source_id
            command = CollectionExecuteCommand(
                run_id=occurrence.run_id,
                source_id=occurrence.source_id,
                policy_id=occurrence.policy_id,
            )
            await uow.outbox.append(
                command.to_message(occurred_at=_T0),
                stream_name=_WORK_STREAM,
            )
            await uow.rollback()

        conn = _raw_connect(database_settings)
        try:
            second_runs = _count(
                conn,
                "SELECT count(*) FROM collection_run WHERE policy_id = %s",
                (str(second.policy_id),),
            )
            outbox_after = _count(
                conn,
                "SELECT count(*) FROM message_outbox "
                "WHERE message_type = 'collection.execute'",
                (),
            )
        finally:
            conn.close()
        assert second_runs == 0
        assert outbox_after == 1

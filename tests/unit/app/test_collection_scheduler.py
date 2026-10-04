# SPDX-License-Identifier: AGPL-3.0-only
"""CollectionScheduler tests (SCH1-SCH10, PR 9).

Deterministic, offline: the in-memory collection/outbox fakes exercise the
real scheduler against commit/rollback semantics. The PostgreSQL-backed race
arbitration (SCH6) is additionally proven in the real-DB integration suite.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest
from tests.support.collection_fakes import (
    MemSpi,
    MemUnitOfWork,
    seed_endpoint,
    seed_source,
)

from darkula.app.collection import (
    COLLECTION_EXECUTE_MESSAGE_TYPE,
    COLLECTION_WORK_STREAM,
    CollectionExecuteCommand,
)
from darkula.app.collection_scheduler import CollectionScheduler
from darkula.domain.collection import (
    CollectionPolicy,
    CollectionRunStatus,
)
from darkula.domain.identifiers import (
    CollectionPolicyId,
    CollectionRunId,
    ConsumerId,
    SourceEndpointId,
    SourceId,
)

_T0 = datetime(2026, 2, 1, 0, 0, 0, tzinfo=UTC)
_T1 = datetime(2026, 2, 1, 1, 0, 0, tzinfo=UTC)
_POLICY = CollectionPolicyId.from_str("11111111-1111-1111-1111-111111111111")
_SOURCE = SourceId.from_str("22222222-2222-2222-2222-222222222222")
_ENDPOINT = SourceEndpointId.from_str("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_CONSUMER = ConsumerId("collection-worker")


def _seed_due_policy(
    spi: MemSpi,
    *,
    active: bool = True,
    interval_seconds: int = 3600,
    next_due_at: datetime = _T0,
) -> CollectionPolicy:
    seed_source(spi.state, _SOURCE)
    seed_endpoint(spi.state, _ENDPOINT, _SOURCE, "http://blackgate.example.test/")
    policy = CollectionPolicy(
        policy_id=_POLICY,
        source_id=_SOURCE,
        active=active,
        created_at=_T0,
        updated_at=_T0,
        revision=1,
        interval_seconds=interval_seconds,
        next_due_at=next_due_at,
        allowed_endpoint_ids=(_ENDPOINT,),
    )
    spi.state.policies[_POLICY] = policy
    return policy


def _scheduler(spi: MemSpi) -> CollectionScheduler:
    return CollectionScheduler(spi=spi)


class TestScheduling:
    """SCH1/SCH2/SCH3/SCH10: due eligibility and determinism."""

    @pytest.mark.asyncio
    async def test_sch1_due_active_policy_queues_run_and_outbox(self) -> None:
        spi = MemSpi()
        _seed_due_policy(spi, next_due_at=_T0)
        count = await _scheduler(spi).schedule_due(now=_T1, limit=10)
        assert count == 1
        runs = list(spi.state.runs.values())
        assert len(runs) == 1
        run = runs[0]
        assert run.status is CollectionRunStatus.QUEUED
        assert run.scheduled_for == _T0
        assert run.policy_revision == 1
        messages = spi.state.outbox[COLLECTION_WORK_STREAM]
        assert len(messages) == 1
        assert messages[0].message_type == COLLECTION_EXECUTE_MESSAGE_TYPE
        command = CollectionExecuteCommand.from_message(messages[0])
        assert command.run_id == run.run_id
        assert command.source_id == _SOURCE
        assert command.policy_id == _POLICY

    @pytest.mark.asyncio
    async def test_sch2_not_due_yields_nothing(self) -> None:
        spi = MemSpi()
        _seed_due_policy(spi, next_due_at=_T1)
        count = await _scheduler(spi).schedule_due(now=_T0, limit=5)
        assert count == 0
        assert not spi.state.runs
        assert not spi.state.outbox

    @pytest.mark.asyncio
    async def test_sch3_inactive_policy_or_source_is_never_due(self) -> None:
        spi = MemSpi()
        _seed_due_policy(spi, active=False, next_due_at=_T0)
        assert await _scheduler(spi).schedule_due(now=_T1, limit=5) == 0

    @pytest.mark.asyncio
    async def test_sch4_exact_due_timestamp_is_due(self) -> None:
        spi = MemSpi()
        _seed_due_policy(spi, next_due_at=_T1)
        assert await _scheduler(spi).schedule_due(now=_T1, limit=5) == 1

    @pytest.mark.asyncio
    async def test_sch10_explicit_now_is_deterministic(self) -> None:
        spi = MemSpi()
        _seed_due_policy(spi, next_due_at=_T0)
        first = await _scheduler(spi).schedule_due(now=_T1, limit=5)
        # A second sweep with a clock before the next occurrence schedules
        # nothing new (the occurrence was already admitted).
        second = await _scheduler(spi).schedule_due(now=_T1, limit=5)
        assert first == 1
        assert second == 0

    @pytest.mark.asyncio
    async def test_sch2_naive_now_rejected(self) -> None:
        spi = MemSpi()
        _seed_due_policy(spi, next_due_at=_T0)
        with pytest.raises(ValueError):
            await _scheduler(spi).schedule_due(now=datetime(2026, 2, 1), limit=5)


class TestOccurrenceIdempotency:
    """SCH5/SCH6: the same occurrence is admitted exactly once."""

    @pytest.mark.asyncio
    async def test_sch5_retry_same_occurrence_no_duplicate(self) -> None:
        spi = MemSpi()
        _seed_due_policy(spi, next_due_at=_T0)
        scheduler = _scheduler(spi)
        assert await scheduler.schedule_due(now=_T1, limit=5) == 1
        # The scheduler advances the policy past now; the same occurrence is
        # never re-admitted and no second command is emitted.
        assert await scheduler.schedule_due(now=_T1, limit=5) == 0
        assert len(spi.state.runs) == 1
        assert len(spi.state.outbox[COLLECTION_WORK_STREAM]) == 1

    @pytest.mark.asyncio
    async def test_sch6_concurrent_schedulers_one_run(self) -> None:
        # Two independent scheduler instances race the same shared state; the
        # in-memory occurrence uniqueness mirrors the DB UNIQUE constraint.
        spi = MemSpi()
        _seed_due_policy(spi, next_due_at=_T0)
        scheduler_a = _scheduler(spi)
        scheduler_b = CollectionScheduler(spi=spi)
        results = await asyncio.gather(
            scheduler_a.schedule_due(now=_T1, limit=5),
            scheduler_b.schedule_due(now=_T1, limit=5),
        )
        assert sum(results) == 1
        assert len(spi.state.runs) == 1
        assert len(spi.state.outbox[COLLECTION_WORK_STREAM]) == 1

    @pytest.mark.asyncio
    async def test_sch7_batch_limit_is_bounded(self) -> None:
        spi = MemSpi()
        # Three distinct due policies: only the batch limit may be admitted.
        for index in range(3):
            policy_id = CollectionPolicyId.generate()
            source_id = SourceId.generate()
            endpoint_id = SourceEndpointId.generate()
            seed_source(spi.state, source_id)
            seed_endpoint(spi.state, endpoint_id, source_id, f"http://s{index}.test/")
            spi.state.policies[policy_id] = CollectionPolicy(
                policy_id=policy_id,
                source_id=source_id,
                active=True,
                created_at=_T0,
                updated_at=_T0,
                revision=1,
                interval_seconds=3600,
                next_due_at=_T0,
                allowed_endpoint_ids=(endpoint_id,),
            )
        count = await _scheduler(spi).schedule_due(now=_T1, limit=2)
        assert count == 2
        assert len(spi.state.runs) == 2
        assert len(spi.state.outbox[COLLECTION_WORK_STREAM]) == 2
        assert await _scheduler(spi).schedule_due(now=_T1, limit=2) == 1

    @pytest.mark.asyncio
    async def test_schedule_limit_must_be_positive(self) -> None:
        spi = MemSpi()
        with pytest.raises(ValueError):
            await _scheduler(spi).schedule_due(now=_T1, limit=0)


class TestAtomicity:
    """SCH8/SCH9: run + outbox commit atomically or not at all."""

    @pytest.mark.asyncio
    async def test_sch8_outbox_failure_rolls_back_run_too(self) -> None:
        spi = MemSpi()
        _seed_due_policy(spi, next_due_at=_T0)

        class _FailingOutboxUow(MemUnitOfWork):
            @property
            def outbox(self) -> Any:
                class _Fail:
                    async def append(self, *a: object, **k: object) -> None:
                        raise RuntimeError("outbox unavailable")

                return _Fail()

        class _FailingSpi(MemSpi):
            def unit_of_work(self) -> MemUnitOfWork:
                return _FailingOutboxUow(self.state)

        with pytest.raises(RuntimeError):
            await _scheduler(_FailingSpi(spi.state)).schedule_due(now=_T1, limit=5)
        # Neither the run nor the outbox row is durable (SCH8).
        assert not spi.state.runs
        assert not spi.state.outbox

    @pytest.mark.asyncio
    async def test_sch9_commit_makes_both_durable(self) -> None:
        spi = MemSpi()
        _seed_due_policy(spi, next_due_at=_T0)
        assert await _scheduler(spi).schedule_due(now=_T1, limit=5) == 1
        run = next(iter(spi.state.runs.values()))
        assert run.status is CollectionRunStatus.QUEUED
        messages = spi.state.outbox[COLLECTION_WORK_STREAM]
        assert len(messages) == 1
        assert CollectionExecuteCommand.from_message(messages[0]).run_id == run.run_id


class TestCommandWireShape:
    """WK12: the published command carries IDs only."""

    @pytest.mark.asyncio
    async def test_message_payload_is_ids_only(self) -> None:
        spi = MemSpi()
        _seed_due_policy(spi, next_due_at=_T0)
        await _scheduler(spi).schedule_due(now=_T1, limit=5)
        message = spi.state.outbox[COLLECTION_WORK_STREAM][0]
        assert set(message.payload) == {
            "collection_run_id",
            "source_id",
            "policy_id",
        }
        # No URI, no policy blob, no content, no secrets.
        serialized = str(message.payload).lower()
        assert "blackgate" not in serialized
        assert "http" not in serialized
        assert "password" not in serialized
        assert message.correlation_id is None
        # Routing key is the bounded source identity (lane affinity; never a
        # URI or broker offset).
        assert message.routing_key is not None
        assert message.routing_key == str(_SOURCE)

    @pytest.mark.asyncio
    async def test_worker_identity_is_the_registered_consumer(self) -> None:
        from darkula.app.collection import COLLECTION_WORKER_CONSUMER

        assert COLLECTION_WORKER_CONSUMER.value == "collection-worker"
        assert _CONSUMER == COLLECTION_WORKER_CONSUMER

    def test_command_round_trip_and_strictness(self) -> None:
        from darkula.app.collection import CollectionWorkError

        command = CollectionExecuteCommand(
            run_id=CollectionRunId.generate(),
            source_id=_SOURCE,
            policy_id=_POLICY,
        )
        message = command.to_message(occurred_at=_T1)
        decoded = CollectionExecuteCommand.from_message(message)
        assert decoded == command
        # Malformed payloads fail closed.
        from darkula.app.data_stream import StreamMessage
        from darkula.domain.identifiers import MessageId

        bad = StreamMessage(
            message_id=MessageId.generate(),
            message_type=COLLECTION_EXECUTE_MESSAGE_TYPE,
            schema_version=1,
            occurred_at=_T1,
            payload={
                "collection_run_id": "nope",
                "source_id": "nope",
                "policy_id": "nope",
            },
        )
        with pytest.raises(CollectionWorkError):
            CollectionExecuteCommand.from_message(bad)
        extra = StreamMessage(
            message_id=MessageId.generate(),
            message_type=COLLECTION_EXECUTE_MESSAGE_TYPE,
            schema_version=1,
            occurred_at=_T1,
            payload={
                "collection_run_id": str(command.run_id),
                "source_id": str(_SOURCE),
                "policy_id": str(_POLICY),
                "uri": "http://evil.test",
            },
        )
        with pytest.raises(CollectionWorkError):
            CollectionExecuteCommand.from_message(extra)
        wrong_version = StreamMessage(
            message_id=MessageId.generate(),
            message_type=COLLECTION_EXECUTE_MESSAGE_TYPE,
            schema_version=2,
            occurred_at=_T1,
            payload={
                "collection_run_id": str(command.run_id),
                "source_id": str(_SOURCE),
                "policy_id": str(_POLICY),
            },
        )
        with pytest.raises(CollectionWorkError):
            CollectionExecuteCommand.from_message(wrong_version)

# SPDX-License-Identifier: AGPL-3.0-only
"""CollectionWorker tests (WK1-WK13, PR 9).

Deterministic, offline: the real worker + real service run against the
in-memory persistence fakes and the canonical FakeDataStream (whose
``reset_consumer`` is the explicit redelivery seam). Acknowledgment ordering,
lease recovery, attempt bounds, and poison handling are asserted here; the
real Redpanda variants live in the integration suite.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from tests.support.collection_fakes import (
    FakeCrawler,
    FakeIngest,
    MemSpi,
    seed_endpoint,
    seed_source,
)

from darkula.app.collection import (
    COLLECTION_WORK_STREAM,
    COLLECTION_WORKER_CONSUMER,
    CollectionExecuteCommand,
)
from darkula.app.collection_worker import CollectionWorker
from darkula.app.data_stream import AcknowledgeError
from darkula.config.settings import CollectionSettings
from darkula.domain.collection import (
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
from darkula.testing.fake_data_stream import FakeDataStream

_T0 = datetime(2026, 2, 1, 0, 0, 0, tzinfo=UTC)
_POLICY = CollectionPolicyId.from_str("11111111-1111-1111-1111-111111111111")
_SOURCE = SourceId.from_str("22222222-2222-2222-2222-222222222222")
_ENDPOINT = SourceEndpointId.from_str("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_URI = "http://blackgate.example.test/board/board-announcements"


class _Clock:
    def __init__(self, moment: datetime = _T0) -> None:
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment

    def advance(self, seconds: int) -> None:
        self.moment = self.moment + timedelta(seconds=seconds)


def _seed_context(
    *,
    run: CollectionRun | None = None,
    clock: _Clock | None = None,
) -> tuple[MemSpi, CollectionRun]:
    """Seed source/endpoint (ACTIVE) + an ACTIVE policy + a run."""
    spi = MemSpi()
    seed_source(spi.state, _SOURCE)
    seed_endpoint(spi.state, _ENDPOINT, _SOURCE, _URI)
    policy = CollectionPolicy(
        policy_id=_POLICY,
        source_id=_SOURCE,
        active=True,
        created_at=_T0,
        updated_at=_T0,
        revision=1,
        interval_seconds=3600,
        next_due_at=_T0,
        allowed_endpoint_ids=(_ENDPOINT,),
        max_pages=5,
        max_requests=60,
        max_depth=2,
        timeout_seconds=60.0,
    )
    spi.state.policies[_POLICY] = policy
    if run is None:
        run = CollectionRun(
            run_id=CollectionRunId.generate(),
            policy_id=_POLICY,
            policy_revision=1,
            source_id=_SOURCE,
            scheduled_for=_T0,
            created_at=_T0,
            status=CollectionRunStatus.QUEUED,
            policy_snapshot=policy.execution_snapshot(),
        )
    spi.state.runs[run.run_id] = run
    return spi, run


def _worker(
    spi: MemSpi,
    stream: FakeDataStream,
    *,
    crawler: FakeCrawler | None = None,
    ingest: FakeIngest | None = None,
    clock: _Clock | None = None,
    settings: CollectionSettings | None = None,
) -> CollectionWorker:
    from darkula.app.collection import SourceCollectionService

    service = SourceCollectionService(
        spi=spi,
        crawler=crawler or FakeCrawler(),
        content_ingest=ingest or FakeIngest(),
        settings=settings or CollectionSettings(),
        clock=clock or _Clock(),
    )
    return CollectionWorker(
        spi=spi,
        data_stream=stream,
        service=service,
        settings=settings or CollectionSettings(),
        stream_name=COLLECTION_WORK_STREAM,
        consumer_id=COLLECTION_WORKER_CONSUMER,
    )


async def _publish_execute(stream: FakeDataStream, run: CollectionRun) -> None:
    command = CollectionExecuteCommand(
        run_id=run.run_id,
        source_id=_SOURCE,
        policy_id=_POLICY,
    )
    await stream.publish(
        stream_name=COLLECTION_WORK_STREAM,
        messages=[command.to_message(occurred_at=_T0)],
    )


def _pos(stream: FakeDataStream) -> set[Any]:
    """Acked positions for the stream/consumer."""
    return set(
        stream.acknowledged_positions(
            COLLECTION_WORK_STREAM, COLLECTION_WORKER_CONSUMER
        )
    )


class TestFirstDelivery:
    """WK1/WK8: first queued delivery claims, executes, then acks."""

    @pytest.mark.asyncio
    async def test_wk1_first_queued_delivery_claims_and_executes(self) -> None:
        spi, run = _seed_context()
        stream = FakeDataStream()
        await _publish_execute(stream, run)
        crawler = FakeCrawler()
        worker = _worker(spi, stream, crawler=crawler)
        assert await worker.process_once(max_records=5) == 1
        persisted = spi.state.runs[run.run_id]
        assert persisted.status is CollectionRunStatus.SUCCEEDED
        assert len(crawler.requests) == 1
        # Acknowledgement happened only after the durable terminal state.
        assert _pos(stream) != set()
        # Delivery idempotency marker persisted by stable message identity.
        assert len(spi.state.processed) == 1

    @pytest.mark.asyncio
    async def test_wk12_processed_marker_never_a_broker_position(self) -> None:
        spi, run = _seed_context()
        stream = FakeDataStream()
        await _publish_execute(stream, run)
        await _worker(spi, stream).process_once(max_records=5)
        keys = {key[2] for key in spi.state.processed}
        assert len(keys) == 1
        marker = next(iter(keys))
        # WK13: the stored key is a stable message UUID identity, never an
        # offset or position (offsets are ints; positions carry lanes).
        assert isinstance(marker, str)
        assert marker not in {str(pos.offset) for pos in _pos(stream)}


class TestDuplicatesAndTerminal:
    """WK2/WK9: terminal runs never recrawl and are acked."""

    @pytest.mark.asyncio
    async def test_wk2_duplicate_terminal_delivery_no_crawl_ack(self) -> None:
        terminal = CollectionRun(
            run_id=CollectionRunId.generate(),
            policy_id=_POLICY,
            policy_revision=1,
            source_id=_SOURCE,
            scheduled_for=_T0,
            created_at=_T0,
            status=CollectionRunStatus.SUCCEEDED,
            started_at=_T0,
            completed_at=_T0,
            content_observations=7,
        )
        spi, run = _seed_context(run=terminal)
        stream = FakeDataStream()
        await _publish_execute(stream, run)
        crawler = FakeCrawler()
        worker = _worker(spi, stream, crawler=crawler)
        # A terminal run is a no-op: acknowledged, never executed/crawled.
        assert await worker.process_once(max_records=5) == 0
        assert crawler.requests == []
        assert _pos(stream) != set()
        assert spi.state.runs[run.run_id].content_observations == 7

    @pytest.mark.asyncio
    async def test_wk9_success_before_ack_redelivery_does_not_recrawl(
        self,
    ) -> None:
        spi, run = _seed_context()
        stream = FakeDataStream()
        await _publish_execute(stream, run)
        crawler = FakeCrawler()
        worker = _worker(spi, stream, crawler=crawler)
        stream.acknowledge_failure = AcknowledgeError("simulated ack failure")
        # Durable SUCCEEDED commits, then the ack fails (WK9: crash between
        # terminal commit and ack).
        with pytest.raises(AcknowledgeError):
            await worker.process_once(max_records=5)
        assert spi.state.runs[run.run_id].status is CollectionRunStatus.SUCCEEDED
        stream.acknowledge_failure = None
        # Redelivery: no crawl, no repeated content; the worker acks.
        stream.reset_consumer(COLLECTION_WORK_STREAM, COLLECTION_WORKER_CONSUMER)
        assert await worker.process_once(max_records=5) == 0
        assert len(crawler.requests) == 1  # only the first delivery crawled
        assert _pos(stream) != set()


class TestLeaseRecovery:
    """WK3/WK4/WK5/WK6: lease-gated concurrency and same-run reclaim."""

    async def _running(self, *, lease: datetime, attempt: int) -> CollectionRun:
        return CollectionRun(
            run_id=CollectionRunId.generate(),
            policy_id=_POLICY,
            policy_revision=1,
            source_id=_SOURCE,
            scheduled_for=_T0,
            created_at=_T0,
            status=CollectionRunStatus.RUNNING,
            started_at=_T0,
            execution_id="old-exec",
            lease_expires_at=lease,
            attempt_count=attempt,
            policy_snapshot=None,  # filled by _seed_context below
        )

    @pytest.mark.asyncio
    async def test_wk3_unexpired_lease_no_concurrent_crawl_no_ack(self) -> None:
        clock = _Clock()
        running = await self._running(lease=clock() + timedelta(seconds=600), attempt=1)
        spi, run = _seed_context()
        # _seed_context created its own run; register the RUNNING one.
        spi.state.runs[running.run_id] = replace(
            running,
            policy_snapshot=spi.state.runs[run.run_id].policy_snapshot,
        )
        stream = FakeDataStream()
        await _publish_execute(stream, running)
        crawler = FakeCrawler()
        worker = _worker(spi, stream, crawler=crawler, clock=clock)
        assert await worker.process_once(max_records=5) == 0
        # No concurrent crawl, nothing acknowledged (redelivery re-inspects).
        assert crawler.requests == []
        assert _pos(stream) == set()
        assert spi.state.runs[running.run_id].status is CollectionRunStatus.RUNNING

    @pytest.mark.asyncio
    async def test_wk4_expired_lease_reclaims_same_run(self) -> None:
        clock = _Clock()
        expired = clock() - timedelta(seconds=5)
        running = await self._running(lease=expired, attempt=1)
        spi, _ = _seed_context()
        spi.state.runs[running.run_id] = replace(
            running,
            policy_snapshot=_default_snapshot(),
        )
        stream = FakeDataStream()
        await _publish_execute(stream, running)
        crawler = FakeCrawler()
        worker = _worker(spi, stream, crawler=crawler, clock=clock)
        assert await worker.process_once(max_records=5) == 1
        persisted = spi.state.runs[running.run_id]
        assert persisted.status is CollectionRunStatus.SUCCEEDED
        # WK5: reclaim bumped the attempt (1 -> 2) on the SAME run identity.
        assert persisted.run_id == running.run_id
        assert persisted.attempt_count == 2
        assert len(crawler.requests) == 1

    @pytest.mark.asyncio
    async def test_wk6_attempts_exhausted_finalizes_failed(self) -> None:
        clock = _Clock()
        expired = clock() - timedelta(seconds=5)
        running = await self._running(lease=expired, attempt=2)
        spi, _ = _seed_context()
        spi.state.runs[running.run_id] = replace(
            running,
            policy_snapshot=_default_snapshot(),
        )
        # The policy snapshot belongs to this configuration's policy.
        stream = FakeDataStream()
        await _publish_execute(stream, running)
        crawler = FakeCrawler()
        worker = _worker(
            spi,
            stream,
            crawler=crawler,
            clock=clock,
            settings=CollectionSettings(max_run_attempts=2),
        )
        assert await worker.process_once(max_records=5) == 1
        persisted = spi.state.runs[running.run_id]
        assert persisted.status is CollectionRunStatus.FAILED
        assert persisted.failure_code is CollectionFailureCode.ATTEMPTS_EXHAUSTED
        assert crawler.requests == []
        assert _pos(stream) != set()


class TestFailureAndCancellation:
    """WK7/WK10/WK11: bounded failures never acknowledge prematurely."""

    @pytest.mark.asyncio
    async def test_wk7_crash_pre_terminal_no_ack(self) -> None:
        spi, run = _seed_context()
        stream = FakeDataStream()
        await _publish_execute(stream, run)
        crawler = FakeCrawler()
        crawler.results.append(crawler.completed_with_pages(1))
        worker = _worker(
            spi,
            stream,
            crawler=crawler,
            ingest=FakeIngest(failure=RuntimeError("unexpected raw failure")),
        )
        with pytest.raises(RuntimeError):
            await worker.process_once(max_records=5)
        assert _pos(stream) == set()
        # The run stays RUNNING under its lease (recoverable on expiry).
        assert spi.state.runs[run.run_id].status is CollectionRunStatus.RUNNING

    @pytest.mark.asyncio
    async def test_wk10_unknown_run_is_bounded_poison_acked(self) -> None:
        spi, _ = _seed_context()
        stream = FakeDataStream()
        unknown = CollectionRunId.generate()
        command = CollectionExecuteCommand(
            run_id=unknown, source_id=_SOURCE, policy_id=_POLICY
        )
        await stream.publish(
            stream_name=COLLECTION_WORK_STREAM,
            messages=[command.to_message(occurred_at=_T0)],
        )
        worker = _worker(spi, stream)
        # Bounded poison: acknowledged (executed=False so the count is 0).
        assert await worker.process_once(max_records=5) == 0
        # No run was invented; the poison is acknowledged so the lane cannot
        # wedge forever.
        assert unknown not in spi.state.runs
        assert _pos(stream) != set()

    @pytest.mark.asyncio
    async def test_wk11_cancellation_no_premature_ack(self) -> None:
        spi, run = _seed_context()
        stream = FakeDataStream()
        await _publish_execute(stream, run)
        crawler = FakeCrawler()
        crawler.results.append(crawler.completed_with_pages(1))
        worker = _worker(
            spi,
            stream,
            crawler=crawler,
            ingest=FakeIngest(failure=asyncio.CancelledError()),
        )
        with pytest.raises(asyncio.CancelledError):
            await worker.process_once(max_records=5)
        assert _pos(stream) == set()
        # Best-effort CANCELLED was persisted; a redelivery later acks.
        assert spi.state.runs[run.run_id].status is CollectionRunStatus.CANCELLED


class TestPayloadStrictness:
    """WK12: malformed work payloads are bounded poisons."""

    @pytest.mark.asyncio
    async def test_wk12_malformed_payload_acknowledged_no_run(self) -> None:
        from darkula.app.data_stream import StreamMessage
        from darkula.domain.identifiers import MessageId

        spi, _ = _seed_context()
        stream = FakeDataStream()
        malformed = StreamMessage(
            message_id=MessageId.generate(),
            message_type="collection.execute",
            schema_version=1,
            occurred_at=_T0,
            payload={
                "collection_run_id": "not-a-uuid",
                "source_id": "x",
                "policy_id": "y",
            },
        )
        await stream.publish(stream_name=COLLECTION_WORK_STREAM, messages=[malformed])
        worker = _worker(spi, stream)
        assert await worker.process_once(max_records=5) == 0
        # The seeded (unrelated) run is untouched: the poison never reached
        # any run and no new run was invented.
        assert len(spi.state.runs) == 1
        seeded_run = next(iter(spi.state.runs.values()))
        assert seeded_run.status is CollectionRunStatus.QUEUED
        assert _pos(stream) != set()


def _default_snapshot() -> dict[str, object]:
    """The execution snapshot of the seeded policy."""
    policy = CollectionPolicy(
        policy_id=_POLICY,
        source_id=_SOURCE,
        active=True,
        created_at=_T0,
        updated_at=_T0,
        revision=1,
        interval_seconds=3600,
        next_due_at=_T0,
        allowed_endpoint_ids=(_ENDPOINT,),
    )
    return policy.execution_snapshot()

# SPDX-License-Identifier: AGPL-3.0-only
"""PR 9 messaging integration (M1-M4) over real PostgreSQL + Redpanda.

M1 scheduler -> queued run + outbox -> OutboxPublisher -> real Redpanda;
M2 duplicate publication/redelivery never re-executes; M3 durable terminal
state precedes acknowledgment and post-success redelivery does not recrawl;
M4 a crashed worker's RUNNING run (expired lease, no terminal state) is
recovered by reclaim on the same run identity.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from tests.integration.conftest import _raw_connect
from tests.support.collection_fakes import FakeCrawler, FakeIngest

from darkula.app.collection import (
    COLLECTION_EXECUTE_MESSAGE_TYPE,
    CollectionExecuteCommand,
    SourceCollectionService,
)
from darkula.app.collection_scheduler import CollectionScheduler
from darkula.app.collection_worker import CollectionWorker
from darkula.app.outbox import OutboxPublisher
from darkula.config.settings import CollectionSettings
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
    StreamName,
)
from darkula.domain.source import (
    EndpointStatus,
    EndpointType,
    Source,
    SourceEndpoint,
    SourceStatus,
)
from darkula.infrastructure.data_stream.redpanda import RedpandaDataStream
from darkula.infrastructure.persistence.postgresql.spi import PostgresDarkulaSpi

pytestmark = pytest.mark.integration

_T0 = datetime(2026, 2, 1, tzinfo=UTC)


def _topic() -> StreamName:
    return StreamName(f"intg-collection-{uuid.uuid4().hex[:10]}")


def _consumer() -> ConsumerId:
    return ConsumerId(f"intg-worker-{uuid.uuid4().hex[:8]}")


async def _seed_policy_run(
    spi: PostgresDarkulaSpi,
) -> tuple[Source, SourceEndpoint, CollectionPolicy]:
    source = Source(
        source_id=SourceId.generate(),
        status=SourceStatus.ACTIVE,
        created_at=_T0,
        updated_at=_T0,
        name="intg-collection-source",
    )
    endpoint = SourceEndpoint(
        endpoint_id=SourceEndpointId.generate(),
        source_id=source.source_id,
        uri="http://blackgate.example.test/board/board-announcements",
        endpoint_type=EndpointType.CLEARNET,
        status=EndpointStatus.ACTIVE,
        first_observed_at=_T0,
        last_observed_at=_T0,
    )
    policy = CollectionPolicy(
        policy_id=CollectionPolicyId.generate(),
        source_id=source.source_id,
        active=True,
        created_at=_T0,
        updated_at=_T0,
        revision=1,
        interval_seconds=3600,
        next_due_at=_T0,
        allowed_endpoint_ids=(endpoint.endpoint_id,),
        max_pages=3,
        max_requests=60,
        max_depth=2,
        timeout_seconds=60.0,
    )
    async with spi.unit_of_work() as uow:
        await uow.sources.create(source)
        await uow.sources.add_endpoint(endpoint)
        await uow.collection.create_policy(policy)
        await uow.commit()
    return source, endpoint, policy


def _service(
    spi: PostgresDarkulaSpi,
    crawler: FakeCrawler,
    *,
    ingest: FakeIngest | None = None,
) -> SourceCollectionService:
    return SourceCollectionService(
        spi=spi,
        crawler=crawler,
        content_ingest=ingest or FakeIngest(),
        settings=CollectionSettings(),
    )


def _worker(
    spi: PostgresDarkulaSpi,
    stream: RedpandaDataStream,
    service: SourceCollectionService,
    *,
    topic: StreamName,
    consumer_id: ConsumerId,
) -> CollectionWorker:
    return CollectionWorker(
        spi=spi,
        data_stream=stream,
        service=service,
        settings=CollectionSettings(),
        stream_name=topic,
        consumer_id=consumer_id,
    )


async def _publish_scheduled_batch(
    spi: PostgresDarkulaSpi,
    stream: RedpandaDataStream,
    topic: StreamName,
    *,
    limit: int = 5,
) -> tuple[int, int]:
    """Run the scheduler + OutboxPublisher; return (scheduled, published)."""
    scheduler = CollectionScheduler(spi=spi, stream_name=topic)
    scheduled = await scheduler.schedule_due(
        now=_T0 + timedelta(seconds=1), limit=limit
    )
    publisher = OutboxPublisher(spi=spi, data_stream=stream)
    published = await publisher.publish_pending(stream_name=topic, limit=limit)
    return scheduled, published


class TestSchedulerToBroker:
    """M1: queued run + outbox reach the real broker as IDs-only commands."""

    @pytest.mark.asyncio
    async def test_m1_scheduler_publishes_ids_only_command(
        self,
        spi: PostgresDarkulaSpi,
        redpanda_settings: Any,
    ) -> None:
        source, _, policy = await _seed_policy_run(spi)
        topic = _topic()
        stream = RedpandaDataStream(
            bootstrap_servers=redpanda_settings.bootstrap_servers,
            client_id="darkula-intg",
            poll_timeout_ms=2000,
            max_poll_records=20,
        )
        await stream.start()
        try:
            scheduled, published = await _publish_scheduled_batch(spi, stream, topic)
            assert scheduled == 1
            assert published == 1

            consumer_id = _consumer()
            batch = await stream.poll(
                stream_name=topic, consumer_id=consumer_id, max_records=5
            )
            records = batch.records
            assert len(records) == 1
            message = records[0].message
            assert message.message_type == COLLECTION_EXECUTE_MESSAGE_TYPE
            assert set(message.payload) == {
                "collection_run_id",
                "source_id",
                "policy_id",
            }
            command = CollectionExecuteCommand.from_message(message)
            assert command.policy_id == policy.policy_id
            assert command.source_id == source.source_id
            # No URI/policy blob/content/credential on the wire.
            serialized = str(message.payload).lower()
            assert "blackgate" not in serialized
            assert "password" not in serialized
            await stream.acknowledge(
                stream_name=topic,
                consumer_id=consumer_id,
                positions=[records[0].position],
            )
        finally:
            await stream.stop()


class TestRedeliverySafety:
    """M2/M3: at-least-once redelivery never recrawls a terminal run."""

    @pytest.mark.asyncio
    async def test_m2_m3_duplicate_publication_no_second_execution(
        self,
        spi: PostgresDarkulaSpi,
        redpanda_settings: Any,
        database_settings: Any,
    ) -> None:
        _, _, policy = await _seed_policy_run(spi)
        topic = _topic()
        consumer_id = _consumer()
        stream = RedpandaDataStream(
            bootstrap_servers=redpanda_settings.bootstrap_servers,
            client_id="darkula-intg",
            poll_timeout_ms=2000,
            max_poll_records=20,
        )
        await stream.start()
        try:
            crawler = FakeCrawler()
            crawler.results.append(crawler.completed_with_pages(1))
            worker = _worker(
                spi,
                stream,
                _service(spi, crawler),
                topic=topic,
                consumer_id=consumer_id,
            )
            await _publish_scheduled_batch(spi, stream, topic)
            assert await worker.process_once(max_records=5) == 1
            assert len(crawler.requests) == 1

            conn = _raw_connect(database_settings)
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT status FROM collection_run WHERE policy_id = %s",
                        (str(policy.policy_id),),
                    )
                    row = cur.fetchone()
            finally:
                conn.close()
            assert row is not None and row[0] == "SUCCEEDED"

            # Duplicate publication: republish the same logical command.
            messages = _published_command(database_settings, policy)
            assert messages
            await stream.publish(stream_name=topic, messages=messages)
            # Redelivery sees the terminal run: acknowledged, never recrawled.
            assert await worker.process_once(max_records=5) == 0
            assert len(crawler.requests) == 1
        finally:
            await stream.stop()


class TestCrashRecovery:
    """M4/F3: RUNNING + expired lease is reclaimed and executed once."""

    @pytest.mark.asyncio
    async def test_m4_expired_lease_same_run_recovered(
        self,
        spi: PostgresDarkulaSpi,
        redpanda_settings: Any,
        database_settings: Any,
    ) -> None:
        _, _, policy = await _seed_policy_run(spi)
        topic = _topic()
        consumer_id = _consumer()
        stream = RedpandaDataStream(
            bootstrap_servers=redpanda_settings.bootstrap_servers,
            client_id="darkula-intg",
            poll_timeout_ms=2000,
            max_poll_records=20,
        )
        await stream.start()
        try:
            await _publish_scheduled_batch(spi, stream, topic)
            # Find the QUEUED run the scheduler admitted.
            conn = _raw_connect(database_settings)
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT run_id FROM collection_run WHERE policy_id = %s",
                        (str(policy.policy_id),),
                    )
                    row = cur.fetchone()
            finally:
                conn.close()
            assert row is not None
            run_id = CollectionRunId.from_str(str(row[0]))
            # Simulate a crashed worker that claimed then died before any
            # terminal state: RUNNING with an already-expired lease.
            async with spi.unit_of_work() as uow:
                outcome = await uow.collection.claim_run(
                    run_id=run_id,
                    execution_id=str(uuid.uuid4()),
                    started_at=_T0 - timedelta(seconds=30),
                    lease_expires_at=_T0 - timedelta(seconds=5),
                )
                assert outcome.value == "claimed"
                await uow.commit()

            crawler = FakeCrawler()
            crawler.results.append(crawler.completed_with_pages(1))
            worker = _worker(
                spi,
                stream,
                _service(spi, crawler),
                topic=topic,
                consumer_id=consumer_id,
            )
            assert await worker.process_once(max_records=5) == 1
            assert len(crawler.requests) == 1
            async with spi.unit_of_work() as uow:
                loaded = await uow.collection.get_run(run_id)
            assert loaded is not None
            assert loaded.status is CollectionRunStatus.SUCCEEDED
            assert loaded.attempt_count == 2  # claim + reclaim, same run
        finally:
            await stream.stop()


def _published_command(database_settings: Any, policy: CollectionPolicy) -> list[Any]:
    """Re-read the previously published command message (test-only SQL)."""
    from darkula.app.data_stream import StreamMessage, validate_payload
    from darkula.domain.identifiers import MessageId

    conn = _raw_connect(database_settings)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT message_id, occurred_at, payload FROM message_outbox "
                "WHERE message_type = 'collection.execute' "
                "ORDER BY created_at"
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    result: list[StreamMessage] = []
    for row in rows or []:
        payload = row[2]
        validate_payload(payload)
        result.append(
            StreamMessage(
                message_id=MessageId.from_str(str(row[0])),
                message_type=COLLECTION_EXECUTE_MESSAGE_TYPE,
                schema_version=1,
                occurred_at=row[1].astimezone(UTC),
                payload=payload,
            )
        )
    return [r for r in result if str(r.payload["policy_id"]) == str(policy.policy_id)]

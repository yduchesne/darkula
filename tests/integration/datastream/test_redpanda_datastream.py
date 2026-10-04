# SPDX-License-Identifier: AGPL-3.0-only
"""V1: real Redpanda DataStream vertical slice.

``start -> publish -> poll -> verify -> acknowledge -> poll again`` against
the Darkula-owned Redpanda at 39092:9092. Asserts message semantics,
explicit acknowledgement, empty re-poll after acknowledgement, and
independent consumer groups. Topics are per-test uuids so Redis/topic
state never leaks between tests.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio

from darkula.app.data_stream import (
    MessageBatch,
    StreamMessage,
    StreamPosition,
)
from darkula.config.settings import DataStreamSettings
from darkula.domain.identifiers import ConsumerId, MessageId, StreamName
from darkula.infrastructure.data_stream.redpanda import RedpandaDataStream

pytestmark = pytest.mark.integration


def _message(tag: str) -> StreamMessage:
    return StreamMessage(
        message_id=MessageId.generate(),
        message_type="intg.probe",
        schema_version=1,
        occurred_at=datetime.now(UTC),
        payload={"tag": tag, "seq": 1},
        routing_key=f"rk-{tag}",
    )


@pytest_asyncio.fixture
async def stream(
    redpanda_settings: DataStreamSettings,
) -> AsyncIterator[RedpandaDataStream]:
    """One started RedpandaDataStream per test."""
    instance = RedpandaDataStream(
        bootstrap_servers=redpanda_settings.bootstrap_servers,
        client_id="darkula-intg-datastream",
        poll_timeout_ms=redpanda_settings.poll_timeout_ms,
        max_poll_records=redpanda_settings.max_poll_records,
    )
    await instance.start()
    try:
        yield instance
    finally:
        await instance.stop()


def _fresh_stream() -> StreamName:
    return StreamName(f"intg-v1-{uuid4().hex[:12]}")


class TestRealRedpandaDataStream:
    @pytest.mark.asyncio
    async def test_publish_poll_acknowledge_poll_again(
        self, stream: RedpandaDataStream
    ) -> None:
        topic = _fresh_stream()
        consumer = ConsumerId(f"intg-a-{uuid4().hex[:8]}")
        first = _message("first")
        second = _message("second")

        result = await stream.publish(stream_name=topic, messages=[first, second])
        assert result.message_count == 2

        batch = await stream.poll(
            stream_name=topic, consumer_id=consumer, max_records=10
        )
        assert isinstance(batch, MessageBatch)
        assert len(batch.records) == 2
        # deterministic partition/offset ordering in the single-partition topic
        assert [r.position for r in batch.records] == [
            StreamPosition(lane="0", offset=0),
            StreamPosition(lane="0", offset=1),
        ]
        assert [r.message for r in batch.records] == [first, second]

        await stream.acknowledge(
            stream_name=topic,
            consumer_id=consumer,
            positions=[r.position for r in batch.records],
        )

        # polling never acknowledges: the second poll sees nothing already acked
        again = await stream.poll(
            stream_name=topic, consumer_id=consumer, max_records=10
        )
        assert again.records == ()

    @pytest.mark.asyncio
    async def test_independent_consumer_groups(
        self, stream: RedpandaDataStream
    ) -> None:
        topic = _fresh_stream()
        message = _message("shared")
        await stream.publish(stream_name=topic, messages=[message])

        consumer_a = ConsumerId(f"intg-a-{uuid4().hex[:8]}")
        consumer_b = ConsumerId(f"intg-b-{uuid4().hex[:8]}")

        batch_a = await stream.poll(
            stream_name=topic, consumer_id=consumer_a, max_records=5
        )
        assert len(batch_a.records) == 1
        await stream.acknowledge(
            stream_name=topic,
            consumer_id=consumer_a,
            positions=[r.position for r in batch_a.records],
        )

        # consumer B belongs to a different group: it still sees the message
        batch_b = await stream.poll(
            stream_name=topic, consumer_id=consumer_b, max_records=5
        )
        assert [r.message.message_id for r in batch_b.records] == [message.message_id]

    @pytest.mark.asyncio
    async def test_message_semantics_round_trip(
        self, stream: RedpandaDataStream
    ) -> None:
        topic = _fresh_stream()
        consumer = ConsumerId(f"intg-c-{uuid4().hex[:8]}")
        message = _message("semantics")
        await stream.publish(stream_name=topic, messages=[message])
        batch = await stream.poll(
            stream_name=topic, consumer_id=consumer, max_records=5
        )
        assert len(batch.records) == 1
        decoded = batch.records[0].message
        assert decoded == message
        assert decoded.payload == message.payload
        assert decoded.occurred_at == message.occurred_at

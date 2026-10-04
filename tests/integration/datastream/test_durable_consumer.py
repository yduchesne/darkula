# SPDX-License-Identifier: AGPL-3.0-only
"""V3: duplicate durable processing against real PostgreSQL + Redpanda.

Delivers the same logical message more than once (including at a different
broker position) and asserts exactly one durable effect for one consumer
identity, with effect + processed marker + outgoing outbox committed
atomically through the reliable consumer primitive.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import psycopg
import pytest

from darkula.app.consumer import ReliableConsumer, ReliableMessageHandler
from darkula.app.data_stream import StreamMessage
from darkula.app.persistence import UnitOfWork
from darkula.domain.identifiers import ConsumerId, MessageId, StreamName
from darkula.infrastructure.data_stream.redpanda import RedpandaDataStream
from darkula.infrastructure.persistence.postgresql.spi import PostgresDarkulaSpi

pytestmark = pytest.mark.integration

_OUTGOING = "intg.consumer_effect"


def _message() -> StreamMessage:
    return StreamMessage(
        message_id=MessageId.generate(),
        message_type="intg.effect_input",
        schema_version=1,
        occurred_at=datetime.now(UTC),
        payload={"n": 1},
    )


class _OutgoingOutboxHandler(ReliableMessageHandler):
    """Durable effect: append one outgoing outbox record inside the UoW."""

    async def handle(self, message: StreamMessage, uow: UnitOfWork) -> None:
        await uow.outbox.append(
            StreamMessage(
                message_id=MessageId.generate(),
                message_type=_OUTGOING,
                schema_version=1,
                occurred_at=datetime.now(UTC),
                payload={"caused_by": str(message.message_id)},
            ),
            stream_name=StreamName(f"intg-outgoing-{uuid4().hex[:8]}"),
        )


def _outgoing_outbox_count(database_settings: Any) -> int:
    """Test-only SQL: count durable handler effects."""
    conn = psycopg.connect(database_settings.conninfo())
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM message_outbox WHERE message_type = %s",
                (_OUTGOING,),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    assert row is not None
    return int(row[0])


class TestDuplicateDurableProcessing:
    @pytest.mark.asyncio
    async def test_one_durable_effect_across_duplicate_delivery(
        self,
        spi: PostgresDarkulaSpi,
        database_settings: Any,
        redpanda_settings: Any,
    ) -> None:
        topic = StreamName(f"intg-v3-{uuid4().hex[:12]}")
        consumer_id = ConsumerId(f"intg-v3-{uuid4().hex[:8]}")
        message = _message()

        stream = RedpandaDataStream(
            bootstrap_servers=redpanda_settings.bootstrap_servers,
            client_id="darkula-intg-consumer",
            poll_timeout_ms=redpanda_settings.poll_timeout_ms,
            max_poll_records=redpanda_settings.max_poll_records,
        )
        await stream.start()
        try:
            consumer = ReliableConsumer(
                spi=spi,
                data_stream=stream,
                stream_name=topic,
                consumer_id=consumer_id,
                handler=_OutgoingOutboxHandler(),
            )

            # First delivery: effect + marker + outgoing outbox atomically.
            await stream.publish(stream_name=topic, messages=[message])
            assert await consumer.process_once(max_records=5) == 1
            assert _outgoing_outbox_count(database_settings) == 1

            # Duplicate delivery of the same logical message (different
            # broker position): the durable effect must not repeat.
            await stream.publish(stream_name=topic, messages=[message])
            second = await consumer.process_once(max_records=5)
            assert second == 0
            assert _outgoing_outbox_count(database_settings) == 1

            # The processed marker exists for this consumer+message.
            assert (
                await _processed_count(
                    database_settings, topic, consumer_id, message.message_id
                )
                == 1
            )
        finally:
            await stream.stop()


async def _processed_count(
    database_settings: Any,
    topic: StreamName,
    consumer_id: ConsumerId,
    message_id: MessageId,
) -> int:
    """Test-only SQL: return processed-marker count for the identity."""
    conn = psycopg.connect(database_settings.conninfo())
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM processed_message "
                "WHERE stream_name = %s AND consumer_id = %s AND message_id = %s",
                (topic.value, consumer_id.value, str(message_id)),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    assert row is not None
    return int(row[0])

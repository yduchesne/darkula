# SPDX-License-Identifier: AGPL-3.0-only
"""V2: real PostgreSQL outbox -> real Redpanda vertical slice.

Appends an outbox row through the production repository/stored function in
one unit of work, publishes via the OutboxPublisher to real Redpanda,
polls with the production adapter, and asserts the same message_id arrives
and ``published_at`` is only set after publication.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import psycopg
import pytest

from darkula.app.data_stream import StreamMessage
from darkula.app.outbox import OutboxPublisher
from darkula.domain.identifiers import ConsumerId, MessageId, StreamName
from darkula.infrastructure.data_stream.redpanda import RedpandaDataStream
from darkula.infrastructure.persistence.postgresql.spi import PostgresDarkulaSpi

pytestmark = pytest.mark.integration


def _message() -> StreamMessage:
    return StreamMessage(
        message_id=MessageId.generate(),
        message_type="intg.outbox_event",
        schema_version=1,
        occurred_at=datetime.now(UTC),
        payload={"source": "intg-v2"},
        routing_key="lane-a",
    )


def _outbox_published_at(database_settings: Any, message_id: MessageId) -> bool:
    """Test-only SQL: return whether the outbox row is marked published."""
    conn = psycopg.connect(database_settings.conninfo())
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT published_at IS NOT NULL FROM message_outbox "
                "WHERE message_id = %s",
                (str(message_id),),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    assert row is not None, "expected an outbox row for this message"
    return bool(row[0])


class TestOutboxToRedpanda:
    @pytest.mark.asyncio
    async def test_outbox_publish_to_real_redpanda(
        self,
        spi: PostgresDarkulaSpi,
        database_settings: Any,
        redpanda_settings: Any,
    ) -> None:
        topic = StreamName(f"intg-v2-{uuid4().hex[:12]}")
        consumer = ConsumerId(f"intg-v2-{uuid4().hex[:8]}")
        message = _message()

        # 1. State+event atomicity: append the outbox row in one UoW.
        async with spi.unit_of_work() as uow:
            await uow.outbox.append(message, stream_name=topic)
            await uow.commit()
        assert _outbox_published_at(database_settings, message.message_id) is False

        # 2. Publish through the production publisher -> real Redpanda.
        stream = RedpandaDataStream(
            bootstrap_servers=redpanda_settings.bootstrap_servers,
            client_id="darkula-intg-outbox",
            poll_timeout_ms=redpanda_settings.poll_timeout_ms,
            max_poll_records=redpanda_settings.max_poll_records,
        )
        await stream.start()
        try:
            publisher = OutboxPublisher(spi=spi, data_stream=stream)
            marked = await publisher.publish_pending(stream_name=topic, limit=10)
            assert marked == 1
            assert _outbox_published_at(database_settings, message.message_id) is True

            # 3. Production poll sees the same logical message.
            batch = await stream.poll(
                stream_name=topic, consumer_id=consumer, max_records=10
            )
            assert len(batch.records) == 1
            assert batch.records[0].message.message_id == message.message_id
            assert batch.records[0].message == message
        finally:
            await stream.stop()

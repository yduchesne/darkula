# SPDX-License-Identifier: AGPL-3.0-only
"""Smallest reusable reliable consumer-processing primitive (PR 5).

One :class:`ReliableConsumer` wraps the exact ordering required for
at-least-once delivery with durable idempotency:

.. code-block:: text

    poll
     -> open UoW
     -> already processed (stream_name, consumer_id, message_id)?
          yes: no business effect, no outgoing outbox
          no:  handler durable work + outgoing outbox + processed marker
     -> commit
     -> acknowledge broker positions

Frozen properties:

- **acknowledgement happens only after a durable PostgreSQL commit**;
  polling never acknowledges (the ``DataStream`` contract);
- the processed-marker and the business effect commit atomically in one
  unit of work, so a duplicate delivery after a crash can never repeat the
  durable effect and can never emit a duplicate outgoing outbox record
  (``message_id`` UNIQUE);
- idempotency is keyed by the stable ``message_id``, never by a broker
  ``StreamPosition`` (redelivery may carry a different position);
- handler failure rolls back the whole batch: no marker, no commit, no ack
  -- records are redelivered; a handler that permanently fails blocks its
  lane (quarantine/DLQ is explicitly out of PR 5 scope);
- ``asyncio.CancelledError`` propagates unchanged; the open unit of work
  rolls back and nothing is acknowledged;
- iteration is sequential by design (no unbounded async fan-out); the
  caller owns the retry cadence (there is no hidden retry loop here).

``handler`` is the application seam: it performs the durable business
effect inside the shared unit of work and may append outgoing outbox
records through ``uow.outbox``. No crawler/recon/extraction handlers exist
in PR 5; real business consumers arrive with their feature PRs.
"""

from __future__ import annotations

import time
from typing import Protocol

from darkula.app.data_stream import DataStream, StreamMessage
from darkula.app.persistence import DarkulaSpi, UnitOfWork
from darkula.domain.identifiers import ConsumerId, StreamName
from darkula.telemetry.metrics import get_counter, get_histogram
from darkula.telemetry.tracing import get_tracer

#: Metric names (bounded; never message/correlation IDs or payloads).
PROCESS_COUNT = "darkula.consumer.process.count"
PROCESS_DURATION = "darkula.consumer.process.duration"
DUPLICATE_COUNT = "darkula.consumer.process.duplicate"
PROCESS_SPAN = "consumer.process"

_STREAM_ATTR = "darkula.stream"
_CONSUMER_ATTR = "darkula.consumer"


class ReliableMessageHandler(Protocol):
    """Application seam invoked inside the consumer's shared unit of work.

    Implementations perform durable work (via ``uow`` repositories) and may
    append outgoing outbox records through ``uow.outbox``; everything they
    write commits atomically with the processed-message marker.
    """

    async def handle(self, message: StreamMessage, uow: UnitOfWork) -> None:
        """Handle one newly-delivered message inside the shared UoW."""


class ReliableConsumer:
    """Poll -> durably process -> commit -> acknowledge, per batch."""

    def __init__(
        self,
        *,
        spi: DarkulaSpi,
        data_stream: DataStream,
        stream_name: StreamName,
        consumer_id: ConsumerId,
        handler: ReliableMessageHandler,
    ) -> None:
        """Bind one consumer identity to one stream and handler."""
        self._spi = spi
        self._data_stream = data_stream
        self._stream_name = stream_name
        self._consumer_id = consumer_id
        self._handler = handler

    async def process_once(self, *, max_records: int) -> int:
        """Process one bounded poll batch; return newly-handled count.

        Polls up to ``max_records`` records, runs the handler for each
        first-time delivery inside one unit of work, commits, and only then
        acknowledges the broker positions. Duplicates already processed by
        this consumer are committed as no-ops (no effect, no outgoing
        outbox) and counted separately.
        """
        if max_records < 1:
            raise ValueError("max_records must be >= 1")
        attributes = {
            _STREAM_ATTR: self._stream_name.value,
            _CONSUMER_ATTR: self._consumer_id.value,
        }
        get_counter(PROCESS_COUNT).add(1, attributes)
        start_ns = time.perf_counter_ns()
        tracer = get_tracer()
        try:
            with tracer.start_as_current_span(PROCESS_SPAN, attributes=attributes):
                batch = await self._data_stream.poll(
                    stream_name=self._stream_name,
                    consumer_id=self._consumer_id,
                    max_records=max_records,
                )
                if not batch.records:
                    return 0
                new_effects = 0
                duplicates = 0
                async with self._spi.unit_of_work() as uow:
                    for record in batch.records:
                        first = await uow.processed_messages.record(
                            stream_name=self._stream_name,
                            consumer_id=self._consumer_id,
                            message_id=record.message.message_id,
                        )
                        if not first:
                            duplicates += 1
                            continue
                        await self._handler.handle(record.message, uow)
                        new_effects += 1
                    # Business effects + processed markers + any outgoing
                    # outbox rows commit atomically here.
                    await uow.commit()
                if duplicates:
                    get_counter(DUPLICATE_COUNT).add(duplicates, attributes)
                await self._data_stream.acknowledge(
                    stream_name=self._stream_name,
                    consumer_id=self._consumer_id,
                    positions=[record.position for record in batch.records],
                )
                return new_effects
        finally:
            get_histogram(PROCESS_DURATION).record(
                (time.perf_counter_ns() - start_ns) / 1e9,
                attributes=attributes,
            )


__all__ = ["ReliableConsumer", "ReliableMessageHandler"]

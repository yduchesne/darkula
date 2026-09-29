# SPDX-License-Identifier: AGPL-3.0-only
"""Canonical deterministic FakeDataStream (PR 3).

Implements the existing :class:`~darkula.app.data_stream.DataStream` contract
without any broker/provider concept:

- publish records by :class:`StreamName`, using each message's routing key
  as the lane or a deterministic default lane;
- positions are per-lane monotonic offsets (never message identity);
- polling never acknowledges; acknowledgement is explicit and per consumer,
  stream, and position only;
- repeated acknowledgement is deterministic/idempotent;
- consumers are isolated (one consumer's ack never affects another's view);
- streams are isolated (state is keyed by ``StreamName``);
- optional scripted failures for publish/poll/acknowledge propagate the
  exact scripted exception unchanged (including ``asyncio.CancelledError``);
- :meth:`reset_consumer` is the smallest explicit replay/duplicate seam for
  reliability tests.

The fake is a test double for app-level semantics; it is never a
replacement for the production Redpanda adapter (PR 5).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from darkula.app.data_stream import (
    DataStream,
    MessageBatch,
    PublishResult,
    StreamMessage,
    StreamPosition,
    StreamRecord,
)
from darkula.domain.identifiers import ConsumerId, StreamName

#: Deterministic lane used whenever a published message has no routing key.
DEFAULT_LANE = "default"


@dataclass(slots=True)
class _StreamState:
    """Per-stream records, per-lane offsets, and per-consumer acks."""

    records: list[StreamRecord] = field(default_factory=list)
    offsets: dict[str, int] = field(default_factory=dict)
    acknowledged: dict[ConsumerId, set[StreamPosition]] = field(default_factory=dict)


class FakeDataStream(DataStream):
    """Deterministic, offline, inspectable DataStream test double."""

    def __init__(self) -> None:
        """Start with no streams, no lifecycle, and no scripted failures."""
        self._streams: dict[StreamName, _StreamState] = {}
        self._started = False
        self.publish_failure: BaseException | None = None
        self.poll_failure: BaseException | None = None
        self.acknowledge_failure: BaseException | None = None

    @property
    def started(self) -> bool:
        """Return whether the lifecycle ``start`` has been called."""
        return self._started

    async def start(self) -> None:
        """Start the stream lifecycle."""
        self._started = True

    async def stop(self) -> None:
        """Stop the stream lifecycle."""
        self._started = False

    def _state(self, stream_name: StreamName) -> _StreamState:
        """Return the per-stream state, creating it deterministically."""
        return self._streams.setdefault(stream_name, _StreamState())

    async def _publish(
        self,
        *,
        stream_name: StreamName,
        messages: Sequence[StreamMessage],
    ) -> PublishResult:
        """Record messages under per-lane monotonic offsets."""
        if self.publish_failure is not None:
            raise self.publish_failure
        state = self._state(stream_name)
        for message in messages:
            lane = message.routing_key or DEFAULT_LANE
            offset = state.offsets.get(lane, 0)
            state.records.append(
                StreamRecord(
                    message=message,
                    position=StreamPosition(lane=lane, offset=offset),
                )
            )
            state.offsets[lane] = offset + 1
        return PublishResult(message_count=len(messages))

    async def _poll(
        self,
        *,
        stream_name: StreamName,
        consumer_id: ConsumerId,
        max_records: int,
    ) -> MessageBatch:
        """Return up to ``max_records`` unacknowledged records; never acks."""
        if self.poll_failure is not None:
            raise self.poll_failure
        state = self._state(stream_name)
        pending = [
            record
            for record in state.records
            if record.position not in state.acknowledged.get(consumer_id, frozenset())
        ][:max_records]
        return MessageBatch(records=tuple(pending))

    async def _acknowledge(
        self,
        *,
        stream_name: StreamName,
        consumer_id: ConsumerId,
        positions: Sequence[StreamPosition],
    ) -> None:
        """Acknowledge positions for one consumer, idempotently."""
        if self.acknowledge_failure is not None:
            raise self.acknowledge_failure
        state = self._state(stream_name)
        state.acknowledged.setdefault(consumer_id, set()).update(positions)

    def published_messages(self, stream_name: StreamName) -> tuple[StreamMessage, ...]:
        """Return every message published to ``stream_name``, in order."""
        return tuple(record.message for record in self._state(stream_name).records)

    def acknowledged_positions(
        self,
        stream_name: StreamName,
        consumer_id: ConsumerId,
    ) -> frozenset[StreamPosition]:
        """Return the acknowledged positions for one consumer/stream."""
        return frozenset(
            self._state(stream_name).acknowledged.get(consumer_id, frozenset())
        )

    def pending_records(
        self,
        stream_name: StreamName,
        consumer_id: ConsumerId,
    ) -> tuple[StreamRecord, ...]:
        """Return records that would be delivered by the next poll."""
        acknowledged = self.acknowledged_positions(stream_name, consumer_id)
        return tuple(
            record
            for record in self._state(stream_name).records
            if record.position not in acknowledged
        )

    def reset_consumer(self, stream_name: StreamName, consumer_id: ConsumerId) -> None:
        """Replay seam: forget every ack for one consumer on one stream.

        The consumer's next poll re-delivers all previously acknowledged
        records, simulating duplicate delivery after a crash/restart.
        """
        state = self._state(stream_name)
        state.acknowledged.pop(consumer_id, None)


__all__ = ["DEFAULT_LANE", "FakeDataStream"]

# SPDX-License-Identifier: AGPL-3.0-only
"""Reliable consumer primitive tests (C1-C10).

A deterministic fake UnitOfWork applies processed markers / outgoing
outbox rows / handler effects to shared state only on commit, and the
canonical FakeDataStream simulates redelivery (including different stream
positions for the same message_id). No database or broker is required.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Self

import pytest

from darkula.app.consumer import ReliableConsumer, ReliableMessageHandler
from darkula.app.data_stream import StreamMessage
from darkula.app.persistence import DarkulaSpi, UnitOfWork
from darkula.domain.identifiers import ConsumerId, MessageId, StreamName
from darkula.testing.fake_data_stream import FakeDataStream

_STREAM = StreamName("events")
_CONSUMER_A = ConsumerId("consumer-a")
_CONSUMER_B = ConsumerId("consumer-b")
_MOMENT = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


@dataclass(slots=True)
class _Shared:
    """Committed fake persistence state (mutated only on commit)."""

    processed: set[tuple[str, str, str]] = field(default_factory=set)
    effects: list[str] = field(default_factory=list)
    outgoing_outbox: list[str] = field(default_factory=list)


def _message() -> StreamMessage:
    return StreamMessage(
        message_id=MessageId.generate(),
        message_type="crawl_requested",
        schema_version=1,
        occurred_at=_MOMENT,
        payload={"n": 1},
    )


def _same_message(message: StreamMessage) -> StreamMessage:
    """Return a message with the same identity but a fresh position later."""
    return StreamMessage(
        message_id=message.message_id,
        message_type=message.message_type,
        schema_version=message.schema_version,
        occurred_at=message.occurred_at,
        payload=dict(message.payload),
        routing_key=message.routing_key,
    )


class _FakeUoW(UnitOfWork):
    def __init__(self, shared: _Shared) -> None:
        self.shared = shared
        self._pending_processed: set[tuple[str, str, str]] = set()
        self._pending_effects: list[str] = []
        self._pending_outbox: list[str] = []
        self.commits = 0
        self.rollbacks = 0

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if exc_type is not None:
            self.rollbacks += 1
            self._pending_processed.clear()
            self._pending_effects.clear()
            self._pending_outbox.clear()

    async def commit(self) -> None:
        self.commits += 1
        self.shared.processed.update(self._pending_processed)
        self.shared.effects.extend(self._pending_effects)
        self.shared.outgoing_outbox.extend(self._pending_outbox)
        self._pending_processed.clear()
        self._pending_effects.clear()
        self._pending_outbox.clear()

    async def rollback(self) -> None:
        self.rollbacks += 1
        self._pending_processed.clear()
        self._pending_effects.clear()
        self._pending_outbox.clear()

    def record_effect(self, name: str) -> None:
        """Test seam: the handler records a durable effect in this UoW."""
        self._pending_effects.append(name)

    async def append_outgoing(self, message: StreamMessage) -> None:
        """Test seam: the handler appends an outgoing outbox record."""
        self._pending_outbox.append(str(message.message_id))

    @property
    def source_candidates(self) -> Any:
        raise AssertionError("consumer test exposes no candidate repository")

    @property
    def sources(self) -> Any:
        raise AssertionError("consumer test exposes no source repository")

    @property
    def outbox(self) -> Any:
        return self  # test seam: handlers use append_outgoing via this repo

    @property
    def processed_messages(self) -> Any:
        return self  # test seam: record() below

    @property
    def content(self) -> Any:
        raise AssertionError("consumer test exposes no content repository")

    async def record(
        self,
        *,
        stream_name: StreamName,
        consumer_id: ConsumerId,
        message_id: MessageId,
    ) -> bool:
        key = (stream_name.value, consumer_id.value, str(message_id))
        if key in self.shared.processed or key in self._pending_processed:
            return False
        self._pending_processed.add(key)
        return True


class _RecordingHandler(ReliableMessageHandler):
    """Handler that writes an effect + outgoing outbox through the UoW."""

    async def handle(self, message: StreamMessage, uow: UnitOfWork) -> None:
        uow_view = uow
        assert isinstance(uow_view, _FakeUoW)
        uow_view.record_effect(f"effect:{message.message_id}")
        await uow_view.append_outgoing(message)


class _FailingHandler(ReliableMessageHandler):
    """Handler that raises; used for failure/cancellation scenarios."""

    def __init__(self, failure: BaseException) -> None:
        self.failure = failure

    async def handle(self, message: StreamMessage, uow: UnitOfWork) -> None:
        raise self.failure


class _Spi(DarkulaSpi):
    def __init__(self, shared: _Shared) -> None:
        self.shared = shared

    def unit_of_work(self) -> UnitOfWork:
        return _FakeUoW(self.shared)


def _consumer(
    shared: _Shared,
    stream: FakeDataStream,
    consumer_id: ConsumerId = _CONSUMER_A,
    handler: ReliableMessageHandler | None = None,
) -> ReliableConsumer:
    return ReliableConsumer(
        spi=_Spi(shared),
        data_stream=stream,
        stream_name=_STREAM,
        consumer_id=consumer_id,
        handler=handler or _RecordingHandler(),
    )


class TestFirstDeliveryAndDuplicates:
    """C1/C2/C3/C10: effect+marker commit; duplicates are suppressed."""

    @pytest.mark.asyncio
    async def test_first_delivery_commits_effect_and_marker(self) -> None:
        shared = _Shared()
        stream = FakeDataStream()
        message = _message()
        await stream.publish(stream_name=_STREAM, messages=[message])
        consumer = _consumer(shared, stream)
        assert await consumer.process_once(max_records=5) == 1
        assert shared.effects == [f"effect:{message.message_id}"]
        assert shared.outgoing_outbox == [str(message.message_id)]
        assert (
            _STREAM.value,
            _CONSUMER_A.value,
            str(message.message_id),
        ) in shared.processed
        # ack happened after commit
        assert stream.acknowledged_positions(_STREAM, _CONSUMER_A) == {
            _position_of(stream)
        }

    @pytest.mark.asyncio
    async def test_duplicate_redelivery_has_no_repeated_effect(self) -> None:
        shared = _Shared()
        stream = FakeDataStream()
        message = _message()
        await stream.publish(stream_name=_STREAM, messages=[message])
        # redeliver the same logical message at a different position
        await stream.publish(stream_name=_STREAM, messages=[_same_message(message)])
        consumer = _consumer(shared, stream)
        assert await consumer.process_once(max_records=5) == 1
        # replay: forget acks, redeliver the same message_id again
        stream.reset_consumer(_STREAM, _CONSUMER_A)
        second = await consumer.process_once(max_records=5)
        assert second == 0
        assert shared.effects == [f"effect:{message.message_id}"]
        # the replayed records were acknowledged after a commit (no-op)
        assert len(shared.outgoing_outbox) == 1

    @pytest.mark.asyncio
    async def test_same_id_different_consumer_is_independently_processable(
        self,
    ) -> None:
        shared = _Shared()
        stream = FakeDataStream()
        message = _message()
        await stream.publish(stream_name=_STREAM, messages=[message])
        consumer_a = _consumer(shared, stream, _CONSUMER_A)
        consumer_b = _consumer(shared, stream, _CONSUMER_B)
        assert await consumer_a.process_once(max_records=5) == 1
        stream.reset_consumer(_STREAM, _CONSUMER_B)
        assert await consumer_b.process_once(max_records=5) == 1
        assert shared.effects == [
            f"effect:{message.message_id}",
            f"effect:{message.message_id}",
        ]

    @pytest.mark.asyncio
    async def test_duplicate_within_one_batch_is_suppressed(self) -> None:
        shared = _Shared()
        stream = FakeDataStream()
        message = _message()
        await stream.publish(stream_name=_STREAM, messages=[message])
        await stream.publish(stream_name=_STREAM, messages=[_same_message(message)])
        consumer = _consumer(shared, stream)
        assert await consumer.process_once(max_records=5) == 1
        assert shared.effects == [f"effect:{message.message_id}"]
        assert len(shared.outgoing_outbox) == 1


class TestHandlerFailure:
    """C4/C8: handler failure and cancellation roll back, never ack."""

    @pytest.mark.asyncio
    async def test_handler_failure_rolls_back_no_ack(self) -> None:
        shared = _Shared()
        stream = FakeDataStream()
        message = _message()
        await stream.publish(stream_name=_STREAM, messages=[message])
        consumer = _consumer(
            shared, stream, handler=_FailingHandler(RuntimeError("boom"))
        )
        with pytest.raises(RuntimeError):
            await consumer.process_once(max_records=5)
        assert shared.effects == []
        assert shared.processed == set()
        assert stream.acknowledged_positions(_STREAM, _CONSUMER_A) == frozenset()

    @pytest.mark.asyncio
    async def test_cancellation_before_commit_rolls_back_no_ack(self) -> None:
        shared = _Shared()
        stream = FakeDataStream()
        message = _message()
        await stream.publish(stream_name=_STREAM, messages=[message])
        consumer = _consumer(
            shared, stream, handler=_FailingHandler(asyncio.CancelledError())
        )
        with pytest.raises(asyncio.CancelledError):
            await consumer.process_once(max_records=5)
        assert shared.effects == []
        assert shared.processed == set()
        assert stream.acknowledged_positions(_STREAM, _CONSUMER_A) == frozenset()


class TestAckFailure:
    """C6/C7/C9: commit-success + ack-failure is safe; redelivery dedupes."""

    @pytest.mark.asyncio
    async def test_ack_failure_after_commit_is_safe_redelivery(self) -> None:
        shared = _Shared()
        stream = FakeDataStream()
        message = _message()
        await stream.publish(stream_name=_STREAM, messages=[message])
        consumer = _consumer(shared, stream)
        stream.acknowledge_failure = RuntimeError("coordinator lost")
        with pytest.raises(RuntimeError):
            await consumer.process_once(max_records=5)
        # durable effect committed before the ack failure
        assert shared.effects == [f"effect:{message.message_id}"]
        assert (
            _STREAM.value,
            _CONSUMER_A.value,
            str(message.message_id),
        ) in shared.processed
        # redelivery after ack failure: no repeated effect, then ack works
        stream.acknowledge_failure = None
        stream.reset_consumer(_STREAM, _CONSUMER_A)
        assert await consumer.process_once(max_records=5) == 0
        assert shared.effects == [f"effect:{message.message_id}"]
        assert len(shared.outgoing_outbox) == 1

    @pytest.mark.asyncio
    async def test_cancellation_after_commit_before_ack_is_safe(self) -> None:
        shared = _Shared()
        stream = FakeDataStream()
        message = _message()
        await stream.publish(stream_name=_STREAM, messages=[message])
        consumer = _consumer(shared, stream)
        stream.acknowledge_failure = asyncio.CancelledError()
        with pytest.raises(asyncio.CancelledError):
            await consumer.process_once(max_records=5)
        assert shared.effects == [f"effect:{message.message_id}"]
        stream.acknowledge_failure = None
        stream.reset_consumer(_STREAM, _CONSUMER_A)
        assert await consumer.process_once(max_records=5) == 0
        assert shared.effects == [f"effect:{message.message_id}"]


class TestOrdering:
    """poll -> commit -> acknowledge; never ack before commit."""

    @pytest.mark.asyncio
    async def test_acknowledge_never_precedes_commit(self) -> None:
        shared = _Shared()
        stream = FakeDataStream()
        message = _message()
        await stream.publish(stream_name=_STREAM, messages=[message])
        consumer = _consumer(shared, stream)
        await consumer.process_once(max_records=5)
        # the batch's positions were acknowledged
        assert stream.acknowledged_positions(_STREAM, _CONSUMER_A) == {
            _position_of(stream)
        }


def _position_of(stream: FakeDataStream) -> Any:
    """Return the deterministic position of the sole published record."""
    from darkula.app.data_stream import StreamPosition

    return StreamPosition(lane="default", offset=0)

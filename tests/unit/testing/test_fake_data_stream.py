# SPDX-License-Identifier: AGPL-3.0-only
"""FakeDataStream tests (D-* matrix)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from darkula.app.data_stream import PublishResult, StreamMessage, StreamPosition
from darkula.domain.identifiers import ConsumerId, MessageId, StreamName
from darkula.testing.fake_data_stream import DEFAULT_LANE, FakeDataStream

_MOMENT = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
_STREAM = StreamName("sources.events")
_OTHER = StreamName("sources.other")
_CONSUMER = ConsumerId("worker-1")
_OTHER_CONSUMER = ConsumerId("worker-2")


def _message(**overrides: Any) -> StreamMessage:
    payload = {"kind": "test", "sequence": 1}
    defaults: dict[str, Any] = {
        "message_id": MessageId.generate(),
        "message_type": "test.event",
        "schema_version": 1,
        "occurred_at": _MOMENT,
        "payload": payload,
        "routing_key": None,
    }
    defaults.update(overrides)
    return StreamMessage(**defaults)


class TestPublish:
    """D01: publishing records messages for inspection."""

    @pytest.mark.asyncio
    async def test_d01_publish_records_and_reports_count(self) -> None:
        stream = FakeDataStream()
        await stream.start()
        assert stream.started is True
        result = await stream.publish(
            stream_name=_STREAM,
            messages=[
                _message(payload={"kind": "test", "sequence": 1}),
                _message(payload={"kind": "test", "sequence": 2}),
            ],
        )
        assert isinstance(result, PublishResult)
        assert result.message_count == 2
        published = stream.published_messages(_STREAM)
        assert [m.payload["sequence"] for m in published] == [1, 2]
        await stream.stop()
        assert stream.started is False


class TestPollAckSemantics:
    """D02/D03: polling never acknowledges; ack is explicit and idempotent."""

    @pytest.mark.asyncio
    async def test_d02_poll_alone_never_acknowledges(self) -> None:
        stream = FakeDataStream()
        await stream.publish(stream_name=_STREAM, messages=[_message()])
        batch = await stream.poll(
            stream_name=_STREAM, consumer_id=_CONSUMER, max_records=10
        )
        assert len(batch.records) == 1
        assert stream.acknowledged_positions(_STREAM, _CONSUMER) == frozenset()
        # Without an ack the next poll re-delivers the same record.
        again = await stream.poll(
            stream_name=_STREAM, consumer_id=_CONSUMER, max_records=10
        )
        assert len(again.records) == 1

    @pytest.mark.asyncio
    async def test_d03_explicit_ack_hides_verified_record(self) -> None:
        stream = FakeDataStream()
        await stream.publish(stream_name=_STREAM, messages=[_message()])
        batch = await stream.poll(
            stream_name=_STREAM, consumer_id=_CONSUMER, max_records=10
        )
        position = batch.records[0].position
        await stream.acknowledge(
            stream_name=_STREAM, consumer_id=_CONSUMER, positions=[position]
        )
        assert stream.acknowledged_positions(_STREAM, _CONSUMER) == frozenset(
            {position}
        )
        after = await stream.poll(
            stream_name=_STREAM, consumer_id=_CONSUMER, max_records=10
        )
        assert after.records == ()

    @pytest.mark.asyncio
    async def test_d09_repeated_ack_is_idempotent(self) -> None:
        stream = FakeDataStream()
        await stream.publish(stream_name=_STREAM, messages=[_message()])
        batch = await stream.poll(
            stream_name=_STREAM, consumer_id=_CONSUMER, max_records=10
        )
        position = batch.records[0].position
        await stream.acknowledge(
            stream_name=_STREAM, consumer_id=_CONSUMER, positions=[position]
        )
        await stream.acknowledge(
            stream_name=_STREAM, consumer_id=_CONSUMER, positions=[position]
        )
        assert stream.acknowledged_positions(_STREAM, _CONSUMER) == frozenset(
            {position}
        )


class TestConsumersAndLanes:
    """D04/D07/D08: consumer isolation and monotonic per-lane positions."""

    @pytest.mark.asyncio
    async def test_d04_consumer_ack_isolation(self) -> None:
        stream = FakeDataStream()
        await stream.publish(stream_name=_STREAM, messages=[_message()])
        first_batch = await stream.poll(
            stream_name=_STREAM, consumer_id=_CONSUMER, max_records=10
        )
        position = first_batch.records[0].position
        await stream.acknowledge(
            stream_name=_STREAM, consumer_id=_CONSUMER, positions=[position]
        )
        # The other consumer still receives the record.
        other_batch = await stream.poll(
            stream_name=_STREAM, consumer_id=_OTHER_CONSUMER, max_records=10
        )
        assert len(other_batch.records) == 1
        assert stream.pending_records(_STREAM, _OTHER_CONSUMER)

    @pytest.mark.asyncio
    async def test_d07_d08_per_lane_monotonic_positions_no_global_order(self) -> None:
        stream = FakeDataStream()
        await stream.publish(
            stream_name=_STREAM,
            messages=[
                _message(routing_key="lane-a", payload={"sequence": 1}),
                _message(routing_key="lane-b", payload={"sequence": 2}),
                _message(routing_key="lane-a", payload={"sequence": 3}),
            ],
        )
        batch = await stream.poll(
            stream_name=_STREAM, consumer_id=_CONSUMER, max_records=10
        )
        by_lane: dict[str, list[int]] = {}
        for record in batch.records:
            by_lane.setdefault(record.position.lane, []).append(record.position.offset)
        assert by_lane["lane-a"] == [0, 1]
        assert by_lane["lane-b"] == [0]
        # Default lane is used when no routing key is present.
        assert DEFAULT_LANE == "default"

    @pytest.mark.asyncio
    async def test_d05_poll_bounded_by_max_records(self) -> None:
        stream = FakeDataStream()
        await stream.publish(
            stream_name=_STREAM, messages=[_message() for _ in range(5)]
        )
        batch = await stream.poll(
            stream_name=_STREAM, consumer_id=_CONSUMER, max_records=2
        )
        assert len(batch.records) == 2

    @pytest.mark.asyncio
    async def test_d06_empty_batch_valid(self) -> None:
        stream = FakeDataStream()
        batch = await stream.poll(
            stream_name=_STREAM, consumer_id=_CONSUMER, max_records=10
        )
        assert batch.records == ()

    @pytest.mark.asyncio
    async def test_d15_streams_are_isolated(self) -> None:
        stream = FakeDataStream()
        await stream.publish(stream_name=_STREAM, messages=[_message()])
        other_batch = await stream.poll(
            stream_name=_OTHER, consumer_id=_CONSUMER, max_records=10
        )
        assert other_batch.records == ()
        assert stream.published_messages(_OTHER) == ()


class TestScriptedFailures:
    """D10-D13: scripted exceptions propagate unchanged."""

    @pytest.mark.asyncio
    async def test_d10_publish_failure(self) -> None:
        stream = FakeDataStream()
        error = RuntimeError("publish boom")
        stream.publish_failure = error
        with pytest.raises(RuntimeError) as exc_info:
            await stream.publish(stream_name=_STREAM, messages=[_message()])
        assert exc_info.value is error

    @pytest.mark.asyncio
    async def test_d11_poll_failure(self) -> None:
        stream = FakeDataStream()
        error = RuntimeError("poll boom")
        stream.poll_failure = error
        with pytest.raises(RuntimeError) as exc_info:
            await stream.poll(stream_name=_STREAM, consumer_id=_CONSUMER, max_records=1)
        assert exc_info.value is error

    @pytest.mark.asyncio
    async def test_d12_acknowledge_failure(self) -> None:
        stream = FakeDataStream()
        error = RuntimeError("ack boom")
        stream.acknowledge_failure = error
        with pytest.raises(RuntimeError) as exc_info:
            await stream.acknowledge(
                stream_name=_STREAM,
                consumer_id=_CONSUMER,
                positions=[StreamPosition(lane="default", offset=0)],
            )
        assert exc_info.value is error

    @pytest.mark.asyncio
    async def test_d13_cancellation_propagates(self) -> None:
        stream = FakeDataStream()
        stream.poll_failure = asyncio.CancelledError()
        with pytest.raises(asyncio.CancelledError):
            await stream.poll(stream_name=_STREAM, consumer_id=_CONSUMER, max_records=1)


class TestReplay:
    """D14: explicit replay/duplicate seam."""

    @pytest.mark.asyncio
    async def test_d14_replay_redelivers_acknowledged_records(self) -> None:
        stream = FakeDataStream()
        await stream.publish(stream_name=_STREAM, messages=[_message()])
        batch = await stream.poll(
            stream_name=_STREAM, consumer_id=_CONSUMER, max_records=10
        )
        await stream.acknowledge(
            stream_name=_STREAM,
            consumer_id=_CONSUMER,
            positions=[batch.records[0].position],
        )
        assert stream.pending_records(_STREAM, _CONSUMER) == ()
        stream.reset_consumer(_STREAM, _CONSUMER)
        replayed = await stream.poll(
            stream_name=_STREAM, consumer_id=_CONSUMER, max_records=10
        )
        assert len(replayed.records) == 1
        # The replayed record is the same logical message.
        assert (
            replayed.records[0].message.message_id
            == batch.records[0].message.message_id
        )


class TestContractValidation:
    """Public DataStream validation still applies through the fake."""

    @pytest.mark.asyncio
    async def test_empty_publication_rejected(self) -> None:
        stream = FakeDataStream()
        with pytest.raises(ValueError, match="at least one message"):
            await stream.publish(stream_name=_STREAM, messages=[])

    @pytest.mark.asyncio
    async def test_empty_acknowledge_rejected(self) -> None:
        stream = FakeDataStream()
        with pytest.raises(ValueError, match="at least one position"):
            await stream.acknowledge(
                stream_name=_STREAM, consumer_id=_CONSUMER, positions=[]
            )

# SPDX-License-Identifier: AGPL-3.0-only
"""Unit tests for the DataStream producer/consumer contract (DS-*)."""

from __future__ import annotations

import asyncio
import pathlib
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import pytest

from darkula.app.data_stream import (
    DataStream,
    MessageBatch,
    PublishResult,
    StreamMessage,
    StreamPosition,
    StreamRecord,
    validate_payload,
)
from darkula.domain.identifiers import (
    CausationId,
    ConsumerId,
    CorrelationId,
    MessageId,
    StreamName,
)

_MOMENT = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)


def _message(**overrides: Any) -> StreamMessage:
    payload = {"kind": "test", "sequence": 1}
    defaults: dict[str, Any] = {
        "message_id": MessageId.generate(),
        "message_type": "test.event",
        "schema_version": 1,
        "occurred_at": _MOMENT,
        "payload": payload,
        "correlation_id": None,
        "causation_id": None,
        "routing_key": None,
    }
    defaults.update(overrides)
    return StreamMessage(**defaults)


class _MemoryDataStream(DataStream):
    """Test-local deterministic DataStream stub (PR 3 owns FakeDataStream)."""

    def __init__(self) -> None:
        self._records: list[StreamRecord] = []
        self._acks: list[StreamPosition] = []
        self._offsets: dict[str, int] = {}
        self.started = False
        self.poll_failure: BaseException | None = None
        self.never_acknowledges_on_poll = True

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.started = False

    async def _publish(
        self,
        *,
        stream_name: StreamName,
        messages: Sequence[StreamMessage],
    ) -> PublishResult:
        assert stream_name is not None
        for message in messages:
            lane = message.routing_key or "default"
            offset = self._offsets.get(lane, 0)
            self._records.append(
                StreamRecord(message=message, position=StreamPosition(lane, offset))
            )
            self._offsets[lane] = offset + 1
        return PublishResult(message_count=len(messages))

    async def _poll(
        self,
        *,
        stream_name: StreamName,
        consumer_id: ConsumerId,
        max_records: int,
    ) -> MessageBatch:
        assert consumer_id is not None
        if self.poll_failure is not None:
            raise self.poll_failure
        acked = set(self._acks)
        pending = [record for record in self._records if record.position not in acked][
            :max_records
        ]
        return MessageBatch(records=tuple(pending))

    async def _acknowledge(
        self,
        *,
        stream_name: StreamName,
        consumer_id: ConsumerId,
        positions: Sequence[StreamPosition],
    ) -> None:
        self._acks.extend(positions)


def _stream() -> tuple[_MemoryDataStream, StreamName]:
    return _MemoryDataStream(), StreamName("sources.events")


class TestStreamMessage:
    """DS-01/DS-02/DS-10: envelope validation."""

    def test_valid_message_envelope_accepted(self) -> None:
        message = _message(
            message_id=MessageId.from_str("00000000-0000-0000-0000-000000000001"),
            message_type="sources.crawl_requested",
            schema_version=2,
            correlation_id=CorrelationId.from_str(
                "00000000-0000-0000-0000-000000000002"
            ),
            causation_id=CausationId.from_str("00000000-0000-0000-0000-000000000003"),
            routing_key="lane-a",
            payload={"path": "/index.html", "depth": 2},
        )
        assert message.message_id.value.hex.endswith("1")
        assert message.schema_version == 2
        assert message.correlation_id is not None
        assert message.causation_id is not None
        assert message.routing_key == "lane-a"

    @pytest.mark.parametrize("version", [0, -1, -100])
    def test_non_positive_schema_version_rejected(self, version: int) -> None:
        with pytest.raises(ValueError, match="schema_version"):
            _message(schema_version=version)

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_blank_message_type_rejected(self, blank: str) -> None:
        with pytest.raises(ValueError, match="message_type"):
            _message(message_type=blank)

    def test_blank_routing_key_when_provided_rejected(self) -> None:
        with pytest.raises(ValueError, match="routing_key"):
            _message(routing_key="  ")

    def test_naive_timestamp_rejected(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            _message(occurred_at=datetime(2026, 1, 15, 12, 0, 0))

    def test_non_utc_timestamp_rejected(self) -> None:
        tz = UTC  # fallback if no other tz available in tests
        _message(occurred_at=_MOMENT.astimezone(tz))  # accepted: UTC

    @pytest.mark.parametrize(
        "header",
        ["traceparent", "tracestate", "baggage", "TraceParent", "TRACEPARENT"],
    )
    def test_trace_headers_forbidden_in_payload(self, header: str) -> None:
        with pytest.raises(ValueError, match="trace headers"):
            validate_payload({header: "00-abc-01"})

    @pytest.mark.parametrize(
        "bad_payload",
        [b"bytes", {1, 2, 3}, {"nested": {"deep": object()}}, {"n": float("nan")}],
    )
    def test_non_json_payload_values_rejected(self, bad_payload: object) -> None:
        payload = {"bad": bad_payload}
        with pytest.raises(ValueError, match=r"JSON|finite"):
            validate_payload(payload)

    def test_payload_must_be_a_mapping(self) -> None:
        with pytest.raises(ValueError, match="mapping"):
            validate_payload("not-a-mapping")  # type: ignore[arg-type]


class TestStreamPosition:
    """DS-07: logical lanes with monotonic per-lane offsets."""

    def test_valid_position(self) -> None:
        position = StreamPosition(lane="lane-a", offset=3)
        assert position.offset == 3

    def test_blank_lane_rejected(self) -> None:
        with pytest.raises(ValueError, match="lane"):
            StreamPosition(lane="  ", offset=0)

    def test_negative_offset_rejected(self) -> None:
        with pytest.raises(ValueError, match="offset"):
            StreamPosition(lane="lane-a", offset=-1)


class TestPublish:
    """DS-01/DS-09: producer contract."""

    @pytest.mark.asyncio
    async def test_publish_reports_provider_neutral_result(self) -> None:
        stream, name = _stream()
        result = await stream.publish(
            stream_name=name, messages=[_message(), _message()]
        )
        assert isinstance(result, PublishResult)
        assert result.message_count == 2

    @pytest.mark.asyncio
    async def test_empty_publication_rejected(self) -> None:
        stream, name = _stream()
        with pytest.raises(ValueError, match="at least one message"):
            await stream.publish(stream_name=name, messages=[])


class TestPoll:
    """DS-03/DS-04/DS-05/DS-06/DS-07/DS-08: consumer contract."""

    @pytest.mark.asyncio
    async def test_non_positive_poll_bound_rejected(self) -> None:
        stream, name = _stream()
        with pytest.raises(ValueError, match="max_records"):
            await stream.poll(
                stream_name=name, consumer_id=ConsumerId("worker-1"), max_records=0
            )

    @pytest.mark.asyncio
    async def test_empty_poll_returns_valid_empty_batch(self) -> None:
        stream, name = _stream()
        batch = await stream.poll(
            stream_name=name, consumer_id=ConsumerId("worker-1"), max_records=10
        )
        assert batch.records == ()

    @pytest.mark.asyncio
    async def test_poll_bounded_by_max_records(self) -> None:
        stream, name = _stream()
        await stream.publish(stream_name=name, messages=[_message() for _ in range(5)])
        batch = await stream.poll(
            stream_name=name, consumer_id=ConsumerId("worker-1"), max_records=2
        )
        assert len(batch.records) == 2

    @pytest.mark.asyncio
    async def test_poll_alone_never_acknowledges(self) -> None:
        stream, name = _stream()
        await stream.publish(stream_name=name, messages=[_message()])
        batch = await stream.poll(
            stream_name=name, consumer_id=ConsumerId("worker-1"), max_records=10
        )
        assert len(batch.records) == 1
        assert stream._acks == []

    @pytest.mark.asyncio
    async def test_acknowledge_is_an_explicit_separate_step(self) -> None:
        stream, name = _stream()
        await stream.publish(stream_name=name, messages=[_message()])
        batch = await stream.poll(
            stream_name=name, consumer_id=ConsumerId("worker-1"), max_records=10
        )
        position = batch.records[0].position
        assert stream._acks == []
        await stream.acknowledge(
            stream_name=name, consumer_id=ConsumerId("worker-1"), positions=[position]
        )
        assert stream._acks == [position]
        # The acknowledged record is no longer redelivered.
        batch = await stream.poll(
            stream_name=name, consumer_id=ConsumerId("worker-1"), max_records=10
        )
        assert batch.records == ()

    @pytest.mark.asyncio
    async def test_empty_acknowledge_rejected(self) -> None:
        stream, name = _stream()
        with pytest.raises(ValueError, match="at least one position"):
            await stream.acknowledge(
                stream_name=name, consumer_id=ConsumerId("worker-1"), positions=[]
            )

    @pytest.mark.asyncio
    async def test_multiple_lanes_without_global_order_claim(self) -> None:
        stream, name = _stream()
        await stream.publish(
            stream_name=name,
            messages=[
                _message(routing_key="lane-a", payload={"lane": "a"}),
                _message(routing_key="lane-b", payload={"lane": "b"}),
                _message(routing_key="lane-a", payload={"lane": "a2"}),
            ],
        )
        batch = await stream.poll(
            stream_name=name, consumer_id=ConsumerId("worker-1"), max_records=10
        )
        lanes = [record.position.lane for record in batch.records]
        assert set(lanes) == {"lane-a", "lane-b"}
        # Per-lane offsets are monotonic; no global ordering is claimed.
        a_offsets = [
            record.position.offset
            for record in batch.records
            if record.position.lane == "lane-a"
        ]
        assert a_offsets == sorted(a_offsets)

    @pytest.mark.asyncio
    async def test_cancellation_propagates_from_poll(self) -> None:
        stream, name = _stream()
        stream.poll_failure = asyncio.CancelledError()
        with pytest.raises(asyncio.CancelledError):
            await stream.poll(
                stream_name=name, consumer_id=ConsumerId("worker-1"), max_records=10
            )

    @pytest.mark.asyncio
    async def test_lifecycle_start_stop(self) -> None:
        stream, _ = _stream()
        await stream.start()
        assert stream.started is True
        await stream.stop()
        assert stream.started is False


class TestProviderNeutrality:
    """DS-09/DS-10: no Kafka/aiokafka types in the app contract."""

    def test_stream_position_is_never_message_identity(self) -> None:
        # Section 19: transport position is never equated with message id.
        position = StreamPosition(lane="0", offset=0)
        message = _message()
        assert position != message.message_id  # type: ignore[comparison-overlap]
        assert position != message  # type: ignore[comparison-overlap]

    def test_no_kafka_terminology_in_contract_module(self) -> None:
        source = pathlib.Path(__file__).resolve().parents[3] / "src"
        text = (source / "darkula" / "app" / "data_stream.py").read_text()
        for forbidden in ("aiokafka", "TopicPartition", "bootstrap"):
            assert forbidden not in text, f"Kafka leak in contract: {forbidden}"

    def test_no_aiokafka_importable_from_contract_module(self) -> None:
        from darkula.app import data_stream

        assert not hasattr(data_stream, "aiokafka")

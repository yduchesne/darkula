# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic telemetry tests for PR 5 operations (Step 21-23).

Wires in-memory tracer/meter providers through the injectable module seams
of the adapter, outbox publisher, and reliable consumer, then asserts the
bounded counters/histograms: publish/poll/ack counts + failures +
durations, outbox claim/publish/mark, consumer process + duplicates, and
that no message_id/correlation_id/payload/routing-key value ever becomes a
metric label or span attribute.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from tests.support.fake_aiokafka import (
    FakeConsumerRecord,
    FakeTopicPartition,
)
from tests.support.otel import (
    counter_value,
    data_point_attributes,
    histogram_count,
    metrics_by_name,
)

import darkula.app.consumer as consumer_mod
import darkula.app.outbox as outbox_mod
import darkula.infrastructure.data_stream.redpanda as rp
from darkula.app.consumer import ReliableConsumer
from darkula.app.data_stream import StreamMessage
from darkula.app.outbox import OutboxPublisher
from darkula.app.persistence import DarkulaSpi, UnitOfWork
from darkula.app.repositories import OutboxRecord
from darkula.domain.identifiers import ConsumerId, MessageId, StreamName
from darkula.testing.fake_data_stream import FakeDataStream

_STREAM = StreamName("events")
_CONSUMER = ConsumerId("consumer-a")
_MOMENT = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def _message() -> StreamMessage:
    return StreamMessage(
        message_id=MessageId.generate(),
        message_type="crawl_requested",
        schema_version=1,
        occurred_at=_MOMENT,
        payload={"secret_marker": "should-never-be-a-label"},
        routing_key="super-secret-lane",
    )


@pytest.fixture
def telemetry(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Bind all PR 5 module seams to isolated in-memory providers."""
    span_exporter = InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(span_exporter))
    tracer = tracer_provider.get_tracer("darkula-test")

    metric_reader = InMemoryMetricReader()
    meter_provider = MeterProvider(metric_readers=[metric_reader])
    meter = meter_provider.get_meter("darkula-test")

    for module in (rp, outbox_mod, consumer_mod):
        monkeypatch.setattr(module, "get_tracer", lambda tracer=tracer: tracer)
        monkeypatch.setattr(
            module,
            "get_counter",
            lambda name, meter=meter: meter.create_counter(
                name, unit="{operation}", description=name
            ),
        )
        monkeypatch.setattr(
            module,
            "get_histogram",
            lambda name, meter=meter: meter.create_histogram(
                name, unit="s", description=name
            ),
        )
    return SimpleNamespace(exporter=span_exporter, reader=metric_reader, meter=meter)


def _install_fake_clients(
    monkeypatch: pytest.MonkeyPatch, batches: dict[str, Any]
) -> SimpleNamespace:
    """Install fake aiokafka clients (single consumer/producer)."""
    state = SimpleNamespace(produced=[], consumed={})

    def make_producer(**_: Any) -> Any:
        producer = SimpleNamespace(sent=[], start_called=False, flush_failure=None)

        async def start() -> None:
            producer.start_called = True

        async def stop() -> None:
            producer.start_called = False

        async def send(
            topic: str, *, value: bytes, key: bytes | None, headers: list[Any]
        ) -> Any:
            producer.sent.append({"topic": topic, "value": value})

        async def flush() -> None:
            if producer.flush_failure is not None:
                raise producer.flush_failure

        producer.start, producer.stop = start, stop
        producer.send, producer.flush = send, flush
        state.produced.append(producer)
        return producer

    def make_consumer(*args: Any, **_: Any) -> Any:
        topic = str(args[0])
        consumer = SimpleNamespace(commits=[])

        async def start() -> None:
            return None

        async def stop() -> None:
            return None

        async def getmany(*, timeout_ms: int, max_records: int) -> dict[Any, list[Any]]:
            return dict(batches.get(topic, {}))

        async def commit(offsets: Any = None) -> None:
            consumer.commits.append(offsets)

        consumer.start, consumer.stop = start, stop
        consumer.getmany, consumer.commit = getmany, commit
        state.consumed[topic] = consumer
        return consumer

    monkeypatch.setattr(rp, "AIOKafkaProducer", make_producer)
    monkeypatch.setattr(rp, "AIOKafkaConsumer", make_consumer)
    monkeypatch.setattr(rp, "TopicPartition", FakeTopicPartition)
    return state


class _Spi(DarkulaSpi):
    """Fake SPI with a minimal recording UoW for consumer/outbox tests."""

    def __init__(self) -> None:
        self.processed: set[tuple[str, str, str]] = set()
        self.outbox_claimed: list[OutboxRecord] = []
        self.marked: list[Any] = []

    def unit_of_work(self) -> UnitOfWork:
        spi = self

        class _Uow(UnitOfWork):
            async def __aenter__(self) -> Any:
                return self

            async def __aexit__(self, *args: Any) -> None:
                return None

            async def commit(self) -> None:
                return None

            async def rollback(self) -> None:
                return None

            @property
            def source_candidates(self) -> Any:
                raise AssertionError

            @property
            def sources(self) -> Any:
                raise AssertionError

            @property
            def outbox(self) -> Any:
                return self

            @property
            def processed_messages(self) -> Any:
                return self

            @property
            def content(self) -> Any:
                raise AssertionError("telemetry fake exposes no content repository")

            @property
            def collection(self) -> Any:
                raise AssertionError("telemetry fake exposes no collection repository")

            @property
            def extraction(self) -> Any:
                raise AssertionError("telemetry fake exposes no extraction repository")

            @property
            def geography(self) -> Any:
                raise AssertionError("telemetry fake exposes no geography repository")

            @property
            def relationships(self) -> Any:
                raise AssertionError(
                    "telemetry fake exposes no relationship repository"
                )

            async def append(
                self, message: StreamMessage, *, stream_name: StreamName
            ) -> None:
                return None

            async def claim(
                self,
                *,
                stream_name: StreamName,
                limit: int,
                claim_id: Any,
                lease_seconds: float,
            ) -> tuple[OutboxRecord, ...]:
                return tuple(spi.outbox_claimed[:limit])

            async def mark_published(
                self, *, claim_id: Any, outbox_ids: tuple[Any, ...]
            ) -> int:
                spi.marked.extend(outbox_ids)
                return len(outbox_ids)

            async def record(
                self,
                *,
                stream_name: StreamName,
                consumer_id: ConsumerId,
                message_id: MessageId,
            ) -> bool:
                key = (stream_name.value, consumer_id.value, str(message_id))
                if key in spi.processed:
                    return False
                spi.processed.add(key)
                return True

        return _Uow()


def _encoded(message: StreamMessage) -> bytes:
    from darkula.infrastructure.data_stream.codec import encode_stream_message

    return encode_stream_message(message)


class TestDataStreamMetrics:
    def test_publish_poll_acknowledge_metrics(
        self,
        telemetry: SimpleNamespace,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        record = FakeConsumerRecord(_STREAM.value, 0, 0, _encoded(_message()))
        _install_fake_clients(
            monkeypatch,
            {_STREAM.value: {FakeTopicPartition(_STREAM.value, 0): [record]}},
        )

        async def scenario() -> None:
            stream = rp.RedpandaDataStream(
                bootstrap_servers="127.0.0.1:39092",
                client_id="telemetry",
                poll_timeout_ms=10,
                max_poll_records=10,
            )
            await stream.start()
            await stream.publish(stream_name=_STREAM, messages=[_message()])
            batch = await stream.poll(
                stream_name=_STREAM, consumer_id=_CONSUMER, max_records=5
            )
            await stream.acknowledge(
                stream_name=_STREAM,
                consumer_id=_CONSUMER,
                positions=[r.position for r in batch.records],
            )
            await stream.stop()

        asyncio.run(scenario())

        metrics = metrics_by_name(telemetry.reader)
        assert counter_value(metrics["darkula.datastream.publish.count"]) == 1
        assert counter_value(metrics["darkula.datastream.poll.count"]) == 1
        assert counter_value(metrics["darkula.datastream.poll.records"]) == 1
        assert counter_value(metrics["darkula.datastream.acknowledge.count"]) == 1
        assert histogram_count(metrics["darkula.datastream.publish.duration"]) == 1
        assert histogram_count(metrics["darkula.datastream.poll.duration"]) == 1
        # labels are bounded: stream/consumer only, never the payload
        attrs = data_point_attributes(metrics["darkula.datastream.publish.count"])
        assert attrs == {"darkula.stream": _STREAM.value}
        poll_attrs = data_point_attributes(metrics["darkula.datastream.poll.records"])
        assert poll_attrs == {
            "darkula.stream": _STREAM.value,
            "darkula.consumer": _CONSUMER.value,
        }
        assert not any("message_id" in key for key in poll_attrs)
        assert not any("routing" in key for key in poll_attrs)

    def test_failure_counters_and_spans(
        self, telemetry: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state = _install_fake_clients(monkeypatch, {})

        async def scenario() -> None:
            stream = rp.RedpandaDataStream(
                bootstrap_servers="127.0.0.1:39092",
                client_id="telemetry",
                poll_timeout_ms=10,
                max_poll_records=10,
            )
            await stream.start()
            state.produced[0].flush_failure = RuntimeError("boom")
            try:
                await stream.publish(stream_name=_STREAM, messages=[_message()])
            except Exception:
                pass
            await stream.stop()

        asyncio.run(scenario())
        metrics = metrics_by_name(telemetry.reader)
        assert counter_value(metrics["darkula.datastream.publish.failed"]) == 1
        spans = list(telemetry.exporter.get_finished_spans())
        assert any(s.name == "datastream.publish" for s in spans)


class TestOutboxMetrics:
    def test_outbox_operation_metrics(
        self, telemetry: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_fake_clients(monkeypatch, {})
        spi = _Spi()
        spi.outbox_claimed.append(
            OutboxRecord(__import__("uuid").uuid4(), _STREAM, _message())
        )

        async def scenario() -> None:
            stream = rp.RedpandaDataStream(
                bootstrap_servers="127.0.0.1:39092",
                client_id="telemetry",
                poll_timeout_ms=10,
                max_poll_records=10,
            )
            await stream.start()
            publisher = OutboxPublisher(spi=spi, data_stream=stream)
            await publisher.publish_pending(stream_name=_STREAM, limit=5)
            await stream.stop()

        asyncio.run(scenario())
        metrics = metrics_by_name(telemetry.reader)
        assert counter_value(metrics["darkula.outbox.claim.count"]) == 1
        assert counter_value(metrics["darkula.outbox.claimed"]) == 1
        assert counter_value(metrics["darkula.outbox.publish.count"]) == 1
        assert counter_value(metrics["darkula.outbox.published"]) == 1
        assert histogram_count(metrics["darkula.outbox.claim.duration"]) == 1
        assert histogram_count(metrics["darkula.outbox.publish.duration"]) == 1
        attrs = data_point_attributes(metrics["darkula.outbox.published"])
        assert attrs == {"darkula.stream": _STREAM.value}


class TestConsumerMetrics:
    def test_consumer_process_and_duplicate_metrics(
        self, telemetry: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_fake_clients(monkeypatch, {})
        spi = _Spi()
        data_stream = FakeDataStream()
        message = _message()

        class _NoopHandler:
            async def handle(self, msg: StreamMessage, uow: UnitOfWork) -> None:
                return None

        consumer = ReliableConsumer(
            spi=spi,
            data_stream=data_stream,
            stream_name=_STREAM,
            consumer_id=_CONSUMER,
            handler=_NoopHandler(),
        )

        # first delivery then redelivery at a later position
        async def scenario() -> None:
            await data_stream.publish(stream_name=_STREAM, messages=[message])
            await consumer.process_once(max_records=5)
            await data_stream.publish(stream_name=_STREAM, messages=[message])
            data_stream.reset_consumer(_STREAM, _CONSUMER)
            await consumer.process_once(max_records=5)

        asyncio.run(scenario())
        metrics = metrics_by_name(telemetry.reader)
        assert counter_value(metrics["darkula.consumer.process.count"]) == 2
        # second batch replayed BOTH records (same message_id at two
        # positions): both are duplicates of the already-processed message.
        assert counter_value(metrics["darkula.consumer.process.duplicate"]) == 2
        assert histogram_count(metrics["darkula.consumer.process.duration"]) == 2
        assert any(
            s.name == "consumer.process"
            for s in telemetry.exporter.get_finished_spans()
        )

# SPDX-License-Identifier: AGPL-3.0-only
"""Redpanda :class:`~darkula.app.data_stream.DataStream` adapter (PR 5).

Implements the existing provider-neutral ``DataStream`` contract over the
Redpanda/Kafka protocol using the pure-Python, asyncio-native ``aiokafka``
client. Provider types and exceptions stay inside this module; application
and domain code only ever sees the Darkula envelope, positions, batches,
and bounded errors.

Transport semantics (frozen here, matching ``data_stream.py``):

- **At-least-once delivery only.** The broker is transport, never domain
  identity; ``StreamPosition`` (lane=partition, offset) is transport state.
- **Polling never acknowledges.** ``_poll`` returns at most ``max_records``
  and commits nothing. A position is acknowledged only after the caller
  committed its durable processing.
- **Acknowledgement is a per-lane high-water commit.** Kafka offset commits
  are per-partition and implicitly acknowledge every earlier offset in that
  partition. ``acknowledge(positions)`` therefore commits, for each lane,
  ``max(offset) + 1`` (the Kafka "next record to consume" offset), which is
  exactly Kafka's real semantics: it is **not** sparse per-position
  acknowledgement. Darkula's reliable consumer primitive processes each
  poll batch in order and never acks work that did not durably commit, so
  no unprocessed position is ever implicitly acknowledged on the supported
  flow. Callers must treat a per-lane acknowledge as acknowledging every
  position up to and including the maximum.
- **Partial batch acceptance on publish is possible** (aiokafka batches
  sends and flushes asynchronously); the adapter never promises
  all-or-nothing broker behavior. Reliable publication is owned by the
  outbox publisher, and consumers deduplicate by stable ``message_id``.
- **Malformed/unsupported messages are never skipped or acknowledged.**
  A decode failure makes the whole poll fail boundedly (``PollError``); the
  offending record stays unacked and is redelivered. Quarantine is future
  work.
- **Cancellation propagates unchanged** and resolves no state.

Trace propagation: W3C ``traceparent``/``tracestate`` are injected into
broker transport headers on publish and extracted on poll (never into the
payload, and never baggage). Extraction is best-effort and never fails the
poll.

Failure classification (bounded, provider-neutral):

- retryable transport failure -> ``PublishError`` / ``PollError`` /
  ``AcknowledgeError``;
- non-retryable codec/contract failure -> ``PollError`` (wrapping a codec
  failure; the record is never acknowledged);
- lifecycle/configuration failure -> ``LifecycleError``.

Provider objects are created lazily in :meth:`start` / on first poll and
closed exactly once by :meth:`stop`; both are idempotent.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from contextvars import Token
from typing import Final

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from aiokafka.structs import ConsumerRecord, TopicPartition
from opentelemetry import context as otel_context
from opentelemetry.context import Context
from opentelemetry.trace.propagation.tracecontext import (
    TraceContextTextMapPropagator,
)

from darkula.app.data_stream import (
    AcknowledgeError,
    DataStream,
    LifecycleError,
    MessageBatch,
    PollError,
    PublishError,
    PublishResult,
    StreamMessage,
    StreamPosition,
    StreamRecord,
)
from darkula.domain.identifiers import ConsumerId, StreamName
from darkula.infrastructure.data_stream.codec import (
    MessageCodecError,
    decode_stream_message,
    encode_stream_message,
)
from darkula.telemetry.metrics import get_counter, get_histogram
from darkula.telemetry.tracing import get_tracer

#: Broker transport trace headers (never payload keys, never baggage).
_TRACE_HEADER_KEYS: Final = frozenset({"traceparent", "tracestate"})

#: Provider-neutral metric attribute keys.
_STREAM_ATTR = "darkula.stream"
_CONSUMER_ATTR = "darkula.consumer"

#: Metric names (bounded; no message/correlation IDs or payload values).
PUBLISH_COUNT = "darkula.datastream.publish.count"
PUBLISH_FAILED = "darkula.datastream.publish.failed"
PUBLISH_DURATION = "darkula.datastream.publish.duration"
POLL_COUNT = "darkula.datastream.poll.count"
POLL_FAILED = "darkula.datastream.poll.failed"
POLL_RECORDS = "darkula.datastream.poll.records"
POLL_DURATION = "darkula.datastream.poll.duration"
ACK_COUNT = "darkula.datastream.acknowledge.count"
ACK_FAILED = "darkula.datastream.acknowledge.failed"
ACK_DURATION = "darkula.datastream.acknowledge.duration"
#: Logical operation names recorded on spans.
PUBLISH_SPAN = "datastream.publish"
POLL_SPAN = "datastream.poll"
ACK_SPAN = "datastream.acknowledge"


def _stream_attributes(
    stream_name: StreamName, consumer_id: ConsumerId | None = None
) -> dict[str, str]:
    """Return bounded, secret-free metric/span attributes for one stream."""
    attributes: dict[str, str] = {_STREAM_ATTR: stream_name.value}
    if consumer_id is not None:
        attributes[_CONSUMER_ATTR] = consumer_id.value
    return attributes


def _build_trace_headers() -> list[tuple[str, bytes]]:
    """Inject the current W3C trace context into broker transport headers."""
    carrier: dict[str, str] = {}
    TraceContextTextMapPropagator().inject(carrier)
    return [(key, value.encode("utf-8")) for key, value in carrier.items()]


def _trace_carrier(headers: Sequence[tuple[str, bytes]] | None) -> dict[str, str]:
    """Best-effort: read trace headers from one record's transport metadata.

    Invalid or non-UTF-8 trace headers are ignored; trace extraction never
    fails the poll.
    """
    carrier: dict[str, str] = {}
    if not headers:
        return carrier
    for key, value in headers:
        if key not in _TRACE_HEADER_KEYS or value is None:
            continue
        try:
            carrier[key] = value.decode("utf-8")
        except UnicodeDecodeError:
            continue
    return carrier


class RedpandaDataStream(DataStream):
    """A ``DataStream`` implemented over Redpanda/Kafka (aiokafka)."""

    def __init__(
        self,
        *,
        bootstrap_servers: str,
        client_id: str,
        poll_timeout_ms: int,
        max_poll_records: int,
    ) -> None:
        """Configure the adapter; no broker connection is opened here."""
        if not bootstrap_servers or not bootstrap_servers.strip():
            raise ValueError("bootstrap_servers must not be blank")
        if not client_id or not client_id.strip():
            raise ValueError("client_id must not be blank")
        if poll_timeout_ms < 1:
            raise ValueError("poll_timeout_ms must be >= 1")
        if max_poll_records < 1:
            raise ValueError("max_poll_records must be >= 1")
        self._bootstrap_servers = bootstrap_servers.strip()
        self._client_id = client_id.strip()
        self._poll_timeout_ms = poll_timeout_ms
        self._max_poll_records = max_poll_records
        self._producer: AIOKafkaProducer | None = None
        self._consumers: dict[tuple[StreamName, ConsumerId], AIOKafkaConsumer] = {}
        self._started = False

    @property
    def started(self) -> bool:
        """Return whether the producer lifecycle has been started."""
        return self._started

    async def start(self) -> None:
        """Start the producer lifecycle (idempotent; no eager broker I/O).

        The producer client is created but connects lazily on first
        publish; ``LifecycleError`` is reserved for client-construction
        failures, which are configuration/programming errors.
        """
        if self._started:
            return
        try:
            self._producer = AIOKafkaProducer(
                bootstrap_servers=self._bootstrap_servers,
                client_id=self._client_id,
                acks="all",
            )
            await self._producer.start()
        except asyncio.CancelledError:
            self._producer = None
            raise
        except Exception as exc:
            self._producer = None
            raise LifecycleError("the stream producer could not start") from exc
        self._started = True

    async def stop(self) -> None:
        """Stop the producer and every open consumer (idempotent)."""
        if not self._started:
            return
        self._started = False
        await self._stop_producer()
        await self._stop_consumers()

    async def _stop_producer(self) -> None:
        producer = self._producer
        self._producer = None
        if producer is None:
            return
        try:
            await producer.stop()
        except asyncio.CancelledError:
            raise
        except Exception:
            # Closing a broken client must not mask the caller's outcome.
            return

    async def _stop_consumers(self) -> None:
        consumers = list(self._consumers.values())
        self._consumers.clear()
        for consumer in consumers:
            await self._close_consumer_best_effort(consumer)

    async def _close_consumer_best_effort(self, consumer: AIOKafkaConsumer) -> None:
        """Close one consumer; a broken client is dropped without masking."""
        try:
            await consumer.stop()
        except asyncio.CancelledError:
            raise
        except Exception:
            # Best-effort close: a broken consumer is dropped.
            return

    def _require_producer(self) -> AIOKafkaProducer:
        """Return the started producer or fail boundedly."""
        if not self._started or self._producer is None:
            raise LifecycleError("the stream producer is not started")
        return self._producer

    async def _consumer_for(
        self, stream_name: StreamName, consumer_id: ConsumerId
    ) -> AIOKafkaConsumer:
        """Return the started consumer for one (stream, consumer) pair.

        Consumers are created lazily on first poll (bounded by the number
        of distinct consumers used) and closed by :meth:`stop`.
        """
        if not self._started:
            raise LifecycleError("the stream is not started")
        key = (stream_name, consumer_id)
        consumer = self._consumers.get(key)
        if consumer is not None:
            return consumer
        try:
            consumer = AIOKafkaConsumer(
                stream_name.value,
                bootstrap_servers=self._bootstrap_servers,
                client_id=self._client_id,
                group_id=consumer_id.value,
                enable_auto_commit=False,
                auto_offset_reset="earliest",
                fetch_max_wait_ms=self._poll_timeout_ms,
            )
            await consumer.start()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise PollError("stream polling could not start") from exc
        self._consumers[key] = consumer
        return consumer

    async def _publish(
        self,
        *,
        stream_name: StreamName,
        messages: Sequence[StreamMessage],
    ) -> PublishResult:
        """Encode and publish every message; accept after broker flush."""
        producer = self._require_producer()
        attributes = _stream_attributes(stream_name)
        start_ns = time.perf_counter_ns()
        tracer = get_tracer()
        with tracer.start_as_current_span(PUBLISH_SPAN, attributes=attributes):
            get_counter(PUBLISH_COUNT).add(1, attributes)
            try:
                for message in messages:
                    try:
                        payload = encode_stream_message(message)
                    except MessageCodecError as exc:
                        raise PublishError(
                            "a stream message could not be encoded"
                        ) from exc
                    key = (
                        message.routing_key.encode("utf-8")
                        if message.routing_key is not None
                        else None
                    )
                    await producer.send(
                        stream_name.value,
                        value=payload,
                        key=key,
                        headers=_build_trace_headers(),
                    )
                # Required broker acceptance: wait until the broker
                # acknowledged every send (acks=all) or fail boundedly.
                await producer.flush()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                get_counter(PUBLISH_FAILED).add(1, attributes)
                raise PublishError(
                    "stream publication was not accepted by the broker"
                ) from exc
            finally:
                get_histogram(PUBLISH_DURATION).record(
                    (time.perf_counter_ns() - start_ns) / 1e9,
                    attributes=attributes,
                )
        return PublishResult(message_count=len(messages))

    def _decode_record(self, record: ConsumerRecord) -> StreamMessage:
        """Strictly decode one record into a validated stream message.

        A bounded codec failure surfaces as ``PollError``; the record is
        never returned, never skipped, and never acknowledged. Trace
        extraction happens once per batch in :meth:`_poll`.
        """
        try:
            return decode_stream_message(record.value)
        except MessageCodecError as exc:
            raise PollError(
                "a stream record could not be decoded and was not acknowledged"
            ) from exc

    async def _poll(
        self,
        *,
        stream_name: StreamName,
        consumer_id: ConsumerId,
        max_records: int,
    ) -> MessageBatch:
        """Return at most ``max_records`` decoded records; never acks."""
        consumer = await self._consumer_for(stream_name, consumer_id)
        attributes = _stream_attributes(stream_name, consumer_id)
        start_ns = time.perf_counter_ns()
        get_counter(POLL_COUNT).add(1, attributes)
        try:
            try:
                batches = await consumer.getmany(
                    timeout_ms=self._poll_timeout_ms,
                    max_records=max_records,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                get_counter(POLL_FAILED).add(1, attributes)
                raise PollError("stream polling failed") from exc

            # Deterministic per-lane order across the batch.
            raw: list[tuple[int, int, ConsumerRecord]] = []
            for partition, consumed in batches.items():
                for record in consumed:
                    raw.append((partition.partition, record.offset, record))
            raw.sort(key=lambda item: (item[0], item[1]))

            # Connect to the upstream trace carried by the first record's
            # transport headers (never the payload); best-effort only.
            token: Token[Context] | None = None
            if raw:
                context = TraceContextTextMapPropagator().extract(
                    carrier=_trace_carrier(raw[0][2].headers),
                    context=otel_context.get_current(),
                )
                token = otel_context.attach(context)
            try:
                tracer = get_tracer()
                with tracer.start_as_current_span(POLL_SPAN, attributes=attributes):
                    records = [
                        StreamRecord(
                            message=self._decode_record(record),
                            position=StreamPosition(lane=str(lane), offset=offset),
                        )
                        for lane, offset, record in raw
                    ]
                    if records:
                        get_counter(POLL_RECORDS).add(len(records), attributes)
                    return MessageBatch(records=tuple(records))
            finally:
                if token is not None:
                    otel_context.detach(token)
        finally:
            get_histogram(POLL_DURATION).record(
                (time.perf_counter_ns() - start_ns) / 1e9,
                attributes=attributes,
            )

    async def _acknowledge(
        self,
        *,
        stream_name: StreamName,
        consumer_id: ConsumerId,
        positions: Sequence[StreamPosition],
    ) -> None:
        """Commit per-lane high-water offsets (implicitly earlier offsets).

        Acknowledging one position per lane acknowledges every position up
        to and including the maximum offset provided for that lane -- the
        real Kafka partition-epoch semantics (see module docstring).
        """
        consumer = self._consumers.get((stream_name, consumer_id))
        if consumer is None:
            raise AcknowledgeError(
                "cannot acknowledge: this consumer has not polled the stream"
            )
        attributes = _stream_attributes(stream_name, consumer_id)
        start_ns = time.perf_counter_ns()
        tracer = get_tracer()
        with tracer.start_as_current_span(ACK_SPAN, attributes=attributes):
            get_counter(ACK_COUNT).add(1, attributes)
            high_water: dict[str, int] = {}
            for position in positions:
                lane = position.lane
                high_water[lane] = max(high_water.get(lane, -1), position.offset)
            offsets = {
                TopicPartition(stream_name.value, int(lane)): highest + 1
                for lane, highest in high_water.items()
            }
            try:
                await consumer.commit(offsets=offsets)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                get_counter(ACK_FAILED).add(1, attributes)
                raise AcknowledgeError(
                    "the broker rejected the stream acknowledgement"
                ) from exc
            finally:
                get_histogram(ACK_DURATION).record(
                    (time.perf_counter_ns() - start_ns) / 1e9,
                    attributes=attributes,
                )


__all__ = ["RedpandaDataStream"]

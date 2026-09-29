# SPDX-License-Identifier: AGPL-3.0-only
"""Darkula-owned producer/consumer stream contract.

``DataStream`` is the Darkula-wide asynchronous command/event and reference
plane (Redpanda/Kafka is only a future adapter behind this boundary). It
models **both** producing and consuming through one coherent abstraction:

- publishing typed messages to a logical :class:`StreamName`;
- polling a bounded number of records from a stream as a consumer;
- explicit acknowledgement after the caller's durable application
  processing has succeeded.

Frozen semantics:

> Polling never acknowledges. A caller acknowledges only after the
> corresponding durable application processing has succeeded.

The message envelope is Darkula-owned and serialization-neutral
(``payload`` is a JSON-compatible mapping, not JSON bytes and never trace
headers). Exact Redpanda topic names, partitions, offsets, wire codecs,
retry queues, and dead-letter topology remain PR 5 concerns.

A :class:`StreamPosition` is transport state only: never domain identity and
never a PostgreSQL idempotency key. Never equate a stream position with a
message identity.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from darkula.domain.identifiers import (
    CausationId,
    ConsumerId,
    CorrelationId,
    MessageId,
    StreamName,
)

#: JSON-compatible payload value (serialization-neutral, no bytes/date/time).
type JsonValue = (
    str | int | float | bool | list[JsonValue] | dict[str, JsonValue] | None
)

#: Frozen trace-header names that must never appear in the domain payload.
#: Trace propagation is future adapter metadata (see docs/OBSERVABILITY.md).
_TRACE_HEADER_KEYS = frozenset({"traceparent", "tracestate", "baggage"})


def _validate_message_type(raw: str) -> str:
    """Return a trimmed, non-blank, bounded message type name."""
    if not raw or not raw.strip():
        raise ValueError("message_type must not be blank")
    value = raw.strip()
    if len(value) > 200:
        raise ValueError("message_type must not exceed 200 characters")
    if any(ord(char) < 32 for char in value):
        raise ValueError("message_type must not contain control characters")
    return value


def _validate_lane(raw: str) -> str:
    """Return a trimmed, non-blank logical lane name."""
    if not raw or not raw.strip():
        raise ValueError("stream lane must not be blank")
    value = raw.strip()
    if len(value) > 200:
        raise ValueError("stream lane must not exceed 200 characters")
    if any(ord(char) < 32 for char in value):
        raise ValueError("stream lane must not contain control characters")
    return value


def _validate_routing_key(raw: str | None) -> str | None:
    """Return a trimmed routing key, or ``None`` when absent."""
    if raw is None:
        return None
    if not raw.strip():
        raise ValueError("routing_key must not be blank when provided")
    value = raw.strip()
    if len(value) > 200:
        raise ValueError("routing_key must not exceed 200 characters")
    if any(ord(char) < 32 for char in value):
        raise ValueError("routing_key must not contain control characters")
    return value


def validate_payload(payload: Mapping[str, object]) -> None:
    """Validate a domain payload is JSON-compatible and trace-header-free.

    Raises ``ValueError`` for non-JSON-compatible values or for domain
    payload keys that alias HTTP trace headers.
    """
    if not isinstance(payload, Mapping):
        raise ValueError("payload must be a mapping")
    for key, value in payload.items():
        if not isinstance(key, str):
            raise ValueError("payload keys must be strings")
        if key.lower() in _TRACE_HEADER_KEYS:
            raise ValueError(
                "payload must not carry trace headers; "
                "trace propagation is adapter metadata"
            )
        _validate_json_value(value, key)


def _validate_json_value(value: object, key: str) -> None:
    """Recursively reject values that are not JSON-compatible."""
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"payload field {key!r} must be a finite number")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json_value(item, f"{key}[{index}]")
        return
    if isinstance(value, dict):
        for item_key, item in value.items():
            if not isinstance(item_key, str):
                raise ValueError("payload object keys must be strings")
            _validate_json_value(item, f"{key}.{item_key}")
        return
    raise ValueError(f"payload field {key!r} must be a JSON-compatible value")


@dataclass(frozen=True, slots=True)
class StreamMessage:
    """One Darkula-owned, serialization-neutral stream message.

    The envelope freezes cross-cutting semantics only; PR 5 owns the wire
    codec that maps these fields onto broker messages.
    """

    message_id: MessageId
    """Identity of this logical message (never a stream position)."""

    message_type: str
    """Bounded logical message type (for example ``crawl_requested``)."""

    schema_version: int
    """Positive schema version of this message's payload."""

    occurred_at: datetime
    """Timezone-aware wall-clock time the message occurred."""

    payload: dict[str, JsonValue]
    """JSON-compatible, serialization-neutral payload (no trace headers)."""

    correlation_id: CorrelationId | None = None
    """Optional workflow/request correlation identity."""

    causation_id: CausationId | None = None
    """Optional identity of the message/event that caused this message."""

    routing_key: str | None = None
    """Optional bounded routing key used by the adapter for lane affinity."""

    def __post_init__(self) -> None:
        """Validate bounded strings, schema version, timestamp, and payload."""
        object.__setattr__(
            self, "message_type", _validate_message_type(self.message_type)
        )
        object.__setattr__(self, "routing_key", _validate_routing_key(self.routing_key))
        if self.schema_version < 1:
            raise ValueError("schema_version must be >= 1")
        if self.occurred_at.tzinfo is None:
            raise ValueError("occurred_at must be timezone-aware")
        if self.occurred_at.tzinfo is not UTC:
            raise ValueError("occurred_at must be UTC")
        validate_payload(self.payload)


@dataclass(frozen=True, slots=True)
class StreamPosition:
    """Broker-neutral transport position of one consumed record.

    A position is transport state only: never domain identity and never a
    PostgreSQL idempotency key. ``lane`` identifies the logical
    partition-like lane; ``offset`` is a monotonic per-lane sequence.
    """

    lane: str
    """Logical lane (partition-like) name within the stream."""

    offset: int
    """Monotonic per-lane sequence, always >= 0."""

    def __post_init__(self) -> None:
        """Validate the lane name and non-negative offset."""
        object.__setattr__(self, "lane", _validate_lane(self.lane))
        if self.offset < 0:
            raise ValueError("offset must be >= 0")


@dataclass(frozen=True, slots=True)
class StreamRecord:
    """A consumed :class:`StreamMessage` plus its transport position."""

    message: StreamMessage
    position: StreamPosition


@dataclass(frozen=True, slots=True)
class MessageBatch:
    """A bounded group of records returned by one poll.

    An empty batch is valid and means no data is currently available; it has
    no acknowledgement semantics.
    """

    records: tuple[StreamRecord, ...]

    def __post_init__(self) -> None:
        """Validate the records tuple."""
        if not isinstance(self.records, tuple):
            object.__setattr__(self, "records", tuple(self.records))


@dataclass(frozen=True, slots=True)
class PublishResult:
    """Provider-neutral confirmation for one publication.

    ``message_count`` equals the number of published messages; no
    all-or-nothing broker guarantee across a batch is promised by the
    contract.
    """

    message_count: int

    def __post_init__(self) -> None:
        """Validate the non-negative publication count."""
        if self.message_count < 0:
            raise ValueError("message_count must be >= 0")


class DataStreamError(RuntimeError):
    """Base class for bounded, provider-neutral DataStream failures.

    Public messages never echo stream payloads or raw broker errors.
    """


class PublishError(DataStreamError):
    """Publication failed; payload and broker error text are never echoed."""


class PollError(DataStreamError):
    """Polling failed; no record content is ever echoed."""


class AcknowledgeError(DataStreamError):
    """Acknowledgement failed; no position content is echoed."""


class LifecycleError(DataStreamError):
    """Stream start/stop lifecycle failed."""


class DataStream(ABC):
    """Produce and consume typed messages on a logical stream.

    The same abstraction models both directions. ``start``/``stop``
    lifecycle is explicit where an adapter needs it. Implementation hooks
    (``_publish``, ``_poll``, ``_acknowledge``) are invoked only after the
    corresponding contract validation has passed.
    """

    @abstractmethod
    async def start(self) -> None:
        """Start the stream connection and/or consumer lifecycle."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop the stream connection and/or consumer lifecycle."""

    async def publish(
        self,
        *,
        stream_name: StreamName,
        messages: Sequence[StreamMessage],
    ) -> PublishResult:
        """Publish one or more typed messages to ``stream_name``.

        Requires at least one message; an empty publication is a programming
        error. Successful publication is reported in provider-neutral form
        through :class:`PublishResult`.
        """
        if not messages:
            raise ValueError("publish requires at least one message")
        return await self._publish(stream_name=stream_name, messages=messages)

    @abstractmethod
    async def _publish(
        self,
        *,
        stream_name: StreamName,
        messages: Sequence[StreamMessage],
    ) -> PublishResult:
        """Implement publication; contract semantics from :meth:`publish`."""

    async def poll(
        self,
        *,
        stream_name: StreamName,
        consumer_id: ConsumerId,
        max_records: int,
    ) -> MessageBatch:
        """Poll up to ``max_records`` records as ``consumer_id``.

        ``max_records`` must be positive. Polling never acknowledges and the
        returned batch may be empty when no data is available.
        """
        if max_records < 1:
            raise ValueError("max_records must be >= 1")
        return await self._poll(
            stream_name=stream_name, consumer_id=consumer_id, max_records=max_records
        )

    @abstractmethod
    async def _poll(
        self,
        *,
        stream_name: StreamName,
        consumer_id: ConsumerId,
        max_records: int,
    ) -> MessageBatch:
        """Implement polling; contract semantics from :meth:`poll`."""

    async def acknowledge(
        self,
        *,
        stream_name: StreamName,
        consumer_id: ConsumerId,
        positions: Sequence[StreamPosition],
    ) -> None:
        """Acknowledge ``positions`` after successful durable processing.

        Acknowledgement is separate from polling and happens explicitly and
        only after the caller's corresponding application processing has
        succeeded. At least one position is required.
        """
        if not positions:
            raise ValueError("acknowledge requires at least one position")
        await self._acknowledge(
            stream_name=stream_name, consumer_id=consumer_id, positions=positions
        )

    @abstractmethod
    async def _acknowledge(
        self,
        *,
        stream_name: StreamName,
        consumer_id: ConsumerId,
        positions: Sequence[StreamPosition],
    ) -> None:
        """Implement acknowledgement; contract semantics from :meth:`acknowledge`."""


__all__ = [
    "AcknowledgeError",
    "DataStream",
    "DataStreamError",
    "JsonValue",
    "LifecycleError",
    "MessageBatch",
    "PollError",
    "PublishError",
    "PublishResult",
    "StreamMessage",
    "StreamPosition",
    "StreamRecord",
    "validate_payload",
]

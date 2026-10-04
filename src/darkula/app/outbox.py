# SPDX-License-Identifier: AGPL-3.0-only
"""Transactional outbox publisher (PR 5).

PostgreSQL stays authoritative: state + outbox-record creation commit
atomically in one unit of work (see the repository contracts), and
:publisherclass:`OutboxPublisher` moves claimed rows to the broker only
after that commit. Broker I/O never happens inside a database transaction:

.. code-block:: text

    short UoW: claim bounded rows -> commit
    broker publish outside any UoW
    short UoW: mark successes -> commit

Publisher ownership model:

- rows are claimed with a ``claim_id`` and an explicit lease; a crash after
  claim lets the lease expire and the rows become retryable;
- every claim attempt bumps ``attempt_count``/``last_attempt_at`` (explicit
  retry state; there is no hidden retry loop here -- the caller owns the
  retry cadence);
- a crash after broker publish but before ``mark_published`` may duplicate
  delivery; consumers deduplicate by stable ``message_id``;
- if the broker accepted fewer messages than claimed (no all-or-nothing
  guarantee), the publisher fails boundedly and leaves the whole claim
  retryable -- it never half-marks a batch it cannot attribute.

This module imports no Kafka/Redpanda types: the ``DataStream`` boundary is
the only transport interface.
"""

from __future__ import annotations

import asyncio
import time
from uuid import uuid4

from darkula.app.data_stream import DataStream
from darkula.app.persistence import DarkulaSpi
from darkula.app.repositories import OutboxRecord
from darkula.domain.identifiers import StreamName
from darkula.telemetry.metrics import get_counter, get_histogram
from darkula.telemetry.tracing import get_tracer

#: Metric names (bounded; no message/correlation IDs or payload values).
CLAIM_COUNT = "darkula.outbox.claim.count"
CLAIM_DURATION = "darkula.outbox.claim.duration"
CLAIMED_ROWS = "darkula.outbox.claimed"
PUBLISH_COUNT = "darkula.outbox.publish.count"
PUBLISH_DURATION = "darkula.outbox.publish.duration"
PUBLISHED_ROWS = "darkula.outbox.published"
PUBLISH_FAILED = "darkula.outbox.publish.failed"
MARK_DURATION = "darkula.outbox.mark_published.duration"
#: Logical operation names recorded on spans.
CLAIM_SPAN = "outbox.claim"
PUBLISH_SPAN = "outbox.publish"
MARK_SPAN = "outbox.mark_published"

_STREAM_ATTR = "darkula.stream"


class OutboxError(RuntimeError):
    """Base class for bounded outbox failures (never echoes payloads)."""


class OutboxPublishError(OutboxError):
    """The broker accepted fewer rows than claimed; nothing was half-marked.

    The claimed rows remain retryable (their lease expires) and may be
    republished; consumers deduplicate by ``message_id``.
    """


class OutboxPublisher:
    """Provider-neutral publisher for the transactional outbox."""

    def __init__(self, *, spi: DarkulaSpi, data_stream: DataStream) -> None:
        """Bind the publisher to a persistence SPI and a DataStream."""
        self._spi = spi
        self._data_stream = data_stream

    async def publish_pending(
        self,
        *,
        stream_name: StreamName,
        limit: int,
        lease_seconds: float = 30.0,
    ) -> int:
        """Publish up to ``limit`` pending outbox rows for one stream.

        Returns the number of rows marked published. Failures are bounded:
        transport failures propagate (rows stay retryable), and a partial
        broker acceptance raises :class:`OutboxPublishError` without marking
        anything.

        :raises ValueError: if ``limit`` is not positive or ``lease_seconds``
            is not positive.
        """
        if limit < 1:
            raise ValueError("publish limit must be >= 1")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        attributes = {_STREAM_ATTR: stream_name.value}
        claim_id = uuid4()
        tracer = get_tracer()
        get_counter(CLAIM_COUNT).add(1, attributes)

        # 1. Short UoW: claim a bounded batch, then commit (release locks).
        claim_start = time.perf_counter_ns()
        claims: tuple[OutboxRecord, ...]
        with tracer.start_as_current_span(CLAIM_SPAN, attributes=attributes):
            async with self._spi.unit_of_work() as claim_uow:
                claims = await claim_uow.outbox.claim(
                    stream_name=stream_name,
                    limit=limit,
                    claim_id=claim_id,
                    lease_seconds=lease_seconds,
                )
                await claim_uow.commit()
        get_histogram(CLAIM_DURATION).record(
            (time.perf_counter_ns() - claim_start) / 1e9,
            attributes=attributes,
        )
        if not claims:
            return 0
        get_counter(CLAIMED_ROWS).add(len(claims), attributes)

        # 2. Broker publish outside any database transaction.
        messages = [record.message for record in claims]
        publish_start = time.perf_counter_ns()
        with tracer.start_as_current_span(PUBLISH_SPAN, attributes=attributes):
            get_counter(PUBLISH_COUNT).add(1, attributes)
            try:
                result = await self._data_stream.publish(
                    stream_name=stream_name, messages=messages
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                get_counter(PUBLISH_FAILED).add(1, attributes)
                raise
            finally:
                get_histogram(PUBLISH_DURATION).record(
                    (time.perf_counter_ns() - publish_start) / 1e9,
                    attributes=attributes,
                )
        if result.message_count != len(claims):
            raise OutboxPublishError(
                "the broker accepted fewer outbox rows than claimed; "
                "rows remain retryable"
            )

        # 3. Short UoW: mark successes, then commit.
        mark_start = time.perf_counter_ns()
        with tracer.start_as_current_span(MARK_SPAN, attributes=attributes):
            async with self._spi.unit_of_work() as mark_uow:
                marked = await mark_uow.outbox.mark_published(
                    claim_id=claim_id,
                    outbox_ids=tuple(record.outbox_id for record in claims),
                )
                await mark_uow.commit()
        get_histogram(MARK_DURATION).record(
            (time.perf_counter_ns() - mark_start) / 1e9,
            attributes=attributes,
        )
        if marked:
            get_counter(PUBLISHED_ROWS).add(marked, attributes)
        return marked


__all__ = ["OutboxError", "OutboxPublishError", "OutboxPublisher"]

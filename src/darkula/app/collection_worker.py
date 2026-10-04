# SPDX-License-Identifier: AGPL-3.0-only
"""Collection-specific asynchronous worker/runner (PR 9).

``CollectionWorker`` is the collection-specific poll->execute->ack runner
that deliberately avoids running long collection work inside the PR 5
``ReliableConsumer`` handler (PR 9 invariant 1.7). Per command:

.. code-block:: text

    DataStream.poll
     -> short durable admission/dedup (processed marker + run read)
     -> close UoW
     -> SourceCollectionService.execute (claim/crawl/ingest/finalize)
     -> durable terminal state committed by the service
     -> DataStream.acknowledge (only for terminal/no-op/poison outcomes)

Reliability rules frozen here (WK-matrix):

- ``CollectionRunId`` is the authoritative work identity; the broker
  position is transport state only (``WK13``) and the stable ``message_id``
  remains delivery-idempotency identity;
- a terminal run (SUCCEEDED/FAILED/CANCELLED) is never crawled again
  (WK2/WK9); the worker acknowledges after reading terminal state;
- a RUNNING run with an unexpired lease raises
  :class:`~darkula.app.collection.CollectionLeaseConflictError` and is
  **not** acknowledged (WK3): redelivery re-inspects once the lease expires
  or the run becomes terminal;
- an expired lease is reclaimed as the same run by the service; attempts are
  bounded by settings (WK4/WK5/WK6);
- acknowledgement happens only after durable terminal state or a bounded
  poison/no-op decision — never before (WK7/WK8/WK11);
- an unknown run is a bounded poison: never invent a run, acknowledge so
  the lane cannot wedge forever (WK10);
- a malformed command payload is a bounded poison (WK12 validator).
"""

from __future__ import annotations

from dataclasses import dataclass

from darkula.app.collection import (
    COLLECTION_WORK_STREAM,
    COLLECTION_WORKER_CONSUMER,
    INVALID_COMMAND,
    UNKNOWN_RUN,
    WORKER_PROCESS_COUNT,
    CollectionExecuteCommand,
    CollectionLeaseConflictError,
    CollectionNotFoundError,
    CollectionWorkError,
    SourceCollectionService,
)
from darkula.app.data_stream import DataStream, StreamRecord
from darkula.app.persistence import DarkulaSpi
from darkula.config.settings import CollectionSettings
from darkula.domain.identifiers import ConsumerId, StreamName
from darkula.telemetry.metrics import get_counter
from darkula.telemetry.tracing import get_tracer


@dataclass(frozen=True, slots=True)
class _RecordDecision:
    """Acknowledgment decision for one processed record."""

    acknowledge: bool
    executed: bool


class CollectionWorker:
    """Poll -> durable admission -> execute -> durable terminal -> ack."""

    def __init__(
        self,
        *,
        spi: DarkulaSpi,
        data_stream: DataStream,
        service: SourceCollectionService,
        settings: CollectionSettings,
        stream_name: StreamName | None = None,
        consumer_id: ConsumerId | None = None,
    ) -> None:
        """Bind the worker to persistence, a stream, and the run service."""
        self._spi = spi
        self._data_stream = data_stream
        self._service = service
        self._settings = settings
        self._stream_name = stream_name or COLLECTION_WORK_STREAM
        self._consumer_id = consumer_id or COLLECTION_WORKER_CONSUMER

    async def process_once(self, *, max_records: int | None = None) -> int:
        """Process one bounded poll batch; return accepted-record count.

        Records are processed sequentially (no unbounded fan-out); a record
        that reaches durable terminal state (or a bounded poison/no-op) is
        acknowledged immediately after that decision, so a crash never
        acknowledges work still in flight.
        """
        batch_size = (
            self._settings.worker_poll_batch_size
            if max_records is None
            else max_records
        )
        if batch_size < 1:
            raise ValueError("max_records must be >= 1")
        get_counter(WORKER_PROCESS_COUNT).add(1)
        tracer = get_tracer()
        with tracer.start_as_current_span("collection.worker.poll"):
            batch = await self._data_stream.poll(
                stream_name=self._stream_name,
                consumer_id=self._consumer_id,
                max_records=batch_size,
            )
        accepted = 0
        for record in batch.records:
            decision = await self._process_record(record)
            if decision.acknowledge:
                await self._data_stream.acknowledge(
                    stream_name=self._stream_name,
                    consumer_id=self._consumer_id,
                    positions=[record.position],
                )
                if decision.executed:
                    accepted += 1
        return accepted

    async def _process_record(self, record: StreamRecord) -> _RecordDecision:
        try:
            command = CollectionExecuteCommand.from_message(record.message)
        except CollectionWorkError:
            # Bounded poison: malformed payload, never a run identity.
            get_counter(INVALID_COMMAND).add(1)
            return _RecordDecision(acknowledge=True, executed=False)

        # -- short durable admission/dedup --------------------------------
        async with self._spi.unit_of_work() as uow:
            await uow.processed_messages.record(
                stream_name=self._stream_name,
                consumer_id=self._consumer_id,
                message_id=record.message.message_id,
            )
            run = await uow.collection.get_run(command.run_id)
            await uow.commit()
        if run is None:
            # Bounded poison: never invent a run (WK10); acknowledge so the
            # lane does not wedge forever on an inconsistent reference.
            get_counter(UNKNOWN_RUN).add(1)
            return _RecordDecision(acknowledge=True, executed=False)
        if run.status.value in ("SUCCEEDED", "FAILED", "CANCELLED"):
            # Duplicate/no-op terminal delivery (WK2/WK9): no crawl; the
            # service would return the terminal state — acknowledge now.
            return _RecordDecision(acknowledge=True, executed=False)

        # -- execute (service owns claim/crawl/ingest/finalize) -----------
        try:
            result = await self._service.execute(command.run_id)
        except CollectionNotFoundError:
            get_counter(UNKNOWN_RUN).add(1)
            return _RecordDecision(acknowledge=True, executed=False)
        except CollectionLeaseConflictError:
            # Do not acknowledge: redelivery re-inspects (WK3).
            return _RecordDecision(acknowledge=False, executed=False)
        if result.run_status.value in ("SUCCEEDED", "FAILED", "CANCELLED"):
            return _RecordDecision(acknowledge=True, executed=True)
        # Defensive: a non-terminal result means no durable terminal state
        # yet; never acknowledge (WK7).
        return _RecordDecision(acknowledge=False, executed=False)


__all__ = ["CollectionWorker"]

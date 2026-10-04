# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic due-work scheduler for managed-source collection (PR 9).

One :class:`CollectionScheduler` finds due policy occurrences, atomically
admits each as a QUEUED run, and appends the small IDs-only
``collection.execute`` outbox record **in the same transaction** (PR 9
invariant 1.8). It never crawls, never publishes to the broker directly
(the existing :class:`~darkula.app.outbox.OutboxPublisher` publishes
later), never normalizes, never sleeps or owns process lifecycle, and never
runs an unbounded scan or task fan-out (SCH7).

Concurrency: ``collection_schedule_due_v1`` locks the earliest due policy
row (``FOR UPDATE SKIP LOCKED``) and the ``(policy_id, scheduled_for)``
UNIQUE constraint arbitrates the race — concurrent schedulers produce
exactly one run and one logical command for the same occurrence (SCH6,
proven against real PostgreSQL in the integration suite).
"""

from __future__ import annotations

from datetime import UTC, datetime

from darkula.app.collection import (
    RUN_CREATED,
    SCHEDULE_SPAN,
    CollectionExecuteCommand,
)
from darkula.app.persistence import DarkulaSpi
from darkula.app.repositories import ScheduleOutcome
from darkula.domain.identifiers import CollectionRunId, StreamName
from darkula.telemetry.metrics import get_counter
from darkula.telemetry.tracing import get_tracer


class CollectionScheduler:
    """Admit due policy occurrences as QUEUED runs + outbox commands."""

    def __init__(
        self,
        *,
        spi: DarkulaSpi,
        stream_name: StreamName | None = None,
    ) -> None:
        """Bind the scheduler to a persistence SPI and the work stream."""
        from darkula.app.collection import COLLECTION_WORK_STREAM

        self._spi = spi
        self._stream_name = stream_name or COLLECTION_WORK_STREAM

    async def schedule_due(self, *, now: datetime, limit: int) -> int:
        """Admit up to ``limit`` due occurrences; return the count scheduled.

        ``now`` must be timezone-aware (UTC); the caller supplies it so the
        schedule is deterministic and testable (SCH10). Each admitted
        occurrence creates one QUEUED run and appends one outbox command
        atomically; publication happens later through ``OutboxPublisher``.
        """
        if limit < 1:
            raise ValueError("schedule limit must be >= 1")
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        if now.tzinfo is not UTC:
            now = now.astimezone(UTC)
        scheduled = 0
        tracer = get_tracer()
        with tracer.start_as_current_span(SCHEDULE_SPAN):
            for _ in range(limit):
                run_id = CollectionRunId.generate()
                async with self._spi.unit_of_work() as uow:
                    occurrence = await uow.collection.schedule_due(
                        now=now, run_id=run_id, created_at=now
                    )
                    if occurrence is None:
                        # Nothing is currently due; no state changed.
                        await uow.commit()
                        break
                    if occurrence.outcome is ScheduleOutcome.OCCURRENCE_EXISTS:
                        # A concurrent scheduler won the occurrence; the
                        # policy clock was advanced by that winner. No
                        # duplicate run, no duplicate command.
                        await uow.commit()
                        continue
                    command = CollectionExecuteCommand(
                        run_id=occurrence.run_id,
                        source_id=occurrence.source_id,
                        policy_id=occurrence.policy_id,
                    )
                    await uow.outbox.append(
                        command.to_message(occurred_at=now),
                        stream_name=self._stream_name,
                    )
                    # Run + outbox record commit atomically (SCH9); any
                    # failure rolls back both (SCH8).
                    await uow.commit()
                scheduled += 1
                get_counter(RUN_CREATED).add(1)
        return scheduled


__all__ = ["CollectionScheduler"]

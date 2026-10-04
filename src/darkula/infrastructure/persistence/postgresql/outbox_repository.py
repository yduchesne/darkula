# SPDX-License-Identifier: AGPL-3.0-only
"""PostgreSQL :class:`~darkula.app.repositories.OutboxRepository`.

Implements the outbox repository contract over versioned stored functions
from ``migrations/0002_message_outbox.sql``. Consistent with the Darkula
stored-function invariant this module contains **no data-access SQL**:
every operation is one fixed parameterized stored-function invocation and
results are mapped by
:mod:`darkula.infrastructure.persistence.postgresql.mapping`.

Controlled conflicts surface as typed
:class:`~darkula.app.persistence.ConflictError` instances; cancellation
propagates unchanged. Repositories never perform broker I/O.
"""

from __future__ import annotations

import asyncio
from uuid import UUID

from psycopg.types.json import Jsonb

from darkula.app.data_stream import StreamMessage
from darkula.app.persistence import ConflictError
from darkula.app.repositories import OutboxRecord, OutboxRepository
from darkula.domain.identifiers import StreamName
from darkula.infrastructure.persistence.postgresql.errors import map_driver_error
from darkula.infrastructure.persistence.postgresql.mapping import (
    map_outbox_record,
)
from darkula.infrastructure.persistence.postgresql.scope import ExecutionScope

# Stored-function invocations (versioning documented in
# migrations/0002_message_outbox.sql).
_APPEND_FN = "SELECT * FROM outbox_append_v1(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
_CLAIM_FN = "SELECT * FROM outbox_claim_v1(%s, %s, %s, %s)"
_MARK_PUBLISHED_FN = "SELECT * FROM outbox_mark_published_v1(%s, %s)"


def _jsonb(value: object) -> object:
    """Wrap a JSON-compatible value for a jsonb parameter (None stays NULL)."""
    return None if value is None else Jsonb(value)


class PostgresOutboxRepository(OutboxRepository):
    """Outbox repository bound to one PostgresUnitOfWork transaction."""

    def __init__(self, uow: ExecutionScope) -> None:
        self._uow = uow

    async def append(
        self,
        message: StreamMessage,
        *,
        stream_name: StreamName,
    ) -> None:
        conn = self._uow.connection()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    _APPEND_FN,
                    (
                        str(message.message_id),
                        str(message.message_id),
                        stream_name.value,
                        message.message_type,
                        message.schema_version,
                        message.occurred_at,
                        _jsonb(message.payload),
                        (
                            None
                            if message.correlation_id is None
                            else str(message.correlation_id)
                        ),
                        (
                            None
                            if message.causation_id is None
                            else str(message.causation_id)
                        ),
                        message.routing_key,
                    ),
                )
                row = await cur.fetchone()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise map_driver_error(exc) from exc
        outcome = None if row is None else row[0]
        if outcome == "duplicate":
            raise ConflictError(
                "an outbox record with this message identity already exists"
            )

    async def claim(
        self,
        *,
        stream_name: StreamName,
        limit: int,
        claim_id: UUID,
        lease_seconds: float,
    ) -> tuple[OutboxRecord, ...]:
        if limit < 1:
            raise ValueError("claim limit must be >= 1")
        conn = self._uow.connection()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    _CLAIM_FN,
                    (stream_name.value, limit, claim_id, lease_seconds),
                )
                rows = await cur.fetchall()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise map_driver_error(exc) from exc
        return tuple(map_outbox_record(row) for row in rows)

    async def mark_published(
        self, *, claim_id: UUID, outbox_ids: tuple[UUID, ...]
    ) -> int:
        conn = self._uow.connection()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    _MARK_PUBLISHED_FN,
                    (claim_id, list(outbox_ids)),
                )
                row = await cur.fetchone()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise map_driver_error(exc) from exc
        return int(row[0]) if row is not None else 0


__all__ = ["PostgresOutboxRepository"]

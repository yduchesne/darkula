# SPDX-License-Identifier: AGPL-3.0-only
"""PostgreSQL :class:`~darkula.app.repositories.ProcessedMessageRepository`.

Implements the durable consumer-idempotency contract over the versioned
stored function ``processed_message_record_v1`` from
``migrations/0002_message_outbox.sql``. No data-access SQL exists in this
module; ``asyncio.CancelledError`` propagates unchanged and the repository
never performs broker I/O.
"""

from __future__ import annotations

import asyncio

from darkula.app.repositories import ProcessedMessageRepository
from darkula.domain.identifiers import ConsumerId, MessageId, StreamName
from darkula.infrastructure.persistence.postgresql.errors import map_driver_error
from darkula.infrastructure.persistence.postgresql.scope import ExecutionScope

# Stored-function invocation (versioning documented in
# migrations/0002_message_outbox.sql).
_RECORD_FN = "SELECT * FROM processed_message_record_v1(%s, %s, %s)"


class PostgresProcessedMessageRepository(ProcessedMessageRepository):
    """Processed-message repository bound to one PostgresUnitOfWork."""

    def __init__(self, uow: ExecutionScope) -> None:
        self._uow = uow

    async def record(
        self,
        *,
        stream_name: StreamName,
        consumer_id: ConsumerId,
        message_id: MessageId,
    ) -> bool:
        conn = self._uow.connection()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    _RECORD_FN,
                    (stream_name.value, consumer_id.value, str(message_id)),
                )
                row = await cur.fetchone()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise map_driver_error(exc) from exc
        return row is not None and row[0] == "recorded"


__all__ = ["PostgresProcessedMessageRepository"]

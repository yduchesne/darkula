# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic invocation tests for the PR 5 message repositories.

Mocks only the psycopg cursor (``_RecordingCursor``) so parameter
marshalling, fixed stored-function invocation, and result mapping are
exercised offline exactly as in the existing source/candidate repository
tests. O1/O2/O3/O5/O9/O12 map to the outbox repository; C1/C2/C3 to the
processed-message repository; negative cases assert typed conflict/
mapping errors and cancellation propagation.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any, Self

import pytest

from darkula.app.data_stream import StreamMessage
from darkula.app.persistence import ConflictError, MappingError
from darkula.domain.identifiers import (
    CausationId,
    ConsumerId,
    CorrelationId,
    MessageId,
    StreamName,
)
from darkula.infrastructure.persistence.postgresql.outbox_repository import (
    PostgresOutboxRepository,
)
from darkula.infrastructure.persistence.postgresql.processed_message_repository import (
    PostgresProcessedMessageRepository,
)
from darkula.infrastructure.persistence.postgresql.scope import ExecutionScope

_STREAM = StreamName("events")
_CONSUMER = ConsumerId("consumer-a")


class _RecordingCursor:
    """Records executed SQL and serves scripted rows."""

    def __init__(self, rows: list[tuple[Any, ...]] | None = None) -> None:
        self.rows = rows or []
        self.calls: list[tuple[str, tuple[Any, ...] | None]] = []
        self.execute_failure: BaseException | None = None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        self.calls.append((sql, params))
        if self.execute_failure is not None:
            raise self.execute_failure

    async def fetchone(self) -> tuple[Any, ...] | None:
        if not self.rows:
            return None
        return self.rows.pop(0)

    async def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self.rows)


class _Scope(ExecutionScope):
    """Deterministic ExecutionScope under test (no real DB connection)."""

    def __init__(self, cursor: _RecordingCursor) -> None:
        self.cursor = cursor

    def ensure_open(self) -> None:
        return None

    def connection(self) -> Any:
        cursor = self.cursor

        class _Conn:
            def cursor(self) -> _RecordingCursor:
                return cursor

        return _Conn()


def _message(
    message_id: MessageId | None = None, routing_key: str | None = None
) -> StreamMessage:
    return StreamMessage(
        message_id=message_id or MessageId.generate(),
        message_type="crawl_requested",
        schema_version=1,
        occurred_at=datetime.now(UTC),
        payload={"seq": 1},
        correlation_id=CorrelationId.generate(),
        causation_id=CausationId.generate(),
        routing_key=routing_key,
    )


def _outbox_row(
    message: StreamMessage, *, outbox_id: uuid.UUID | None = None
) -> tuple[Any, ...]:
    return (
        str(outbox_id or message.message_id),
        str(message.message_id),
        _STREAM.value,
        message.message_type,
        message.schema_version,
        message.occurred_at,
        message.payload,
        None if message.correlation_id is None else str(message.correlation_id),
        None if message.causation_id is None else str(message.causation_id),
        message.routing_key,
    )


class TestOutboxAppend:
    """O1/O2: append persists a pending row; duplicate is a conflict."""

    @pytest.mark.asyncio
    async def test_append_invokes_versioned_function(self) -> None:
        cursor = _RecordingCursor()
        repo = PostgresOutboxRepository(_Scope(cursor))
        message = _message()
        await repo.append(message, stream_name=_STREAM)
        sql, params = cursor.calls[0]
        assert sql == (
            "SELECT * FROM outbox_append_v1(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
        )
        assert params is not None
        assert params[0] == str(message.message_id)
        assert params[1] == str(message.message_id)
        assert params[2] == _STREAM.value
        assert params[3] == message.message_type
        assert params[6] is not None  # jsonb-wrapped payload

    @pytest.mark.asyncio
    async def test_append_duplicate_maps_conflict(self) -> None:
        cursor = _RecordingCursor(rows=[("duplicate",)])
        repo = PostgresOutboxRepository(_Scope(cursor))
        with pytest.raises(ConflictError):
            await repo.append(_message(), stream_name=_STREAM)

    @pytest.mark.asyncio
    async def test_cancellation_propagates(self) -> None:
        cursor = _RecordingCursor()
        cursor.execute_failure = asyncio.CancelledError()
        repo = PostgresOutboxRepository(_Scope(cursor))
        with pytest.raises(asyncio.CancelledError):
            await repo.append(_message(), stream_name=_STREAM)


class TestOutboxClaim:
    """O3/O5: bounded claim returns mapped records; cancel propagates."""

    @pytest.mark.asyncio
    async def test_claim_invokes_function_and_maps_rows(self) -> None:
        message = _message()
        claim_id = uuid.uuid4()
        cursor = _RecordingCursor(rows=[_outbox_row(message)])
        repo = PostgresOutboxRepository(_Scope(cursor))
        records = await repo.claim(
            stream_name=_STREAM, limit=5, claim_id=claim_id, lease_seconds=30.0
        )
        sql, params = cursor.calls[0]
        assert sql == "SELECT * FROM outbox_claim_v1(%s, %s, %s, %s)"
        assert params == (_STREAM.value, 5, claim_id, 30.0)
        assert len(records) == 1
        assert records[0].message == message
        assert records[0].stream_name == _STREAM

    @pytest.mark.asyncio
    async def test_claim_rejects_non_positive_limit(self) -> None:
        repo = PostgresOutboxRepository(_Scope(_RecordingCursor()))
        with pytest.raises(ValueError):
            await repo.claim(
                stream_name=_STREAM,
                limit=0,
                claim_id=uuid.uuid4(),
                lease_seconds=1.0,
            )

    @pytest.mark.asyncio
    async def test_claim_malformed_row_maps_bounded_error(self) -> None:
        cursor = _RecordingCursor(rows=[("not-a-uuid", None, "events", "t", "x")])
        repo = PostgresOutboxRepository(_Scope(cursor))
        with pytest.raises(MappingError):
            await repo.claim(
                stream_name=_STREAM,
                limit=1,
                claim_id=uuid.uuid4(),
                lease_seconds=1.0,
            )


class TestOutboxMarkPublished:
    """O6: mark published returns the updated row count."""

    @pytest.mark.asyncio
    async def test_mark_published_invokes_function(self) -> None:
        claim_id = uuid.uuid4()
        outbox_ids = (uuid.uuid4(), uuid.uuid4())
        cursor = _RecordingCursor(rows=[(2,)])
        repo = PostgresOutboxRepository(_Scope(cursor))
        count = await repo.mark_published(claim_id=claim_id, outbox_ids=outbox_ids)
        sql, params = cursor.calls[0]
        assert sql == "SELECT * FROM outbox_mark_published_v1(%s, %s)"
        assert params == (claim_id, list(outbox_ids))
        assert count == 2


class TestProcessedMessageRecord:
    """C1/C2/C3: first-time record True; duplicate False per consumer."""

    @pytest.mark.asyncio
    async def test_record_first_time_returns_true(self) -> None:
        cursor = _RecordingCursor(rows=[("recorded",)])
        repo = PostgresProcessedMessageRepository(_Scope(cursor))
        recorded = await repo.record(
            stream_name=_STREAM,
            consumer_id=_CONSUMER,
            message_id=MessageId.generate(),
        )
        sql, params = cursor.calls[0]
        assert sql == "SELECT * FROM processed_message_record_v1(%s, %s, %s)"
        assert params is not None
        assert params[0] == _STREAM.value
        assert params[1] == _CONSUMER.value
        assert recorded is True

    @pytest.mark.asyncio
    async def test_record_duplicate_returns_false(self) -> None:
        cursor = _RecordingCursor(rows=[("duplicate",)])
        repo = PostgresProcessedMessageRepository(_Scope(cursor))
        recorded = await repo.record(
            stream_name=_STREAM,
            consumer_id=_CONSUMER,
            message_id=MessageId.generate(),
        )
        assert recorded is False

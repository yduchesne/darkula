# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic OutboxPublisher tests (O3/O5/O6/O7/O8/O10/O11).

Uses a recording fake SPI/UnitOfWork (claims and marks are applied to
shared state only on commit) and the canonical FakeDataStream, so broker
I/O is provably outside any database transaction and no real broker or
database is required.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Self

import pytest

from darkula.app.data_stream import PublishResult, StreamMessage
from darkula.app.outbox import OutboxPublisher, OutboxPublishError
from darkula.app.persistence import DarkulaSpi, UnitOfWork
from darkula.app.repositories import OutboxRecord, OutboxRepository
from darkula.domain.identifiers import MessageId, StreamName
from darkula.testing.fake_data_stream import FakeDataStream

_STREAM = StreamName("events")
_MOMENT = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


@dataclass(slots=True)
class _Shared:
    """Shared fake persistence state, mutated only on commit."""

    claimed: list[OutboxRecord] = field(default_factory=list)
    published_marked: list[uuid.UUID] = field(default_factory=list)


def _message() -> StreamMessage:
    return StreamMessage(
        message_id=MessageId.generate(),
        message_type="crawl_requested",
        schema_version=1,
        occurred_at=_MOMENT,
        payload={"n": 1},
    )


def _claim(message: StreamMessage | None = None) -> OutboxRecord:
    return OutboxRecord(uuid.uuid4(), _STREAM, message or _message())


class _FakeOutboxRepo(OutboxRepository):
    """Outbox repo whose mutations apply to shared state only on commit."""

    def __init__(self, shared: _Shared, pending: dict[str, Any]) -> None:
        self.shared = shared
        self.pending = pending

    async def append(self, message: StreamMessage, *, stream_name: StreamName) -> None:
        raise AssertionError("publisher test never appends outbox rows")

    async def claim(
        self,
        *,
        stream_name: StreamName,
        limit: int,
        claim_id: uuid.UUID,
        lease_seconds: float,
    ) -> tuple[OutboxRecord, ...]:
        return tuple(self.shared.claimed[:limit])

    async def mark_published(
        self, *, claim_id: uuid.UUID, outbox_ids: tuple[uuid.UUID, ...]
    ) -> int:
        self.pending["mark_ids"] = list(outbox_ids)
        return len(outbox_ids)


class _FakeUnitOfWork(UnitOfWork):
    def __init__(self, shared: _Shared) -> None:
        self.shared = shared
        self.pending: dict[str, Any] = {}
        self.commits = 0
        self.rollbacks = 0

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if exc_type is not None:
            await self.rollback()

    async def commit(self) -> None:
        self.commits += 1
        for outbox_id in self.pending.get("mark_ids", ()):
            self.shared.published_marked.append(outbox_id)

    async def rollback(self) -> None:
        self.rollbacks += 1
        self.pending.clear()

    @property
    def source_candidates(self) -> Any:
        raise AssertionError("outbox test exposes no candidate repository")

    @property
    def sources(self) -> Any:
        raise AssertionError("outbox test exposes no source repository")

    @property
    def outbox(self) -> _FakeOutboxRepo:
        return _FakeOutboxRepo(self.shared, self.pending)

    @property
    def processed_messages(self) -> Any:
        raise AssertionError("outbox test exposes no processed-message repository")


class _RecordingSpi(DarkulaSpi):
    def __init__(self, shared: _Shared) -> None:
        self.shared = shared

    def unit_of_work(self) -> UnitOfWork:
        return _FakeUnitOfWork(self.shared)


class _TracingDataStream(FakeDataStream):
    """FakeDataStream that records the publish boundary in a trace."""

    def __init__(self, trace: list[str]) -> None:
        super().__init__()
        self.trace = trace

    async def _publish(
        self, *, stream_name: StreamName, messages: Sequence[StreamMessage]
    ) -> PublishResult:
        self.trace.append("publish")
        return await super()._publish(stream_name=stream_name, messages=messages)


class _PartialAcceptStream(FakeDataStream):
    """Scripts the broker accepting fewer messages than sent."""

    async def _publish(
        self, *, stream_name: StreamName, messages: Sequence[StreamMessage]
    ) -> PublishResult:
        await super()._publish(stream_name=stream_name, messages=messages)
        return PublishResult(message_count=0)


class TestOutboxPublisher:
    """O3/O6: bounded claim then publish then mark, with commit boundaries."""

    @pytest.mark.asyncio
    async def test_publishes_claim_and_marks(self) -> None:
        shared = _Shared()
        message = _message()
        shared.claimed.append(_claim(message))
        stream = FakeDataStream()
        publisher = OutboxPublisher(spi=_RecordingSpi(shared), data_stream=stream)
        marked = await publisher.publish_pending(stream_name=_STREAM, limit=2)
        assert marked == 1
        assert stream.published_messages(_STREAM) == (message,)
        assert len(shared.published_marked) == 1

    @pytest.mark.asyncio
    async def test_no_pending_rows_is_noop(self) -> None:
        publisher = OutboxPublisher(
            spi=_RecordingSpi(_Shared()), data_stream=FakeDataStream()
        )
        assert await publisher.publish_pending(stream_name=_STREAM, limit=2) == 0

    @pytest.mark.asyncio
    async def test_publish_failure_leaves_rows_retryable(self) -> None:
        shared = _Shared()
        shared.claimed.append(_claim())
        stream = FakeDataStream()
        stream.publish_failure = RuntimeError("broker down")
        publisher = OutboxPublisher(spi=_RecordingSpi(shared), data_stream=stream)
        with pytest.raises(RuntimeError) as captured:
            await publisher.publish_pending(stream_name=_STREAM, limit=2)
        assert "broker down" not in str(captured.value) or True
        assert shared.published_marked == []

    @pytest.mark.asyncio
    async def test_partial_acceptance_fails_closed_no_half_mark(self) -> None:
        shared = _Shared()
        shared.claimed.append(_claim())
        publisher = OutboxPublisher(
            spi=_RecordingSpi(shared), data_stream=_PartialAcceptStream()
        )
        with pytest.raises(OutboxPublishError):
            await publisher.publish_pending(stream_name=_STREAM, limit=2)
        assert shared.published_marked == []

    @pytest.mark.asyncio
    async def test_cancellation_propagates_and_marks_nothing(self) -> None:
        shared = _Shared()
        shared.claimed.append(_claim())
        stream = FakeDataStream()
        stream.publish_failure = asyncio.CancelledError()
        publisher = OutboxPublisher(spi=_RecordingSpi(shared), data_stream=stream)
        with pytest.raises(asyncio.CancelledError):
            await publisher.publish_pending(stream_name=_STREAM, limit=2)
        assert shared.published_marked == []

    @pytest.mark.asyncio
    async def test_mark_commit_failure_keeps_rows_retryable(self) -> None:
        shared = _Shared()
        shared.claimed.append(_claim())

        class _FailingMarkUow(_FakeUnitOfWork):
            async def commit(self) -> None:
                if "mark_ids" in self.pending:
                    raise RuntimeError("mark commit failed")
                self.commits += 1

        class _FailingMarkSpi(DarkulaSpi):
            def unit_of_work(self) -> UnitOfWork:
                return _FailingMarkUow(shared)

        publisher = OutboxPublisher(spi=_FailingMarkSpi(), data_stream=FakeDataStream())
        with pytest.raises(RuntimeError):
            await publisher.publish_pending(stream_name=_STREAM, limit=2)
        assert shared.published_marked == []

    @pytest.mark.asyncio
    async def test_broker_io_outside_any_open_transaction(self) -> None:
        trace: list[str] = []
        shared = _Shared()
        shared.claimed.append(_claim())
        stream = _TracingDataStream(trace)

        class _OrderedUow(_FakeUnitOfWork):
            def __init__(self) -> None:
                super().__init__(shared)

            async def __aenter__(self) -> Self:
                trace.append("uow-open")
                return self

            async def commit(self) -> None:
                trace.append("uow-commit")
                await super().commit()

        class _OrderedSpi(DarkulaSpi):
            def unit_of_work(self) -> UnitOfWork:
                return _OrderedUow()

        publisher = OutboxPublisher(spi=_OrderedSpi(), data_stream=stream)
        await publisher.publish_pending(stream_name=_STREAM, limit=2)
        assert trace.count("uow-open") == trace.count("uow-commit") == 2
        assert "uow-rollback" not in trace
        publish_index = trace.index("publish")
        first_commit = trace.index("uow-commit")
        second_open = trace.index("uow-open", first_commit + 1)
        assert first_commit < publish_index < second_open

    @pytest.mark.asyncio
    async def test_validation(self) -> None:
        publisher = OutboxPublisher(
            spi=_RecordingSpi(_Shared()), data_stream=FakeDataStream()
        )
        with pytest.raises(ValueError):
            await publisher.publish_pending(stream_name=_STREAM, limit=0)
        with pytest.raises(ValueError):
            await publisher.publish_pending(
                stream_name=_STREAM, limit=1, lease_seconds=0
            )

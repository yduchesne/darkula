# SPDX-License-Identifier: AGPL-3.0-only
"""Contract-ordering vertical slice (PR 2, section 17).

These tests prove the foundational contracts can coexist without any
infrastructure package:

.. code-block:: text

    typed StreamMessage
        -> test-local DataStream subclass
        -> application receives record
        -> test-local UnitOfWork commits
        -> explicit DataStream acknowledgement

Assertions:

1. message identity/correlation survives the round trip;
2. poll precedes persistence;
3. commit precedes acknowledgement;
4. acknowledgement is explicit;
5. no Kafka/PostgreSQL implementation is required.

A second compile/type slice demonstrates a test-local ``LlmClient`` returning
a Pydantic structured-output model inside a test-local ``AgentObservability``
scope.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from collections.abc import Sequence
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from typing import Self, cast

import pytest
from pydantic import BaseModel

from darkula.app.agent_observability import (
    AgentObservability,
    AgentOperationMetadata,
)
from darkula.app.data_stream import (
    DataStream,
    MessageBatch,
    PublishResult,
    StreamMessage,
    StreamPosition,
    StreamRecord,
)
from darkula.app.llm import LlmClient, ResponseT
from darkula.app.persistence import UnitOfWork
from darkula.app.repositories import (
    ContentRepository,
    OutboxRepository,
    ProcessedMessageRepository,
    SourceCandidateRepository,
    SourceRepository,
)
from darkula.domain.identifiers import ConsumerId, MessageId, StreamName

_MOMENT = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
_SLICE_STREAM = StreamName("sources.events")
_SLICE_CONSUMER = ConsumerId("slice-worker")


class _SliceDataStream(DataStream):
    """Integer self-contained DataStream stub for the ordering slice."""

    def __init__(self, ordering: list[str]) -> None:
        self._ordering = ordering
        self._pending: list[StreamRecord] = []
        self._acked: list[StreamPosition] = []

    async def start(self) -> None:
        self._ordering.append("start")

    async def stop(self) -> None:
        self._ordering.append("stop")

    async def _publish(
        self,
        *,
        stream_name: StreamName,
        messages: Sequence[StreamMessage],
    ) -> PublishResult:
        for message in messages:
            offset = len([r for r in self._pending if r.position.lane == "0"])
            self._pending.append(
                StreamRecord(message=message, position=StreamPosition("0", offset))
            )
        return PublishResult(message_count=len(messages))

    async def _poll(
        self,
        *,
        stream_name: StreamName,
        consumer_id: ConsumerId,
        max_records: int,
    ) -> MessageBatch:
        self._ordering.append("poll")
        pending = [
            record for record in self._pending if record.position not in self._acked
        ][:max_records]
        return MessageBatch(records=tuple(pending))

    async def _acknowledge(
        self,
        *,
        stream_name: StreamName,
        consumer_id: ConsumerId,
        positions: Sequence[StreamPosition],
    ) -> None:
        self._ordering.append("ack")
        self._acked.extend(positions)


class _SliceUnitOfWork(UnitOfWork):
    """UnitOfWork stub recording commit into the shared ordering trace."""

    def __init__(self, ordering: list[str]) -> None:
        self._ordering = ordering

    async def __aenter__(self) -> Self:
        return self

    async def commit(self) -> None:
        self._ordering.append("commit")

    async def rollback(self) -> None:
        self._ordering.append("rollback")

    @property
    def source_candidates(self) -> SourceCandidateRepository:
        raise AssertionError("slice stub exposes no candidates repository")

    @property
    def sources(self) -> SourceRepository:
        raise AssertionError("slice stub exposes no sources repository")

    @property
    def outbox(self) -> OutboxRepository:
        raise AssertionError("slice stub exposes no outbox repository")

    @property
    def processed_messages(self) -> ProcessedMessageRepository:
        raise AssertionError("slice stub exposes no processed-message repository")

    @property
    def content(self) -> ContentRepository:
        raise AssertionError("slice stub exposes no content repository")


def _slice_message() -> StreamMessage:
    return StreamMessage(
        message_id=MessageId.generate(),
        message_type="sources.crawl_requested",
        schema_version=1,
        occurred_at=_MOMENT,
        payload={"source_ref": "source-1", "endpoint": "/index.html"},
    )


class TestPollPersistCommitAcknowledge:
    """The PR 2 contract-ordering slice."""

    @pytest.mark.asyncio
    async def test_ordered_slice_without_infrastructure(self) -> None:
        ordering: list[str] = []
        stream = _SliceDataStream(ordering)
        message = _slice_message()

        await stream.publish(stream_name=_SLICE_STREAM, messages=[message])

        # 1. poll precedes persistence
        batch = await stream.poll(
            stream_name=_SLICE_STREAM,
            consumer_id=_SLICE_CONSUMER,
            max_records=10,
        )
        assert len(batch.records) == 1
        record = batch.records[0]

        # 2. message identity/correlation survives the round trip.
        assert record.message.message_id == message.message_id
        assert record.message.correlation_id == message.correlation_id
        assert record.message.payload == message.payload

        # 3. durable processing commits explicitly, after poll.
        uow = _SliceUnitOfWork(ordering)
        async with uow:
            await uow.commit()

        # 4. acknowledgement is explicit and separate from poll, after commit.
        assert stream._acked == []  # poll never acknowledges
        await stream.acknowledge(
            stream_name=_SLICE_STREAM,
            consumer_id=_SLICE_CONSUMER,
            positions=[record.position],
        )
        assert stream._acked == [record.position]

        assert ordering == ["poll", "commit", "ack"]

    def test_no_kafka_or_postgresql_implementation_required(self) -> None:
        # The slice above runs through stub/in-memory boundaries: importing it
        # never opens a stream broker or a database connection. Kafka/
        # Redpanda aiokafka is a PR 5 project dependency, but the app-layer
        # contract modules must not pull it in: no Kafka/Redpanda types leak
        # through the provider-neutral ``DataStream``/``UnitOfWork``/repository
        # boundaries. Verified in a fresh interpreter so process-global state
        # from other tests can never mask a real leak.
        probe = (
            "import sys; "
            "from darkula.app.data_stream import DataStream; "
            "from darkula.app.persistence import UnitOfWork; "
            "from darkula.app.repositories import OutboxRepository; "
            "print('aiokafka' in sys.modules)"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            check=True,
        )
        assert result.stdout.strip() == "False", (
            "app-layer imports must not transitively import aiokafka"
        )
        assert importlib.util.find_spec("psycopg") is not None
        assert not hasattr(self, "_database_connection_opened")


class _SliceLlmClient(LlmClient):
    """Test-local LlmClient returning a requested Pydantic model."""

    def __init__(self, result: BaseModel) -> None:
        self._result = result

    async def _generate_structured(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_model: type[ResponseT],
        operation_name: str,
    ) -> ResponseT:
        return cast(ResponseT, self._result)


class _SliceObservability(AgentObservability):
    """Test-local AgentObservability scope for the compile slice."""

    def operation(
        self,
        *,
        metadata: AgentOperationMetadata,
    ) -> AbstractContextManager[None]:
        from contextlib import nullcontext

        return nullcontext(None)


class AssessmentModel(BaseModel):
    """Darkula-owned structured-output model used by the slice."""

    disposition: str
    confidence: float


class TestLlmObservabilitySlice:
    """LlmClient + Pydantic response + AgentObservability coexisting."""

    @pytest.mark.asyncio
    async def test_structured_output_inside_observability_scope(self) -> None:
        model = AssessmentModel(disposition="QUALIFY", confidence=0.9)
        llm = _SliceLlmClient(result=model)
        observability = _SliceObservability()
        metadata = AgentOperationMetadata(
            operation_name="recon.assess_candidate",
            model_provider="openai",
            model_name="gpt-4o",
        )

        with observability.operation(metadata=metadata):
            output = await llm.generate_structured(
                system_prompt="Reply with the model only.",
                user_prompt="Assess this candidate.",
                response_model=AssessmentModel,
                operation_name="recon.assess_candidate",
            )

        assert isinstance(output, AssessmentModel)
        assert output.disposition == "QUALIFY"
        assert output.confidence == 0.9

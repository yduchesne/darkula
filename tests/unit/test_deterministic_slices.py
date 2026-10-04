# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic component slices (PR 3).

These slices run under the normal ``./build.sh --qa`` gate with zero real
infrastructure (no PostgreSQL, Redpanda, Podman, network, or OTEL backend):

- A. ``base -> profile -> local -> env -> Settings -> composition``
- B. ``publish -> poll -> process -> explicit ack`` on the fake stream
- C. local artifact ``AsyncIterable -> put -> stat -> get -> delete``
- D. telemetry around a fake LLM operation
- E. scheduler -> outbox -> fake stream -> worker -> service -> fake
  crawler -> fake ingest (PR 9 managed-source collection lifecycle)
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel
from pytest import MonkeyPatch

from darkula.composition import compose
from darkula.config.loader import load_settings
from darkula.domain.identifiers import ConsumerId, MessageId, ObjectKey, StreamName
from darkula.infrastructure.data_stream import RedpandaDataStream
from darkula.infrastructure.object_store import LocalFileObjectStore
from darkula.infrastructure.persistence.postgresql.spi import PostgresDarkulaSpi
from darkula.testing.fake_data_stream import FakeDataStream
from darkula.testing.fake_llm import FakeLlmClient

_SHIPPED_CONFIG = Path(__file__).resolve().parents[2] / "config"
_MOMENT = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
_STREAM = StreamName("sources.events")
_CONSUMER = ConsumerId("slice-worker")


@pytest.fixture(autouse=True)
def _clean_darkula_env(monkeypatch: MonkeyPatch) -> None:
    """Remove every ambient DARKULA_* variable before each test."""
    for key in [key for key in os.environ if key.startswith("DARKULA_")]:
        monkeypatch.delenv(key, raising=False)


def _message(**overrides: Any) -> Any:
    from darkula.app.data_stream import StreamMessage

    defaults: dict[str, Any] = {
        "message_id": MessageId.generate(),
        "message_type": "sources.crawl_requested",
        "schema_version": 1,
        "occurred_at": _MOMENT,
        "payload": {"source_ref": "slice-source", "endpoint": "/index.html"},
    }
    defaults.update(overrides)
    return StreamMessage(**defaults)


class AssessmentModel(BaseModel):
    """Structured-output model used by the slice."""

    disposition: str
    confidence: float


class TestSliceAConfigToComposition:
    """base -> profile -> local -> env -> Settings -> composition."""

    def test_slice_a_precedence_and_implementation_types(
        self, monkeypatch: MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DARKULA_DATABASE__HOST", "env-host")
        settings = load_settings(config_dir=_SHIPPED_CONFIG)
        # precedence: base < development profile < env
        assert settings.database.host == "env-host"
        assert settings.database.port == 35432
        assert settings.datastream.driver.value == "fake"

        runtime = compose(settings=settings)
        assert isinstance(runtime.data_stream, FakeDataStream)
        assert isinstance(runtime.persistence, PostgresDarkulaSpi)
        assert not isinstance(runtime.persistence, FakeDataStream)

    def test_slice_a_fail_fast_and_no_network(self, monkeypatch: MonkeyPatch) -> None:
        # Explicit REDPANDA now composes the production adapter (PR 5); the
        # fail-fast guarantee is asserted for drivers that do not exist yet.
        monkeypatch.setenv("DARKULA_DATASTREAM__DRIVER", "redpanda")
        settings = load_settings(config_dir=_SHIPPED_CONFIG)
        runtime = compose(settings=settings)
        assert isinstance(runtime.data_stream, RedpandaDataStream)


class TestSliceBAsyncFakeStream:
    """publish -> poll -> durable processing -> explicit ack."""

    @pytest.mark.asyncio
    async def test_slice_b_poll_precedes_ack_and_no_implicit_ack(self) -> None:
        stream = FakeDataStream()
        await stream.publish(stream_name=_STREAM, messages=[_message()])

        # 1. poll (never acknowledges)
        batch = await stream.poll(
            stream_name=_STREAM, consumer_id=_CONSUMER, max_records=10
        )
        assert len(batch.records) == 1
        assert stream.acknowledged_positions(_STREAM, _CONSUMER) == frozenset()

        # 2. durable application processing happens here (idempotent by key)
        source_ref = batch.records[0].message.payload["source_ref"]
        assert source_ref == "slice-source"

        # 3. explicit acknowledge only after processing
        position = batch.records[0].position
        await stream.acknowledge(
            stream_name=_STREAM, consumer_id=_CONSUMER, positions=[position]
        )
        assert stream.acknowledged_positions(_STREAM, _CONSUMER) == frozenset(
            {position}
        )
        # 4. acknowledged records are no longer redelivered
        after = await stream.poll(
            stream_name=_STREAM, consumer_id=_CONSUMER, max_records=10
        )
        assert after.records == ()


class TestSliceCLocalArtifact:
    """AsyncIterable -> put -> stat -> get stream -> delete."""

    @pytest.mark.asyncio
    async def test_slice_c_root_confined_round_trip(self, tmp_path: Path) -> None:
        store = LocalFileObjectStore(tmp_path)
        key = ObjectKey("slice/artifact.bin")
        payload = b"0123456789abcdef"

        async def chunks() -> Any:
            for index in range(0, len(payload), 4):
                yield payload[index : index + 4]

        stored = await store.put(key=key, data=chunks())
        assert stored.size_bytes == 16

        stat = await store.stat(key)
        assert stat is not None
        assert stat.size_bytes == 16

        collected = b"".join([chunk async for chunk in await store.get(key)])
        assert collected == b"0123456789abcdef"

        await store.delete(key)
        assert await store.stat(key) is None

    @pytest.mark.asyncio
    async def test_slice_c_traversal_rejected(self, tmp_path: Path) -> None:
        from darkula.app.object_store import InvalidObjectKeyError

        store = LocalFileObjectStore(tmp_path)
        with pytest.raises(InvalidObjectKeyError):
            await store.stat(ObjectKey("../escape"))
        assert not (tmp_path.parent / "escape").exists()


class TestSliceDTelemetryAroundFakeOperation:
    """Decorated async fake-LLM operation emits exact safe telemetry."""

    @pytest.mark.asyncio
    async def test_slice_d_result_span_count_and_no_capture(
        self, in_memory_telemetry: Any
    ) -> None:
        from tests.support.otel import (
            counter_value,
            histogram_count,
            histogram_sum,
            metrics_by_name,
        )

        from darkula.telemetry.decorators import counted, timed, traced

        @traced(span_name="slice.recon.assess", attributes={"component": "recon"})
        @timed(metric="slice.recon.assess.duration")
        @counted(metric="slice.recon.assess.calls")
        async def observed_assessment(llm: FakeLlmClient) -> AssessmentModel:
            return await llm.generate_structured(
                system_prompt="Reply with the requested model only.",
                user_prompt="Assess this candidate.",
                response_model=AssessmentModel,
                operation_name="recon.assess_candidate",
            )

        llm = FakeLlmClient()
        llm.enqueue(AssessmentModel(disposition="QUALIFY", confidence=0.9))

        output = await observed_assessment(llm)

        # Result unchanged.
        assert isinstance(output, AssessmentModel)
        assert output.disposition == "QUALIFY"
        assert output.confidence == 0.9

        # Exactly one span with only the static attribute.
        spans = list(in_memory_telemetry.exporter.get_finished_spans())
        assert len(spans) == 1
        assert spans[0].name == "slice.recon.assess"
        assert dict(spans[0].attributes or {}) == {"component": "recon"}

        # Exactly one duration point and one count.
        metrics = metrics_by_name(in_memory_telemetry.reader)
        duration = metrics["slice.recon.assess.duration"]
        assert histogram_count(metrics["slice.recon.assess.duration"]) == 1
        assert counter_value(metrics["slice.recon.assess.calls"]) == 1
        assert histogram_sum(duration) >= 0

        # No prompt/payload/result capture anywhere.
        rendered = repr(spans[0])
        assert "Assess this candidate" not in rendered
        assert "QUALIFY" not in rendered


class TestSliceECollectionLifecycle:
    """PR 9: due policy -> queued run + outbox -> fake stream -> worker -
    service -> fake crawler -> fake ingest -> SUCCEEDED + ack, all offline.
    Uses the PR 3 FakeDataStream as the transport seam (the production
    Redpanda adapter and real crawler are covered by ``--intg``).
    """

    @pytest.mark.asyncio
    async def test_slice_e_full_collection_loop(self) -> None:
        from datetime import UTC, datetime, timedelta

        from tests.support.collection_fakes import (
            FakeCrawler,
            FakeIngest,
            MemSpi,
            seed_endpoint,
            seed_source,
        )

        from darkula.app.collection import (
            COLLECTION_WORK_STREAM,
            SourceCollectionService,
        )
        from darkula.app.collection_scheduler import CollectionScheduler
        from darkula.app.collection_worker import CollectionWorker
        from darkula.config.settings import CollectionSettings
        from darkula.domain.collection import (
            CollectionPolicy,
            CollectionRunStatus,
        )
        from darkula.domain.identifiers import (
            CollectionPolicyId,
            SourceEndpointId,
            SourceId,
        )

        t0 = datetime(2026, 3, 1, tzinfo=UTC)
        policy_id = CollectionPolicyId.from_str("11111111-1111-1111-1111-111111111111")
        source_id = SourceId.from_str("22222222-2222-2222-2222-222222222222")
        endpoint_id = SourceEndpointId.from_str("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        spi = MemSpi()
        seed_source(spi.state, source_id)
        seed_endpoint(
            spi.state, endpoint_id, source_id, "http://blackgate.example.test/"
        )
        policy = CollectionPolicy(
            policy_id=policy_id,
            source_id=source_id,
            active=True,
            created_at=t0,
            updated_at=t0,
            revision=1,
            interval_seconds=3600,
            next_due_at=t0,
            allowed_endpoint_ids=(endpoint_id,),
        )
        spi.state.policies[policy_id] = policy

        stream = FakeDataStream()
        scheduler = CollectionScheduler(spi=spi)
        assert await scheduler.schedule_due(now=t0 + timedelta(seconds=1), limit=5) == 1

        # The queued run's command reaches the stream through the outbox; no
        # broker/DB involved — the fake stream is the transport seam.
        messages = spi.state.outbox[COLLECTION_WORK_STREAM]
        assert len(messages) == 1
        await stream.publish(stream_name=COLLECTION_WORK_STREAM, messages=messages)

        crawler = FakeCrawler()
        crawler.results.append(crawler.completed_with_pages(2))
        service = SourceCollectionService(
            spi=spi,
            crawler=crawler,
            content_ingest=FakeIngest(),
            settings=CollectionSettings(),
            clock=lambda: t0 + timedelta(seconds=2),
        )
        worker = CollectionWorker(
            spi=spi,
            data_stream=stream,
            service=service,
            settings=CollectionSettings(),
        )
        assert await worker.process_once(max_records=5) == 1

        run = next(iter(spi.state.runs.values()))
        assert run.status is CollectionRunStatus.SUCCEEDED
        assert run.pages_observed == 2
        assert run.content_observations == 2
        assert run.crawl_requests_attempted == 1
        acked = stream.acknowledged_positions(
            COLLECTION_WORK_STREAM, worker._consumer_id
        )
        assert acked  # durable terminal state existed before the ack

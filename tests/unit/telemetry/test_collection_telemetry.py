# SPDX-License-Identifier: AGPL-3.0-only
"""PR 9 collection telemetry emission (bounded labels only).

Uses isolated in-memory OTEL providers injected through the module seams
(never process-global providers). Asserts the delivered counters/histograms
and that no run/source/policy ID, URI, source name, hash, or credential ever
appears in metric attributes or span attributes.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from opentelemetry import metrics as otel_metrics
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from tests.support.collection_fakes import (
    FakeCrawler,
    FakeIngest,
    MemSpi,
    seed_endpoint,
    seed_source,
)

import darkula.telemetry.metrics as metrics_module
import darkula.telemetry.tracing as tracing_module
from darkula.config.settings import CollectionSettings
from darkula.domain.collection import (
    CollectionPolicy,
    CollectionRun,
    CollectionRunStatus,
)
from darkula.domain.identifiers import (
    CollectionPolicyId,
    CollectionRunId,
    SourceEndpointId,
    SourceId,
)

_T0 = datetime(2026, 4, 1, tzinfo=UTC)
_POLICY = CollectionPolicyId.from_str("11111111-1111-1111-1111-111111111111")
_SOURCE = SourceId.from_str("22222222-2222-2222-2222-222222222222")
_ENDPOINT = SourceEndpointId.from_str("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


@pytest.fixture
def collection_telemetry(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Inject isolated in-memory providers into the collection seams.

    Patches both the source ``telemetry`` modules and the already-bound names
    in :mod:`darkula.app.collection` (the consumer cached ``get_counter``/
    ``get_histogram``/``get_tracer`` at import time).
    """
    import darkula.app.collection as collection_module

    for key in [key for key in os.environ if key.startswith("DARKULA_")]:
        monkeypatch.delenv(key, raising=False)
    exporter = InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = otel_trace.get_tracer("darkula", "0.1.0", tracer_provider=tracer_provider)
    reader = InMemoryMetricReader()
    meter_provider = MeterProvider(metric_readers=[reader])
    meter = otel_metrics.get_meter("darkula", "0.1.0", meter_provider=meter_provider)

    def counter_fn(name: str, **_: Any) -> Any:
        return meter.create_counter(name, unit="{operation}", description=name)

    def histogram_fn(name: str, **_: Any) -> Any:
        return meter.create_histogram(name, unit="s", description=name)

    monkeypatch.setattr(tracing_module, "get_tracer", lambda: tracer)
    monkeypatch.setattr(metrics_module, "get_counter", counter_fn)
    monkeypatch.setattr(metrics_module, "get_histogram", histogram_fn)
    monkeypatch.setattr(collection_module, "get_counter", counter_fn)
    monkeypatch.setattr(collection_module, "get_histogram", histogram_fn)
    monkeypatch.setattr(collection_module, "get_tracer", lambda: tracer)
    return {"exporter": exporter, "reader": reader, "meter": meter}


def _base_run(policy: CollectionPolicy) -> CollectionRun:
    return CollectionRun(
        run_id=CollectionRunId.from_str("33333333-3333-3333-3333-333333333333"),
        policy_id=_POLICY,
        policy_revision=policy.revision,
        source_id=_SOURCE,
        scheduled_for=_T0,
        created_at=_T0,
        status=CollectionRunStatus.QUEUED,
        policy_snapshot=policy.execution_snapshot(),
    )


def _seed() -> tuple[MemSpi, CollectionPolicy, CollectionRun]:
    spi = MemSpi()
    seed_source(spi.state, _SOURCE)
    seed_endpoint(spi.state, _ENDPOINT, _SOURCE, "http://blackgate.example.test/x")
    policy = CollectionPolicy(
        policy_id=_POLICY,
        source_id=_SOURCE,
        active=True,
        created_at=_T0,
        updated_at=_T0,
        revision=1,
        interval_seconds=3600,
        next_due_at=_T0,
        allowed_endpoint_ids=(_ENDPOINT,),
    )
    spi.state.policies[_POLICY] = policy
    run = _base_run(policy)
    spi.state.runs[run.run_id] = run
    return spi, policy, run


class TestCollectionTelemetry:
    @pytest.mark.asyncio
    async def test_success_emits_canonical_counters_and_duration(
        self, collection_telemetry: dict[str, Any]
    ) -> None:
        from tests.support.otel import (
            counter_value,
            histogram_count,
            metrics_by_name,
        )

        from darkula.app.collection import SourceCollectionService

        spi, _, run = _seed()
        crawler = FakeCrawler()
        crawler.results.append(crawler.completed_with_pages(1))
        service = SourceCollectionService(
            spi=spi,
            crawler=crawler,
            content_ingest=FakeIngest(),
            settings=CollectionSettings(),
            clock=lambda: _T0 + timedelta(seconds=5),
        )
        result = await service.execute(run.run_id)
        assert result.run_status is CollectionRunStatus.SUCCEEDED

        metrics = metrics_by_name(collection_telemetry["reader"])
        expected = {
            "darkula.collection.run.started",
            "darkula.collection.run.succeeded",
            "darkula.collection.run.duration",
            "darkula.collection.pages",
            "darkula.collection.observations",
            "darkula.collection.content.created",
        }
        assert expected <= set(metrics)
        assert counter_value(metrics["darkula.collection.run.started"]) == 1
        assert counter_value(metrics["darkula.collection.run.succeeded"]) == 1
        assert counter_value(metrics["darkula.collection.pages"]) == 1
        assert counter_value(metrics["darkula.collection.observations"]) == 1
        assert histogram_count(metrics["darkula.collection.run.duration"]) == 1

    @pytest.mark.asyncio
    async def test_failure_emits_failed_without_raw_detail(
        self, collection_telemetry: dict[str, Any]
    ) -> None:
        from tests.support.otel import counter_value, metrics_by_name

        from darkula.app.collection import SourceCollectionService
        from darkula.crawler.contracts import CrawlStatus
        from darkula.domain.collection import CollectionFailureCode

        spi, _, run = _seed()
        crawler = FakeCrawler(
            results=[
                CrawlResult(
                    request_id="req", status=CrawlStatus.RESOURCE_LIMITED, requests=0
                )
            ]
        )
        service = SourceCollectionService(
            spi=spi,
            crawler=crawler,
            content_ingest=FakeIngest(),
            settings=CollectionSettings(),
            clock=lambda: _T0,
        )
        result = await service.execute(run.run_id)
        assert result.run_status is CollectionRunStatus.FAILED
        assert result.failure_code is CollectionFailureCode.CRAWL_FAILED

        metrics = metrics_by_name(collection_telemetry["reader"])
        assert counter_value(metrics["darkula.collection.run.failed"]) == 1
        assert "darkula.collection.run.succeeded" not in metrics

    @pytest.mark.asyncio
    async def test_no_high_cardinality_attributes_exist(
        self, collection_telemetry: dict[str, Any]
    ) -> None:
        from tests.support.otel import metric_data_points, metrics_by_name

        from darkula.app.collection import SourceCollectionService

        spi, _, run = _seed()
        service = SourceCollectionService(
            spi=spi,
            crawler=FakeCrawler(),
            content_ingest=FakeIngest(),
            settings=CollectionSettings(),
            clock=lambda: _T0,
        )
        await service.execute(run.run_id)
        metrics = metrics_by_name(collection_telemetry["reader"])
        for name, metric in metrics.items():
            for point in metric_data_points(metric):
                attrs = dict(point.attributes or {})
                for key, value in attrs.items():
                    assert key == "darkula.outcome", f"{name}: unexpected key {key}"
                    assert value in ("success", "error")
        # No span may carry sensitive/high-cardinality values either.
        spans = list(collection_telemetry["exporter"].get_finished_spans())
        assert spans
        for span in spans:
            rendered = repr(span.attributes or {})
            assert str(run.run_id) not in rendered
            assert str(_SOURCE) not in rendered
            assert "blackgate" not in rendered.lower()


from darkula.crawler.contracts import CrawlResult  # noqa: E402

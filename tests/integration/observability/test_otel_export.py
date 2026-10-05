# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Real OTLP export integration (PR 16) — matrix OI16.

Proves the delivered topology:

    Darkula instrumentation -> OTLP/HTTP -> OTEL Collector
                                          -> Jaeger (traces)
                                          -> Prometheus (metrics)

The test uses the real Collector/Jaeger/Prometheus provisioned by
``./build.sh --intg`` (``scripts/darkula_observability.sh``); it is offline
apart from those local services. Queries use bounded polling.
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime

import pytest

from darkula.app.data_stream import MessageBatch, StreamMessage
from darkula.config.settings import DataStreamSettings, TelemetryExport
from darkula.domain.identifiers import ConsumerId, MessageId, StreamName
from darkula.infrastructure.data_stream.redpanda import RedpandaDataStream
from darkula.telemetry.decorators import counted, traced
from darkula.telemetry.setup import TelemetryRuntime, configure_telemetry
from darkula.telemetry.tracing import get_tracer

pytestmark = pytest.mark.integration

_COLLECTOR_OTLP_URL = "http://127.0.0.1:34318"
_JAEGER_URL = "http://127.0.0.1:31686"
_PROMETHEUS_URL = "http://127.0.0.1:39090"
_SERVICE_NAME = "darkula-otlp-intg"
_SPAN_NAME = "darkula.otlp_intg.op"
_COUNTER_NAME = "darkula.otlp_intg.calls"
#: Prometheus normalizes the OTel counter name to ``..._total``.
_COUNTER_METRIC = "darkula_otlp_intg_calls_total"

_POLL_TIMEOUT_SECONDS = 60
_POLL_INTERVAL_SECONDS = 1.0

#: Redpanda trace-propagation proof (TP16A).
_TRACE_ROOT_SPAN = "test.redpanda_trace.root"
_PUBLISH_SPAN = "datastream.publish"
_POLL_SPAN = "datastream.poll"
_STREAM_TAG = "darkula.stream"
_PROBE_MESSAGE_TYPE = "integration.trace.probe"

#: Sentinels that must never appear in exported telemetry. They are not
#: emitted by this test; the assertion guards against accidental capture.
_SENSITIVE_SENTINELS = (
    "ghost_admin",
    "blackgate-test-password",
    "darkula-local-intg",
    "sk-test-secret",
    "IGNORE ALL PREVIOUS INSTRUCTIONS",
)


@counted(metric=_COUNTER_NAME)
@traced(span_name=_SPAN_NAME)
def _instrumented_operation() -> int:
    return 42


def _get_json(url: str, *, timeout: float = 5.0) -> dict[str, object]:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise AssertionError(f"expected JSON object from {url}")
    return payload


def _wait_until(
    probe: Callable[[], bool],
    *,
    timeout: float = _POLL_TIMEOUT_SECONDS,
    interval: float = _POLL_INTERVAL_SECONDS,
) -> None:
    """Poll a probe with a bounded deadline; never sleep unbounded."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if probe():
                return
        except urllib.error.URLError, OSError:
            pass
        time.sleep(interval)
    raise AssertionError(f"telemetry probe did not settle within {timeout}s")


def _jaeger_has_span() -> bool:
    query = urllib.parse.urlencode({"service": _SERVICE_NAME, "limit": "100"})
    payload = _get_json(f"{_JAEGER_URL}/api/traces?{query}")
    data = payload.get("data")
    if not isinstance(data, list):
        return False
    for trace in data:
        if not isinstance(trace, dict):
            continue
        for span in trace.get("spans", []):
            if isinstance(span, dict) and span.get("operationName") == _SPAN_NAME:
                return True
    return False


def _prometheus_has_metric(metric: str) -> bool:
    query = urllib.parse.urlencode({"query": metric})
    payload = _get_json(f"{_PROMETHEUS_URL}/api/v1/query?{query}")
    if payload.get("status") != "success":
        return False
    data = payload.get("data")
    if not isinstance(data, dict):
        return False
    result = data.get("result")
    return isinstance(result, list) and len(result) > 0


@pytest.fixture(scope="module")
def otlp_runtime() -> Iterator[TelemetryRuntime]:
    """Compose one real OTLP runtime and register it as the global provider."""
    runtime = configure_telemetry(
        enabled=True,
        service_name=_SERVICE_NAME,
        export=TelemetryExport.OTLP,
        otlp_endpoint=_COLLECTOR_OTLP_URL,
        timeout_seconds=5.0,
        metric_interval_seconds=2.0,
        register_globals=True,
    )
    try:
        yield runtime
    finally:
        runtime.shutdown()


class TestOtlpExport:
    """OI16-1..OI16-6."""

    def test_oi16_1_2_4_5_span_and_metric_exported(
        self, otlp_runtime: TelemetryRuntime
    ) -> None:
        assert _instrumented_operation() == 42
        assert otlp_runtime.force_flush(timeout_millis=10_000) is True
        _wait_until(_jaeger_has_span)
        _wait_until(lambda: _prometheus_has_metric(_COUNTER_METRIC))

    def test_oi16_6_sensitive_sentinels_absent(
        self, otlp_runtime: TelemetryRuntime
    ) -> None:
        _instrumented_operation()
        otlp_runtime.force_flush(timeout_millis=10_000)
        trace_query = urllib.parse.urlencode({"service": _SERVICE_NAME, "limit": "100"})
        raw_traces = json.dumps(_get_json(f"{_JAEGER_URL}/api/traces?{trace_query}"))
        raw_metrics = json.dumps(
            _get_json(
                f"{_PROMETHEUS_URL}/api/v1/query?"
                + urllib.parse.urlencode({"query": _COUNTER_METRIC})
            )
        )
        exported = raw_traces + raw_metrics
        for sentinel in _SENSITIVE_SENTINELS:
            assert sentinel not in exported, f"sensitive sentinel leaked: {sentinel}"


def _probe_message() -> StreamMessage:
    return StreamMessage(
        message_id=MessageId.generate(),
        message_type=_PROBE_MESSAGE_TYPE,
        schema_version=1,
        occurred_at=datetime.now(UTC),
        payload={"probe": True},
    )


def _redpanda_stream(settings: DataStreamSettings) -> RedpandaDataStream:
    return RedpandaDataStream(
        bootstrap_servers=settings.bootstrap_servers,
        client_id="darkula-intg-trace",
        poll_timeout_ms=2000,
        max_poll_records=10,
    )


async def _await_records(
    stream: RedpandaDataStream,
    topic: StreamName,
    consumer_id: ConsumerId,
) -> MessageBatch:
    deadline = time.monotonic() + _POLL_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        batch = await stream.poll(
            stream_name=topic, consumer_id=consumer_id, max_records=10
        )
        if batch.records:
            return batch
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
    raise AssertionError("no records polled within the bounded deadline")


def _trace_by_id(trace_id_hex: str) -> dict[str, object] | None:
    payload = _get_json(f"{_JAEGER_URL}/api/traces/{trace_id_hex}")
    data = payload.get("data")
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0]
    return None


def _tag_value(span: dict[str, object], key: str) -> str | None:
    tags = span.get("tags")
    if not isinstance(tags, list):
        return None
    for tag in tags:
        if isinstance(tag, dict) and tag.get("key") == key:
            value = tag.get("value")
            return value if isinstance(value, str) else None
    return None


def _span_with_stream(
    spans: list[dict[str, object]], operation: str, stream_value: str
) -> dict[str, object] | None:
    for span in spans:
        if span.get("operationName") == operation and (
            _tag_value(span, _STREAM_TAG) == stream_value
        ):
            return span
    return None


def _parent_span_id(span: dict[str, object]) -> str | None:
    references = span.get("references")
    if not isinstance(references, list):
        return None
    for reference in references:
        if isinstance(reference, dict) and reference.get("refType") == "CHILD_OF":
            span_id = reference.get("spanID")
            return span_id if isinstance(span_id, str) else None
    return None


class TestRedpandaTracePropagation:
    """TP16A-1..TP16A-8: W3C context through real Redpanda into Jaeger."""

    @pytest.mark.asyncio
    async def test_oi16_3_real_redpanda_trace_lineage(
        self,
        otlp_runtime: TelemetryRuntime,
        redpanda_settings: DataStreamSettings,
    ) -> None:
        stream = _redpanda_stream(redpanda_settings)
        await stream.start()
        try:
            positive = StreamName(f"intg-trace-{uuid.uuid4().hex[:12]}")
            negative = StreamName(f"intg-trace-neg-{uuid.uuid4().hex[:12]}")
            consumer_id = ConsumerId(f"intg-trace-c-{uuid.uuid4().hex[:8]}")
            tracer = get_tracer()

            # TP16A-1: publish under a known active upstream span.
            with tracer.start_as_current_span(_TRACE_ROOT_SPAN) as root:
                root_context = root.get_span_context()
                root_trace_id = root_context.trace_id
                root_span_id = format(root_context.span_id, "016x")
                await stream.publish(stream_name=positive, messages=[_probe_message()])
            # TP16A-6 negative control: publish with no active context.
            await stream.publish(stream_name=negative, messages=[_probe_message()])

            # TP16A-2/3: read both through the real broker/adapter.
            await _await_records(stream, positive, consumer_id)
            await _await_records(stream, negative, consumer_id)

            assert otlp_runtime.force_flush(timeout_millis=10_000) is True
            trace_id_hex = format(root_trace_id, "032x")
            # TP16A-8: bounded polling, never an unbounded wait.
            _wait_until(lambda: _trace_by_id(trace_id_hex) is not None)
            trace = _trace_by_id(trace_id_hex)
            assert trace is not None
            spans = trace.get("spans")
            assert isinstance(spans, list) and spans
            typed_spans = [span for span in spans if isinstance(span, dict)]

            # TP16A-4/5: same trace and correct ancestor chain.
            root_span = next(
                span
                for span in typed_spans
                if span.get("operationName") == _TRACE_ROOT_SPAN
            )
            publish_span = _span_with_stream(typed_spans, _PUBLISH_SPAN, positive.value)
            poll_span = _span_with_stream(typed_spans, _POLL_SPAN, positive.value)
            assert publish_span is not None, "upstream publish span missing"
            assert poll_span is not None, "downstream poll span missing"
            assert root_span.get("traceID") == trace_id_hex
            assert publish_span.get("traceID") == trace_id_hex
            assert poll_span.get("traceID") == trace_id_hex
            assert _parent_span_id(publish_span) == root_span_id
            assert _parent_span_id(poll_span) == publish_span.get("spanID")

            # TP16A-6: the no-context message is not falsely parented to root.
            tagged_streams = {
                value
                for span in typed_spans
                if (value := _tag_value(span, _STREAM_TAG)) is not None
            }
            assert positive.value in tagged_streams
            assert negative.value not in tagged_streams

            # TP16A-7: no sensitive sentinel is present in the trace payload.
            exported = json.dumps(trace)
            for sentinel in _SENSITIVE_SENTINELS:
                assert sentinel not in exported, (
                    f"sensitive sentinel leaked: {sentinel}"
                )
        finally:
            await stream.stop()

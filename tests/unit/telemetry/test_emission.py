# SPDX-License-Identifier: AGPL-3.0-only
"""OTEL SDK emission tests for traced/timed/counted (T-* matrix).

All emission tests inject in-memory providers through the decorator module
seams (see ``tests/unit/conftest.py``); no Collector, exporter, network, or
global provider is involved.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import StatusCode
from tests.support.otel import (
    counter_value,
    data_point_attributes,
    histogram_count,
    histogram_sum,
    metrics_by_name,
)

from darkula.telemetry.decorators import counted, timed, traced
from darkula.telemetry.setup import TelemetryRuntime, configure_telemetry

_COUNTED_METRIC = "test.op.calls"


@traced(span_name="test.sync_op")
def _sync_add(left: int, right: int) -> int:
    return left + right


@traced(span_name="test.async_op")
async def _async_echo(value: str) -> str:
    return value


@traced(span_name="test.boom.op")
def _sync_boom() -> None:
    raise RuntimeError("boom")


@traced(span_name="test.cancel.op")
async def _async_cancel() -> None:
    raise asyncio.CancelledError()


@traced(span_name="test.parent.op")
def _parent() -> int:
    return _child() + 1


@traced(span_name="test.child.op")
def _child() -> int:
    return 1


@timed(metric="test.sync_timed.duration")
def _sync_timed() -> None:
    time.sleep(0.005)


@timed(metric="test.async_timed.duration")
async def _async_timed() -> None:
    await asyncio.sleep(0.005)


@timed(metric="test.error_timed.duration")
def _timed_boom() -> None:
    raise ValueError("timed boom")


@timed(metric="test.cancel_timed.duration")
async def _timed_cancel() -> None:
    raise asyncio.CancelledError()


@counted(metric=_COUNTED_METRIC)
def _count_one(value: int) -> int:
    return value


@counted(metric=_COUNTED_METRIC)
def _count_boom() -> None:
    raise KeyError("boom")


@counted(metric=_COUNTED_METRIC)
async def _count_cancel() -> None:
    raise asyncio.CancelledError()


def _spans(exporter: InMemorySpanExporter) -> list[ReadableSpan]:
    return list(exporter.get_finished_spans())


def _parent_span_id(span: ReadableSpan) -> int | None:
    """Return a finished span's parent span id, or ``None`` for roots."""
    parent = span.parent
    if parent is None:
        return None
    return parent.span_id


def _span_context_id(span: ReadableSpan) -> int | None:
    """Return a finished span's own context id, or ``None`` when absent."""
    context = span.get_span_context()
    if context is None:
        return None
    return context.span_id


class TestTraced:
    """T01-T05/T08: spans for sync/async/error/cancel/nesting/metadata."""

    def test_t01_traced_sync_emits_one_span(
        self, in_memory_telemetry: SimpleNamespace
    ) -> None:
        assert _sync_add(2, 3) == 5
        spans = _spans(in_memory_telemetry.exporter)
        assert len(spans) == 1
        span = spans[0]
        assert span.name == "test.sync_op"
        assert span.status.status_code is not StatusCode.ERROR
        assert span.attributes == {}

    @pytest.mark.asyncio
    async def test_t02_traced_async_emits_one_span(
        self, in_memory_telemetry: SimpleNamespace
    ) -> None:
        assert await _async_echo("value") == "value"
        spans = _spans(in_memory_telemetry.exporter)
        assert len(spans) == 1
        assert spans[0].name == "test.async_op"

    def test_t03_exception_marks_span_error_and_propagates(
        self, in_memory_telemetry: SimpleNamespace
    ) -> None:
        with pytest.raises(RuntimeError, match="boom"):
            _sync_boom()
        (span,) = _spans(in_memory_telemetry.exporter)
        assert span.status.status_code is StatusCode.ERROR
        assert len(span.events) >= 1  # at least the record_exception event

    @pytest.mark.asyncio
    async def test_t04_cancellation_propagates_without_error_status(
        self, in_memory_telemetry: SimpleNamespace
    ) -> None:
        with pytest.raises(asyncio.CancelledError):
            await _async_cancel()
        (span,) = _spans(in_memory_telemetry.exporter)
        assert span.status.status_code is not StatusCode.ERROR

    def test_t05_nested_spans_are_parent_child(
        self, in_memory_telemetry: SimpleNamespace
    ) -> None:
        assert _parent() == 2
        spans = _spans(in_memory_telemetry.exporter)
        by_name = {span.name: span for span in spans}
        assert set(by_name) == {"test.parent.op", "test.child.op"}
        child = by_name["test.child.op"]
        parent = by_name["test.parent.op"]
        child_parent_id = _parent_span_id(child)
        parent_context_id = _span_context_id(parent)
        assert child_parent_id is not None
        assert parent_context_id is not None
        assert child_parent_id == parent_context_id

    def test_t08_wraps_preserves_metadata(self) -> None:
        @traced(span_name="test.meta.op")
        def documented() -> int:
            """Original docstring."""
            return 7

        assert documented.__name__ == "documented"
        assert documented.__doc__ == "Original docstring."
        assert documented() == 7

    def test_t07_static_attributes_only_no_args_results(
        self, in_memory_telemetry: SimpleNamespace
    ) -> None:
        @traced(span_name="test.attr.op", attributes={"component": "loader"})
        def with_attrs(left: int, right: int) -> int:
            return left + right

        assert with_attrs(1, 2) == 3
        (span,) = _spans(in_memory_telemetry.exporter)
        # Only the bounded static attribute is attached; never args/results.
        assert dict(span.attributes or {}) == {"component": "loader"}


class TestNoProvider:
    """T06: unconfigured OTEL is a behavior-preserving no-op."""

    def test_t06_no_provider_behavior_preserved(self) -> None:
        # No fixture: the decorator resolves the API no-op proxy tracer.
        assert _sync_add(40, 2) == 42

    def test_t18_disabled_telemetry_runtime_is_empty(self) -> None:
        runtime = configure_telemetry(enabled=False, service_name="darkula-test")
        assert isinstance(runtime, TelemetryRuntime)
        assert runtime.tracer_provider is None
        assert runtime.meter_provider is None


class TestTimed:
    """T09-T12: seconds histograms with bounded outcomes."""

    def test_t09_timed_success_records_one_success_point(
        self, in_memory_telemetry: SimpleNamespace
    ) -> None:
        _sync_timed()
        metrics = metrics_by_name(in_memory_telemetry.reader)
        metric = metrics["test.sync_timed.duration"]
        assert histogram_count(metric) == 1
        assert data_point_attributes(metric)["darkula.outcome"] == "success"

    def test_t10_timed_error_records_error_point_and_propagates(
        self, in_memory_telemetry: SimpleNamespace
    ) -> None:
        with pytest.raises(ValueError, match="timed boom"):
            _timed_boom()
        metrics = metrics_by_name(in_memory_telemetry.reader)
        metric = metrics["test.error_timed.duration"]
        assert histogram_count(metric) == 1
        assert data_point_attributes(metric)["darkula.outcome"] == "error"

    @pytest.mark.asyncio
    async def test_t11_timed_cancellation_records_no_point(
        self, in_memory_telemetry: SimpleNamespace
    ) -> None:
        with pytest.raises(asyncio.CancelledError):
            await _timed_cancel()
        metrics = metrics_by_name(in_memory_telemetry.reader)
        assert "test.cancel_timed.duration" not in metrics

    @pytest.mark.asyncio
    async def test_t12_monotonic_duration_is_seconds_sized(
        self, in_memory_telemetry: SimpleNamespace
    ) -> None:
        await _async_timed()
        metrics = metrics_by_name(in_memory_telemetry.reader)
        metric = metrics["test.async_timed.duration"]
        assert histogram_count(metric) == 1
        total = histogram_sum(metric)
        assert 0.0 <= total < 2.0
        assert data_point_attributes(metric)["darkula.outcome"] == "success"

    def test_timed_static_attributes_merged_with_outcome(
        self, in_memory_telemetry: SimpleNamespace
    ) -> None:
        @timed(metric="test.attr_timed.duration", attributes={"component": "svc"})
        def timed_with_attrs() -> None:
            return None

        timed_with_attrs()
        metric = metrics_by_name(in_memory_telemetry.reader)["test.attr_timed.duration"]
        assert data_point_attributes(metric)["component"] == "svc"
        assert data_point_attributes(metric)["darkula.outcome"] == "success"


class TestCounted:
    """T13-T15: invocation counters count every outcome exactly once."""

    def test_t13_counted_success(self, in_memory_telemetry: SimpleNamespace) -> None:
        _count_one(41)
        metric = metrics_by_name(in_memory_telemetry.reader)[_COUNTED_METRIC]
        assert counter_value(metric) == 1

    def test_t14_counted_failure_still_counts(
        self, in_memory_telemetry: SimpleNamespace
    ) -> None:
        with pytest.raises(KeyError):
            _count_boom()
        metric = metrics_by_name(in_memory_telemetry.reader)[_COUNTED_METRIC]
        assert counter_value(metric) == 1

    @pytest.mark.asyncio
    async def test_t15_counted_cancellation_still_counts(
        self, in_memory_telemetry: SimpleNamespace
    ) -> None:
        with pytest.raises(asyncio.CancelledError):
            await _count_cancel()
        metric = metrics_by_name(in_memory_telemetry.reader)[_COUNTED_METRIC]
        assert counter_value(metric) == 1


class TestValidation:
    """T16/T17: invalid/unsafe decorator input fails at decoration time."""

    def test_t16_span_name_with_control_characters_rejected(self) -> None:
        with pytest.raises(ValueError, match="control characters"):
            traced(span_name="test.\nop")

    def test_t16_overlong_span_name_rejected(self) -> None:
        with pytest.raises(ValueError, match="exceed"):
            traced(span_name="x" * 201)

    def test_t16_duration_metric_requires_canonical_suffix(self) -> None:
        with pytest.raises(ValueError, match=r"\.duration"):
            timed(metric="test.op.millis")

    def test_t17_unbounded_attribute_value_rejected(self) -> None:
        with pytest.raises(ValueError, match="exceed"):
            traced(span_name="test.op", attributes={"component": "x" * 257})

    def test_t17_control_character_in_attribute_value_rejected(self) -> None:
        with pytest.raises(ValueError, match="control characters"):
            traced(span_name="test.op", attributes={"component": "a\nb"})

    def test_t17_blank_attribute_value_rejected(self) -> None:
        with pytest.raises(ValueError, match="not be blank"):
            traced(span_name="test.op", attributes={"component": "  "})

    @pytest.mark.parametrize(
        "key", ["api_key", "prompt", "password", "session_token", "auth_value"]
    )
    def test_t17_secret_like_attribute_rejected(self, key: str) -> None:
        with pytest.raises(ValueError, match="secret-like"):
            traced(span_name="test.op", attributes={key: "safe-looking-value"})


class TestSetup:
    """configure_telemetry composition behavior."""

    def test_enabled_runtime_carries_service_identity(self) -> None:
        runtime = configure_telemetry(
            enabled=True, service_name="darkula-worker", register_globals=False
        )
        assert runtime.tracer_provider is not None
        assert runtime.meter_provider is not None
        assert runtime.tracer_provider.resource.attributes["service.name"] == (
            "darkula-worker"
        )

    def test_blank_service_name_rejected(self) -> None:
        with pytest.raises(ValueError, match="service_name"):
            configure_telemetry(enabled=True, service_name="  ")

    def test_global_registration_guarded_against_double_install(self) -> None:
        configure_telemetry(
            enabled=True, service_name="darkula-global", register_globals=True
        )
        with pytest.raises(RuntimeError, match="already installed"):
            configure_telemetry(
                enabled=True, service_name="darkula-global", register_globals=True
            )

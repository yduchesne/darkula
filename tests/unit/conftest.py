# SPDX-License-Identifier: AGPL-3.0-only
"""Shared deterministic OpenTelemetry fixtures for the unit suite (PR 3).

Telemetry tests never rely on process-global OTel providers (which OTel's
public API only allows setting once). Instead they build in-memory
providers/exporters and inject them through the telemetry module seams,
keeping every test isolated and order-independent.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

import darkula.telemetry.decorators as _decorators
from darkula.telemetry.tracing import INSTRUMENTATION_SCOPE

_EXTENSION_VERSION = "0.1.0"


@pytest.fixture
def span_exporter() -> InMemorySpanExporter:
    """Return a fresh in-memory span exporter per test."""
    return InMemorySpanExporter()


@pytest.fixture
def metric_reader() -> InMemoryMetricReader:
    """Return a fresh in-memory metric reader per test."""
    return InMemoryMetricReader()


@pytest.fixture
def in_memory_telemetry(
    monkeypatch: pytest.MonkeyPatch,
    span_exporter: InMemorySpanExporter,
    metric_reader: InMemoryMetricReader,
) -> SimpleNamespace:
    """Wire the decorator seams to isolated in-memory providers.

    The returned namespace exposes ``tracer``, ``meter``, ``exporter`` and
    ``reader`` so tests can assert on finished spans and recorded metrics.
    """
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(span_exporter))
    tracer = trace.get_tracer(
        INSTRUMENTATION_SCOPE,
        _EXTENSION_VERSION,
        tracer_provider=tracer_provider,
    )

    meter_provider = MeterProvider(metric_readers=[metric_reader])
    meter = metrics.get_meter(
        INSTRUMENTATION_SCOPE,
        _EXTENSION_VERSION,
        meter_provider=meter_provider,
    )

    monkeypatch.setattr(_decorators, "get_tracer", lambda: tracer)
    monkeypatch.setattr(
        _decorators,
        "get_histogram",
        lambda name, *, unit="s", description="": meter.create_histogram(
            name, unit=unit, description=description
        ),
    )
    monkeypatch.setattr(
        _decorators,
        "get_counter",
        lambda name, **_: meter.create_counter(
            name, unit="{operation}", description=name
        ),
    )
    return SimpleNamespace(
        tracer=tracer,
        meter=meter,
        exporter=span_exporter,
        reader=metric_reader,
    )

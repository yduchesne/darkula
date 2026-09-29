# SPDX-License-Identifier: AGPL-3.0-only
"""In-memory OpenTelemetry helpers for deterministic telemetry tests.

These helpers read recorded spans/metrics back from the SDK's in-memory
exporter/reader and expose narrow accessors so tests never reason about
OpenTelemetry's internal data unions. No Collector, exporter, network, or
background reader is involved.
"""

from __future__ import annotations

from typing import Any

from opentelemetry.sdk.metrics.export import InMemoryMetricReader, Metric


def metrics_by_name(reader: InMemoryMetricReader) -> dict[str, Metric]:
    """Return recorded metrics indexed by name from an in-memory reader."""
    data = reader.get_metrics_data()
    if data is None:
        return {}
    result: dict[str, Metric] = {}
    for resource_metrics in data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                result[metric.name] = metric
    return result


def counter_value(metric: Metric) -> int:
    """Return the summed value of a recorded monotonic counter."""
    return int(metric.data.data_points[0].value)  # type: ignore[union-attr]


def counter_is_monotonic(metric: Metric) -> bool:
    """Return whether the recorded counter metric is monotonic."""
    return bool(metric.data.is_monotonic)  # type: ignore[union-attr]


def histogram_count(metric: Metric) -> int:
    """Return the total sample count of a recorded histogram."""
    total = 0
    for point in metric.data.data_points:
        total += int(point.count)  # type: ignore[union-attr]
    return total


def histogram_sum(metric: Metric) -> float:
    """Return the total recorded sum of a histogram (seconds)."""
    total = 0.0
    for point in metric.data.data_points:
        total += float(point.sum)  # type: ignore[union-attr]
    return total


def data_point_attributes(metric: Metric) -> dict[str, Any]:
    """Return the attributes of the first recorded data point."""
    return dict(metric.data.data_points[0].attributes or {})


def metric_data_points(metric: Metric) -> list[Any]:
    """Return every recorded data point of a metric (union-safe)."""
    return list(metric.data.data_points)


__all__ = [
    "counter_is_monotonic",
    "counter_value",
    "data_point_attributes",
    "histogram_count",
    "histogram_sum",
    "metric_data_points",
    "metrics_by_name",
]

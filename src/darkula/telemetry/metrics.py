# SPDX-License-Identifier: AGPL-3.0-only
"""Canonical meter access and metric units (PR 3).

Duration histograms use the OpenTelemetry/UCUM seconds unit (``"s"``) and
follow the canonical ``<operation>.duration`` metric-name suffix enforced by
:mod:`darkula.telemetry.decorators`. Invocation counters use the generic
``{operation}`` unit. ``get_meter``/``get_counter``/``get_histogram`` are
the injectable module seams used by the decorators and by deterministic
tests.
"""

from __future__ import annotations

from opentelemetry import metrics
from opentelemetry.metrics import Counter, Histogram, Meter, MeterProvider

from darkula.telemetry.tracing import (
    _TELEMETRY_VERSION,
    INSTRUMENTATION_SCOPE,
)

#: Canonical duration unit: seconds (UCUM ``"s"``).
DURATION_UNIT = "s"

#: Generic counter unit for invocation counters.
COUNTER_UNIT = "{operation}"


def get_meter(*, meter_provider: MeterProvider | None = None) -> Meter:
    """Return the canonical Darkula meter bound to ``meter_provider``.

    With no provider the meter is the process-global meter (an API no-op
    proxy when none has been configured).
    """
    if meter_provider is not None:
        return metrics.get_meter(
            INSTRUMENTATION_SCOPE,
            _TELEMETRY_VERSION,
            meter_provider=meter_provider,
        )
    return metrics.get_meter(INSTRUMENTATION_SCOPE, _TELEMETRY_VERSION)


def get_counter(name: str, *, meter_provider: MeterProvider | None = None) -> Counter:
    """Return a monotonic counter instrument for ``name``."""
    meter = get_meter(meter_provider=meter_provider)
    return meter.create_counter(name, unit=COUNTER_UNIT, description=name)


def get_histogram(
    name: str,
    *,
    unit: str = DURATION_UNIT,
    description: str = "",
    meter_provider: MeterProvider | None = None,
) -> Histogram:
    """Return a histogram instrument for ``name`` (seconds by default)."""
    meter = get_meter(meter_provider=meter_provider)
    return meter.create_histogram(name, unit=unit, description=description)


__all__ = [
    "COUNTER_UNIT",
    "DURATION_UNIT",
    "get_counter",
    "get_histogram",
    "get_meter",
]

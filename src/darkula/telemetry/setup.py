# SPDX-License-Identifier: AGPL-3.0-only
"""OpenTelemetry provider composition for Darkula (PR 3).

``configure_telemetry`` builds local in-process tracer/meter providers for
the requested service identity. It installs **no** exporters and starts
**no** background readers or threads: PR 3 stays deterministic and offline
(an OTEL Collector and OTLP exporters remain future work; telemetry tests
use in-memory SDK components instead).

``register_globals`` (default ``False``) controls whether the built
providers are also installed as the process-global OTel providers. The PR 3
codebase never needs globals (decorators resolve through injectable module
seams); future process entry points may opt in once, and the module guards
against double installation with a bounded error.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider

_state_lock = threading.Lock()
_globals_installed = False


@dataclass(frozen=True)
class TelemetryRuntime:
    """The local providers established by one telemetry composition.

    Both fields are ``None`` when telemetry is disabled; the decorators then
    fall back to the OpenTelemetry no-op API and stay behavior-preserving.
    """

    tracer_provider: TracerProvider | None = None
    meter_provider: MeterProvider | None = None


def configure_telemetry(
    *,
    enabled: bool,
    service_name: str,
    register_globals: bool = False,
) -> TelemetryRuntime:
    """Compose the local telemetry providers (or none when disabled).

    ``service_name`` must be non-blank and becomes the OTel ``service.name``
    resource attribute. No network, exporter, or background thread is ever
    created by PR 3.
    """
    global _globals_installed
    normalized = service_name.strip()
    if not normalized:
        raise ValueError("service_name must not be blank")

    if not enabled:
        return TelemetryRuntime()

    resource = Resource.create({SERVICE_NAME: normalized})
    tracer_provider = TracerProvider(resource=resource)
    meter_provider = MeterProvider(resource=resource)

    if register_globals:
        with _state_lock:
            if _globals_installed:
                raise RuntimeError(
                    "OTEL global telemetry providers are already installed"
                )
            trace.set_tracer_provider(tracer_provider)
            metrics.set_meter_provider(meter_provider)
            _globals_installed = True

    return TelemetryRuntime(
        tracer_provider=tracer_provider,
        meter_provider=meter_provider,
    )


__all__ = ["TelemetryRuntime", "configure_telemetry"]

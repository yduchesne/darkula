# SPDX-License-Identifier: AGPL-3.0-only
"""OpenTelemetry provider composition for Darkula (PR 3; OTLP export PR 16).

``configure_telemetry`` builds the in-process tracer/meter providers for the
requested service identity. When ``export`` is ``NONE`` it behaves exactly as
PR 3 did: local providers with no exporters, no background readers, and no
network. When ``export`` is ``OTLP`` it composes the official OTLP/HTTP span
exporter behind a ``BatchSpanProcessor`` and a periodic OTLP/HTTP metric
reader/exporter, giving Darkula a real exporter/Collector path.

Rules frozen here:

- application/domain code never imports an exporter; only this telemetry
  infrastructure module does;
- no arbitrary header map or vendor exporter selection is supported;
- ``force_flush``/``shutdown`` are bounded and fail-open: an exporter or
  backend failure never raises into domain behavior;
- ``asyncio``/application cancellation semantics are unaffected (these are
  synchronous lifecycle helpers);
- disabling telemetry remains a no-op.

``register_globals`` (default ``False``) installs the providers as the
process-global OTel providers. Production composition opts in only when OTLP
export is selected, so installed decorators emit to the composed exporter; a
double installation is guarded with a bounded error.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
    OTLPMetricExporter,
)
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    MetricExporter,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter

from darkula.config.settings import TelemetryExport

#: Bounded default lifecycle timeout for flush/shutdown (milliseconds).
DEFAULT_LIFECYCLE_TIMEOUT_MILLIS = 10_000

_state_lock = threading.Lock()
_globals_installed = False


@dataclass(frozen=True)
class TelemetryRuntime:
    """The providers established by one telemetry composition.

    Both fields are ``None`` when telemetry is disabled; the decorators then
    fall back to the OpenTelemetry no-op API and stay behavior-preserving.
    """

    tracer_provider: TracerProvider | None = None
    meter_provider: MeterProvider | None = None

    def force_flush(
        self, *, timeout_millis: int = DEFAULT_LIFECYCLE_TIMEOUT_MILLIS
    ) -> bool:
        """Flush both providers fail-open, returning whether both flushed.

        An exporter/backend failure is contained: this never raises and never
        changes domain behavior.
        """
        if self.tracer_provider is None and self.meter_provider is None:
            return True
        flushed = True
        if self.tracer_provider is not None:
            try:
                flushed = self.tracer_provider.force_flush(timeout_millis) and flushed
            except Exception:
                flushed = False
        if self.meter_provider is not None:
            try:
                flushed = self.meter_provider.force_flush(timeout_millis) and flushed
            except Exception:
                flushed = False
        return flushed

    def shutdown(
        self, *, timeout_millis: int = DEFAULT_LIFECYCLE_TIMEOUT_MILLIS
    ) -> None:
        """Shut both providers down fail-open (never raises)."""
        if self.meter_provider is not None:
            try:
                self.meter_provider.shutdown(timeout_millis=timeout_millis)
            except Exception:  # nosec B110 - fail-open telemetry lifecycle
                pass
        if self.tracer_provider is not None:
            try:
                self.tracer_provider.shutdown()
            except Exception:  # nosec B110 - fail-open telemetry lifecycle
                pass


def _normalize_service_name(service_name: str) -> str:
    normalized = service_name.strip()
    if not normalized:
        raise ValueError("service_name must not be blank")
    return normalized


def _otlp_signal_endpoint(base: str, signal_path: str) -> str:
    if not base.strip():
        raise ValueError("otlp_endpoint must not be blank")
    return base.rstrip("/") + signal_path


def configure_telemetry(
    *,
    enabled: bool,
    service_name: str,
    export: TelemetryExport = TelemetryExport.NONE,
    otlp_endpoint: str | None = None,
    timeout_seconds: float = 10.0,
    metric_interval_seconds: float = 60.0,
    register_globals: bool = False,
    span_exporter: SpanExporter | None = None,
    metric_exporter: MetricExporter | None = None,
) -> TelemetryRuntime:
    """Compose the telemetry providers (or none when disabled).

    ``span_exporter``/``metric_exporter`` are deterministic test seams; when
    omitted with ``export=OTLP`` the official OTLP/HTTP exporters are built
    against ``otlp_endpoint``.
    """
    global _globals_installed
    normalized = _normalize_service_name(service_name)

    if not enabled:
        return TelemetryRuntime()

    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if metric_interval_seconds <= 0:
        raise ValueError("metric_interval_seconds must be positive")

    resource = Resource.create({SERVICE_NAME: normalized})
    tracer_provider = TracerProvider(resource=resource)

    if export is TelemetryExport.OTLP:
        if otlp_endpoint is None:
            raise ValueError("OTLP export requires otlp_endpoint")
        exporter = span_exporter or OTLPSpanExporter(
            endpoint=_otlp_signal_endpoint(otlp_endpoint, "/v1/traces"),
            timeout=timeout_seconds,
        )
        tracer_provider.add_span_processor(
            BatchSpanProcessor(
                exporter,
                export_timeout_millis=timeout_seconds * 1000,
            )
        )
        metric_export = metric_exporter or OTLPMetricExporter(
            endpoint=_otlp_signal_endpoint(otlp_endpoint, "/v1/metrics"),
            timeout=timeout_seconds,
        )
        metric_reader = PeriodicExportingMetricReader(
            metric_export,
            export_interval_millis=metric_interval_seconds * 1000,
            export_timeout_millis=timeout_seconds * 1000,
        )
        meter_provider = MeterProvider(
            resource=resource, metric_readers=[metric_reader]
        )
    else:
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


__all__ = [
    "DEFAULT_LIFECYCLE_TIMEOUT_MILLIS",
    "TelemetryRuntime",
    "configure_telemetry",
]

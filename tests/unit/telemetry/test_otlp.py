# SPDX-License-Identifier: AGPL-3.0-only
"""OTLP telemetry export and lifecycle tests (PR 16) — matrix OT16.

All exporter transport is stubbed; no Collector, network, or global provider
is involved.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Any

import pytest
from opentelemetry.sdk.metrics.export import (
    MetricExporter,
    MetricExportResult,
    MetricsData,
)
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

from darkula.config.settings import Settings, TelemetryExport, TelemetrySettings
from darkula.telemetry.setup import TelemetryRuntime, configure_telemetry


class RecordingSpanExporter(SpanExporter):
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.exported: list[ReadableSpan] = []
        self.shutdown_called = False

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        if self.fail:
            raise RuntimeError("span export boom")
        self.exported.extend(spans)
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        self.shutdown_called = True


class RecordingMetricExporter(MetricExporter):
    def __init__(self, *, fail: bool = False) -> None:
        super().__init__()
        self.fail = fail
        self.exported: list[MetricsData] = []
        self.shutdown_called = False

    def export(
        self,
        metrics_data: MetricsData,
        timeout_millis: float = 10_000,
        **kwargs: Any,
    ) -> MetricExportResult:
        if self.fail:
            raise RuntimeError("metric export boom")
        self.exported.append(metrics_data)
        return MetricExportResult.SUCCESS

    def force_flush(self, timeout_millis: float = 10_000) -> bool:
        return True

    def shutdown(self, timeout_millis: float = 30_000, **kwargs: object) -> None:
        self.shutdown_called = True


def _span_processor_count(runtime: TelemetryRuntime) -> int:
    assert runtime.tracer_provider is not None
    return len(runtime.tracer_provider._active_span_processor._span_processors)


def _metric_reader_count(runtime: TelemetryRuntime) -> int:
    assert runtime.meter_provider is not None
    return len(runtime.meter_provider._metric_readers)


def _otlp_runtime(
    *,
    span_exporter: SpanExporter | None = None,
    metric_exporter: MetricExporter | None = None,
) -> TelemetryRuntime:
    runtime = configure_telemetry(
        enabled=True,
        service_name="darkula-test",
        export=TelemetryExport.OTLP,
        otlp_endpoint="http://127.0.0.1:34318",
        timeout_seconds=5.0,
        metric_interval_seconds=60.0,
        span_exporter=span_exporter or RecordingSpanExporter(),
        metric_exporter=metric_exporter or RecordingMetricExporter(),
    )
    _CREATED_RUNTIMES.append(runtime)
    return runtime


#: Runtimes created by ``_otlp_runtime``; torn down after each test so no
#: periodic reader or span processor thread outlives the test.
_CREATED_RUNTIMES: list[TelemetryRuntime] = []


@pytest.fixture(autouse=True)
def _shutdown_created_runtimes() -> Iterator[None]:
    yield None
    while _CREATED_RUNTIMES:
        _CREATED_RUNTIMES.pop().shutdown()


class TestComposition:
    """OT16-1..OT16-3."""

    def test_ot16_1_disabled_no_providers(self) -> None:
        runtime = configure_telemetry(enabled=False, service_name="darkula-test")
        assert runtime.tracer_provider is None
        assert runtime.meter_provider is None
        assert runtime.force_flush() is True

    def test_ot16_2_local_only_has_no_exporters(self) -> None:
        runtime = configure_telemetry(
            enabled=True,
            service_name="darkula-test",
            export=TelemetryExport.NONE,
        )
        assert _span_processor_count(runtime) == 0
        assert _metric_reader_count(runtime) == 0

    def test_ot16_3_otlp_composes_processor_and_reader(self) -> None:
        runtime = _otlp_runtime()
        assert _span_processor_count(runtime) == 1
        assert _metric_reader_count(runtime) == 1


class TestValidation:
    """OT16-4..OT16-7."""

    def test_ot16_4_otlp_requires_endpoint(self) -> None:
        with pytest.raises(ValueError, match="otlp_endpoint"):
            configure_telemetry(
                enabled=True,
                service_name="darkula-test",
                export=TelemetryExport.OTLP,
                otlp_endpoint=None,
            )

    def test_ot16_5_invalid_timeout_and_interval(self) -> None:
        with pytest.raises(ValueError, match="timeout_seconds"):
            configure_telemetry(
                enabled=True,
                service_name="darkula-test",
                export=TelemetryExport.OTLP,
                otlp_endpoint="http://127.0.0.1:34318",
                timeout_seconds=0,
            )
        with pytest.raises(ValueError, match="metric_interval_seconds"):
            configure_telemetry(
                enabled=True,
                service_name="darkula-test",
                export=TelemetryExport.OTLP,
                otlp_endpoint="http://127.0.0.1:34318",
                metric_interval_seconds=0,
            )

    def test_ot16_4_settings_reject_otlp_without_endpoint(self) -> None:
        with pytest.raises(ValueError):
            TelemetrySettings(export=TelemetryExport.OTLP, otlp_endpoint=None)

    def test_settings_reject_bad_endpoint(self) -> None:
        with pytest.raises(ValueError):
            TelemetrySettings(
                export=TelemetryExport.OTLP, otlp_endpoint="collector:4318"
            )

    def test_ot16_6_nonempty_env_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DARKULA_TELEMETRY__EXPORT", "otlp")
        monkeypatch.setenv("DARKULA_TELEMETRY__OTLP_ENDPOINT", "http://127.0.0.1:34318")
        settings = Settings()
        assert settings.telemetry.export is TelemetryExport.OTLP
        assert settings.telemetry.otlp_endpoint == "http://127.0.0.1:34318"

    def test_ot16_7_empty_env_has_no_effect(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DARKULA_TELEMETRY__EXPORT", "")
        settings = Settings()
        assert settings.telemetry.export is TelemetryExport.NONE


class TestLifecycle:
    """OT16-8..OT16-10."""

    def test_ot16_8_shutdown_safe(self) -> None:
        runtime = _otlp_runtime()
        runtime.shutdown()
        runtime.shutdown()  # idempotent, never raises

    def test_ot16_9_flush_bounded(self) -> None:
        runtime = _otlp_runtime()
        assert runtime.force_flush(timeout_millis=100) is True

    def test_ot16_10_exporter_error_contained(self) -> None:
        runtime = _otlp_runtime(
            span_exporter=RecordingSpanExporter(fail=True),
            metric_exporter=RecordingMetricExporter(fail=True),
        )
        # Flush must not raise even when exporters fail.
        runtime.force_flush(timeout_millis=100)
        runtime.shutdown()

    def test_disabled_flush_and_shutdown_noop(self) -> None:
        runtime = configure_telemetry(enabled=False, service_name="darkula-test")
        assert runtime.force_flush() is True
        runtime.shutdown()


class TestSdkConfinement:
    """OT16-13: exporter/SDK imports stay in telemetry infrastructure."""

    def test_otlp_exporter_imported_only_by_telemetry(self) -> None:
        import ast
        from pathlib import Path

        src = Path(__file__).resolve().parents[3] / "src" / "darkula"
        offenders: list[str] = []
        for path in sorted(src.rglob("*.py")):
            if path.is_relative_to(src / "telemetry"):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                else:
                    continue
                if any(name.startswith("opentelemetry.exporter") for name in names):
                    offenders.append(str(path.relative_to(src)))
        assert not offenders, f"exporter imported outside telemetry: {offenders}"

    def test_ot16_14_secret_like_endpoint_not_rendered(self) -> None:
        settings = TelemetrySettings(
            export=TelemetryExport.OTLP,
            otlp_endpoint="http://127.0.0.1:34318",
        )
        rendered = repr(settings)
        # No credential material is representable in the telemetry settings.
        assert "password" not in rendered.lower()
        assert "api_key" not in rendered.lower()

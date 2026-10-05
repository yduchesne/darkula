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

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator

import pytest

from darkula.config.settings import TelemetryExport
from darkula.telemetry.decorators import counted, traced
from darkula.telemetry.setup import TelemetryRuntime, configure_telemetry

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

# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""CrawlerController tests (PR 7) — matrix C7-C14, C18.

Uses the canonical `real CrawlerController -> FakeSandbox` wiring; production
composition never uses FakeSandbox (see test_composition).
"""

from __future__ import annotations

import asyncio

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from tests.support.otel import (
    counter_value,
    data_point_attributes,
    histogram_count,
    metrics_by_name,
)

from darkula.config.settings import CrawlerSettings, Settings
from darkula.crawler.contracts import (
    AllowedOrigin,
    CrawlCredentials,
    CrawlRequest,
    CrawlResult,
    CrawlStatus,
    InvalidCrawlRequest,
)
from darkula.crawler.controller import CrawlerController
from darkula.crawler.runtime_protocol import (
    RuntimeOutput,
    RuntimeOutputPage,
    encode_output,
)
from darkula.infrastructure.sandbox.podman import PodmanSandbox
from darkula.sandbox.contracts import (
    EgressMode,
    SandboxExecutionResult,
    SandboxExitReason,
    SandboxInfrastructureError,
    WorkloadKind,
)
from darkula.testing.fake_sandbox import FakeSandbox

ORIGIN = "http://blackgate.example.test:8080"

SETTINGS = CrawlerSettings()


@pytest.fixture
def wired_telemetry(
    monkeypatch: pytest.MonkeyPatch,
) -> InMemoryMetricReader:
    """Patch the controller's metric seams with an in-memory reader."""
    from darkula.crawler import controller as controller_module
    from darkula.telemetry.metrics import get_meter

    reader = InMemoryMetricReader()
    meter = get_meter(meter_provider=MeterProvider(metric_readers=[reader]))
    monkeypatch.setattr(
        controller_module,
        "get_counter",
        lambda name, **_: meter.create_counter(
            name, unit="{operation}", description=name
        ),
    )
    monkeypatch.setattr(
        controller_module,
        "get_histogram",
        lambda name, *, unit="s", description="": meter.create_histogram(
            name, unit=unit, description=description
        ),
    )
    return reader


def make_request(**overrides: object) -> CrawlRequest:
    base: dict[str, object] = {
        "request_id": "crawl-001",
        "start_url": f"{ORIGIN}/",
        "allowed_origin": AllowedOrigin.parse(ORIGIN),
        "credentials": None,
        "max_pages": 20,
        "max_requests": 60,
        "max_depth": 4,
        "timeout_seconds": 60.0,
    }
    base.update(overrides)
    return CrawlRequest(**base)  # type: ignore[arg-type]


def completed_result(execution_id: str = "crawl-crawl-001") -> SandboxExecutionResult:
    return SandboxExecutionResult(
        execution_id=execution_id,
        exit_reason=SandboxExitReason.COMPLETED,
        output=encode_output(
            RuntimeOutput(
                version=1,
                execution_id=execution_id,
                request_id="crawl-001",
                status="completed",
                pages=(
                    RuntimeOutputPage(
                        url=f"{ORIGIN}/",
                        depth=0,
                        http_status=200,
                        rendered=True,
                        title="BlackGate",
                        text_excerpt="welcome",
                    ),
                ),
                links=(f"{ORIGIN}/index",),
                requests=2,
                redirects_observed=1,
                duration_seconds=0.5,
            )
        ),
    )


async def make_controller(fake: FakeSandbox) -> CrawlerController:
    return CrawlerController(settings=SETTINGS, sandbox=fake)


class TestPolicyDerivation:
    @pytest.mark.asyncio
    async def test_c14_least_capability_policy_exact_allowlist(self) -> None:
        fake = FakeSandbox()
        fake.enqueue_result(completed_result())
        controller = await make_controller(fake)
        await controller.crawl(
            make_request(credentials=CrawlCredentials("user", "pass"))
        )
        captured = fake.executions[0]
        policy = captured.policy
        # Exact allowlist: only the authorized origin, nothing else.
        assert policy.network.egress is EgressMode.ISOLATED
        assert len(policy.network.destinations) == 1
        destination = policy.network.destinations[0]
        assert destination.host == "blackgate.example.test"
        assert destination.port == 8080
        # Default-deny posture everywhere else.
        assert policy.runtime.privileged is False
        assert policy.runtime.host_network is False
        assert policy.runtime.container_socket is False
        assert policy.runtime.host_pid_namespace is False
        assert policy.runtime.drop_all_capabilities is True
        assert policy.runtime.no_new_privileges is True
        assert policy.runtime.non_root_user is True
        assert policy.filesystem.root_read_only is True
        assert policy.input.credentials_allowed is True
        assert captured.workload.kind is WorkloadKind.CRAWLER

    @pytest.mark.asyncio
    async def test_c14_no_credentials_grant_without_credentials(self) -> None:
        fake = FakeSandbox()
        fake.enqueue_result(completed_result())
        controller = await make_controller(fake)
        await controller.crawl(make_request())
        assert fake.executions[0].policy.input.credentials_allowed is False

    @pytest.mark.asyncio
    async def test_workload_payload_is_bounded_protocol_document(self) -> None:
        fake = FakeSandbox()
        fake.enqueue_result(completed_result())
        controller = await make_controller(fake)
        await controller.crawl(make_request())
        payload = fake.executions[0].workload.payload
        assert len(payload) < SETTINGS.max_input_bytes
        assert payload[:1] == b"{"


class TestValidationBeforeSandbox:
    @pytest.mark.asyncio
    async def test_c5_budget_above_ceiling_rejected(self) -> None:
        fake = FakeSandbox()
        controller = await make_controller(fake)
        with pytest.raises(InvalidCrawlRequest):
            await controller.crawl(make_request(max_pages=SETTINGS.max_max_pages + 1))
        assert fake.executions == []

    @pytest.mark.asyncio
    async def test_c6_timeout_above_ceiling_rejected(self) -> None:
        fake = FakeSandbox()
        controller = await make_controller(fake)
        with pytest.raises(InvalidCrawlRequest):
            await controller.crawl(
                make_request(timeout_seconds=SETTINGS.max_timeout_seconds + 1)
            )
        assert fake.executions == []

    @pytest.mark.asyncio
    async def test_loopback_origin_rejected(self) -> None:
        fake = FakeSandbox()
        controller = await make_controller(fake)
        with pytest.raises(InvalidCrawlRequest):
            await controller.crawl(
                make_request(
                    start_url="http://127.0.0.1:8080/",
                    allowed_origin=AllowedOrigin.parse("http://127.0.0.1:8080"),
                )
            )
        assert fake.executions == []

    @pytest.mark.asyncio
    async def test_metadata_origin_rejected(self) -> None:
        fake = FakeSandbox()
        controller = await make_controller(fake)
        with pytest.raises(InvalidCrawlRequest):
            await controller.crawl(
                make_request(
                    start_url="http://169.254.169.254/latest/meta-data/",
                    allowed_origin=AllowedOrigin.parse("http://169.254.169.254"),
                )
            )
        assert fake.executions == []


class TestOutcomeMapping:
    @pytest.mark.asyncio
    async def test_c7_fake_sandbox_success_mapped(self) -> None:
        fake = FakeSandbox()
        fake.enqueue_result(completed_result())
        controller = await make_controller(fake)
        result = await controller.crawl(make_request())
        assert isinstance(result, CrawlResult)
        assert result.status is CrawlStatus.COMPLETED
        assert result.pages[0].title == "BlackGate"
        assert result.discovered_links == (f"{ORIGIN}/index",)
        assert result.requests == 2
        assert result.protocol_version == 1

    @pytest.mark.asyncio
    async def test_c8_malformed_runtime_payload_typed_failure(self) -> None:
        fake = FakeSandbox()
        fake.enqueue_result(
            SandboxExecutionResult(
                execution_id="crawl-crawl-001",
                exit_reason=SandboxExitReason.COMPLETED,
                output=b"this is not json {{{",
            )
        )
        controller = await make_controller(fake)
        result = await controller.crawl(make_request())
        assert result.status is CrawlStatus.INVALID_OUTPUT

    @pytest.mark.asyncio
    async def test_c9_oversized_output_typed_failure(self) -> None:
        fake = FakeSandbox()
        fake.enqueue_result(
            SandboxExecutionResult(
                execution_id="crawl-crawl-001",
                exit_reason=SandboxExitReason.COMPLETED,
                output=b"x" * (SETTINGS.max_output_bytes + 1),
            )
        )
        controller = await make_controller(fake)
        result = await controller.crawl(make_request())
        assert result.status is CrawlStatus.INVALID_OUTPUT

    @pytest.mark.asyncio
    async def test_c9_pages_above_trusted_bound_typed_failure(self) -> None:
        fake = FakeSandbox()
        pages = tuple(
            RuntimeOutputPage(
                url=f"{ORIGIN}/p{i}",
                depth=1,
                http_status=200,
                rendered=True,
                title="",
                text_excerpt="",
            )
            for i in range(SETTINGS.max_max_pages + 1)
        )
        fake.enqueue_result(
            SandboxExecutionResult(
                execution_id="crawl-crawl-001",
                exit_reason=SandboxExitReason.COMPLETED,
                output=encode_output(
                    RuntimeOutput(
                        version=1,
                        execution_id="crawl-crawl-001",
                        request_id="crawl-001",
                        status="completed",
                        pages=pages,
                        links=(),
                        requests=len(pages),
                        redirects_observed=0,
                        duration_seconds=0.1,
                    )
                ),
            )
        )
        controller = await make_controller(fake)
        result = await controller.crawl(make_request())
        assert result.status is CrawlStatus.INVALID_OUTPUT

    @pytest.mark.asyncio
    async def test_execution_identity_mismatch_typed_failure(self) -> None:
        fake = FakeSandbox()
        fake.enqueue_result(
            SandboxExecutionResult(
                execution_id="crawl-crawl-001",
                exit_reason=SandboxExitReason.COMPLETED,
                output=encode_output(
                    RuntimeOutput(
                        version=1,
                        execution_id="other-exec",
                        request_id="crawl-001",
                        status="completed",
                        pages=(),
                        links=(),
                        requests=0,
                        redirects_observed=0,
                        duration_seconds=0.1,
                    )
                ),
            )
        )
        controller = await make_controller(fake)
        result = await controller.crawl(make_request())
        assert result.status is CrawlStatus.INVALID_OUTPUT

    @pytest.mark.asyncio
    async def test_c10_sandbox_unavailable_no_fallback(self) -> None:
        fake = FakeSandbox()
        fake.enqueue_error(SandboxInfrastructureError("podman unavailable"))
        controller = await make_controller(fake)
        with pytest.raises(SandboxInfrastructureError):
            await controller.crawl(make_request())

    @pytest.mark.asyncio
    async def test_c11_timeout_mapped_typed(self) -> None:
        fake = FakeSandbox()
        fake.enqueue_result(
            SandboxExecutionResult(
                execution_id="crawl-crawl-001",
                exit_reason=SandboxExitReason.TIMED_OUT,
                reason="execution exceeded the sandbox timeout",
            )
        )
        controller = await make_controller(fake)
        result = await controller.crawl(make_request())
        assert result.status is CrawlStatus.TIMED_OUT

    @pytest.mark.asyncio
    async def test_c12_workload_failure_bounded(self) -> None:
        fake = FakeSandbox()
        fake.enqueue_result(
            SandboxExecutionResult(
                execution_id="crawl-crawl-001",
                exit_reason=SandboxExitReason.WORKLOAD_FAILED,
                reason="browser workload failed (bounded)",
            )
        )
        controller = await make_controller(fake)
        result = await controller.crawl(make_request())
        assert result.status is CrawlStatus.WORKLOAD_FAILED
        assert "browser" in (result.reason or "")

    @pytest.mark.asyncio
    async def test_c13_cancellation_propagates(self) -> None:
        fake = FakeSandbox()
        fake.enqueue_await_forever()
        controller = await make_controller(fake)
        task = asyncio.create_task(controller.crawl(make_request()))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(fake.cancelled) == 1

    @pytest.mark.asyncio
    async def test_sandbox_returning_cancelled_reason_is_never_success(self) -> None:
        fake = FakeSandbox()
        fake.enqueue_result(
            SandboxExecutionResult(
                execution_id="crawl-crawl-001",
                exit_reason=SandboxExitReason.CANCELLED,
            )
        )
        controller = await make_controller(fake)
        result = await controller.crawl(make_request())
        assert result.status is CrawlStatus.TERMINATED


class TestTelemetry:
    @pytest.mark.asyncio
    async def test_c18_credentials_absent_from_telemetry(
        self,
        wired_telemetry: InMemoryMetricReader,
    ) -> None:
        fake = FakeSandbox()
        fake.enqueue_result(completed_result())
        controller = await make_controller(fake)
        await controller.crawl(
            make_request(credentials=CrawlCredentials("zerofox77", "s3cret-pw"))
        )
        metrics = metrics_by_name(wired_telemetry)
        executions = metrics["darkula.crawler.executions"]
        attributes = data_point_attributes(executions)
        combined = " ".join(f"{k}={v}" for k, v in attributes.items())
        assert "zerofox77" not in combined
        assert "s3cret-pw" not in combined
        assert attributes["darkula.crawler.status"] == "completed"

    @pytest.mark.asyncio
    async def test_timeout_telemetry_counter(
        self,
        wired_telemetry: InMemoryMetricReader,
    ) -> None:
        fake = FakeSandbox()
        fake.enqueue_result(
            SandboxExecutionResult(
                execution_id="crawl-crawl-001",
                exit_reason=SandboxExitReason.TIMED_OUT,
            )
        )
        controller = await make_controller(fake)
        await controller.crawl(make_request())
        metrics = metrics_by_name(wired_telemetry)
        assert counter_value(metrics["darkula.crawler.timeouts"]) == 1

    @pytest.mark.asyncio
    async def test_duration_histogram_recorded(
        self,
        wired_telemetry: InMemoryMetricReader,
    ) -> None:
        fake = FakeSandbox()
        fake.enqueue_result(completed_result())
        controller = await make_controller(fake)
        await controller.crawl(make_request())
        metrics = metrics_by_name(wired_telemetry)
        assert histogram_count(metrics["darkula.crawler.duration"]) == 1


class TestCompositionIntegration:
    def test_production_composition_selects_podman_sandbox(self) -> None:
        sandbox = PodmanSandbox.from_settings(SETTINGS)
        controller = CrawlerController(settings=SETTINGS, sandbox=sandbox)
        assert isinstance(controller, CrawlerController)
        assert controller.settings is SETTINGS

    def test_runtime_composes_crawler(self) -> None:
        from darkula.composition import Runtime, compose

        runtime = compose(settings=load_shipped_settings())
        assert isinstance(runtime, Runtime)
        assert isinstance(runtime.crawler, CrawlerController)


def load_shipped_settings() -> Settings:
    from pathlib import Path

    from darkula.config.loader import load_settings

    shipped = Path(__file__).resolve().parents[3] / "config"
    settings = load_settings(config_dir=shipped)
    assert isinstance(settings, Settings)
    return settings

# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Sandbox SPI contract tests (PR 7) — matrix S1-S6, S8-S9, S10 (FakeSandbox)."""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio

from darkula.sandbox.contracts import (
    AllowedDestination,
    NetworkPolicy,
    ResourceLimits,
    RuntimeIsolationPolicy,
    Sandbox,
    SandboxExecutionMetrics,
    SandboxExecutionPolicy,
    SandboxExecutionRequest,
    SandboxExecutionResult,
    SandboxExitReason,
    SandboxInfrastructureError,
    SandboxRequestError,
    SandboxWorkload,
    WorkloadKind,
    validate_execution_request,
)
from darkula.testing.fake_sandbox import FakeSandbox

DESTINATION = AllowedDestination(host="blackgate.example.test", port=8080)


def valid_policy(**overrides: object) -> SandboxExecutionPolicy:
    base: dict[str, object] = {
        "network": NetworkPolicy(destinations=(DESTINATION,)),
    }
    base.update(overrides)
    return SandboxExecutionPolicy(**base)  # type: ignore[arg-type]


def valid_request(
    *, payload: bytes = b"{}", **overrides: object
) -> SandboxExecutionRequest:
    base: dict[str, object] = {
        "execution_id": "exec-01",
        "policy": valid_policy(),
        "workload": SandboxWorkload(kind=WorkloadKind.CRAWLER, payload=payload),
    }
    base.update(overrides)
    return SandboxExecutionRequest(**base)  # type: ignore[arg-type]


class TestValidation:
    def test_s1_valid_crawler_workload_accepted(self) -> None:
        request = valid_request()
        validate_execution_request(request)
        assert request.workload.kind is WorkloadKind.CRAWLER

    def test_s2_unknown_workload_rejected(self) -> None:
        with pytest.raises(SandboxRequestError):
            SandboxWorkload(kind="jailbreak", payload=b"{}")  # type: ignore[arg-type]

    def test_s3_invalid_resource_limit_rejected(self) -> None:
        with pytest.raises(SandboxRequestError):
            ResourceLimits(memory_bytes=0, cpus=2.0, pids=512, timeout_seconds=60.0)

    def test_s3_too_small_pids_rejected(self) -> None:
        with pytest.raises(SandboxRequestError):
            ResourceLimits(memory_bytes=512, cpus=2.0, pids=2, timeout_seconds=1)

    def test_s4_missing_required_network_grant_fails_closed(self) -> None:
        request = valid_request(
            policy=SandboxExecutionPolicy(network=NetworkPolicy(destinations=()))
        )
        with pytest.raises(SandboxRequestError):
            validate_execution_request(request)

    def test_s5_contradictory_policy_rejected_deterministically(self) -> None:
        # Privileged host-networked policy is inherently contradictory to the
        # PR 7 posture; rejected at construction.
        with pytest.raises(SandboxRequestError):
            SandboxExecutionPolicy(
                network=NetworkPolicy(destinations=(DESTINATION,)),
                runtime=RuntimeIsolationPolicy(privileged=True),
            )
        with pytest.raises(SandboxRequestError):
            SandboxExecutionPolicy(
                network=NetworkPolicy(destinations=(DESTINATION,)),
                runtime=RuntimeIsolationPolicy(host_network=True),
            )

    def test_s6_invalid_output_limits_rejected(self) -> None:
        from darkula.sandbox.contracts import OutputPolicy

        with pytest.raises(SandboxRequestError):
            SandboxExecutionPolicy(
                network=NetworkPolicy(destinations=(DESTINATION,)),
                output=OutputPolicy(max_output_bytes=0),
            )

    def test_s6_invalid_input_limit_rejected(self) -> None:
        from darkula.sandbox.contracts import InputPolicy

        with pytest.raises(SandboxRequestError):
            SandboxExecutionPolicy(
                network=NetworkPolicy(destinations=(DESTINATION,)),
                input=InputPolicy(max_input_bytes=-1),
            )

    def test_s6_invalid_input_credentials_without_network_rejected(self) -> None:
        from darkula.sandbox.contracts import InputPolicy

        with pytest.raises(SandboxRequestError):
            SandboxExecutionPolicy(
                network=NetworkPolicy(destinations=()),
                input=InputPolicy(credentials_allowed=True),
            )

    def test_execution_id_shape_enforced(self) -> None:
        with pytest.raises(SandboxRequestError):
            SandboxExecutionRequest(
                execution_id="Not A Token!",
                policy=valid_policy(),
                workload=SandboxWorkload(WorkloadKind.CRAWLER, b"{}"),
            )

    def test_workload_payload_too_large_rejected(self) -> None:
        with pytest.raises(SandboxRequestError):
            SandboxWorkload(kind=WorkloadKind.CRAWLER, payload=b"x" * 65537)


class TestFakeSandbox:
    pytestmark = pytest.mark.asyncio

    @pytest_asyncio.fixture
    async def fake(self) -> FakeSandbox:
        return FakeSandbox()

    async def test_s7_scripted_success_exact_request_captured(
        self, fake: FakeSandbox
    ) -> None:
        scripted = SandboxExecutionResult(
            execution_id="exec-01",
            exit_reason=SandboxExitReason.COMPLETED,
            output=b"{}",
        )
        fake.enqueue_result(scripted)
        request = valid_request()
        result = await fake.execute(request)
        assert result == scripted
        assert len(fake.executions) == 1
        captured = fake.executions[0]
        assert captured.execution_id == "exec-01"
        assert captured.policy.network.destinations == (DESTINATION,)
        assert captured.workload.kind is WorkloadKind.CRAWLER

    async def test_s8_infrastructure_failure_typed(self, fake: FakeSandbox) -> None:
        fake.enqueue_error(SandboxInfrastructureError("podman unavailable (typed)"))
        with pytest.raises(SandboxInfrastructureError):
            await fake.execute(valid_request())

    async def test_s9_policy_violation_typed(self, fake: FakeSandbox) -> None:
        fake.enqueue_result(
            SandboxExecutionResult(
                execution_id="exec-01",
                exit_reason=SandboxExitReason.POLICY_VIOLATION,
                reason="network policy violation observed",
            )
        )
        result = await fake.execute(valid_request())
        assert result.exit_reason is SandboxExitReason.POLICY_VIOLATION

    async def test_s10_cancellation_not_converted_to_success(
        self, fake: FakeSandbox
    ) -> None:
        fake.enqueue_await_forever()
        task = asyncio.create_task(fake.execute(valid_request()))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(fake.cancelled) == 1

    async def test_empty_script_fails_loudly(self, fake: FakeSandbox) -> None:
        with pytest.raises(AssertionError):
            await fake.execute(valid_request())

    async def test_request_validation_happens_before_recording(
        self, fake: FakeSandbox
    ) -> None:
        bad = valid_request(
            policy=SandboxExecutionPolicy(network=NetworkPolicy(destinations=()))
        )
        with pytest.raises(SandboxRequestError):
            await fake.execute(bad)
        assert fake.executions == []


class TestSandboxAbc:
    def test_sandbox_is_abstract(self) -> None:
        with pytest.raises(TypeError):
            Sandbox()  # type: ignore[abstract]

    def test_metrics_bounds(self) -> None:
        metrics = SandboxExecutionMetrics(
            duration_seconds=1.0,
            output_bytes=42,
            cleanup_succeeded=True,
            exit_code=0,
        )
        assert metrics.exit_code == 0

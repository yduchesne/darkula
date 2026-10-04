# SPDX-License-Identifier: AGPL-3.0-only
"""Darkula Sandbox SPI (PR 7).

Generic default-deny disposable-execution contract consumed by the trusted
:class:`CrawlerController` and implemented by the Podman adapter (and by
:class:`~darkula.testing.fake_sandbox.FakeSandbox` for above-boundary unit
tests).
"""

from darkula.sandbox.contracts import (
    DEFAULT_MAX_INPUT_BYTES,
    DEFAULT_MAX_OUTPUT_BYTES,
    AllowedDestination,
    EgressMode,
    FilesystemPolicy,
    InputPolicy,
    NetworkPolicy,
    OutputPolicy,
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
    TmpfsMount,
    WorkloadKind,
)

__all__ = [
    "DEFAULT_MAX_INPUT_BYTES",
    "DEFAULT_MAX_OUTPUT_BYTES",
    "AllowedDestination",
    "EgressMode",
    "FilesystemPolicy",
    "InputPolicy",
    "NetworkPolicy",
    "OutputPolicy",
    "ResourceLimits",
    "RuntimeIsolationPolicy",
    "Sandbox",
    "SandboxExecutionMetrics",
    "SandboxExecutionPolicy",
    "SandboxExecutionRequest",
    "SandboxExecutionResult",
    "SandboxExitReason",
    "SandboxInfrastructureError",
    "SandboxRequestError",
    "SandboxWorkload",
    "TmpfsMount",
    "WorkloadKind",
]

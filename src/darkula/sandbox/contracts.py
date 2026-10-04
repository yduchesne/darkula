# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Darkula-owned generic Sandbox SPI (PR 7).

``Sandbox`` is a first-class abstraction independent of crawler semantics: it
executes an *allow-listed* workload inside one disposable, heavily isolated
execution environment subject to an explicit capability policy. It must
remain usable later for isolated parsing/archive/media workloads and future
Tor-compatible crawler egress — therefore the contract takes no arbitrary
shell command strings and no Darkula-service-specific deny fields.

Ownership and semantics frozen here:

- policies are **default-deny**: capability lists describe what an execution
  may do (network destinations, writable filesystem paths, resource ceilings,
  runtime isolation posture, bounded inputs, bounded outputs); everything not
  granted is denied. There is no ``deny_postgres``/``deny_redpanda``/...
  vocabulary;
- exactly one disposable execution per ``execute()`` call; the adapter must
  terminate and clean up on success, workload failure, timeout, policy
  failure, and cancellation;
- ``asyncio.CancelledError`` propagates after cleanup and is never converted
  into a success result;
- Podman/container/browser types never leak through this contract;
- failures are typed, never collapsed into ``RuntimeError``;
- the network policy describes *allowed destinations*; the enforcing adapter
  (``PodmanSandbox`` on a Darkula-owned internal network) must prove denial
  below any workload logic.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum

#: Bounded token shape for execution identities (safe for names/logs).
_EXECUTION_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

#: Upper bound for any raw payload crossing into the sandbox.
DEFAULT_MAX_INPUT_BYTES = 65536

#: Upper bound for any output crossing back out of the sandbox.
DEFAULT_MAX_OUTPUT_BYTES = 1024 * 1024


class SandboxRequestError(ValueError):
    """A sandbox execution request failed validation (programming error).

    Raised before any container/environment is created; messages are bounded
    and never echo workload payloads.
    """


class SandboxInfrastructureError(RuntimeError):
    """The sandbox adapter itself failed (Podman unavailable, ownership
    cannot be verified, cleanup failed)...

    Never implies workload semantics; never triggers host-side fallback.
    """


class WorkloadKind(StrEnum):
    """Allow-listed sandbox workload types.

    ``CRAWLER`` is the PR 7 workload: deterministic browser navigation of an
    explicitly authorized origin. A workload kind is the only command the
    adapter may execute — there is no generic command-string workload.
    """

    CRAWLER = "crawler"


class EgressMode(StrEnum):
    """Network egress capability of one execution.

    ``ISOLATED`` grants connectivity only to the explicitly listed
    destinations on a Darkula-owned internal topology; everything else
    (Internet, host, other containers, private/LAN, cloud metadata) is
    denied by the enforcing adapter.
    """

    ISOLATED = "isolated"


@dataclass(frozen=True, slots=True)
class AllowedDestination:
    """One authorized network destination (parsed origin semantics).

    ``host`` is the DNS name or literal IP the sandbox may reach on the
    Darkula-owned network; ``port`` is the service port.
    """

    host: str
    port: int

    def __post_init__(self) -> None:
        if (
            not self.host
            or len(self.host) > 253
            or any(ord(ch) < 33 or ord(ch) > 126 for ch in self.host)
        ):
            raise SandboxRequestError("network destination host is invalid")
        if self.port < 1 or self.port > 65535:
            raise SandboxRequestError("network destination port is out of range")

    @classmethod
    def from_origin(cls, scheme: str, host: str, port: int) -> AllowedDestination:
        """Build the allowed destination for a validated http(s) origin."""
        if scheme not in ("http", "https"):
            raise SandboxRequestError("network policy requires an http(s) origin")
        return cls(host=host, port=port)


@dataclass(frozen=True, slots=True)
class NetworkPolicy:
    """Default-deny network capability of one execution.

    Only the explicitly listed destinations are reachable (enforced by the
    adapter topology, below workload logic); all other destinations are
    denied. DNS resolution is restricted to the configured destinations.
    """

    egress: EgressMode = EgressMode.ISOLATED
    destinations: tuple[AllowedDestination, ...] = ()


@dataclass(frozen=True, slots=True)
class TmpfsMount:
    """One bounded ephemeral writable path (no host bind mounts in PR 7)."""

    path: str
    size_bytes: int

    def __post_init__(self) -> None:
        if not self.path.startswith("/") or self.path == "/":
            raise SandboxRequestError("tmpfs path must be an absolute sub-path")
        if self.size_bytes < 1 or self.size_bytes > 2 * 1024 * 1024 * 1024:
            raise SandboxRequestError("tmpfs size is out of range")


@dataclass(frozen=True, slots=True)
class FilesystemPolicy:
    """Filesystem capability of one execution.

    The root filesystem is read-only; only the listed tmpfs mounts are
    writable; no host bind mounts and no container socket exist.
    """

    root_read_only: bool = True
    tmpfs: tuple[TmpfsMount, ...] = (
        TmpfsMount("/tmp", 256 * 1024 * 1024),  # nosec B108 - sandbox tmpfs
        TmpfsMount("/dev/shm", 256 * 1024 * 1024),  # nosec B108 - sandbox tmpfs
    )


@dataclass(frozen=True, slots=True)
class ResourceLimits:
    """Bounded resource ceilings of one execution.

    All limits are positive and bounded; a zero/unset value is never
    interpreted as *unlimited* — validation rejects it.
    """

    memory_bytes: int = 1024 * 1024 * 1024
    cpus: float = 2.0
    pids: int = 512
    timeout_seconds: float = 60.0

    def __post_init__(self) -> None:
        if self.memory_bytes < 1 or self.memory_bytes > 8 * 1024 * 1024 * 1024:
            raise SandboxRequestError("memory limit is out of range")
        if self.cpus < 0.25 or self.cpus > 32.0:
            raise SandboxRequestError("cpu limit is out of range")
        if self.pids < 16 or self.pids > 65536:
            raise SandboxRequestError("pids limit is out of range")
        if self.timeout_seconds < 0.001 or self.timeout_seconds > 86400.0:
            raise SandboxRequestError("timeout is out of range")


@dataclass(frozen=True, slots=True)
class RuntimeIsolationPolicy:
    """Runtime/isolation posture of one execution.

    Defaults are the strongest practical rootless restrictions compatible
    with Chromium: non-privileged, no host network/pid, no container socket,
    all unnecessary capabilities dropped, no-new-privileges, no devices.
    """

    privileged: bool = False
    host_network: bool = False
    container_socket: bool = False
    host_pid_namespace: bool = False
    drop_all_capabilities: bool = True
    no_new_privileges: bool = True
    devices: tuple[str, ...] = ()
    non_root_user: bool = True


@dataclass(frozen=True, slots=True)
class InputPolicy:
    """Bounded input capability of one execution.

    ``credentials_allowed`` permits narrowly scoped source authentication
    material (for example the synthetic BlackGate login) to cross the
    boundary; it is never granted when the workload does not need it.
    """

    max_input_bytes: int = DEFAULT_MAX_INPUT_BYTES
    credentials_allowed: bool = False

    def __post_init__(self) -> None:
        if self.max_input_bytes < 1 or self.max_input_bytes > 64 * 1024 * 1024:
            raise SandboxRequestError("input bound is out of range")


@dataclass(frozen=True, slots=True)
class OutputPolicy:
    """Bounded output capability of one execution.

    PR 7 transfers only small structured JSON reference payloads; no durable
    artifacts cross back (PR 8 owns ObjectStore persistence, and output
    handles are execution-scoped, never host filesystem paths).
    """

    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES

    def __post_init__(self) -> None:
        if self.max_output_bytes < 1 or self.max_output_bytes > 256 * 1024 * 1024:
            raise SandboxRequestError("output bound is out of range")


@dataclass(frozen=True, slots=True)
class SandboxExecutionPolicy:
    """The complete default-deny policy of one execution.

    Validation rejects contradictory combinations eagerly and
    deterministically so a misconfigured request fails before any resource
    is created.
    """

    network: NetworkPolicy
    filesystem: FilesystemPolicy = FilesystemPolicy()
    resources: ResourceLimits = ResourceLimits()
    runtime: RuntimeIsolationPolicy = RuntimeIsolationPolicy()
    input: InputPolicy = InputPolicy()
    output: OutputPolicy = OutputPolicy()

    def __post_init__(self) -> None:
        if self.runtime.privileged:
            raise SandboxRequestError("privileged sandbox executions are unsupported")
        if self.runtime.host_network:
            raise SandboxRequestError("host-network sandbox executions are unsupported")
        if self.runtime.container_socket:
            raise SandboxRequestError(
                "a container socket in the sandbox is unsupported"
            )
        if self.runtime.host_pid_namespace:
            raise SandboxRequestError(
                "host PID namespace in the sandbox is unsupported"
            )
        if self.runtime.devices:
            raise SandboxRequestError("sandbox devices are unsupported in PR 7")
        if self.network.egress is not EgressMode.ISOLATED:
            raise SandboxRequestError(
                "only isolated network egress is supported in PR 7"
            )
        if self.input.credentials_allowed and self.network.destinations == ():
            raise SandboxRequestError(
                "credentials require at least one authorized destination"
            )


@dataclass(frozen=True, slots=True)
class SandboxWorkload:
    """One allow-listed workload with a bounded, serialized input.

    ``payload`` is the schema-versioned serialized input document for the
    workload (UTF-8, bounded; never arbitrary Python objects and never raw
    shell commands). For the crawler workload it is the encoded
    controller-runtime protocol document.
    """

    kind: WorkloadKind
    payload: bytes

    def __post_init__(self) -> None:
        if self.kind not in WorkloadKind:
            raise SandboxRequestError(f"unknown workload kind: {self.kind!r}")
        if not isinstance(self.payload, bytes):
            raise SandboxRequestError("workload payload must be bytes")
        if len(self.payload) > DEFAULT_MAX_INPUT_BYTES:
            raise SandboxRequestError(
                "workload payload exceeds the default input bound"
            )


class SandboxExitReason(StrEnum):
    """Bounded exit-reason vocabulary of one sandbox execution.

    ``COMPLETED`` means the workload produced a valid bounded output;
    workload/browser failures, timeouts, resource-limit terminations, policy
    violations, cancellations, infrastructure failures, and invalid/oversized
    output are all distinct and never collapsed.
    """

    COMPLETED = "completed"
    WORKLOAD_FAILED = "workload_failed"
    TIMED_OUT = "timed_out"
    RESOURCE_LIMIT = "resource_limit"
    POLICY_VIOLATION = "policy_violation"
    TERMINATED = "terminated"
    INFRASTRUCTURE_FAILURE = "infrastructure_failure"
    INVALID_OR_OVERSIZED_OUTPUT = "invalid_or_oversized_output"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class SandboxExecutionMetrics:
    """Bounded, secret-free execution metrics (safe for telemetry)."""

    duration_seconds: float = 0.0
    container_execution_time_seconds: float = 0.0
    output_bytes: int = 0
    cleanup_succeeded: bool = True
    exit_code: int | None = None


def validate_execution_request(request: SandboxExecutionRequest) -> None:
    """Validate one execution request against workload-specific invariants.

    Called by every Sandbox implementation before any resource is created:

    - the workload kind must be allow-listed (already checked eagerly);
    - a crawler workload requires at least one authorized network
      destination (an isolated crawler with no reachable origin is a
      contradictory policy and fails closed);
    - the serialized payload must fit the declared input bound.
    """
    if request.workload.kind is WorkloadKind.CRAWLER and (
        request.policy.network.destinations == ()
    ):
        raise SandboxRequestError(
            "crawler workloads require at least one authorized network "
            "destination (default-deny)"
        )
    if len(request.workload.payload) > request.policy.input.max_input_bytes:
        raise SandboxRequestError("workload payload exceeds the declared input bound")


@dataclass(frozen=True, slots=True)
class SandboxExecutionRequest:
    """One disposable sandbox execution request.

    ``execution_id`` is the isolated workload identity (distinct from the
    crawl request ID and from any container identity).
    """

    execution_id: str
    policy: SandboxExecutionPolicy
    workload: SandboxWorkload

    def __post_init__(self) -> None:
        if not isinstance(self.execution_id, str):
            raise SandboxRequestError("execution_id must be a string")
        if _EXECUTION_ID_RE.fullmatch(self.execution_id) is None:
            raise SandboxRequestError(
                "execution_id must match the bounded token shape "
                "'^[a-z0-9][a-z0-9-]{0,63}$'"
            )


@dataclass(frozen=True, slots=True)
class SandboxExecutionResult:
    """Bounded outcome of one sandbox execution.

    ``output`` is the bounded raw bytes the workload produced (only present
    for ``COMPLETED``); ``reason`` is a bounded, secret-free diagnostic;
    ``error`` is a bounded typed failure category for infrastructure
    failures. Host filesystem paths are never exposed as artifact identity.
    """

    execution_id: str
    exit_reason: SandboxExitReason
    output: bytes | None = None
    metrics: SandboxExecutionMetrics = SandboxExecutionMetrics()
    reason: str | None = None


class Sandbox(ABC):
    """Darkula-owned generic disposable-execution capability.

    Implementations must create exactly one fresh isolated execution per
    ``execute()`` call, enforce the full policy below workload logic, clean
    up on every terminal path, propagate cancellation after cleanup, and
    never fall back to unsandboxed execution.
    """

    @abstractmethod
    async def execute(self, request: SandboxExecutionRequest) -> SandboxExecutionResult:
        """Execute one allow-listed workload and return its typed result."""
        raise NotImplementedError


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
    "validate_execution_request",
]
